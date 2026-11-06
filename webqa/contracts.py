from __future__ import annotations

import re
from typing import Any

from .utils import json_text, sha


def _finding(
    contract_type: str,
    rule_key: str,
    status: str,
    severity: str,
    title: str,
    expected: str,
    actual: str,
    evidence: dict | None = None,
) -> dict:
    value = {
        "contract_type": contract_type,
        "rule_key": rule_key,
        "status": status,
        "severity": severity,
        "title": title,
        "expected": expected,
        "actual": actual,
        "evidence": evidence or {},
    }
    value["fingerprint"] = sha(
        "|".join([contract_type, rule_key, title, expected, actual]), 32
    )
    return value


def _status_allowed(status: int, expected: list[Any]) -> bool:
    for item in expected:
        if isinstance(item, int) and status == item:
            return True
        text = str(item).strip().lower()
        if text.isdigit() and status == int(text):
            return True
        if re.fullmatch(r"[1-5]xx", text) and status // 100 == int(text[0]):
            return True
    return False


def _path_values(value: Any, path: str) -> list[Any]:
    parts = [part for part in str(path).split(".") if part]
    current = [value]
    for part in parts:
        next_values: list[Any] = []
        for item in current:
            if part == "*" and isinstance(item, list):
                next_values.extend(item)
            elif isinstance(item, dict) and part in item:
                next_values.append(item[part])
            elif isinstance(item, list) and part.isdigit() and int(part) < len(item):
                next_values.append(item[int(part)])
        current = next_values
        if not current:
            break
    return current


def _simple_schema_errors(instance: Any, schema: dict, path: str = "$") -> list[str]:
    errors: list[str] = []
    expected_type = schema.get("type")
    type_map = {
        "object": dict,
        "array": list,
        "string": str,
        "number": (int, float),
        "integer": int,
        "boolean": bool,
        "null": type(None),
    }
    if expected_type in type_map and not isinstance(instance, type_map[expected_type]):
        return [f"{path} 应为 {expected_type}，实际为 {type(instance).__name__}"]
    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}={instance!r} 不在枚举 {schema['enum']!r} 中")
    if isinstance(instance, dict):
        for key in schema.get("required") or []:
            if key not in instance:
                errors.append(f"{path} 缺少必填字段 {key}")
        for key, child_schema in (schema.get("properties") or {}).items():
            if key in instance and isinstance(child_schema, dict):
                errors.extend(_simple_schema_errors(instance[key], child_schema, f"{path}.{key}"))
    if isinstance(instance, list) and isinstance(schema.get("items"), dict):
        for index, item in enumerate(instance[:100]):
            errors.extend(_simple_schema_errors(item, schema["items"], f"{path}[{index}]"))
    return errors[:50]


def validate_json_schema(instance: Any, schema: dict) -> list[str]:
    try:
        from jsonschema import Draft202012Validator

        validator = Draft202012Validator(schema)
        return [
            f"{'/'.join(str(part) for part in error.absolute_path) or '$'}: {error.message}"
            for error in sorted(validator.iter_errors(instance), key=lambda item: list(item.path))[:50]
        ]
    except ImportError:
        return _simple_schema_errors(instance, schema)
    except Exception as exc:
        return [f"JSON Schema 本身无效：{type(exc).__name__}: {exc}"]


def evaluate_api_contracts(
    responses: list[dict],
    settings: dict | None,
    *,
    phase: str = "INITIAL_PAGE",
    page_url: str = "",
) -> list[dict]:
    settings = settings or {}
    if not settings.get("enabled", True):
        return []
    results: list[dict] = []
    if settings.get("automatic_http_errors", True):
        for response in responses:
            status = int(response.get("status") or 0)
            if status >= 400:
                results.append(
                    _finding(
                        "API",
                        "automatic-http-status",
                        "FAIL",
                        "major" if status >= 500 else "minor",
                        "接口返回错误状态",
                        "XHR/fetch 请求应返回 2xx 或受控的 3xx 状态",
                        f"{response.get('method')} {response.get('url')} -> HTTP {status}",
                        {"response": response},
                    )
                )
    for index, rule in enumerate(settings.get("rules") or []):
        key = str(rule.get("key") or f"api-rule-{index + 1}")
        phases = {str(value) for value in rule.get("phases") or []}
        if phases and phase not in phases:
            continue
        page_pattern = str(rule.get("page_url_regex") or ".*")
        try:
            if page_url and not re.search(page_pattern, page_url, re.I):
                continue
        except re.error as exc:
            results.append(
                _finding(
                    "API",
                    key,
                    "UNCONFIRMED",
                    "info",
                    "API 契约页面正则无效",
                    page_pattern,
                    str(exc),
                )
            )
            continue
        pattern = str(rule.get("url_regex") or ".*")
        methods = {str(item).upper() for item in rule.get("methods") or []}
        try:
            matched = [
                response
                for response in responses
                if re.search(pattern, str(response.get("url") or ""), re.I)
                and (not methods or str(response.get("method") or "GET").upper() in methods)
            ]
        except re.error as exc:
            results.append(
                _finding(
                    "API",
                    key,
                    "UNCONFIRMED",
                    "info",
                    "API 契约 URL 正则无效",
                    pattern,
                    str(exc),
                )
            )
            continue
        if not matched:
            if rule.get("required", True) and (phases or phase == "INITIAL_PAGE"):
                results.append(
                    _finding(
                        "API",
                        key,
                        "FAIL",
                        str(rule.get("severity") or "major"),
                        "必需接口未被观察到",
                        f"页面应请求匹配 {pattern!r} 的接口",
                        "本次页面观察没有匹配请求",
                    )
                )
            continue
        expected_statuses = list(rule.get("statuses") or ["2xx"])
        content_types = [str(value).lower() for value in rule.get("content_types") or []]
        for response in matched:
            prefix = f"{response.get('method')} {response.get('url')}"
            status = int(response.get("status") or 0)
            if not _status_allowed(status, expected_statuses):
                results.append(
                    _finding(
                        "API",
                        key,
                        "FAIL",
                        str(rule.get("severity") or "major"),
                        "API 状态码违反契约",
                        str(expected_statuses),
                        f"{prefix} -> HTTP {status}",
                        {"response": response},
                    )
                )
            actual_content_type = str(response.get("content_type") or "").lower()
            if content_types and not any(value in actual_content_type for value in content_types):
                results.append(
                    _finding(
                        "API",
                        key,
                        "FAIL",
                        str(rule.get("severity") or "major"),
                        "API Content-Type 违反契约",
                        str(content_types),
                        actual_content_type or "未提供 Content-Type",
                        {"url": response.get("url")},
                    )
                )
            payload = response.get("json")
            needs_json = bool(
                rule.get("json_schema")
                or rule.get("required_fields")
                or rule.get("enums")
            )
            if needs_json and payload is None:
                results.append(
                    _finding(
                        "API",
                        key,
                        "FAIL",
                        str(rule.get("severity") or "major"),
                        "API 契约需要 JSON，但响应无法解析",
                        "可解析 JSON 响应",
                        str(response.get("body_error") or "没有捕获到 JSON 正文"),
                        {"url": response.get("url")},
                    )
                )
                continue
            if isinstance(rule.get("json_schema"), dict):
                errors = validate_json_schema(payload, rule["json_schema"])
                if errors:
                    results.append(
                        _finding(
                            "API",
                            key,
                            "FAIL",
                            str(rule.get("severity") or "major"),
                            "JSON Schema 校验失败",
                            "响应应符合配置的 JSON Schema",
                            "；".join(errors[:10]),
                            {"url": response.get("url"), "schema_errors": errors},
                        )
                    )
            missing = [
                path
                for path in rule.get("required_fields") or []
                if not _path_values(payload, str(path))
            ]
            if missing:
                results.append(
                    _finding(
                        "API",
                        key,
                        "FAIL",
                        str(rule.get("severity") or "major"),
                        "API 响应缺少必填字段",
                        str(rule.get("required_fields")),
                        f"缺少：{missing}",
                        {"url": response.get("url")},
                    )
                )
            for path, allowed in (rule.get("enums") or {}).items():
                values = _path_values(payload, str(path))
                invalid = [value for value in values if value not in allowed]
                if invalid:
                    results.append(
                        _finding(
                            "API",
                            key,
                            "FAIL",
                            str(rule.get("severity") or "major"),
                            "API 字段超出允许枚举",
                            f"{path} ∈ {allowed}",
                            f"非法值：{invalid[:10]}",
                            {"url": response.get("url"), "path": path},
                        )
                    )
    return results


def evaluate_ui_contracts(page, url: str, settings: dict | None) -> list[dict]:
    settings = settings or {}
    if not settings.get("enabled", True):
        return []
    results: list[dict] = []
    if settings.get("automatic_title", True) and not page.title().strip():
        results.append(
            _finding(
                "UI",
                "automatic-title",
                "FAIL",
                "minor",
                "页面缺少标题",
                "document.title 应提供可识别的页面标题",
                "标题为空",
            )
        )
    if settings.get("automatic_accessible_names", True):
        unnamed = page.evaluate(
            """() => {
              const visible=el=>{const r=el.getBoundingClientRect(),s=getComputedStyle(el);return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden'};
              const named=el=>!!((el.getAttribute('aria-label')||'').trim()||(el.getAttribute('title')||'').trim()||(el.innerText||'').trim()||(el.value||'').trim()||(el.querySelector('img[alt],svg title')?.textContent||'').trim());
              return Array.from(document.querySelectorAll('button,a[href],[role="button"],[role="link"],input[type="submit"],input[type="button"]')).filter(el=>visible(el)&&!named(el)).slice(0,20).map(el=>({tag:el.tagName.toLowerCase(),id:el.id||'',role:el.getAttribute('role')||'',html:el.outerHTML.slice(0,240)}));
            }"""
        )
        if unnamed:
            results.append(
                _finding(
                    "UI",
                    "automatic-accessible-name",
                    "FAIL",
                    "minor",
                    "可交互控件缺少可访问名称",
                    "可见按钮和链接应有文本、ARIA label、title 或图片替代文本",
                    f"发现 {len(unnamed)} 个无名称控件",
                    {"controls": unnamed},
                )
            )
    for index, rule in enumerate(settings.get("rules") or []):
        key = str(rule.get("key") or f"ui-rule-{index + 1}")
        path_regex = str(rule.get("path_regex") or ".*")
        try:
            if not re.search(path_regex, url, re.I):
                continue
        except re.error as exc:
            results.append(
                _finding("UI", key, "UNCONFIRMED", "info", "UI 契约路径正则无效", path_regex, str(exc))
            )
            continue
        severity = str(rule.get("severity") or "major")
        title_regex = rule.get("title_regex")
        if title_regex and not re.search(str(title_regex), page.title(), re.I):
            results.append(
                _finding("UI", key, "FAIL", severity, "页面标题违反 UI 契约", str(title_regex), page.title())
            )
        for selector in rule.get("required_selectors") or []:
            count = page.locator(str(selector)).count()
            if count == 0:
                results.append(
                    _finding("UI", key, "FAIL", severity, "必需 UI 元素不存在", str(selector), "匹配数量 0")
                )
        for region in rule.get("required_regions") or []:
            selector = str(region)
            count = page.locator(selector).count()
            if count == 0:
                results.append(
                    _finding("UI", key, "FAIL", severity, "必需业务区域不存在", selector, "匹配数量 0")
                )
        for expected_text in rule.get("required_text") or []:
            count = page.get_by_text(str(expected_text), exact=False).count()
            if count == 0:
                results.append(
                    _finding("UI", key, "FAIL", severity, "页面缺少必需文案", str(expected_text), "未找到")
                )
        body_text = page.locator("body").inner_text()
        for forbidden in rule.get("forbidden_text") or []:
            if str(forbidden).lower() in body_text.lower():
                results.append(
                    _finding("UI", key, "FAIL", severity, "页面出现禁止文案", f"不应出现 {forbidden!r}", str(forbidden))
                )
        for role_rule in rule.get("required_roles") or []:
            if isinstance(role_rule, str):
                role, name = role_rule, None
            else:
                role = str(role_rule.get("role") or "")
                name = role_rule.get("name")
            locator = page.get_by_role(role, name=name) if name else page.get_by_role(role)
            if locator.count() == 0:
                results.append(
                    _finding(
                        "UI",
                        key,
                        "FAIL",
                        severity,
                        "页面缺少必需 ARIA role",
                        json_text(role_rule),
                        "匹配数量 0",
                    )
                )
    return results
