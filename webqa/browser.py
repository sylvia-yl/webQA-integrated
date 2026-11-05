from __future__ import annotations

import re
from typing import Any
from urllib.parse import unquote, urlsplit

from .utils import canonical_url, json_text, sha


INTERACTIVE_SELECTOR = ",".join(
    [
        "a[href]",
        "button",
        "summary",
        "[role=button]",
        "[role=link]",
        "[role=tab]",
        "[role=menuitem]",
        "input:not([type=hidden])",
        "select",
        "textarea",
    ]
)


COLLECT_JS = r"""
({selector, includeSelectors = [], excludeSelectors = []}) => {
  const compact = value => (value || '').replace(/\s+/g, ' ').trim();
  const matchedSelector = (el, selectors) => selectors.find(value => {
    try { return el.matches(value); } catch (_) { return false; }
  }) || '';
  const testId = el => el.getAttribute('data-testid') || el.getAttribute('data-test') || el.getAttribute('data-qa') || '';
  const containerSignature = el => [
    el.tagName?.toLowerCase() || '',
    el.getAttribute('role') || '',
    el.id || '',
    testId(el),
    compact(el.getAttribute('aria-label')),
  ].join('|').slice(0, 400);
  const sharedContext = el => {
    if (matchedSelector(el, excludeSelectors)) return {scope: '', signature: '', forced: false};
    const includeSelector = matchedSelector(el, includeSelectors);
    if (includeSelector) {
      return {scope: 'configured', signature: 'selector:' + includeSelector, forced: true};
    }
    const header = el.closest('header,[role="banner"]');
    if (header && !header.closest('main,[role="main"],article')) {
      return {scope: 'header', signature: containerSignature(header), forced: false};
    }
    const footer = el.closest('footer,[role="contentinfo"]');
    if (footer && !footer.closest('main,[role="main"],article')) {
      return {scope: 'footer', signature: containerSignature(footer), forced: false};
    }
    const nav = el.closest('nav,[role="navigation"]');
    if (nav && !nav.closest('main,[role="main"],article')) {
      return {scope: 'nav', signature: containerSignature(nav), forced: false};
    }
    let node = el;
    while (node && node.nodeType === 1) {
      const position = getComputedStyle(node).position;
      if (position === 'fixed' || position === 'sticky') {
        return {scope: position, signature: containerSignature(node), forced: false};
      }
      node = node.parentElement;
    }
    return {scope: '', signature: '', forced: false};
  };
  const rendered = el => {
    if (!el || !el.isConnected) return false;
    if (el.closest('[hidden],[inert],[aria-hidden="true"]')) return false;
    const closedDetails = el.closest('details:not([open])');
    if (closedDetails) {
      const summary = closedDetails.querySelector(':scope > summary');
      if (!summary || (el !== summary && !summary.contains(el))) return false;
    }
    let node = el;
    while (node && node.nodeType === 1) {
      const style = getComputedStyle(node);
      if (style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse') return false;
      if (Number(style.opacity) === 0) return false;
      if (style.pointerEvents === 'none') return false;
      node = node.parentElement;
    }
    const rect = el.getBoundingClientRect();
    if (rect.width < 1 || rect.height < 1 || el.getClientRects().length === 0) return false;
    // Off-canvas menus often remain measurable while their fixed container is translated
    // outside the viewport. They are not user-visible until the opening action runs.
    node = el;
    while (node && node.nodeType === 1) {
      const style = getComputedStyle(node);
      const box = node.getBoundingClientRect();
      if ((style.position === 'fixed' || style.position === 'sticky') &&
          (box.right <= 0 || box.bottom <= 0 || box.left >= innerWidth || box.top >= innerHeight)) return false;
      if (node !== el && /(hidden|clip)/.test(style.overflow + style.overflowX + style.overflowY)) {
        const intersects = rect.right > box.left && rect.left < box.right && rect.bottom > box.top && rect.top < box.bottom;
        if (!intersects) return false;
      }
      node = node.parentElement;
    }
    if (el.matches('input[type="hidden"]')) return false;
    const left = Math.max(0, rect.left), right = Math.min(innerWidth, rect.right);
    const top = Math.max(0, rect.top), bottom = Math.min(innerHeight, rect.bottom);
    if (right > left && bottom > top) {
      const points = [
        [(left + right) / 2, (top + bottom) / 2],
        [left + Math.min(4, (right-left)/2), top + Math.min(4, (bottom-top)/2)],
        [right - Math.min(4, (right-left)/2), bottom - Math.min(4, (bottom-top)/2)]
      ];
      const hittable = points.some(([x,y]) => {
        const hit = document.elementFromPoint(x, y);
        return !!hit && (hit === el || el.contains(hit));
      });
      if (!hittable) return false;
    }
    return true;
  };
  const cssPath = el => {
    if (el.id) return '#' + CSS.escape(el.id);
    for (const attr of ['data-testid','data-test','data-qa']) {
      const value = el.getAttribute(attr);
      if (value) return `[${attr}="${CSS.escape(value)}"]`;
    }
    const parts = [];
    let node = el;
    while (node && node.nodeType === 1 && parts.length < 8) {
      let part = node.tagName.toLowerCase();
      const parent = node.parentElement;
      if (parent) {
        const peers = Array.from(parent.children).filter(x => x.tagName === node.tagName);
        if (peers.length > 1) part += `:nth-of-type(${peers.indexOf(node) + 1})`;
      }
      parts.unshift(part);
      node = parent;
      if (node?.tagName === 'BODY') { parts.unshift('body'); break; }
    }
    return parts.join(' > ');
  };
  const accessibleName = el => {
    const aria = compact(el.getAttribute('aria-label'));
    if (aria) return aria;
    const labelledBy = compact(el.getAttribute('aria-labelledby'));
    if (labelledBy) {
      const text = compact(labelledBy.split(/\s+/).map(id => document.getElementById(id)?.innerText || '').join(' '));
      if (text) return text;
    }
    if (el.labels?.length) {
      const text = compact(Array.from(el.labels).map(x => x.innerText).join(' '));
      if (text) return text;
    }
    const direct = compact(el.innerText || el.textContent || el.getAttribute('title') || el.getAttribute('placeholder') || el.value || '');
    if (direct) return direct;
    const namedChild = el.querySelector('[aria-label],[title],img[alt],svg title');
    return compact(namedChild?.getAttribute('aria-label') || namedChild?.getAttribute('title') || namedChild?.getAttribute('alt') || namedChild?.textContent || '');
  };
  const section = el => {
    const dialog = el.closest('dialog,[role=dialog]');
    if (dialog) return '对话框:' + compact(dialog.getAttribute('aria-label') || dialog.querySelector('h1,h2,h3')?.innerText || '未命名');
    const menu = el.closest('[role=menu]');
    if (menu) return '菜单:' + compact(menu.getAttribute('aria-label') || '未命名');
    const details = el.closest('details');
    if (details) return '折叠区:' + compact(details.querySelector(':scope > summary')?.innerText || '未命名');
    const landmark = el.closest('header,nav,main,footer,aside,section');
    if (!landmark) return '';
    if (landmark.tagName === 'HEADER') return '页眉';
    if (landmark.tagName === 'FOOTER') return '页脚';
    if (landmark.tagName === 'NAV') return '导航:' + compact(landmark.getAttribute('aria-label') || '未命名');
    return compact(landmark.querySelector('h1,h2,h3')?.innerText || landmark.tagName.toLowerCase());
  };
  const roleOf = el => el.getAttribute('role') || ({A:'link',BUTTON:'button',SUMMARY:'button',SELECT:'combobox',TEXTAREA:'textbox'}[el.tagName] || (el.tagName === 'INPUT' ? (['button','submit','reset'].includes(el.type) ? 'button' : 'textbox') : ''));
  return Array.from(document.querySelectorAll(selector)).map((el, domIndex) => {
    if (!rendered(el)) return null;
    let href = el.href || el.getAttribute('data-href') || '';
    try { if (href) href = new URL(href, location.href).href; } catch (_) {}
    const rect = el.getBoundingClientRect();
    const visibleText = compact(el.innerText || '');
    const icon = el.querySelector('svg,img');
    let iconName = '';
    if (icon) {
      if (icon.tagName === 'IMG') {
        iconName = compact(icon.getAttribute('alt') || icon.getAttribute('src') || '').split('/').pop()?.replace(/\.[^.]+$/, '') || '';
      } else {
        const classes = Array.from(icon.classList || []);
        iconName = icon.getAttribute('data-lucide') || classes.find(x => x.startsWith('lucide-') && x !== 'lucide')?.replace(/^lucide-/, '') || compact(icon.querySelector('title')?.textContent || '');
      }
    }
    const name = accessibleName(el).slice(0, 240);
    if (!iconName) {
      const clue = name.toLowerCase();
      if (/ai\s*助手|assistant|机器人|robot/.test(clue)) iconName = 'bot';
      else if (/浅色|light theme/.test(clue)) iconName = 'sun';
      else if (/深色|dark theme/.test(clue)) iconName = 'moon';
      else if (/关闭|close/.test(clue)) iconName = 'x';
      else if (/搜索|search/.test(clue)) iconName = 'search';
      else if (/语言|language/.test(clue)) iconName = 'globe';
      else if (/菜单|menu/.test(clue)) iconName = 'menu';
    }
    const visualControl = ['BUTTON','A','SUMMARY'].includes(el.tagName) || ['button','link','tab','menuitem'].includes(roleOf(el));
    const shared = sharedContext(el);
    return {
      tag: el.tagName.toLowerCase(),
      role: roleOf(el),
      type: el.getAttribute('type') || '',
      label: name,
      accessible_name: name,
      visible_text: visibleText.slice(0, 240),
      icon_only: !visibleText && !!name && (visualControl || !!icon),
      icon_name: iconName.slice(0, 120),
      aria: compact(el.getAttribute('aria-label')).slice(0, 240),
      title: compact(el.getAttribute('title')).slice(0, 240),
      placeholder: compact(el.getAttribute('placeholder')).slice(0, 240),
      stable_id: compact(el.id).slice(0, 160),
      testid: compact(testId(el)).slice(0, 160),
      name: compact(el.getAttribute('name')).slice(0, 160),
      document_language: compact(document.documentElement.lang).slice(0, 40),
      href,
      target: el.getAttribute('target') || '',
      download: el.hasAttribute('download'),
      disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true',
      expanded: el.getAttribute('aria-expanded'),
      pressed: el.getAttribute('aria-pressed'),
      checked: typeof el.checked === 'boolean' ? el.checked : null,
      selected: el.tagName === 'SELECT' ? el.value : null,
      section: section(el).slice(0, 160),
      shared_scope: shared.scope,
      shared_container_signature: shared.signature,
      shared_forced: shared.forced,
      selector: cssPath(el),
      domIndex,
      rect: {x: Math.round(rect.x), y: Math.round(rect.y), width: Math.round(rect.width), height: Math.round(rect.height)},
      constraints: {
        required: !!el.required,
        min: el.getAttribute('min'), max: el.getAttribute('max'), step: el.getAttribute('step'),
        minlength: el.getAttribute('minlength'), maxlength: el.getAttribute('maxlength'),
        pattern: el.getAttribute('pattern'), autocomplete: el.getAttribute('autocomplete')
      }
    };
  }).filter(Boolean);
}
"""


SNAPSHOT_JS = r"""
() => {
  const compact = value => (value || '').replace(/\s+/g, ' ').trim();
  const visible = el => {
    if (!el || !el.isConnected || el.closest('[hidden],[inert],[aria-hidden="true"]')) return false;
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width >= 1 && r.height >= 1 && el.getClientRects().length > 0 && s.display !== 'none' && s.visibility !== 'hidden' && Number(s.opacity) !== 0;
  };
  const bodyText = compact(document.body?.innerText || '');
  const normalizedText = bodyText
    .replace(/\b\d{4}-\d{1,2}-\d{1,2}[T\s][\d:.+-Z]+\b/g, '<time>')
    .replace(/\b\d+(?:[.,]\d+)*(?:ms|s|%|K|M|GB|MB)?\b/gi, '<n>');
  const dialogs = Array.from(document.querySelectorAll('dialog,[role=dialog]')).filter(visible)
    .map(el => compact(el.innerText).slice(0, 1000));
  const headings = Array.from(document.querySelectorAll('h1,h2,h3')).filter(visible)
    .map(el => compact(el.innerText).slice(0, 240)).filter(Boolean).slice(0, 30);
  const root = document.documentElement;
  const body = document.body;
  const rootStyle = getComputedStyle(root);
  const bodyStyle = body ? getComputedStyle(body) : rootStyle;
  const theme = {
    html_class: Array.from(root.classList).sort().join(' '),
    data_theme: root.getAttribute('data-theme') || body?.getAttribute('data-theme') || '',
    color_scheme: rootStyle.colorScheme || bodyStyle.colorScheme || '',
    body_background: bodyStyle.backgroundColor || '',
    body_color: bodyStyle.color || '',
  };
  return {
    url: location.href,
    title: document.title,
    body_text: bodyText.slice(0, 12000),
    normalized_text: normalizedText.slice(0, 8000),
    body_chars: bodyText.length,
    dialogs,
    headings,
    scroll_x: Math.round(scrollX),
    scroll_y: Math.round(scrollY),
    forms: document.forms.length,
    theme,
  };
}
"""


def _norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().lower()


def enrich_controls(base_url: str, controls: list[dict]) -> list[dict]:
    enriched: list[dict] = []
    for item in controls:
        label = item.get("label") or item.get("aria") or item.get("title") or item.get("placeholder") or ""
        label = re.sub(r"\s+", " ", label).strip()[:240]
        href = item.get("href") or ""
        if not label and href:
            parts = urlsplit(href)
            path_parts = [unquote(part).replace("-", " ") for part in parts.path.split("/") if part]
            target_name = unquote(parts.fragment).replace("-", " ") if parts.fragment else (path_parts[-1] if path_parts else parts.hostname or href)
            section = item.get("section") or ""
            label = f"{section} → {target_name}" if section else f"链接：{target_name}"
        if not label:
            kind = {"a": "链接", "button": "按钮", "input": "输入控件"}.get(item.get("tag"), item.get("tag") or "控件")
            label = f"未命名{kind}" + (f"（{item.get('section')}）" if item.get("section") else "")
        if item.get("icon_only"):
            icon_names = {
                "bot": "机器人",
                "x": "关闭",
                "bug": "问题反馈",
                "copy": "复制",
                "play": "播放",
                "pause": "暂停",
                "sun": "太阳",
                "moon": "月亮",
                "globe": "地球",
                "menu": "菜单",
                "search": "搜索",
                "chevron-down": "下拉箭头",
                "chevron-up": "收起箭头",
            }
            raw_icon = str(item.get("icon_name") or "").lower()
            icon_label = icon_names.get(raw_icon, raw_icon.replace("-", " ") if raw_icon else "")
            control_kind = "按钮" if item.get("role") == "button" or item.get("tag") == "button" else "链接"
            visual = f"{icon_label}图标{control_kind}" if icon_label else f"图标{control_kind}"
            if visual not in label:
                label = f"{label}（{visual}）"
        item["label"] = label[:240]
        if href.startswith(("http://", "https://", "/")):
            href_key = canonical_url(base_url, href)
            fragment = urlsplit(href).fragment
            if fragment:
                href_key += f"#{fragment}"
        else:
            href_key = href
        semantic_raw = "|".join(
            [
                _norm(item.get("role")),
                _norm(item.get("tag")),
                _norm(item.get("type")),
                _norm(item["label"]),
                _norm(href_key),
                _norm(item.get("section")),
            ]
        )
        behavior_raw = "|".join(
            [
                semantic_raw,
                str(item.get("expanded")),
                str(item.get("pressed")),
                str(item.get("checked")),
                str(item.get("selected")),
            ]
        )
        item["semantic_key"] = sha(semantic_raw, 24)
        item["behavior_key"] = sha(behavior_raw, 24)
        item["control_key"] = sha(behavior_raw + "|" + str(item.get("selector")), 24)
        shared_scope = _norm(item.get("shared_scope"))
        stable_attribute = next(
            (
                f"{key}:{_norm(item.get(key))}"
                for key in ("testid", "stable_id", "name")
                if _norm(item.get(key))
            ),
            "",
        )
        if shared_scope:
            strong_semantics = bool(
                stable_attribute
                or href_key
                or _norm(item.get("aria"))
                or _norm(item.get("title"))
                or item.get("shared_forced")
            )
            item["shared_confidence"] = "high" if strong_semantics else "medium"
            shared_raw = "|".join(
                [
                    _norm(urlsplit(base_url).netloc),
                    _norm(item.get("document_language")),
                    shared_scope,
                    _norm(item.get("shared_container_signature")),
                    _norm(item.get("role")),
                    _norm(item.get("tag")),
                    _norm(item.get("type")),
                    stable_attribute,
                    _norm(item["label"]),
                    _norm(href_key),
                ]
            )
            item["shared_control_key"] = sha(shared_raw, 32)
        else:
            item["shared_confidence"] = "none"
            item["shared_control_key"] = ""
        enriched.append(item)
    return enriched


def collect_controls(
    page,
    base_url: str,
    max_controls: int,
    shared_rules: dict | None = None,
) -> list[dict]:
    shared_rules = shared_rules or {}
    raw = page.evaluate(
        COLLECT_JS,
        {
            "selector": INTERACTIVE_SELECTOR,
            "includeSelectors": shared_rules.get("include_selectors") or [],
            "excludeSelectors": shared_rules.get("exclude_selectors") or [],
        },
    )
    return enrich_controls(base_url, raw[:max_controls])


def snapshot(
    page,
    base_url: str,
    max_controls: int,
    shared_rules: dict | None = None,
) -> tuple[dict, list[dict]]:
    content = page.evaluate(SNAPSHOT_JS)
    controls = collect_controls(page, base_url, max_controls, shared_rules)
    control_state = sorted({item["semantic_key"] for item in controls})
    stable = {
        "url": canonical_url(base_url, content.get("url") or base_url),
        "title": content.get("title") or "",
        "normalized_text": content.get("normalized_text") or "",
        "dialogs": content.get("dialogs") or [],
        "headings": content.get("headings") or [],
        "theme": content.get("theme") or {},
        "controls": control_state,
    }
    content["fingerprint"] = sha(json_text(stable), 32)
    content["visible_text_hash"] = sha(content.get("body_text") or "", 24)
    content["url"] = content.get("url") or page.url
    content["control_count"] = len(controls)
    content.pop("normalized_text", None)
    return content, controls


def locate_control(page, descriptor: dict):
    selector = descriptor.get("selector")
    if selector:
        locator = page.locator(selector).first
        try:
            if locator.count() and locator.is_visible(timeout=800):
                return locator
        except Exception:
            pass
    role = descriptor.get("role")
    label = descriptor.get("accessible_name") or descriptor.get("label")
    if role and label:
        try:
            locator = page.get_by_role(role, name=label, exact=True).first
            if locator.count() and locator.is_visible(timeout=800):
                return locator
        except Exception:
            pass
    placeholder = descriptor.get("placeholder")
    if placeholder:
        try:
            locator = page.get_by_placeholder(placeholder, exact=True).first
            if locator.count() and locator.is_visible(timeout=800):
                return locator
        except Exception:
            pass
    if label:
        return page.get_by_text(label, exact=True).first
    return page.locator(selector or "__webqa_missing_control__").first


def observable_delta(before: dict, after: dict) -> list[str]:
    changes: list[str] = []
    if before.get("url") != after.get("url"):
        changes.append(f"URL：{before.get('url')} → {after.get('url')}")
    if before.get("title") != after.get("title"):
        changes.append("页面标题发生变化")
    if before.get("dialogs") != after.get("dialogs"):
        changes.append(f"可见对话框数量：{len(before.get('dialogs') or [])} → {len(after.get('dialogs') or [])}")
    if before.get("headings") != after.get("headings"):
        changes.append("页面标题区域发生变化")
    if before.get("visible_text_hash") != after.get("visible_text_hash"):
        changes.append("可见内容发生变化")
    if before.get("control_count") != after.get("control_count"):
        changes.append(f"可见控件数量：{before.get('control_count')} → {after.get('control_count')}")
    before_theme = before.get("theme") or {}
    after_theme = after.get("theme") or {}
    if before_theme != after_theme:
        labels = {
            "html_class": "HTML class",
            "data_theme": "data-theme",
            "color_scheme": "color-scheme",
            "body_background": "页面背景色",
            "body_color": "页面文字色",
        }
        changed = [labels.get(key, key) for key in labels if before_theme.get(key) != after_theme.get(key)]
        changes.append(f"主题外观发生变化（{'、'.join(changed)}）")
    if before.get("scroll_y") != after.get("scroll_y"):
        changes.append(f"页面滚动位置：{before.get('scroll_y')} → {after.get('scroll_y')}")
    return changes


def action_step(descriptor: dict) -> dict:
    return {
        "label": f"点击「{descriptor.get('label') or '未命名控件'}」",
        "selector": descriptor.get("selector"),
        "role": descriptor.get("role"),
        "name": descriptor.get("accessible_name") or descriptor.get("label"),
        "href": descriptor.get("href") or None,
        "section": descriptor.get("section") or None,
        "descriptor": descriptor,
    }
