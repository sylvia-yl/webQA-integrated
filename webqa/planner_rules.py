from __future__ import annotations

from decimal import Decimal, InvalidOperation


FORM_TYPES = {"text", "email", "url", "tel", "number", "search", "date", "datetime-local", "textarea", "select"}


def _number(value):
    try:
        return Decimal(str(value)) if value not in {None, ""} else None
    except InvalidOperation:
        return None


def plan_field_cases(control: dict, prefix: str) -> list[dict]:
    """Generate deterministic, non-submitting field cases from HTML constraints."""
    field_type = (control.get("type") or control.get("tag") or "text").lower()
    if field_type not in FORM_TYPES or field_type == "password":
        return []
    constraints = control.get("constraints") or {}
    cases: list[dict] = []
    if constraints.get("required"):
        cases.append({"case_type": "empty", "value": "", "expected": "浏览器或页面提示必填"})
    valid = {
        "email": f"{prefix}ok@example.com",
        "url": "https://example.com/webqa",
        "tel": "13800138000",
        "number": "1",
        "search": f"{prefix}keyword",
        "date": "2026-01-15",
        "datetime-local": "2026-01-15T10:30",
    }.get(field_type, f"{prefix}valid")
    cases.append({"case_type": "valid", "value": valid, "expected": "字段接受合法值"})
    if field_type == "email":
        cases.append({"case_type": "invalid_format", "value": "invalid-email", "expected": "提示邮箱格式错误"})
    elif field_type == "url":
        cases.append({"case_type": "invalid_format", "value": "not-a-url", "expected": "提示 URL 格式错误"})
    elif field_type == "number":
        minimum, maximum = _number(constraints.get("min")), _number(constraints.get("max"))
        step = _number(constraints.get("step")) or Decimal(1)
        if minimum is not None:
            cases.extend(
                [
                    {"case_type": "min", "value": str(minimum), "expected": "接受最小边界"},
                    {"case_type": "below_min", "value": str(minimum - step), "expected": "拒绝小于最小值"},
                ]
            )
        if maximum is not None:
            cases.extend(
                [
                    {"case_type": "max", "value": str(maximum), "expected": "接受最大边界"},
                    {"case_type": "above_max", "value": str(maximum + step), "expected": "拒绝大于最大值"},
                ]
            )
    maxlength = constraints.get("maxlength")
    if maxlength not in {None, ""}:
        length = int(maxlength)
        cases.append({"case_type": "maxlength", "value": "a" * length, "expected": "接受最大长度"})
        cases.append({"case_type": "over_maxlength", "value": "a" * (length + 1), "expected": "截断或拒绝超长值"})
    return cases
