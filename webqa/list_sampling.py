from __future__ import annotations

import copy
import re
from collections import defaultdict
from typing import Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .utils import sha


PAGE_KEYS = {
    "page",
    "p",
    "pageindex",
    "page_index",
    "pagenum",
    "page_num",
    "pageno",
    "page_no",
    "offset",
}


def normalized_collection_url(url: str) -> str:
    parts = urlsplit(url)
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in PAGE_KEYS
    ]
    path = re.sub(r"/page/\d+(?=/|$)", "/page/:page", parts.path, flags=re.I)
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), ""))


def _group_key(url: str, kind: str, hint: str) -> str:
    return sha(f"{normalized_collection_url(url)}|{kind}|{hint}", 32)


def _primary_control(items: list[dict]) -> dict:
    links = [item for item in items if item.get("href")]
    return (links or items)[0]


def _page_context(items: list[dict]) -> tuple[int | None, int | None, int | None]:
    first = next((item.get("pagination_first_page") for item in items if item.get("pagination_first_page") is not None), None)
    current = next((item.get("pagination_current_page") for item in items if item.get("pagination_current_page") is not None), None)
    last = next((item.get("pagination_last_page") for item in items if item.get("pagination_last_page") is not None), None)
    return (
        int(first) if first is not None else None,
        int(current) if current is not None else None,
        int(last) if last is not None else None,
    )


def _infer_page_href(items: list[dict], target_page: int) -> str | None:
    for item in items:
        href = str(item.get("href") or "")
        source_page = item.get("pagination_page_number")
        if not href or source_page is None:
            continue
        parts = urlsplit(href)
        pairs = parse_qsl(parts.query, keep_blank_values=True)
        replaced = False
        inferred: list[tuple[str, str]] = []
        for key, value in pairs:
            if key.lower() in PAGE_KEYS and value == str(source_page):
                inferred.append((key, str(target_page)))
                replaced = True
            else:
                inferred.append((key, value))
        if replaced:
            return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(inferred), ""))
        source_path = f"/page/{source_page}"
        if source_path.lower() in parts.path.lower():
            path = re.sub(
                rf"/page/{int(source_page)}(?=/|$)",
                f"/page/{target_page}",
                parts.path,
                count=1,
                flags=re.I,
            )
            return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))
    return None


def apply_list_sampling(
    controls: list[dict],
    current_url: str,
    settings: dict | None,
    profile_settings: dict | None,
    claim_slot: Callable[[str, str], bool],
) -> tuple[list[dict], list[dict]]:
    """Mark repeated-list and pagination controls as sampled or skipped.

    All controls remain available for the coverage ledger. Storage uses
    ``sampling_decision=skip`` to avoid putting omitted repetitions on the
    execution frontier.
    """

    settings = settings or {}
    profile_settings = profile_settings or {}
    planned = [copy.deepcopy(item) for item in controls]
    if not settings.get("enabled", True):
        return planned, []
    detail_samples = max(
        0,
        int(profile_settings.get("list_item_samples", settings.get("item_detail_samples", 2))),
    )
    pagination_samples = max(
        0,
        int(profile_settings.get("pagination_samples", settings.get("pagination_samples", 3))),
    )
    records: list[dict] = []

    collection_groups: dict[str, list[dict]] = defaultdict(list)
    for item in planned:
        hint = str(item.get("collection_key_hint") or "")
        if hint:
            collection_groups[hint].append(item)

    for hint, group in collection_groups.items():
        key = _group_key(current_url, "items", hint)
        by_index: dict[int, list[dict]] = defaultdict(list)
        for item in group:
            by_index[int(item.get("collection_item_index") or 0)].append(item)
            item["sampling_decision"] = "skip"
            item["sampling_kind"] = "LIST_ITEM"
            item["sampling_group_key"] = key
        indices = sorted(by_index)
        first_page, current_page, last_page = _page_context(group)
        selected: list[tuple[int, str]] = []
        if indices and detail_samples:
            paginated = bool(
                first_page is not None
                and current_page is not None
                and last_page is not None
                and last_page > first_page
            )
            if paginated:
                if current_page == first_page and claim_slot(key, "item-head"):
                    selected.append((indices[0], "head"))
                if detail_samples >= 2 and current_page == last_page and claim_slot(key, "item-tail"):
                    selected.append((indices[-1], "tail"))
            else:
                candidates = [(indices[0], "head")]
                if detail_samples >= 2 and indices[-1] != indices[0]:
                    candidates.append((indices[-1], "tail"))
                for index, position in candidates[:detail_samples]:
                    if claim_slot(key, f"item-{position}"):
                        selected.append((index, position))
        selected_controls: list[dict] = []
        for index, position in selected:
            primary = _primary_control(by_index[index])
            primary["sampling_decision"] = "sample"
            primary["sampling_position"] = position
            primary["list_item_sample"] = True
            selected_controls.append(primary)
        records.append(
            {
                "group_key": key,
                "sample_kind": "LIST_ITEMS",
                "strategy": "paginated-head-tail" if (last_page or 0) > (first_page or 0) else "visible-head-tail",
                "visible_items": len(indices),
                "visible_controls": len(group),
                "selected_count": len(selected_controls),
                "skipped_count": len(group) - len(selected_controls),
                "selected": [
                    {
                        "label": item.get("label") or "",
                        "href": item.get("href") or "",
                        "position": item.get("sampling_position"),
                    }
                    for item in selected_controls
                ],
                "page_context": {
                    "first": first_page,
                    "current": current_page,
                    "last": last_page,
                },
            }
        )

    pagination_groups: dict[str, list[dict]] = defaultdict(list)
    for item in planned:
        hint = str(item.get("pagination_key_hint") or "")
        if hint:
            pagination_groups[hint].append(item)

    for hint, group in pagination_groups.items():
        key = _group_key(current_url, "pagination", hint)
        for item in group:
            item["sampling_decision"] = "skip"
            item["sampling_kind"] = "PAGINATION"
            item["sampling_group_key"] = key
        selected: list[dict] = []
        direct_targets: list[dict] = []
        claimed = pagination_samples > 0 and claim_slot(key, "pagination-plan")
        if claimed:
            numbered = {
                int(item["pagination_page_number"]): item
                for item in group
                if item.get("pagination_page_number") is not None and item.get("href")
            }
            current_page = next(
                (int(item["pagination_current_page"]) for item in group if item.get("pagination_current_page") is not None),
                None,
            )
            if numbered:
                numbers = sorted(numbered)
                first_page = next(
                    (int(item["pagination_first_page"]) for item in group if item.get("pagination_first_page") is not None),
                    numbers[0],
                )
                last_page = next(
                    (int(item["pagination_last_page"]) for item in group if item.get("pagination_last_page") is not None),
                    numbers[-1],
                )
                target_numbers = [first_page]
                if pagination_samples >= 2:
                    target_numbers.append((first_page + last_page) // 2)
                if pagination_samples >= 3:
                    target_numbers.append(last_page)
                for number in target_numbers:
                    if number == current_page:
                        continue
                    item = numbered.get(number)
                    if item and item not in selected:
                        selected.append(item)
                        continue
                    inferred = _infer_page_href(group, number)
                    if inferred and all(value["href"] != inferred for value in direct_targets):
                        direct_targets.append(
                            {"label": f"推导分页 {number}", "href": inferred, "page": number, "direct": True}
                        )
            if not selected and not direct_targets:
                for kind in ("next", "last", "previous", "first"):
                    item = next(
                        (value for value in group if value.get("pagination_kind") == kind and value.get("href")),
                        None,
                    )
                    if item and item not in selected:
                        selected.append(item)
                    if len(selected) >= pagination_samples:
                        break
        for position, item in enumerate(selected[:pagination_samples], start=1):
            item["sampling_decision"] = "sample"
            item["sampling_position"] = f"pagination-{position}"
            item["pagination_sample"] = True
        records.append(
            {
                "group_key": key,
                "sample_kind": "PAGINATION",
                "strategy": "first-middle-last",
                "visible_items": len(group),
                "visible_controls": len(group),
                "selected_count": min(len(selected) + len(direct_targets), pagination_samples),
                "skipped_count": len(group) - min(len(selected), pagination_samples),
                "selected": ([
                    {
                        "label": item.get("label") or "",
                        "href": item.get("href") or "",
                        "page": item.get("pagination_page_number"),
                    }
                    for item in selected[:pagination_samples]
                ] + direct_targets)[:pagination_samples],
                "direct_targets": direct_targets[:pagination_samples],
                "page_context": {
                    "first": next((item.get("pagination_first_page") for item in group if item.get("pagination_first_page") is not None), None),
                    "current": next((item.get("pagination_current_page") for item in group if item.get("pagination_current_page") is not None), None),
                    "last": next((item.get("pagination_last_page") for item in group if item.get("pagination_last_page") is not None), None),
                },
            }
        )
    return planned, records
