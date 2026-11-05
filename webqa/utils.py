from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit


TRACKING_KEYS = {"ref", "source", "fbclid", "gclid"}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def sha(value: str, size: int = 16) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="ignore")).hexdigest()[:size]


def canonical_url(base_url: str, url: str) -> str:
    absolute = urljoin(base_url.rstrip("/") + "/", url)
    parts = urlsplit(absolute)
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    port = parts.port
    netloc = host
    if port and not ((scheme == "https" and port == 443) or (scheme == "http" and port == 80)):
        netloc = f"{host}:{port}"
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    if path != "/":
        path = path.rstrip("/")
    query_pairs = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in TRACKING_KEYS
    ]
    return urlunsplit((scheme, netloc, path, urlencode(sorted(query_pairs)), ""))


def same_host(base_url: str, url: str) -> bool:
    try:
        return (urlsplit(base_url).hostname or "").lower() == (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False


def json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def json_load(value: str | None, default):
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def path_label(path: list[dict] | list[str]) -> str:
    labels: list[str] = []
    for item in path:
        if isinstance(item, str):
            labels.append(item)
        else:
            labels.append(str(item.get("label") or item.get("url") or item.get("action") or "未命名动作"))
    return " → ".join(labels) if labels else "初始页面"
