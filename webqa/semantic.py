from __future__ import annotations

import importlib
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol
from urllib.parse import unquote, urlsplit

from .utils import sha


FAILURE_PATTERNS = (
    r"(?:暂时|当前)?(?:无法|不能|读不到|未能)(?:加载|获取|读取|显示)?(?:到)?(?:数据|内容|结果|页面)?",
    r"(?:接口|服务|服务器|网络).{0,12}(?:没有响应|无响应|不可用|异常|失败|超时)",
    r"(?:数据|内容|结果).{0,10}(?:加载失败|获取失败|不可用|异常)",
    r"(?:failed|unable) to (?:load|fetch|read)|(?:service|server|network|api).{0,16}(?:unavailable|failed|timeout|not responding)",
)
EMPTY_PATTERNS = (
    r"暂无(?:数据|内容|结果|记录)",
    r"没有(?:数据|内容|结果|记录|匹配项)",
    r"没有匹配(?:结果|项)",
    r"未找到(?:数据|内容|结果|记录|匹配项)",
    r"no (?:data|content|results?|records?|matches)(?: yet)?",
    r"nothing (?:here|to show)|empty state",
)
ERROR_PAGE_PATTERNS = (
    r"\b(?:404|500)\b",
    r"page not found|internal server error|application error",
    r"页面不存在|找不到页面|服务器错误|内部错误|发生错误|出错了",
)
FINDING_CLASSES = {
    "CONTENT_UNAVAILABLE",
    "DATA_MISSING",
    "STUCK_LOADING",
    "COPY_QUALITY",
    "BUSINESS_MISMATCH",
    "ORACLE_UNCERTAIN",
    "SEMANTIC_CHECK_UNAVAILABLE",
}


@dataclass
class PageObservation:
    url: str
    title: str = ""
    language: str = ""
    body_text: str = ""
    main_text: str = ""
    headings: list[str] = field(default_factory=list)
    alerts: list[str] = field(default_factory=list)
    statuses: list[str] = field(default_factory=list)
    loading_indicators: list[str] = field(default_factory=list)
    loading_wait_timed_out: bool = False
    ready_state: str = "unknown"
    http_status: int | None = None
    table_count: int = 0
    table_data_rows: int = 0
    list_count: int = 0
    list_items: int = 0
    card_count: int = 0
    form_count: int = 0
    field_count: int = 0
    search_field_count: int = 0
    detail_pairs: int = 0
    broken_images: list[str] = field(default_factory=list)
    truncated_texts: list[str] = field(default_factory=list)
    event_evidence: dict = field(default_factory=dict)

    @property
    def body_chars(self) -> int:
        return len(self.body_text)

    @property
    def main_chars(self) -> int:
        return len(self.main_text)

    @property
    def data_item_count(self) -> int:
        return self.table_data_rows + self.list_items + self.card_count + self.detail_pairs

    def to_dict(self) -> dict:
        value = asdict(self)
        value.update(
            {
                "body_chars": self.body_chars,
                "main_chars": self.main_chars,
                "data_item_count": self.data_item_count,
            }
        )
        return value


@dataclass
class PageUnderstanding:
    primary_type: str
    confidence: str
    candidates: list[str]
    evidence: list[str]

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class OracleRule:
    rule_key: str
    source: str
    expected: Any
    conditions: dict = field(default_factory=dict)
    forbidden_side_effects: list[str] = field(default_factory=list)
    confidence: str = "medium"
    reference: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SemanticFinding:
    classification: str
    title: str
    expected: str
    actual: str
    confidence: str
    severity: str
    oracle_source: str
    evidence: dict = field(default_factory=dict)
    bug_eligible: bool = False
    fingerprint: str = ""

    def to_dict(self) -> dict:
        value = asdict(self)
        if not value["fingerprint"]:
            value["fingerprint"] = sha(
                "|".join(
                    [
                        self.classification,
                        self.expected,
                        self.actual,
                        self.oracle_source,
                    ]
                ),
                32,
            )
        return value


@dataclass
class SemanticEvaluation:
    status: str
    coverage: str
    understanding: PageUnderstanding
    rules: list[OracleRule]
    findings: list[SemanticFinding]

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "coverage": self.coverage,
            "understanding": self.understanding.to_dict(),
            "rules": [rule.to_dict() for rule in self.rules],
            "findings": [finding.to_dict() for finding in self.findings],
        }


class SemanticModel(Protocol):
    """Optional model adapter. Implementations must return structured findings only."""

    def evaluate(
        self,
        observation: PageObservation,
        understanding: PageUnderstanding,
        rules: list[OracleRule],
    ) -> list[SemanticFinding]: ...


class _UnavailableSemanticModel:
    def __init__(self, error: str):
        self.error = error

    def evaluate(self, observation, understanding, rules):
        raise RuntimeError(self.error)


def load_semantic_model(settings: dict | None) -> SemanticModel | None:
    """Load a local adapter as ``package.module:factory`` without coupling WebQA to one vendor."""
    model_settings = (settings or {}).get("model") or {}
    if not model_settings.get("enabled"):
        return None
    provider = str(model_settings.get("provider") or "none").lower()
    if provider != "python":
        return _UnavailableSemanticModel(f"不支持的语义模型 provider：{provider}")
    adapter = str(model_settings.get("adapter") or "")
    try:
        module_name, object_name = adapter.split(":", 1)
        factory = getattr(importlib.import_module(module_name), object_name)
        instance = factory(dict(model_settings.get("options") or {}))
        if not callable(getattr(instance, "evaluate", None)):
            raise TypeError("适配器实例没有 evaluate(observation, understanding, rules) 方法")
        return instance
    except Exception as exc:
        return _UnavailableSemanticModel(
            f"无法加载语义模型适配器 {adapter!r}：{type(exc).__name__}: {exc}"
        )


def normalize_oracle_rules(raw_rules: Any) -> list[OracleRule]:
    if raw_rules is None:
        return []
    if isinstance(raw_rules, (str, dict)):
        raw_rules = [raw_rules]
    rules: list[OracleRule] = []
    for index, raw in enumerate(raw_rules or []):
        if isinstance(raw, str):
            source = "config.expected_behavior"
            expected: Any = raw.strip()
            conditions: dict = {}
            forbidden: list[str] = []
            confidence = "medium"
            reference = ""
            explicit_key = ""
        elif isinstance(raw, dict):
            source = str(raw.get("source") or "config.expected_behavior")
            expected = raw.get("expected_result", raw.get("expected", raw.get("rule", "")))
            conditions = dict(raw.get("conditions") or raw.get("applies_to") or {})
            forbidden = [str(item) for item in raw.get("forbidden_side_effects") or []]
            confidence = str(raw.get("confidence") or "medium").lower()
            reference = str(raw.get("reference") or raw.get("requirement") or "")
            explicit_key = str(raw.get("key") or "")
        else:
            continue
        if confidence not in {"low", "medium", "high"}:
            confidence = "medium"
        material = repr((source, expected, conditions, forbidden, reference, index))
        rules.append(
            OracleRule(
                rule_key=explicit_key or f"expected-{sha(material, 20)}",
                source=source,
                expected=expected,
                conditions=conditions,
                forbidden_side_effects=forbidden,
                confidence=confidence,
                reference=reference,
            )
        )
    return rules


def infer_page_type(observation: PageObservation) -> PageUnderstanding:
    path = unquote(urlsplit(observation.url).path).lower()
    text = " ".join([observation.title, *observation.headings, observation.main_text[:1200]]).lower()
    scores: dict[str, int] = {}
    evidence: dict[str, list[str]] = {}

    def add(kind: str, score: int, reason: str) -> None:
        scores[kind] = scores.get(kind, 0) + score
        evidence.setdefault(kind, []).append(reason)

    if observation.search_field_count or re.search(r"/(?:search|find)(?:/|$)|搜索|search", path + " " + text):
        add("SEARCH", 4, "检测到搜索输入或搜索路由/标题")
    if observation.form_count or observation.field_count:
        add("FORM", 2 + min(observation.field_count, 4), "检测到表单和输入字段")
    if re.search(r"/(?:login|signin|sign-in|auth)(?:/|$)|登录|sign in|log in", path + " " + text):
        add("LOGIN", 6, "检测到登录路由或标题")
    if observation.table_count:
        add("LIST", 5, f"检测到 {observation.table_count} 个表格")
    if observation.list_count and observation.list_items:
        add("LIST", 3, f"检测到 {observation.list_items} 个列表项")
    if observation.card_count >= 2:
        add("LIST", 2, f"检测到 {observation.card_count} 个重复卡片")
    if re.search(r"/(?:list|leaderboard|ranking|catalog|users?|items?|orders?)(?:/|$)|榜单|列表|排行", path + " " + text):
        add("LIST", 4, "路由或标题表现为集合页")
    if re.search(r"/(?:dashboard|overview|analytics|metrics)(?:/|$)|仪表盘|概览|数据看板", path + " " + text):
        add("DASHBOARD", 5, "路由或标题表现为仪表盘")
    if observation.detail_pairs >= 2:
        add("DETAIL", 4, f"检测到 {observation.detail_pairs} 组详情字段")
    if re.search(r"/(?:detail|profile|view)/|详情|profile", path + " " + text):
        add("DETAIL", 3, "路由或标题表现为详情页")
    if any(re.search(pattern, text, re.I) for pattern in ERROR_PAGE_PATTERNS):
        add("ERROR", 7, "标题或主区域表现为错误页")
    if any(re.search(pattern, text, re.I) for pattern in EMPTY_PATTERNS):
        add("EMPTY_STATE", 3, "检测到明确空状态文案")
    if not scores:
        add("CONTENT", 1, "未发现更具体的结构，按通用内容页处理")
    ordered = sorted(scores, key=lambda kind: (-scores[kind], kind))
    top = ordered[0]
    top_score = scores[top]
    confidence = "high" if top_score >= 5 else "medium" if top_score >= 3 else "low"
    return PageUnderstanding(top, confidence, ordered, evidence.get(top, []))


def _matches_conditions(rule: OracleRule, observation: PageObservation, understanding: PageUnderstanding) -> bool:
    conditions = rule.conditions or {}
    path = unquote(urlsplit(observation.url).path)
    path_regex = conditions.get("path_regex") or conditions.get("url_regex")
    if path_regex:
        try:
            if not re.search(str(path_regex), observation.url, re.I):
                return False
        except re.error:
            return False
    path_contains = conditions.get("path_contains")
    if path_contains and str(path_contains).lower() not in path.lower():
        return False
    page_types = conditions.get("page_type") or conditions.get("page_types")
    if page_types:
        if isinstance(page_types, str):
            page_types = [page_types]
        if understanding.primary_type not in {str(item).upper() for item in page_types}:
            return False
    return True


def _as_text_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value]
    return [str(value)]


class SemanticAnalyzer:
    def __init__(
        self,
        settings: dict | None = None,
        expected_behavior: Any = None,
        *,
        model: SemanticModel | None = None,
        persisted_rules: list[dict] | None = None,
    ):
        self.settings = settings or {}
        self.model = model
        self.rules = normalize_oracle_rules(expected_behavior)
        for raw in persisted_rules or []:
            evidence = raw.get("evidence") or raw.get("evidence_json") or {}
            if isinstance(evidence, dict) and evidence.get("rule"):
                self.rules.extend(normalize_oracle_rules(evidence["rule"]))

    def evaluate(
        self,
        observation: PageObservation,
        *,
        model_observation: PageObservation | None = None,
    ) -> SemanticEvaluation:
        understanding = infer_page_type(observation)
        applicable = [rule for rule in self.rules if _matches_conditions(rule, observation, understanding)]
        findings = self._deterministic_findings(observation, understanding, applicable)
        model_settings = self.settings.get("model") or {}
        model_requested = bool(model_settings.get("enabled")) or bool(model_settings.get("required"))
        coverage = "DETERMINISTIC_ONLY"
        if self.model is not None:
            try:
                raw_findings = self.model.evaluate(
                    model_observation or observation, understanding, applicable
                )
                findings.extend(self._normalize_model_findings(raw_findings))
                coverage = "DETERMINISTIC_AND_MODEL"
            except Exception as exc:
                coverage = "MODEL_UNAVAILABLE"
                findings.append(
                    SemanticFinding(
                        "SEMANTIC_CHECK_UNAVAILABLE",
                        "语义模型调用失败",
                        "配置的语义模型应返回结构化判断",
                        f"{type(exc).__name__}: {exc}",
                        "high",
                        "info",
                        "semantic.model",
                        {"provider": model_settings.get("provider") or "injected"},
                    )
                )
        elif model_requested:
            coverage = "MODEL_UNAVAILABLE"
            findings.append(
                SemanticFinding(
                    "SEMANTIC_CHECK_UNAVAILABLE",
                    "配置的语义模型不可用",
                    "需要模型参与主观文案与复杂业务语义判断",
                    "没有可用的模型适配器；确定性检查仍已执行",
                    "high",
                    "info",
                    "semantic.model",
                    {"provider": model_settings.get("provider") or "none"},
                )
            )
        ranks = {"low": 1, "medium": 2, "high": 3}
        actionable = [item for item in findings if item.classification != "SEMANTIC_CHECK_UNAVAILABLE"]
        if any(item.bug_eligible and ranks.get(item.confidence, 0) >= 3 for item in actionable):
            status = "SUSPECTED"
        elif actionable or coverage == "MODEL_UNAVAILABLE":
            status = "NEEDS_REVIEW"
        else:
            status = "PASS"
        return SemanticEvaluation(status, coverage, understanding, applicable, findings)

    @staticmethod
    def _normalize_model_findings(raw_findings: Any) -> list[SemanticFinding]:
        if raw_findings is None:
            return []
        if not isinstance(raw_findings, list):
            raise TypeError("语义模型必须返回 SemanticFinding 或字典组成的列表")
        normalized: list[SemanticFinding] = []
        for raw in raw_findings:
            if isinstance(raw, SemanticFinding):
                finding = raw
            elif isinstance(raw, dict):
                classification = str(raw.get("classification") or "ORACLE_UNCERTAIN").upper()
                if classification not in FINDING_CLASSES:
                    classification = "ORACLE_UNCERTAIN"
                confidence = str(raw.get("confidence") or "low").lower()
                if confidence not in {"low", "medium", "high"}:
                    confidence = "low"
                finding = SemanticFinding(
                    classification=classification,
                    title=str(raw.get("title") or "语义模型发现待确认项"),
                    expected=str(raw.get("expected") or "页面内容应符合用户可理解的业务语义"),
                    actual=str(raw.get("actual") or "模型未提供实际结果摘要"),
                    confidence=confidence,
                    severity=str(raw.get("severity") or "unknown"),
                    oracle_source=str(raw.get("oracle_source") or "semantic.model"),
                    evidence=dict(raw.get("evidence") or {}),
                    bug_eligible=bool(raw.get("bug_eligible")),
                )
            else:
                raise TypeError("语义模型发现项必须是 SemanticFinding 或字典")
            # Copy fluency is inherently subjective. It may be reviewed, but the
            # model alone cannot turn it into a confirmed product defect.
            if finding.classification == "COPY_QUALITY":
                finding.bug_eligible = False
            normalized.append(finding)
        return normalized

    def _deterministic_findings(
        self,
        observation: PageObservation,
        understanding: PageUnderstanding,
        rules: list[OracleRule],
    ) -> list[SemanticFinding]:
        findings: list[SemanticFinding] = []
        body = observation.body_text
        semantic_surface = " ".join(
            [observation.title, *observation.headings, *observation.alerts, *observation.statuses, observation.main_text]
        )
        failure_patterns = tuple(FAILURE_PATTERNS) + tuple(self.settings.get("failure_patterns") or [])
        empty_patterns = tuple(EMPTY_PATTERNS) + tuple(self.settings.get("empty_state_patterns") or [])
        failure_matches = [
            match.group(0)
            for pattern in failure_patterns
            for match in re.finditer(pattern, semantic_surface, re.I)
        ]
        empty_state = any(re.search(pattern, semantic_surface, re.I) for pattern in empty_patterns)
        collection_like = understanding.primary_type in {"LIST", "SEARCH", "DASHBOARD"}
        has_collection_structure = bool(observation.table_count or observation.list_count or observation.card_count)
        if failure_matches:
            failure_is_status_like = bool(
                observation.alerts
                or observation.statuses
                or observation.main_chars < 800
                or (
                    collection_like
                    and observation.table_data_rows == 0
                    and observation.card_count == 0
                )
            )
            confidence = "high" if failure_is_status_like else "medium"
            findings.append(
                SemanticFinding(
                    "CONTENT_UNAVAILABLE",
                    "页面明确表示内容或服务不可用",
                    "页面应提供可用内容，或展示真实且可区分的合理空状态",
                    "；".join(dict.fromkeys(failure_matches))[:500],
                    confidence,
                    "major",
                    "deterministic.failure-language",
                    {
                        "matched_text": list(dict.fromkeys(failure_matches))[:10],
                        "data_item_count": observation.data_item_count,
                        "alerts": observation.alerts[:5],
                        "statuses": observation.statuses[:5],
                    },
                    bug_eligible=confidence == "high",
                )
            )
        elif understanding.primary_type == "ERROR" or (
            observation.http_status is not None and observation.http_status >= 400
        ):
            findings.append(
                SemanticFinding(
                    "CONTENT_UNAVAILABLE",
                    "页面进入错误状态",
                    "页面应提供可用内容，或给出符合业务预期的受控错误状态",
                    f"页面类型为 {understanding.primary_type}，HTTP {observation.http_status}",
                    "high",
                    "major",
                    "http-and-page-structure",
                    {
                        "http_status": observation.http_status,
                        "page_type": understanding.to_dict(),
                        "headings": observation.headings[:10],
                    },
                    bug_eligible=True,
                )
            )
        if (
            collection_like
            and (has_collection_structure or understanding.confidence == "high")
            and observation.table_data_rows == 0
            and observation.card_count == 0
            and failure_matches
        ):
            findings.append(
                SemanticFinding(
                    "DATA_MISSING",
                    "数据型页面没有可用数据",
                    "页面的数据区域应展示数据，或展示真实且可区分的合理空状态",
                    "数据项为 0，且页面同时明确表示读取或服务失败",
                    "high",
                    "major",
                    "structure-and-failure-language",
                    {
                        "page_type": understanding.to_dict(),
                        "data_item_count": observation.data_item_count,
                        "matched_text": list(dict.fromkeys(failure_matches))[:10],
                    },
                    bug_eligible=True,
                )
            )
        if observation.loading_wait_timed_out or observation.loading_indicators:
            findings.append(
                SemanticFinding(
                    "STUCK_LOADING",
                    "页面加载状态未结束",
                    "数据加载应在等待窗口内结束，并进入内容、合理空状态或明确错误状态",
                    f"仍有 {len(observation.loading_indicators)} 个可见加载占位",
                    "high",
                    "major",
                    "dom.loading-state",
                    {
                        "timed_out": observation.loading_wait_timed_out,
                        "indicators": observation.loading_indicators[:10],
                    },
                    bug_eligible=True,
                )
            )
        if (
            collection_like
            and has_collection_structure
            and observation.data_item_count == 0
            and not empty_state
            and not failure_matches
            and not observation.loading_indicators
        ):
            confidence = "medium" if understanding.confidence == "high" else "low"
            findings.append(
                SemanticFinding(
                    "ORACLE_UNCERTAIN",
                    "集合区域为空但页面未解释原因",
                    "集合页应展示数据，或明确展示合理空状态",
                    "检测到集合结构，但数据项为 0，且没有识别到空状态或错误状态",
                    confidence,
                    "minor",
                    "structure.inference",
                    {
                        "page_type": understanding.to_dict(),
                        "table_count": observation.table_count,
                        "list_count": observation.list_count,
                    },
                    bug_eligible=False,
                )
            )
        if "�" in body or re.search(r"(?:Ã.|Â.|â€|ðŸ){2,}", body):
            findings.append(
                SemanticFinding(
                    "COPY_QUALITY",
                    "页面存在疑似乱码",
                    "用户可见文案应使用正确字符编码并可读",
                    "检测到 Unicode 替换字符或常见错误解码片段",
                    "high",
                    "minor",
                    "deterministic.encoding",
                    {"sample": body[:1000]},
                    bug_eligible=True,
                )
            )
        if observation.truncated_texts:
            findings.append(
                SemanticFinding(
                    "COPY_QUALITY",
                    "正文文案可能被异常截断",
                    "关键正文应完整可读",
                    f"检测到 {len(observation.truncated_texts)} 段被裁切且无完整文本提示的文案",
                    "low",
                    "minor",
                    "dom.text-overflow",
                    {"samples": observation.truncated_texts[:10]},
                    bug_eligible=False,
                )
            )
        for rule in rules:
            expected = rule.expected
            if not isinstance(expected, dict):
                if self.model is None and str(expected).strip():
                    findings.append(
                        SemanticFinding(
                            "ORACLE_UNCERTAIN",
                            "显式预期尚不能由确定性规则验证",
                            str(expected),
                            "该预期是自然语言描述；请改成结构化条件，或配置语义模型参与判断",
                            rule.confidence,
                            "info",
                            rule.source,
                            {"rule": rule.to_dict()},
                            bug_eligible=False,
                        )
                    )
                continue
            must_contain = _as_text_list(expected.get("must_contain"))
            missing = [item for item in must_contain if item and item.lower() not in body.lower()]
            must_not_contain = _as_text_list(expected.get("must_not_contain"))
            forbidden = [item for item in must_not_contain if item and item.lower() in body.lower()]
            min_items = expected.get("min_items")
            item_failure = False
            if min_items is not None:
                try:
                    item_failure = observation.data_item_count < int(min_items)
                except (TypeError, ValueError):
                    pass
            content_failure = bool(expected.get("content_required")) and observation.body_chars == 0
            loading_failure = bool(expected.get("loading_must_finish")) and bool(
                observation.loading_wait_timed_out or observation.loading_indicators
            )
            if missing or forbidden or item_failure or content_failure or loading_failure:
                actual_parts = []
                if missing:
                    actual_parts.append(f"缺少必需内容：{missing}")
                if forbidden:
                    actual_parts.append(f"出现禁止内容：{forbidden}")
                if item_failure:
                    actual_parts.append(f"数据项 {observation.data_item_count} < {min_items}")
                if content_failure:
                    actual_parts.append("页面无正文")
                if loading_failure:
                    actual_parts.append("加载状态未结束")
                findings.append(
                    SemanticFinding(
                        "BUSINESS_MISMATCH",
                        "页面结果不符合显式业务预期",
                        str(expected),
                        "；".join(actual_parts),
                        rule.confidence,
                        "major",
                        rule.source,
                        {"rule": rule.to_dict(), "data_item_count": observation.data_item_count},
                        bug_eligible=rule.confidence == "high",
                    )
                )
        return findings


OBSERVATION_JS = r"""
() => {
  const compact = value => (value || '').replace(/\s+/g, ' ').trim();
  const visible = el => {
    if (!el || !el.isConnected || el.closest('[hidden],[inert],[aria-hidden="true"]')) return false;
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && el.getClientRects().length > 0 && s.display !== 'none' && s.visibility !== 'hidden' && Number(s.opacity) !== 0;
  };
  const loadingSelector = '[aria-busy="true"],[data-loading="true"],[data-state="loading"],.skeleton,[class*="skeleton"],[class*="spinner"],[class~="loading"]';
  const visibleAll = selector => Array.from(document.querySelectorAll(selector)).filter(visible);
  const tableRows = visibleAll('table tbody tr').filter(row => !row.matches(loadingSelector) && !row.querySelector(loadingSelector));
  const semanticLists = visibleAll('main [role="list"],main ul,main ol').filter(
    list => !list.closest('nav,header,footer,[role="navigation"],[role="menu"]')
  );
  const listItems = semanticLists.reduce((count, list) => count + Array.from(list.querySelectorAll(':scope > [role="listitem"],:scope > li')).filter(visible).length, 0);
  const cards = visibleAll('main [data-testid*="card" i],main [class~="card"],main article');
  const detailPairs = visibleAll('main dl dt,main [data-testid*="detail" i] [class*="label" i]').length;
  const truncated = visibleAll('main p,main li,main td,main [role="cell"]').filter(el => {
    const s = getComputedStyle(el), text = compact(el.innerText);
    if (!text || text.length < 12 || el.title || el.getAttribute('aria-label')) return false;
    return (s.textOverflow === 'ellipsis' && el.scrollWidth > el.clientWidth) || (s.overflow === 'hidden' && el.scrollHeight > el.clientHeight + 2);
  }).map(el => compact(el.innerText).slice(0, 240)).slice(0, 10);
  const bodyText = compact(document.body?.innerText || '');
  const mainText = compact(document.querySelector('main,[role="main"]')?.innerText || '');
  return {
    url: location.href,
    title: document.title,
    language: document.documentElement.lang || '',
    body_text: bodyText.slice(0, 20000),
    main_text: mainText.slice(0, 12000),
    headings: visibleAll('h1,h2,h3').map(el => compact(el.innerText)).filter(Boolean).slice(0, 30),
    alerts: visibleAll('[role="alert"]').map(el => compact(el.innerText)).filter(Boolean).slice(0, 10),
    statuses: visibleAll('[role="status"]').map(el => compact(el.innerText)).filter(Boolean).slice(0, 10),
    loading_indicators: visibleAll(loadingSelector).map(el => compact(el.innerText) || compact(el.className) || el.tagName).slice(0, 20),
    ready_state: document.readyState,
    table_count: visibleAll('main table,[role="main"] table,body > table').length,
    table_data_rows: tableRows.length,
    list_count: semanticLists.length,
    list_items: listItems,
    card_count: cards.length,
    form_count: visibleAll('form').length,
    field_count: visibleAll('input:not([type="hidden"]),select,textarea').length,
    search_field_count: visibleAll('input[type="search"],[role="searchbox"],input[name*="search" i],input[placeholder*="搜索"],input[placeholder*="search" i]').length,
    detail_pairs: detailPairs,
    broken_images: visibleAll('img').filter(img => img.complete && img.naturalWidth === 0).map(img => img.currentSrc || img.src).slice(0, 10),
    truncated_texts: truncated
  };
}
"""


def collect_page_observation(
    page,
    *,
    http_status: int | None = None,
    event_evidence: dict | None = None,
    loading_timeout_ms: int = 8000,
) -> PageObservation:
    raw = page.evaluate(OBSERVATION_JS)
    loading_wait_timed_out = False
    if raw.get("loading_indicators"):
        try:
            page.wait_for_function(
                """() => {
                  const visible=el=>{const r=el.getBoundingClientRect(),s=getComputedStyle(el);return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden'&&Number(s.opacity)!==0};
                  const selector='[aria-busy="true"],[data-loading="true"],[data-state="loading"],.skeleton,[class*="skeleton"],[class*="spinner"],[class~="loading"]';
                  return document.readyState==='complete' && !Array.from(document.querySelectorAll(selector)).some(visible);
                }""",
                timeout=max(1, int(loading_timeout_ms)),
            )
            raw = page.evaluate(OBSERVATION_JS)
        except Exception:
            loading_wait_timed_out = True
            raw = page.evaluate(OBSERVATION_JS)
    allowed = {field.name for field in PageObservation.__dataclass_fields__.values()}
    values = {key: value for key, value in raw.items() if key in allowed}
    values.update(
        {
            "http_status": http_status,
            "event_evidence": event_evidence or {},
            "loading_wait_timed_out": loading_wait_timed_out,
        }
    )
    return PageObservation(**values)
