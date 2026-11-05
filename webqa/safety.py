from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


REDACTED = "[REDACTED]"
SENSITIVE_KEYS = {
    "access_token",
    "api_key",
    "apikey",
    "auth",
    "authorization",
    "cookie",
    "credential",
    "csrf",
    "password",
    "passwd",
    "pwd",
    "refresh_token",
    "secret",
    "session",
    "sessionid",
    "token",
}
DESTRUCTIVE_WORDS = (
    "delete", "remove", "destroy", "erase", "pay", "purchase", "checkout", "place order",
    "publish", "deploy", "withdraw", "transfer", "删除", "移除", "支付", "购买", "结算",
    "发布", "部署", "提现", "转账", "注销账号", "清空",
)
STATEFUL_WORDS = (
    "save", "submit", "send", "create", "update", "upload", "sign in", "sign up", "log in",
    "logout", "保存", "提交", "发送", "创建", "更新", "上传", "登录", "注册", "退出登录",
    "确认修改", "应用更改",
)


class Redactor:
    def __init__(self, settings: dict | None = None):
        settings = settings or {}
        self.enabled = bool(settings.get("redaction_enabled", True))
        self.redact_emails = bool(settings.get("redact_emails", True))
        self.redact_phone_numbers = bool(settings.get("redact_phone_numbers", True))
        configured = {str(value).strip().lower() for value in settings.get("sensitive_keys") or []}
        self.sensitive_keys = SENSITIVE_KEYS | configured

    def _sensitive_key(self, value: object) -> bool:
        key = re.sub(r"[^a-z0-9_]+", "_", str(value or "").strip().lower()).strip("_")
        return key in self.sensitive_keys or any(
            marker in key for marker in ("password", "passwd", "token", "secret", "credential")
        )

    def redact_url(self, value: str) -> str:
        if not self.enabled or not value:
            return value
        try:
            parts = urlsplit(value)
        except ValueError:
            return self.redact_text(value, redact_urls=False)
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            return self.redact_text(value, redact_urls=False)
        host = parts.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        netloc = host
        try:
            port = parts.port
        except ValueError:
            port = None
        if port:
            netloc += f":{port}"
        query = urlencode(
            [(key, REDACTED if self._sensitive_key(key) else item) for key, item in parse_qsl(parts.query, keep_blank_values=True)],
            doseq=True,
        )
        fragment = REDACTED if parts.fragment else ""
        return urlunsplit((parts.scheme, netloc, parts.path, query, fragment))

    def redact_text(self, value: str, *, redact_urls: bool = True) -> str:
        if not self.enabled or not value:
            return value
        text = str(value)
        if redact_urls:
            trailing = ".,;:!?)]}，。；：！？）】》"

            def redact_match(match: re.Match) -> str:
                raw = match.group(0)
                url = raw.rstrip(trailing)
                return self.redact_url(url) + raw[len(url) :]

            text = re.sub(r"https?://[^\s\"'<>]+", redact_match, text)
            text = text.replace(
                "%EF%BC%8C%E5%8F%AA%E8%AE%B0%E5%BD%95%E4%B8%8D%E8%BF%9B%E5%85%A5",
                "，只记录不进入",
            )
        text = re.sub(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", text)
        keys = "|".join(re.escape(key) for key in sorted(self.sensitive_keys, key=len, reverse=True))
        text = re.sub(
            rf"(?i)([\"'])({keys})\1\s*:\s*([\"'])[^\"']*\3",
            lambda match: (
                f"{match.group(1)}{match.group(2)}{match.group(1)}:"
                f"{match.group(3)}{REDACTED}{match.group(3)}"
            ),
            text,
        )
        text = re.sub(
            rf"(?i)\b({keys})\b\s*[:=]\s*([^\s,;&]+)",
            lambda match: f"{match.group(1)}={REDACTED}",
            text,
        )
        if self.redact_emails:
            text = re.sub(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])", "[REDACTED_EMAIL]", text)
        if self.redact_phone_numbers:
            text = re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)", "[REDACTED_PHONE]", text)
        return text

    def redact_value(self, value, *, key: str | None = None):
        if not self.enabled:
            return value
        if key is not None and self._sensitive_key(key):
            return REDACTED
        if isinstance(value, str):
            return self.redact_text(value)
        if isinstance(value, list):
            return [self.redact_value(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self.redact_value(item) for item in value)
        if isinstance(value, dict):
            return {item_key: self.redact_value(item, key=str(item_key)) for item_key, item in value.items()}
        return value


@dataclass(frozen=True)
class ActionRisk:
    level: str
    allowed: bool
    retry_safe: bool
    reason: str
    matched: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _first_match(text: str, words) -> str | None:
    lowered = text.lower()
    return next((str(word) for word in words if str(word).lower() in lowered), None)


def classify_action(descriptor: dict, safety: dict | None = None, prohibited=None) -> ActionRisk:
    safety = safety or {}
    text = " ".join(
        str(descriptor.get(key) or "") for key in ("label", "href", "title", "aria", "type", "role")
    )
    prohibited_match = _first_match(text, prohibited or [])
    if prohibited_match:
        return ActionRisk("DESTRUCTIVE", False, False, "命中项目禁止动作策略", prohibited_match)
    destructive_match = _first_match(text, DESTRUCTIVE_WORDS)
    if destructive_match:
        allowed = bool(safety.get("allow_destructive_actions", False))
        return ActionRisk("DESTRUCTIVE", allowed, False, "动作可能产生不可逆或高影响副作用", destructive_match)
    field_type = str(descriptor.get("type") or "").lower()
    stateful_match = "type=submit" if field_type == "submit" else _first_match(text, STATEFUL_WORDS)
    if stateful_match:
        allowed = bool(safety.get("allow_stateful_actions", False))
        return ActionRisk("STATEFUL", allowed, False, "动作可能产生持久化副作用", stateful_match)
    tag = str(descriptor.get("tag") or "").lower()
    role = str(descriptor.get("role") or "").lower()
    href = str(descriptor.get("href") or "")
    if href or tag in {"a", "summary"} or role in {"link", "tab", "menuitem"}:
        return ActionRisk("READ_ONLY", True, True, "只读导航或可重复选择动作")
    if field_type in {"checkbox", "radio", "file"}:
        allowed = bool(safety.get("allow_stateful_actions", False))
        return ActionRisk("STATEFUL", allowed, False, "输入控件可能自动保存状态", field_type)
    allowed = bool(safety.get("allow_unknown_actions", True))
    return ActionRisk("UNKNOWN", allowed, False, "无法证明动作可安全重试")


@dataclass(frozen=True)
class ExceptionClassification:
    category: str
    retryable: bool
    reason: str
    delay_ms: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


def classify_exception(error: BaseException | str, evidence: dict | None = None, stage: str = "automation") -> ExceptionClassification:
    evidence = evidence or {}
    message = f"{type(error).__name__}: {error}" if isinstance(error, BaseException) else str(error)
    lowered = message.lower()
    statuses = {
        int(item["status"])
        for item in evidence.get("document_responses") or []
        if isinstance(item, dict) and str(item.get("status", "")).isdigit()
    }
    if statuses & {401, 403} or any(
        value in lowered for value in ("http 401", "http 403", "unauthorized", "forbidden", "session expired", "登录已失效")
    ):
        return ExceptionClassification("AUTH_BLOCKED", False, "登录、会话或权限阻塞")
    if 429 in statuses or any(value in lowered for value in ("http 429", "too many requests", "rate limit", "限流")):
        return ExceptionClassification("RATE_LIMITED", True, "服务端限流", 1500)
    if evidence.get("page_errors"):
        return ExceptionClassification("PRODUCT_ERROR", False, "页面脚本异常")
    locator_terms = ("控件不可见", "locator", "strict mode", "not visible", "not attached", "无法重放")
    if stage in {"locate", "replay"} or any(value in lowered for value in locator_terms):
        return ExceptionClassification("LOCATOR_FAILURE", True, "控件定位或状态重放失败", 200)
    if "timeout" in lowered or "超时" in lowered:
        category = "LOAD_TIMEOUT" if stage in {"open", "replay"} else "ACTION_TIMEOUT"
        return ExceptionClassification(category, True, "等待页面或动作完成时超时", 500)
    failed = " ".join(str(value) for value in evidence.get("failed_requests") or []).lower()
    if failed or any(value in lowered for value in ("net::", "err_connection", "connection closed", "network")):
        return ExceptionClassification("NETWORK_FAILURE", True, "网络请求失败", 500)
    return ExceptionClassification("AUTOMATION_ERROR", False, "自动化执行异常")


@dataclass(frozen=True)
class RecoveryDecision:
    status: str
    retry: bool
    reason: str
    delay_ms: int = 0


def decide_recovery(
    classification: ExceptionClassification,
    risk: ActionRisk,
    *,
    action_attempted: bool,
    state_changed: bool,
    attempts: int,
    retry_limit: int,
) -> RecoveryDecision:
    if action_attempted and state_changed:
        status = "TECH_PASS_WITH_WARNINGS" if risk.retry_safe else "APPLIED_UNVERIFIED"
        return RecoveryDecision(status, False, "动作后页面状态已经变化；停止重试以避免重复操作")
    if action_attempted and not risk.retry_safe:
        return RecoveryDecision("UNKNOWN_COMMIT", False, "动作已触发但结果不确定，禁止自动重试")
    if classification.category == "AUTH_BLOCKED":
        return RecoveryDecision("BLOCKED", False, classification.reason)
    if classification.retryable and attempts < retry_limit:
        return RecoveryDecision("RETRY", True, classification.reason, classification.delay_ms)
    return RecoveryDecision("UNCONFIRMED", False, classification.reason)

