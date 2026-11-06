from __future__ import annotations

import re

from .utils import sha


def select_invariant(descriptor: dict, settings: dict | None) -> dict | None:
    settings = settings or {}
    label = str(descriptor.get("label") or "")
    for index, rule in enumerate(settings.get("rules") or []):
        pattern = str(rule.get("label_regex") or ".*")
        try:
            if re.search(pattern, label, re.I):
                return {
                    "type": str(rule.get("type") or "idempotent"),
                    "source": str(rule.get("source") or f"config.invariant-{index + 1}"),
                    "confidence": str(rule.get("confidence") or "high"),
                }
        except re.error:
            continue
    if not settings.get("auto_detect", True):
        return None
    if descriptor.get("expanded") is not None or descriptor.get("pressed") is not None:
        return {"type": "reversible", "source": "aria-state", "confidence": "high"}
    if re.search(r"主题|theme|toggle|switch", label, re.I):
        return {"type": "reversible", "source": "label-inference", "confidence": "medium"}
    if str(descriptor.get("role") or "").lower() == "tab":
        return {"type": "idempotent", "source": "aria-role", "confidence": "high"}
    if re.search(r"加载更多|load more|show more", label, re.I):
        return {"type": "non_decreasing_count", "source": "label-inference", "confidence": "medium"}
    if re.search(r"排序|sort|order by", label, re.I):
        return {"type": "same_collection_members", "source": "label-inference", "confidence": "medium"}
    if re.search(r"筛选|过滤|filter", label, re.I):
        return {"type": "non_increasing_count", "source": "label-inference", "confidence": "medium"}
    if re.search(r"语言|language|中文|english", label, re.I):
        return {"type": "stable_structure", "source": "label-inference", "confidence": "medium"}
    return None


def visible_collection_snapshot(page) -> dict:
    values = page.evaluate(
        """() => {
          const visible=el=>{const r=el.getBoundingClientRect(),s=getComputedStyle(el);return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden'};
          const nodes=[];
          nodes.push(...document.querySelectorAll('main tbody tr,main [role="rowgroup"] [role="row"],main [role="grid"] [role="row"]'));
          nodes.push(...document.querySelectorAll('main article,main [class~="card"],main [data-testid*="card" i]'));
          for(const list of document.querySelectorAll('main [role="list"],main ul,main ol')){
            if(visible(list)&&!list.closest('nav,header,footer,[role="navigation"],[role="menu"]')){
              nodes.push(...list.querySelectorAll(':scope > li,:scope > [role="listitem"]'));
            }
          }
          return Array.from(new Set(nodes)).filter(visible).slice(0,500).map(el=>(el.innerText||el.textContent||'').replace(/\s+/g,' ').trim().slice(0,1000));
        }"""
    )
    hashes = [sha(str(value), 20) for value in values]
    return {"count": len(hashes), "ordered_hashes": hashes, "member_hashes": sorted(hashes)}


def visible_collection_count(page) -> int:
    return int(visible_collection_snapshot(page)["count"])


def page_structure_profile(page) -> dict:
    return page.evaluate(
        """() => {
          const visible=el=>{const r=el.getBoundingClientRect(),s=getComputedStyle(el);return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden'};
          const count=selector=>Array.from(document.querySelectorAll(selector)).filter(visible).length;
          return {
            landmarks: count('main,header,footer,nav,aside,[role="main"],[role="navigation"],[role="complementary"]'),
            headings: count('h1,h2,h3,h4,h5,h6,[role="heading"]'),
            forms: count('form'),
            interactive: count('button,a[href],summary,[role="button"],[role="link"],[role="tab"]'),
            fields: count('input,select,textarea'),
            tables: count('table,[role="table"],[role="grid"]')
          };
        }"""
    )
