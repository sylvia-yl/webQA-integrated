from __future__ import annotations

import re
import threading
import time
import traceback
from pathlib import Path
from urllib.parse import urlsplit

from .browser import action_step, locate_control, observable_delta, snapshot
from .pagepatrol import run_pagepatrol
from .planner_rules import plan_field_cases
from .semantic import (
    PageObservation,
    SemanticAnalyzer,
    collect_page_observation,
    load_semantic_model,
    normalize_oracle_rules,
)
from .safety import (
    ActionRisk,
    Redactor,
    classify_action,
    classify_exception,
    decide_recovery,
)
from .storage import Storage
from .utils import canonical_url, json_load, json_text, same_host, sha


REUSABLE_SHARED_STATUSES = {"TECH_PASS", "STATE_DISCOVERED", "LINK_RECORDED"}


class ActionAttemptError(RuntimeError):
    def __init__(self, cause: BaseException):
        super().__init__(f"{type(cause).__name__}: {cause}")
        self.cause = cause


class EventLog:
    def __init__(self, page, redactor: Redactor):
        self.console_errors: list[str] = []
        self.page_errors: list[str] = []
        self.failed_requests: list[str] = []
        self.responses: list[dict] = []
        self.response_errors: list[str] = []
        self.page = page
        self.redactor = redactor
        self._callbacks = {
            "console": self._console,
            "pageerror": self._page_error,
            "requestfailed": self._request_failed,
            "response": self._response,
        }
        for event, callback in self._callbacks.items():
            page.on(event, callback)

    def _console(self, message) -> None:
        if message.type == "error":
            self.console_errors.append(message.text[:800])

    def _page_error(self, error) -> None:
        self.page_errors.append(str(error)[:800])

    def _request_failed(self, request) -> None:
        failure = request.failure or ""
        if "ERR_ABORTED" not in failure:
            self.failed_requests.append(f"{request.method} {request.url}: {failure}"[:1000])

    def _response(self, response) -> None:
        try:
            resource_type = response.request.resource_type
            if resource_type == "document":
                self.responses.append({"url": response.url, "status": response.status})
            if response.status >= 400 and resource_type in {"document", "xhr", "fetch"}:
                self.response_errors.append(f"{response.status} {resource_type} {response.url}"[:1000])
        except Exception:
            pass

    def detach(self) -> None:
        for event, callback in self._callbacks.items():
            try:
                self.page.remove_listener(event, callback)
            except Exception:
                pass

    def evidence(self) -> dict:
        return self.redactor.redact_value({
            "console_errors": self.console_errors[:10],
            "page_errors": self.page_errors[:10],
            "failed_requests": self.failed_requests[:10],
            "document_responses": self.responses[-10:],
            "response_errors": self.response_errors[:10],
        })

    def clear(self) -> None:
        self.console_errors.clear()
        self.page_errors.clear()
        self.failed_requests.clear()
        self.responses.clear()
        self.response_errors.clear()


class PageExplorer:
    def __init__(
        self,
        storage: Storage,
        run_id: str,
        config: dict,
        output_dir: Path,
        *,
        headed: bool = False,
        pagepatrol_enabled: bool = True,
        stop_event: threading.Event | None = None,
    ):
        self.storage = storage
        self.run_id = run_id
        self.config = config
        self.output_dir = output_dir
        self.headed = headed
        self.pagepatrol_enabled = pagepatrol_enabled
        self.stop_event = stop_event or threading.Event()
        self.target = config["target"]
        self.host = (urlsplit(self.target).hostname or "").lower()
        self.scope = config["scope"]
        self.settings = config["traversal"]
        self.safety = config.get("safety") or {}
        self.redactor = Redactor(self.safety)
        self.semantic_settings = config.get("semantic") or {}
        for rule in normalize_oracle_rules(config.get("expected_behavior")):
            self.storage.upsert_business_rule(
                self.host,
                rule.rule_key,
                self.redactor.redact_text(str(rule.expected)),
                "ACTIVE",
                self.redactor.redact_value({"rule": rule.to_dict()}),
            )
        self.semantic = SemanticAnalyzer(
            self.semantic_settings,
            model=load_semantic_model(self.semantic_settings),
            persisted_rules=self.storage.business_rules(self.host),
        )
        self.shared_settings = (
            (config.get("deduplication") or {}).get("shared_controls") or {}
        )
        self.shared_context_key = sha(
            json_text(
                {
                    "algorithm_version": 1,
                    "host": self.host,
                    "language": config.get("language"),
                    "viewport": self.scope.get("viewport"),
                    "account_mode": (config.get("accounts") or {}).get("mode"),
                    "storage_state": (config.get("accounts") or {}).get("storage_state"),
                    "child_page_check": self.settings.get("child_page_check"),
                    "prohibited": config.get("prohibited") or [],
                    "safety": {
                        key: self.safety.get(key)
                        for key in (
                            "allow_stateful_actions",
                            "allow_destructive_actions",
                            "allow_unknown_actions",
                        )
                    },
                    "shared_controls": self.shared_settings,
                }
            ),
            32,
        )
        self._current_action_attempted = False
        self._current_risk: ActionRisk | None = None

    def _context(self, browser):
        args = {
            "viewport": self.scope["viewport"],
            "locale": self.config.get("language") or "zh-CN",
            "accept_downloads": False,
            "permissions": ["clipboard-read", "clipboard-write"],
        }
        storage_state = self.config.get("accounts", {}).get("storage_state")
        if storage_state and Path(storage_state).exists():
            args["storage_state"] = str(Path(storage_state).resolve())
        context = browser.new_context(**args)
        context.set_default_timeout(int(self.settings["action_timeout_ms"]))
        return context

    def _settle(self, page) -> None:
        try:
            page.wait_for_load_state("domcontentloaded", timeout=3000)
        except Exception:
            pass
        try:
            page.wait_for_load_state("networkidle", timeout=1500)
        except Exception:
            pass
        page.wait_for_timeout(int(self.settings["settle_ms"]))

    def _open(self, context, url: str):
        page = context.new_page()
        log = EventLog(page, self.redactor)
        response = page.goto(url, wait_until="domcontentloaded", timeout=int(self.settings["navigation_timeout_ms"]))
        self._settle(page)
        return page, response, log

    def _snapshot(self, page) -> tuple[dict, list[dict]]:
        snap, controls = snapshot(
            page,
            self.target,
            int(self.settings["max_controls_per_state"]),
            self.shared_settings,
        )
        return self.redactor.redact_value(snap), self.redactor.redact_value(controls)

    def _shared_candidate(self, descriptor: dict) -> bool:
        if not self.shared_settings.get("enabled", True):
            return False
        shared_key = str(descriptor.get("shared_control_key") or "")
        scope = str(descriptor.get("shared_scope") or "")
        confidence = str(descriptor.get("shared_confidence") or "none")
        if not shared_key or scope not in set(self.shared_settings.get("scopes") or []):
            return False
        ranks = {"none": 0, "medium": 1, "high": 2}
        required = str(self.shared_settings.get("min_confidence") or "high")
        return ranks.get(confidence, 0) >= ranks.get(required, 2)

    def _shared_result(self, row: dict) -> dict | None:
        descriptor = row["descriptor"]
        if not self._shared_candidate(descriptor):
            return None
        return self.storage.reusable_shared_control_result(
            self.run_id,
            self.shared_context_key,
            descriptor["shared_control_key"],
            int(row["page_id"]),
        )

    def _verified_shared_result(self, page, row: dict) -> dict | None:
        reused = self._shared_result(row)
        if not reused:
            return None
        _, controls = self._snapshot(page)
        shared_key = row["descriptor"]["shared_control_key"]
        current = next(
            (
                item
                for item in controls
                if item.get("shared_control_key") == shared_key
                and self._shared_candidate(item)
                and not item.get("disabled")
            ),
            None,
        )
        if current is None:
            return None
        reused["current_descriptor"] = current
        return reused

    def _write_screenshot(self, page, path: Path) -> bool:
        if not self.safety.get("capture_screenshots", True):
            return False
        options = {
            "path": str(path),
            "full_page": True,
            "animations": "disabled",
            "timeout": 30000,
        }
        if self.safety.get("mask_form_fields_in_screenshots", True):
            options["mask"] = [
                page.locator("input,textarea,[contenteditable='true'],[data-sensitive],[data-private]")
            ]
            options["mask_color"] = "#111111"
        page.screenshot(**options)
        return True

    def _attempt_click(self, locator) -> None:
        self._current_action_attempted = True
        try:
            locator.click(timeout=int(self.settings["action_timeout_ms"]))
        except Exception as exc:
            raise ActionAttemptError(exc) from exc

    def _replay(self, page, action_path: list[dict]) -> None:
        for step in action_path:
            descriptor = step.get("descriptor") or step
            locator = locate_control(page, descriptor)
            if not locator.is_visible(timeout=1500):
                raise RuntimeError(f"无法重放状态路径，控件不可见：{descriptor.get('label')}")
            locator.scroll_into_view_if_needed(timeout=2000)
            locator.click(timeout=int(self.settings["action_timeout_ms"]))
            self._settle(page)

    def _capture_state_artifact(self, page, page_id: int, state_id: int, fingerprint: str) -> None:
        screen_dir = self.output_dir / "screenshots"
        screen_dir.mkdir(parents=True, exist_ok=True)
        path = screen_dir / f"page-{page_id:04d}-state-{state_id:05d}-{fingerprint[:8]}.png"
        try:
            if self._write_screenshot(page, path):
                self.storage.add_artifact(self.run_id, "screenshot", str(path), page_id=page_id, metadata={"state_id": state_id})
        except Exception as exc:
            self.storage.add_message(
                self.run_id,
                "warning",
                "screenshot",
                self.redactor.redact_text(f"页面 {page_id} 状态 {state_id} 截图失败：{exc}"),
            )

    def _register_state(
        self,
        page,
        page_row: dict,
        action_path: list,
        discovered_by_action_id: int | None,
        priority: int,
    ) -> tuple[int | None, bool, dict, list[dict], int]:
        snap, controls = self._snapshot(page)
        state_id, is_new = self.storage.add_state(
            self.run_id,
            int(page_row["id"]),
            snap,
            action_path,
            discovered_by_action_id,
            int(self.settings["max_states_per_page"]),
        )
        queued = 0
        if state_id is not None and is_new:
            arrival = json_load(page_row.get("arrival_path_json"), [])
            readable_steps = [{"label": step.get("label") or "未命名动作"} for step in action_path]
            _, queued = self.storage.add_controls(
                self.run_id,
                int(page_row["id"]),
                state_id,
                controls,
                priority,
                arrival + readable_steps,
            )
            self._capture_state_artifact(page, int(page_row["id"]), state_id, snap["fingerprint"])
        return state_id, is_new, snap, controls, queued

    def _record_action(self, row: dict, status: str, action_type: str, outcome: str, **extra) -> int:
        descriptor = row["descriptor"]
        evidence = extra.pop("evidence", {})
        evidence.update(extra)
        if self._current_risk is not None:
            evidence.setdefault("risk", self._current_risk.to_dict())
        evidence = self.redactor.redact_value(evidence)
        after_fingerprint = evidence.pop("after_fingerprint", None)
        label = self.redactor.redact_text(descriptor.get("label") or "未命名控件")
        safe_outcome = self.redactor.redact_text(outcome)
        action_id = self.storage.record_action(
            self.run_id,
            int(row["page_id"]),
            int(row["state_id"]),
            int(row["control_id"]),
            int(row["id"]),
            status,
            action_type,
            label,
            row.get("state_fingerprint"),
            after_fingerprint,
            safe_outcome,
            self.redactor.redact_value(row["full_path"]),
            evidence,
        )
        if status in REUSABLE_SHARED_STATUSES and self._shared_candidate(descriptor):
            self.storage.remember_shared_control_result(
                self.run_id,
                self.shared_context_key,
                descriptor["shared_control_key"],
                str(descriptor.get("shared_scope") or ""),
                str(descriptor.get("shared_confidence") or ""),
                int(row["page_id"]),
                int(row["control_id"]),
                action_id,
                status,
                safe_outcome,
                {
                    "label": label,
                    "action_type": action_type,
                    "after_fingerprint": after_fingerprint,
                    "evidence": evidence,
                },
            )
        return action_id

    def _page_count(self) -> int:
        return self.storage.scalar("SELECT COUNT(*) FROM page_nodes WHERE run_id=?", (self.run_id,))

    def _schedule_destination(
        self,
        page_row: dict,
        row: dict,
        destination: str,
        path: list,
    ) -> tuple[int | None, bool]:
        next_depth = int(page_row["depth"]) + 1
        if self._page_count() >= int(self.settings["max_pages"]):
            self.storage.add_message(
                self.run_id,
                "warning",
                "coverage",
                f"页面数量达到上限 {self.settings['max_pages']}，未调度 {destination}",
            )
            return None, False
        return self.storage.add_page(
            self.run_id,
            destination,
            next_depth,
            path,
            int(page_row["id"]),
            int(row["control_id"]),
        )

    def _link_load_result(
        self,
        page,
        log: EventLog,
        *,
        page_id: int,
        phase: str,
        http_status: int | None = None,
        persist: bool = True,
    ) -> dict:
        """Run the same observation and semantic gate for every page entry path."""
        if persist:
            self.storage.record_qa_transition(
                self.run_id,
                page_id,
                phase,
                "EXECUTE",
                "OBSERVE",
                "页面动作或导航完成，开始统一采集 DOM、网络与可见内容",
            )
        if http_status is None:
            for item in reversed(log.responses):
                if canonical_url(self.target, item["url"]) == canonical_url(self.target, page.url):
                    http_status = item["status"]
                    break
        event_evidence = log.evidence()
        observation = collect_page_observation(
            page,
            http_status=http_status,
            event_evidence=event_evidence,
            loading_timeout_ms=int(self.semantic_settings.get("loading_timeout_ms") or 8000),
        )
        observation_data = self.redactor.redact_value(observation.to_dict())
        observation_fields = set(PageObservation.__dataclass_fields__)
        model_observation = PageObservation(
            **{
                key: value
                for key, value in observation_data.items()
                if key in observation_fields
            }
        )
        evaluation = self.semantic.evaluate(
            observation,
            model_observation=model_observation,
        )
        evaluation_data = self.redactor.redact_value(evaluation.to_dict())
        evaluation_id = None
        finding_records = evaluation_data.get("findings") or []
        if persist:
            evaluation_id, finding_records = self.storage.add_semantic_evaluation(
                self.run_id,
                page_id,
                phase,
                self.redactor.redact_url(observation.url),
                observation_data,
                evaluation_data,
            )
            self.storage.record_qa_transition(
                self.run_id,
                page_id,
                phase,
                "OBSERVE",
                "CLASSIFY",
                f"完成页面理解与语义分类：{evaluation.status}",
                {
                    "evaluation_id": evaluation_id,
                    "coverage": evaluation.coverage,
                    "page_type": evaluation.understanding.primary_type,
                    "finding_count": len(finding_records),
                },
            )

        reasons: list[str] = []
        if http_status is not None and http_status >= 400:
            reasons.append(f"文档 HTTP {http_status}")
        if observation.body_chars == 0:
            reasons.append("页面没有可见文本内容")
        if observation.broken_images:
            reasons.append(f"存在 {len(observation.broken_images)} 个可见破图")
        if log.page_errors:
            reasons.append(f"页面脚本异常：{log.page_errors[0]}")
        if log.response_errors:
            reasons.append(f"文档/API 错误响应：{log.response_errors[0]}")
        blocking_semantic = [
            finding
            for finding in finding_records
            if finding.get("classification")
            in {
                "CONTENT_UNAVAILABLE",
                "DATA_MISSING",
                "STUCK_LOADING",
                "COPY_QUALITY",
                "BUSINESS_MISMATCH",
            }
            and finding.get("confidence") == "high"
        ]
        reasons.extend(
            f"{finding.get('classification')}：{finding.get('actual')}"
            for finding in blocking_semantic
        )
        headings = observation.headings or []
        return {
            "final_url": self.redactor.redact_url(observation.url),
            "http_status": http_status,
            "title": self.redactor.redact_text(observation.title),
            "body_chars": observation.body_chars,
            "h1": self.redactor.redact_text(headings[0] if headings else ""),
            "main_chars": observation.main_chars,
            "ready_state": observation.ready_state,
            "loading_indicators": self.redactor.redact_value(observation.loading_indicators),
            "broken_images": self.redactor.redact_value(observation.broken_images),
            "error_page": evaluation.understanding.primary_type == "ERROR",
            "response_errors": event_evidence.get("response_errors") or [],
            "loaded": not reasons,
            "load_check_reasons": reasons,
            "semantic_evaluation_id": evaluation_id,
            "semantic_status": evaluation.status,
            "semantic_coverage": evaluation.coverage,
            "page_type": evaluation.understanding.primary_type,
            "semantic_findings": [
                {
                    key: finding.get(key)
                    for key in (
                        "id",
                        "evaluation_id",
                        "classification",
                        "title",
                        "status",
                        "severity",
                        "confidence",
                        "oracle_source",
                        "expected",
                        "actual",
                        "evidence",
                        "bug_eligible",
                        "fingerprint",
                        "previously_observed",
                        "previous_finding_id",
                    )
                    if key in finding
                }
                for finding in finding_records
            ],
        }

    def _click_link(
        self,
        page,
        context,
        locator,
        descriptor: dict,
        log: EventLog,
        page_id: int,
    ) -> tuple[object, dict]:
        popups: list = []
        popup_logs: list[EventLog] = []

        def on_page(new_page):
            if new_page is not page:
                popups.append(new_page)
                popup_logs.append(EventLog(new_page, self.redactor))

        context.on("page", on_page)
        try:
            locator.scroll_into_view_if_needed(timeout=2500)
            self._attempt_click(locator)
            page.wait_for_timeout(int(self.settings["settle_ms"]))
            destination_page = popups[-1] if popups else page
            if popups:
                try:
                    destination_page.wait_for_load_state(
                        "domcontentloaded", timeout=int(self.settings["navigation_timeout_ms"])
                    )
                except Exception:
                    pass
            else:
                self._settle(destination_page)
            active_log = popup_logs[-1] if popup_logs else log
            result = self._link_load_result(
                destination_page,
                active_log,
                page_id=page_id,
                phase="LINK_DESTINATION",
            )
            result["open_mode"] = "新标签页/窗口" if popups else "当前标签页"
            result["event_evidence"] = active_log.evidence()
            return destination_page, result
        finally:
            try:
                context.remove_listener("page", on_page)
            except Exception:
                pass
            for popup_log in popup_logs:
                popup_log.detach()

    def _capture_link_proof(self, page, page_row: dict, row: dict, final_url: str) -> str | None:
        proof_dir = self.output_dir / "link-checks"
        proof_dir.mkdir(parents=True, exist_ok=True)
        path = proof_dir / (
            f"source-{int(page_row['id']):04d}-control-{int(row['control_id']):06d}-{sha(final_url, 10)}.png"
        )
        try:
            return str(path) if self._write_screenshot(page, path) else None
        except Exception as exc:
            self.storage.add_message(
                self.run_id,
                "warning",
                "link_screenshot",
                self.redactor.redact_text(f"子页面加载验证截图失败：{final_url}；{exc}"),
            )
            return None

    def _handle_known_link(self, page, context, page_row: dict, row: dict, locator, log: EventLog) -> None:
        descriptor = row["descriptor"]
        target = canonical_url(self.target, descriptor["href"])
        if self.settings.get("child_page_check") == "record_only":
            candidate_depth = int(page_row["depth"]) + 1
            reserved, visit = self.storage.reserve_link(
                self.run_id,
                target,
                int(page_row["id"]),
                int(row["state_id"]),
                int(row["control_id"]),
                row["full_path"],
            )
            if reserved:
                status = "LINK_RECORDED"
                if candidate_depth <= int(self.scope["max_page_depth"]):
                    outcome = (
                        f"发现同域子页面链接并收录为第 {candidate_depth} 层候选页；"
                        "当前控件步骤不点击进入，当前页面完成后提升为标准页面"
                    )
                else:
                    outcome = (
                        f"发现同域子页面链接并收录为第 {candidate_depth} 层候选页；"
                        f"当前控件步骤不点击进入，后续将运行深度提高到 {candidate_depth} 时接续测试"
                    )
                self.storage.finish_link(int(visit["id"]), "RECORDED", final_url=target)
                duplicate_visit_id = None
            else:
                status = "DUPLICATE_LINK"
                outcome = f"目标链接此前已记录（{visit['status']}）；本次只记录重复入口，不点击"
                duplicate_visit_id = int(visit["id"])
            self.storage.add_edge(
                self.run_id,
                int(page_row["id"]),
                int(row["state_id"]),
                int(row["control_id"]),
                target,
                status,
                row["full_path"],
                duplicate_visit_id=duplicate_visit_id,
            )
            self._record_action(
                row,
                status,
                "link",
                outcome,
                target=target,
                candidate_depth=candidate_depth,
                duplicate_visit_id=duplicate_visit_id,
            )
            self.storage.finish_frontier(
                int(row["id"]), status, {"target": target, "candidate_depth": candidate_depth}
            )
            return
        max_depth = int(self.scope["max_page_depth"])
        if int(page_row["depth"]) >= max_depth:
            self.storage.add_edge(
                self.run_id,
                int(page_row["id"]),
                int(row["state_id"]),
                int(row["control_id"]),
                target,
                "BOUNDARY",
                row["full_path"],
            )
            self._record_action(row, "BOUNDARY", "link", f"已到第 {max_depth} 层，记录下一层入口但不点击")
            self.storage.finish_frontier(int(row["id"]), "BOUNDARY", {"target": target})
            return

        reserved, visit = self.storage.reserve_link(
            self.run_id,
            target,
            int(page_row["id"]),
            int(row["state_id"]),
            int(row["control_id"]),
            row["full_path"],
        )
        if not reserved:
            existing_page_id = None
            if visit.get("status") == "CONFIRMED":
                existing_page_id, _ = self._schedule_destination(page_row, row, target, row["full_path"])
            self.storage.add_edge(
                self.run_id,
                int(page_row["id"]),
                int(row["state_id"]),
                int(row["control_id"]),
                target,
                "DUPLICATE_LINK",
                row["full_path"],
                target_page_id=existing_page_id,
                duplicate_visit_id=int(visit["id"]),
            )
            self._record_action(
                row,
                "DUPLICATE_LINK",
                "link",
                f"目标链接此前已处理（{visit['status']}），本次只记录，不再点击",
                duplicate_visit_id=visit["id"],
            )
            self.storage.finish_frontier(int(row["id"]), "DUPLICATE_LINK", {"target": target})
            return

        destination_page, result = self._click_link(
            page, context, locator, descriptor, log, int(page_row["id"])
        )
        final_url = canonical_url(self.target, result["final_url"])
        screenshot_path = self._capture_link_proof(destination_page, page_row, row, final_url)
        result["screenshot_path"] = screenshot_path
        if not screenshot_path:
            result["loaded"] = False
            result.setdefault("load_check_reasons", []).append("未能保存目标页面加载证明截图")
        evidence = log.evidence() | result
        if not same_host(self.target, final_url):
            status = "OUT_OF_SCOPE"
            outcome = f"点击后跳转到外域 {final_url}，不进入"
            self.storage.finish_link(int(visit["id"]), status, **result)
            target_page_id = None
        elif result["loaded"]:
            status = "TECH_PASS"
            outcome = (
                f"已在{result.get('open_mode')}真实点击进入，目标页加载校验通过：{final_url}；"
                f"HTTP {result.get('http_status') if result.get('http_status') is not None else '未捕获'}，"
                f"标题「{result.get('title') or '无标题'}」，正文 {result.get('body_chars')} 字符，"
                f"未发现错误页、残留加载占位、失败文档/API 或可见破图；已保存截图。"
                "本轮不深入测试目标页控件，留到下一层。"
            )
            self.storage.finish_link(int(visit["id"]), "CONFIRMED", **result)
            arrival = list(row["full_path"])
            arrival.append({"label": f"已到达：{result.get('title') or final_url}", "url": final_url})
            target_page_id, _ = self._schedule_destination(page_row, row, final_url, arrival)
        else:
            status = "UNCONFIRMED"
            reasons = "；".join(result.get("load_check_reasons") or ["未知加载校验原因"])
            outcome = (
                f"已在{result.get('open_mode')}真实点击进入并保存现场截图，但目标页加载校验未通过："
                f"{final_url}；{reasons}"
            )
            link_status = "RETRY" if int(row["attempts"]) < int(self.settings["retry_limit"]) else "UNCONFIRMED"
            self.storage.finish_link(int(visit["id"]), link_status, error=outcome, **result)
            target_page_id = None
        self.storage.add_edge(
            self.run_id,
            int(page_row["id"]),
            int(row["state_id"]),
            int(row["control_id"]),
            final_url,
            status,
            row["full_path"],
            target_page_id=target_page_id,
        )
        action_id = self._record_action(row, status, "link", outcome, evidence=evidence)
        if screenshot_path:
            self.storage.add_artifact(
                self.run_id,
                "child_page_load_proof",
                screenshot_path,
                page_id=int(page_row["id"]),
                action_id=action_id,
                metadata={
                    "source_url": page_row["url"],
                    "target_url": final_url,
                    "open_mode": result.get("open_mode"),
                    "loaded": result.get("loaded"),
                },
            )
        if status == "UNCONFIRMED":
            self.storage.retry_frontier(int(row["id"]), evidence | {"outcome": outcome}, int(self.settings["retry_limit"]))
        else:
            self.storage.finish_frontier(int(row["id"]), status, evidence)
        if destination_page is not page:
            try:
                destination_page.close()
            except Exception:
                pass

    def _handle_form_control(self, row: dict) -> None:
        descriptor = row["descriptor"]
        cases = plan_field_cases(descriptor, self.config.get("test_data", {}).get("prefix") or "webqa-run-")
        if cases:
            self.storage.add_test_cases(
                self.run_id,
                int(row["page_id"]),
                int(row["state_id"]),
                int(row["control_id"]),
                cases,
            )
            outcome = f"已根据 HTML 约束生成 {len(cases)} 个非提交型表单用例；提交动作受风险策略控制"
            status = "FORM_PLANNED"
        else:
            outcome = "该输入控件缺少可安全自动生成的数据，记录为业务规则未确认"
            status = "NEEDS_SPEC"
        self._record_action(row, status, "form_field", outcome, planned_cases=cases)
        self.storage.finish_frontier(int(row["id"]), status, {"planned_cases": len(cases)})

    @staticmethod
    def _error_signature(value: str) -> str:
        value = re.sub(r"\b\d+(?::\d+)?\b", "<n>", value or "")
        value = re.sub(r"\s+", " ", value).strip().lower()
        return value[:300]

    def _reproduce_script_error(
        self,
        browser,
        page_row: dict,
        row: dict,
        action_id: int,
        expected_error: str,
    ) -> tuple[str, int]:
        signature = self._error_signature(expected_error)
        fingerprint = sha(
            f"{page_row['canonical_url']}|{row['behavior_key']}|{signature}", 32
        )
        bug_id = self.storage.create_bug_candidate(
            self.run_id,
            int(page_row["id"]),
            action_id,
            fingerprint,
            self.redactor.redact_text(f"点击「{row['descriptor'].get('label') or '未命名控件'}」出现页面脚本异常"),
            self.redactor.redact_value(row["full_path"]),
            {"page_error": self.redactor.redact_text(expected_error)},
        )
        capture_traces = bool(self.safety.get("capture_traces", False))
        trace_dir = self.output_dir / "traces"
        screen_dir = self.output_dir / "reproductions"
        if capture_traces:
            trace_dir.mkdir(parents=True, exist_ok=True)
        if self.safety.get("capture_screenshots", True):
            screen_dir.mkdir(parents=True, exist_ok=True)
        reproduced_count = 0
        for attempt in (1, 2):
            context = self._context(browser)
            trace_path = trace_dir / f"bug-{bug_id:05d}-attempt-{attempt}.zip"
            screenshot_path = screen_dir / f"bug-{bug_id:05d}-attempt-{attempt}.png"
            log = None
            evidence: dict = {}
            reproduced = False
            tracing_started = False
            try:
                if capture_traces:
                    context.tracing.start(screenshots=True, snapshots=True, sources=False)
                    tracing_started = True
                page, _, log = self._open(context, page_row["url"])
                self._replay(page, row["action_path"])
                log.clear()
                locator = locate_control(page, row["descriptor"])
                if not locator.is_visible(timeout=1500):
                    raise RuntimeError("复现时目标控件不可见")
                locator.scroll_into_view_if_needed(timeout=2500)
                locator.click(timeout=int(self.settings["action_timeout_ms"]))
                self._settle(page)
                signatures = [self._error_signature(value) for value in log.page_errors]
                reproduced = any(value == signature for value in signatures)
                if not reproduced and signature:
                    reproduced = any(signature[:120] in value or value[:120] in signature for value in signatures if value)
                evidence = log.evidence() | {"expected_signature": signature, "observed_signatures": signatures}
                self._write_screenshot(page, screenshot_path)
            except Exception as exc:
                evidence = (log.evidence() if log else {}) | {
                    "automation_error": self.redactor.redact_text(f"{type(exc).__name__}: {exc}"),
                    "expected_signature": signature,
                }
            finally:
                if tracing_started:
                    try:
                        context.tracing.stop(path=str(trace_path))
                    except Exception as exc:
                        evidence["trace_error"] = self.redactor.redact_text(str(exc))
                if log:
                    log.detach()
                context.close()
            if reproduced:
                reproduced_count += 1
            status = "REPRODUCED" if reproduced else ("AUTOMATION_UNCONFIRMED" if evidence.get("automation_error") else "NOT_REPRODUCED")
            self.storage.add_reproduction(
                bug_id,
                attempt,
                reproduced,
                status,
                self.redactor.redact_value(evidence),
                str(trace_path) if trace_path.exists() else None,
                str(screenshot_path) if screenshot_path.exists() else None,
            )
            if trace_path.exists():
                self.storage.add_artifact(
                    self.run_id,
                    "playwright_trace",
                    str(trace_path),
                    page_id=int(page_row["id"]),
                    action_id=action_id,
                    metadata={"bug_id": bug_id, "attempt": attempt},
                )
            if screenshot_path.exists():
                self.storage.add_artifact(
                    self.run_id,
                    "reproduction_screenshot",
                    str(screenshot_path),
                    page_id=int(page_row["id"]),
                    action_id=action_id,
                    metadata={"bug_id": bug_id, "attempt": attempt},
                )
        if reproduced_count == 2:
            status, triage = "CONFIRMED", "同一脚本异常在两个全新 BrowserContext 中均稳定复现"
        elif reproduced_count == 1:
            status, triage = "FLAKY", "两次独立复现中仅出现一次，归为偶发问题"
        else:
            status, triage = "NOT_REPRODUCED", "两次独立复现均未出现，不列为确认缺陷"
        self.storage.finish_bug(bug_id, status, triage)
        return status, bug_id

    def _reproduce_semantic_findings(
        self,
        browser,
        page_row: dict,
        findings: list[dict],
        *,
        action_path: list | None = None,
        action_id: int | None = None,
    ) -> list[dict]:
        """Confirm deterministic content defects in two fresh BrowserContexts."""
        results: list[dict] = []
        candidates = [
            finding
            for finding in findings
            if finding.get("bug_eligible")
            and finding.get("confidence") == "high"
            and not finding.get("previously_observed")
            and finding.get("classification")
            in {"CONTENT_UNAVAILABLE", "DATA_MISSING", "STUCK_LOADING", "COPY_QUALITY", "BUSINESS_MISMATCH"}
        ]
        action_path = action_path or []
        for finding in candidates:
            classification = str(finding.get("classification") or "BUSINESS_MISMATCH")
            bug_fingerprint = sha(
                "|".join(
                    [
                        canonical_url(self.target, page_row["canonical_url"]),
                        classification,
                        str(finding.get("oracle_source") or ""),
                        str(finding.get("expected") or ""),
                        sha(json_text(action_path), 20),
                    ]
                ),
                32,
            )
            initial_evidence = {
                "classification": classification,
                "oracle_source": finding.get("oracle_source"),
                "expected": finding.get("expected"),
                "actual": finding.get("actual"),
                "confidence": finding.get("confidence"),
                "severity": finding.get("severity"),
                "preconditions": {
                    "url": self.redactor.redact_url(page_row["url"]),
                    "account_mode": (self.config.get("accounts") or {}).get("mode"),
                    "language": self.config.get("language"),
                },
                "reproduction_steps": [
                    f"在全新 BrowserContext 中打开 {self.redactor.redact_url(page_row['url'])}",
                    *[
                        str(step.get("label") or "执行页面内动作")
                        for step in action_path
                        if isinstance(step, dict)
                    ],
                    "等待页面进入内容、空状态或错误状态",
                    f"再次执行 {classification} 语义判断",
                ],
                "impact": "用户无法可靠获得页面承诺的内容或业务结果",
                "evidence": finding.get("evidence") or {},
            }
            bug_id = self.storage.create_bug_candidate(
                self.run_id,
                int(page_row["id"]),
                action_id,
                bug_fingerprint,
                self.redactor.redact_text(str(finding.get("title") or classification)),
                self.redactor.redact_value(
                    json_load(page_row.get("arrival_path_json"), []) + action_path
                ),
                self.redactor.redact_value(initial_evidence),
                severity=str(finding.get("severity") or "unknown"),
            )
            self.storage.record_qa_transition(
                self.run_id,
                int(page_row["id"]),
                "SEMANTIC_REPRODUCTION",
                "CLASSIFY",
                "REPRODUCE",
                f"高可信 {classification} 进入两个独立 BrowserContext 复验",
                {"finding_id": finding.get("id"), "bug_id": bug_id},
                action_id=action_id,
            )
            reproduced_count = 0
            for attempt in (1, 2):
                context = self._context(browser)
                log = None
                screen_dir = self.output_dir / "semantic-reproductions"
                screenshot_path = screen_dir / f"bug-{bug_id:05d}-attempt-{attempt}.png"
                evidence: dict = {}
                reproduced = False
                try:
                    page, response, log = self._open(context, page_row["url"])
                    if action_path:
                        self._replay(page, action_path)
                        log.clear()
                    check = self._link_load_result(
                        page,
                        log,
                        page_id=int(page_row["id"]),
                        phase="SEMANTIC_REPRODUCTION",
                        http_status=response.status if response else None,
                        persist=False,
                    )
                    observed = [
                        item
                        for item in check.get("semantic_findings") or []
                        if item.get("classification") == classification
                        and item.get("confidence") == "high"
                    ]
                    reproduced = bool(observed)
                    evidence = {
                        "classification": classification,
                        "semantic_status": check.get("semantic_status"),
                        "semantic_coverage": check.get("semantic_coverage"),
                        "observed_findings": observed,
                        "event_evidence": log.evidence(),
                    }
                    if self.safety.get("capture_screenshots", True):
                        screen_dir.mkdir(parents=True, exist_ok=True)
                        self._write_screenshot(page, screenshot_path)
                except Exception as exc:
                    evidence = (log.evidence() if log else {}) | {
                        "automation_error": self.redactor.redact_text(
                            f"{type(exc).__name__}: {exc}"
                        ),
                        "classification": classification,
                    }
                finally:
                    if log:
                        log.detach()
                    context.close()
                if reproduced:
                    reproduced_count += 1
                status = (
                    "REPRODUCED"
                    if reproduced
                    else ("AUTOMATION_UNCONFIRMED" if evidence.get("automation_error") else "NOT_REPRODUCED")
                )
                self.storage.add_reproduction(
                    bug_id,
                    attempt,
                    reproduced,
                    status,
                    self.redactor.redact_value(evidence),
                    None,
                    str(screenshot_path) if screenshot_path.exists() else None,
                )
                if screenshot_path.exists():
                    self.storage.add_artifact(
                        self.run_id,
                        "semantic_reproduction_screenshot",
                        str(screenshot_path),
                        page_id=int(page_row["id"]),
                        metadata={
                            "bug_id": bug_id,
                            "attempt": attempt,
                            "classification": classification,
                        },
                    )
            if reproduced_count == 2:
                final_status = "CONFIRMED"
                triage = "同一内容语义异常在两个全新 BrowserContext 中均稳定复现"
            elif reproduced_count == 1:
                final_status = "FLAKY"
                triage = "两次独立复现中仅出现一次，归为偶发内容异常"
            else:
                final_status = "NOT_REPRODUCED"
                triage = "两次独立复现均未出现，不列为确认缺陷"
            self.storage.finish_bug(bug_id, final_status, triage)
            self.storage.record_qa_transition(
                self.run_id,
                int(page_row["id"]),
                "SEMANTIC_REPRODUCTION",
                "REPRODUCE",
                "REPORT",
                triage,
                {"bug_id": bug_id, "status": final_status, "reproduced": reproduced_count},
                action_id=action_id,
            )
            if finding.get("id"):
                self.storage.finish_semantic_finding(
                    int(finding["id"]), final_status, bug_id
                )
            results.append(
                {
                    "finding_id": finding.get("id"),
                    "bug_id": bug_id,
                    "status": final_status,
                    "reproduced": reproduced_count,
                }
            )
        return results

    def _handle_state_action(self, browser, page, context, page_row: dict, row: dict, locator, log: EventLog) -> None:
        before, _ = self._snapshot(page)
        clipboard_before = ""
        try:
            clipboard_before = page.evaluate("navigator.clipboard.readText().catch(() => '')") or ""
        except Exception:
            pass
        locator.scroll_into_view_if_needed(timeout=2500)
        self._attempt_click(locator)
        self._settle(page)
        after, _ = self._snapshot(page)
        clipboard_after = ""
        try:
            clipboard_after = page.evaluate("navigator.clipboard.readText().catch(() => '')") or ""
        except Exception:
            pass
        descriptor = row["descriptor"]
        page_canon = canonical_url(self.target, page_row["canonical_url"])
        after_canon = canonical_url(self.target, after["url"])
        if after_canon != page_canon:
            if not same_host(self.target, after_canon):
                status, outcome = "OUT_OF_SCOPE", f"控件点击后跳转到外域 {after_canon}，不进入"
            elif self.settings.get("child_page_check") == "record_only":
                candidate_depth = int(page_row["depth"]) + 1
                reserved, visit = self.storage.reserve_link(
                    self.run_id,
                    after_canon,
                    int(page_row["id"]),
                    int(row["state_id"]),
                    int(row["control_id"]),
                    row["full_path"],
                )
                if reserved:
                    status = "LINK_RECORDED_AFTER_CLICK"
                    outcome = (
                        f"该控件没有可预判的 href，点击后发生同域导航：{after_canon}；"
                        f"已记录为第 {candidate_depth} 层候选页并关闭本次隔离页面；"
                        "达到解锁深度后再作为标准页面测试"
                    )
                    self.storage.finish_link(int(visit["id"]), "RECORDED", final_url=after_canon)
                    duplicate_visit_id = None
                else:
                    status = "DUPLICATE_AFTER_CLICK"
                    outcome = "点击后才识别到已记录的同域链接；保留候选记录，不在本次控件步骤继续测试子页面"
                    duplicate_visit_id = int(visit["id"])
                self.storage.add_edge(
                    self.run_id,
                    int(page_row["id"]),
                    int(row["state_id"]),
                    int(row["control_id"]),
                    after_canon,
                    status,
                    row["full_path"],
                    duplicate_visit_id=duplicate_visit_id,
                )
            elif int(page_row["depth"]) >= int(self.scope["max_page_depth"]):
                status, outcome = "BOUNDARY_AFTER_CLICK", "无静态链接可供预判；点击后才识别出下一层，已记录且不再深入"
                self.storage.add_edge(
                    self.run_id,
                    int(page_row["id"]),
                    int(row["state_id"]),
                    int(row["control_id"]),
                    after_canon,
                    status,
                    row["full_path"],
                )
            else:
                reserved, visit = self.storage.reserve_link(
                    self.run_id,
                    after_canon,
                    int(page_row["id"]),
                    int(row["state_id"]),
                    int(row["control_id"]),
                    row["full_path"],
                )
                if reserved:
                    load_result = self._link_load_result(
                        page,
                        log,
                        page_id=int(page_row["id"]),
                        phase="DYNAMIC_LINK_DESTINATION",
                    )
                    if load_result["loaded"]:
                        self.storage.finish_link(int(visit["id"]), "CONFIRMED", **load_result)
                        arrival = list(row["full_path"]) + [
                            {
                                "label": f"已到达：{load_result.get('title') or after_canon}",
                                "url": after_canon,
                            }
                        ]
                        target_page_id, _ = self._schedule_destination(
                            page_row, row, after_canon, arrival
                        )
                        status = "TECH_PASS"
                        outcome = f"无 href 控件完成页面跳转：{after_canon}；留到下一层深入测试"
                    else:
                        reasons = "；".join(
                            load_result.get("load_check_reasons") or ["未知加载校验原因"]
                        )
                        status = "UNCONFIRMED"
                        outcome = f"无 href 控件跳转后的页面未通过统一观察：{after_canon}；{reasons}"
                        target_page_id = None
                        self.storage.finish_link(
                            int(visit["id"]), "UNCONFIRMED", error=outcome, **load_result
                        )
                    self.storage.add_edge(
                        self.run_id,
                        int(page_row["id"]),
                        int(row["state_id"]),
                        int(row["control_id"]),
                        after_canon,
                        status,
                        row["full_path"],
                        target_page_id=target_page_id,
                    )
                else:
                    status, outcome = "DUPLICATE_AFTER_CLICK", "点击后才识别到已处理链接；本次记录，不调度重复页面"
            self._record_action(row, status, "navigation", outcome, evidence=log.evidence(), after_fingerprint=after["fingerprint"])
            self.storage.finish_frontier(int(row["id"]), status, {"destination": after_canon})
            return

        next_path = list(row["action_path"]) + [action_step(descriptor)]
        provisional_action_id = self._record_action(
            row,
            "OBSERVED",
            "state_change",
            "动作已执行，正在登记动作后的页面状态",
            evidence=log.evidence(),
            after_fingerprint=after["fingerprint"],
        )
        priority = int(row["priority"]) + 100000
        state_id, is_new, _, _, queued = self._register_state(
            page,
            page_row,
            next_path,
            provisional_action_id,
            priority,
        )
        semantic_result = self._link_load_result(
            page,
            log,
            page_id=int(page_row["id"]),
            phase="STATE_AFTER_ACTION",
        )
        semantic_candidates = [
            finding
            for finding in semantic_result.get("semantic_findings") or []
            if finding.get("bug_eligible")
            and finding.get("confidence") == "high"
            and not finding.get("previously_observed")
        ]
        changes = observable_delta(before, after)
        if clipboard_after and clipboard_after != clipboard_before:
            changes.append(f"剪贴板写入 {len(clipboard_after)} 个字符")
        if log.page_errors:
            status = "SUSPECTED"
            outcome = f"点击期间出现页面脚本异常，列为疑似问题等待独立复现：{log.page_errors[0]}"
        elif semantic_candidates:
            status = "SUSPECTED"
            outcome = "动作后的页面状态触发高可信语义异常：" + "；".join(
                f"{item.get('classification')}（{item.get('actual')}）"
                for item in semantic_candidates
            )
        elif is_new:
            status = "STATE_DISCOVERED"
            outcome = f"发现新页面状态并插队 {queued} 个新控件；" + ("；".join(changes) if changes else "状态指纹发生变化")
        elif after["fingerprint"] != before["fingerprint"]:
            status = "TECH_PASS"
            outcome = "页面成功切换到已登记过的状态；" + ("；".join(changes) if changes else "状态指纹发生变化")
        elif changes:
            status = "TECH_PASS"
            outcome = "；".join(changes)
        else:
            status = "MANUAL_REVIEW"
            outcome = "点击已执行，但没有可稳定观察的页面变化；需要人工确认控件的预期效果"
        final_action_id = self._record_action(
            row,
            status,
            "state_change",
            outcome,
            evidence=log.evidence(),
            after_fingerprint=after["fingerprint"],
            before_theme=before.get("theme") or {},
            after_theme=after.get("theme") or {},
            new_state_id=state_id,
            queued_controls=queued,
            semantic_evaluation_id=semantic_result.get("semantic_evaluation_id"),
            semantic_status=semantic_result.get("semantic_status"),
            semantic_findings=semantic_result.get("semantic_findings") or [],
        )
        self.storage.finish_frontier(
            int(row["id"]),
            status,
            {"outcome": outcome, "new_state_id": state_id, "queued_controls": queued},
        )
        if log.page_errors:
            self._reproduce_script_error(
                browser,
                page_row,
                row,
                final_action_id,
                log.page_errors[0],
            )
        if semantic_candidates:
            self._reproduce_semantic_findings(
                browser,
                page_row,
                semantic_candidates,
                action_path=next_path,
                action_id=final_action_id,
            )

    def _observe_after_failure(self, page, row: dict) -> dict:
        observation = {"available": False, "state_changed": False}
        if page is None or page.is_closed():
            return observation
        try:
            snap, _ = self._snapshot(page)
            observation.update(
                {
                    "available": True,
                    "state_changed": (
                        snap.get("fingerprint") != row.get("state_fingerprint")
                        or canonical_url(self.target, snap.get("url") or "")
                        != canonical_url(self.target, row.get("descriptor", {}).get("page_url") or page.url)
                    ),
                    "fingerprint": snap.get("fingerprint"),
                    "url": self.redactor.redact_url(snap.get("url") or page.url),
                    "title": self.redactor.redact_text(snap.get("title") or ""),
                }
            )
        except Exception as exc:
            observation["observation_error"] = self.redactor.redact_text(f"{type(exc).__name__}: {exc}")
        return observation

    def _execute_frontier(self, browser, page_row: dict, row: dict) -> None:
        context = None
        page = None
        log = None
        stage = "open"
        self._current_action_attempted = False
        risk: ActionRisk = classify_action(
            row["descriptor"], self.safety, self.config.get("prohibited") or []
        )
        self._current_risk = risk
        try:
            context = self._context(browser)
            page, _, log = self._open(context, page_row["url"])
            stage = "replay"
            self._replay(page, row["action_path"])
            log.clear()
            stage = "locate"
            locator = locate_control(page, row["descriptor"])
            if not locator.is_visible(timeout=1500):
                raise RuntimeError("控件在重放到目标状态后不可见")
            descriptor = row["descriptor"]
            if descriptor.get("disabled") or not locator.is_enabled(timeout=1000):
                outcome = "控件在当前重放状态下处于禁用态，已记录且未强制点击"
                self._record_action(row, "DYNAMIC_DISABLED", "disabled", outcome, risk=risk.to_dict())
                self.storage.finish_frontier(
                    int(row["id"]),
                    "DYNAMIC_DISABLED",
                    {"disabled": True, "risk": risk.to_dict(), "outcome": outcome},
                )
                return
            reused = self._verified_shared_result(page, row)
            if reused:
                outcome = (
                    f"已确认共享控件在当前页面仍然可见、可用且指纹一致；"
                    f"复用页面 {reused['source_page_id']}（{reused['source_page_url']}）的 "
                    f"{reused['source_status']} 结果，不重复点击"
                )
                evidence = {
                    "shared_control_key": descriptor["shared_control_key"],
                    "shared_scope": descriptor.get("shared_scope"),
                    "shared_confidence": descriptor.get("shared_confidence"),
                    "source_page_id": reused["source_page_id"],
                    "source_page_url": reused["source_page_url"],
                    "source_control_id": reused["source_control_id"],
                    "source_action_id": reused["source_action_id"],
                    "source_status": reused["source_status"],
                    "source_outcome": reused["source_outcome"],
                }
                action_id = self._record_action(
                    row,
                    "SHARED_CONTROL_REUSED",
                    "shared_control_reuse",
                    outcome,
                    evidence=evidence,
                )
                self.storage.finish_frontier(
                    int(row["id"]),
                    "SHARED_CONTROL_REUSED",
                    evidence | {"outcome": outcome, "action_id": action_id},
                )
                return
            if not risk.allowed:
                outcome = f"{risk.reason}；风险等级 {risk.level}，未执行"
                self._record_action(row, "SKIPPED_RISK", "risk", outcome, risk=risk.to_dict())
                self.storage.finish_frontier(
                    int(row["id"]), "SKIPPED_RISK", {"risk": risk.to_dict(), "outcome": outcome}
                )
                return
            href = descriptor.get("href") or ""
            if href:
                scheme = urlsplit(href).scheme.lower()
                if scheme not in {"http", "https", ""}:
                    self._record_action(row, "OUT_OF_SCOPE", "protocol", f"{scheme}: 协议不在网页测试范围内，未唤起")
                    self.storage.finish_frontier(int(row["id"]), "OUT_OF_SCOPE", {"scheme": scheme})
                    return
                if not same_host(self.target, href):
                    self._record_action(row, "OUT_OF_SCOPE", "external_link", f"外域链接 {href}，只记录不进入")
                    self.storage.add_edge(
                        self.run_id,
                        int(page_row["id"]),
                        int(row["state_id"]),
                        int(row["control_id"]),
                        href,
                        "OUT_OF_SCOPE",
                        self.redactor.redact_value(row["full_path"]),
                    )
                    self.storage.finish_frontier(int(row["id"]), "OUT_OF_SCOPE", {"target": href})
                    return
                if canonical_url(self.target, href) != canonical_url(self.target, page_row["canonical_url"]):
                    stage = "action"
                    self._handle_known_link(page, context, page_row, row, locator, log)
                    return
            tag = descriptor.get("tag")
            field_type = (descriptor.get("type") or "").lower()
            if tag in {"textarea", "select"} or (
                tag == "input" and field_type not in {"button", "submit", "reset", "checkbox", "radio"}
            ):
                stage = "plan"
                self._handle_form_control(row)
                return
            stage = "action"
            self._handle_state_action(browser, page, context, page_row, row, locator, log)
        except Exception as exc:
            original = exc.cause if isinstance(exc, ActionAttemptError) else exc
            error_text = f"{type(original).__name__}: {original}"
            evidence = {
                "error": self.redactor.redact_text(error_text),
                "traceback": self.redactor.redact_text(traceback.format_exc(limit=5)),
                "stage": stage,
                "risk": risk.to_dict(),
            }
            if log:
                evidence.update(log.evidence())
            observation = self._observe_after_failure(page, row)
            evidence["post_failure_observation"] = observation

            decision_delay_ms = 0
            if "控件在重放到目标状态后不可见" in error_text:
                action_status = "STATE_REPLAY_GAP"
                outcome = "该控件属于先前页面状态，但重新执行点击路径后未再次出现；作为覆盖缺口记录，不判为产品缺陷"
                final_status = action_status
                self.storage.finish_frontier(int(row["id"]), final_status, evidence | {"outcome": outcome})
            elif "intercepts pointer events" in error_text:
                action_status = "NOT_ACTIONABLE"
                outcome = "控件被当前可操作元素遮挡或属于非活动轮播项，未强制点击，不判为产品缺陷"
                final_status = action_status
                self.storage.finish_frontier(int(row["id"]), final_status, evidence | {"outcome": outcome})
            elif "element is not enabled" in error_text:
                action_status = "DYNAMIC_DISABLED"
                outcome = "控件在执行时处于禁用态，已记录且未强制点击"
                final_status = action_status
                self.storage.finish_frontier(int(row["id"]), final_status, evidence | {"outcome": outcome})
            else:
                classification = classify_exception(original, evidence, stage)
                evidence["exception_classification"] = classification.to_dict()
                decision = decide_recovery(
                    classification,
                    risk,
                    action_attempted=self._current_action_attempted,
                    state_changed=bool(observation.get("state_changed")),
                    attempts=int(row["attempts"]),
                    retry_limit=int(self.settings["retry_limit"]),
                )
                evidence["recovery_decision"] = {
                    "status": decision.status,
                    "retry": decision.retry,
                    "reason": decision.reason,
                    "delay_ms": decision.delay_ms,
                }
                action_status = decision.status
                outcome = f"{classification.category}：{decision.reason}"
                decision_delay_ms = decision.delay_ms
                if decision.retry:
                    final_status = self.storage.retry_frontier(
                        int(row["id"]), evidence | {"outcome": outcome}, int(self.settings["retry_limit"])
                    )
                else:
                    final_status = decision.status
                    self.storage.finish_frontier(int(row["id"]), final_status, evidence | {"outcome": outcome})

            self._record_action(row, action_status, "recovery", outcome, evidence=evidence)
            href = row["descriptor"].get("href") or ""
            if href and same_host(self.target, href):
                target = canonical_url(self.target, href)
                visits = self.storage.rows(
                    "SELECT id FROM link_visits WHERE run_id=? AND target_url=?", (self.run_id, target)
                )
                if visits:
                    self.storage.finish_link(
                        int(visits[0]["id"]),
                        final_status,
                        error=evidence["error"],
                    )
            if decision_delay_ms:
                time.sleep(decision_delay_ms / 1000)
        finally:
            if log:
                log.detach()
            if context:
                context.close()
            self._current_risk = None

    def process_page(self, page_id: int) -> dict:
        if not self.storage.claim_page(page_id):
            return {"page_id": page_id, "status": "SKIPPED"}
        page_row = self.storage.page(page_id)
        self.storage.record_qa_transition(
            self.run_id,
            page_id,
            "PAGE",
            None,
            "UNDERSTAND_REQUIREMENT",
            "开始处理页面并读取运行需求",
        )
        self.storage.record_qa_transition(
            self.run_id,
            page_id,
            "PAGE",
            "UNDERSTAND_REQUIREMENT",
            "BUILD_ORACLE",
            "已加载显式 expected_behavior、business_rules 与结构推断规则",
            {"oracle_count": len(self.semantic.rules)},
        )
        self.storage.record_qa_transition(
            self.run_id,
            page_id,
            "PAGE",
            "BUILD_ORACLE",
            "PREPARE_PRECONDITIONS",
            "准备账户、语言、视口和隔离 BrowserContext",
        )
        if self.pagepatrol_enabled:
            patrol = run_pagepatrol(self.config, self.output_dir, page_row["url"], page_id)
            for key in ("report_json", "report_html"):
                if patrol.get(key):
                    self.storage.add_artifact(
                        self.run_id,
                        f"pagepatrol_{key.split('_')[-1]}",
                        patrol[key],
                        page_id=page_id,
                        metadata=patrol,
                    )
            if patrol.get("status") not in {"DONE", "DISABLED"}:
                self.storage.add_message(
                    self.run_id,
                    "warning",
                    "pagepatrol",
                    self.redactor.redact_text(
                        f"{page_row['url']} 的 PagePatrol 未确认：{patrol.get('error') or patrol.get('status')}"
                    ),
                    self.redactor.redact_value(patrol),
                )
        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=not self.headed)
                try:
                    context = self._context(browser)
                    log = None
                    try:
                        page, response, log = self._open(context, page_row["url"])
                        http_status = response.status if response else None
                        self.storage.record_qa_transition(
                            self.run_id,
                            page_id,
                            "INITIAL_PAGE",
                            "PREPARE_PRECONDITIONS",
                            "EXECUTE",
                            "前置条件已建立，打开目标页并等待稳定",
                        )
                        initial_result = self._link_load_result(
                            page,
                            log,
                            page_id=page_id,
                            phase="INITIAL_PAGE",
                            http_status=http_status,
                        )
                        body_chars = int(initial_result["body_chars"])
                        if http_status is not None and http_status >= 400:
                            raise RuntimeError(f"页面返回 HTTP {http_status}")
                        if body_chars == 0:
                            raise RuntimeError("页面没有可见文本内容")
                        self.storage.update_page(
                            page_id,
                            title=initial_result["title"],
                            http_status=http_status,
                            final_url=initial_result["final_url"],
                            content_summary=(
                                f"可见文本 {body_chars} 字符；页面类型 {initial_result['page_type']}；"
                                f"语义状态 {initial_result['semantic_status']}；"
                                f"覆盖 {initial_result['semantic_coverage']}"
                            ),
                        )
                        state_id, _, _, controls, queued = self._register_state(page, page_row, [], None, 0)
                        if state_id is None:
                            raise RuntimeError("无法登记页面初始状态")
                        self.storage.add_message(
                            self.run_id,
                            "info",
                            "exploration",
                            self.redactor.redact_text(
                                f"页面 {page_row['url']} 初始状态发现 {len(controls)} 个可见控件，入队 {queued} 个"
                            ),
                        )
                        semantic_reproductions = self._reproduce_semantic_findings(
                            browser,
                            page_row,
                            initial_result.get("semantic_findings") or [],
                        )
                        if initial_result["semantic_status"] != "PASS":
                            self.storage.add_message(
                                self.run_id,
                                "warning",
                                "semantic",
                                self.redactor.redact_text(
                                    f"页面 {page_row['url']} 语义检查为 {initial_result['semantic_status']}，"
                                    f"发现 {len(initial_result.get('semantic_findings') or [])} 项；"
                                    f"独立复验 {len(semantic_reproductions)} 项"
                                ),
                                {
                                    "evaluation_id": initial_result.get("semantic_evaluation_id"),
                                    "status": initial_result.get("semantic_status"),
                                    "coverage": initial_result.get("semantic_coverage"),
                                    "reproductions": semantic_reproductions,
                                },
                            )
                    finally:
                        if log:
                            try:
                                log.detach()
                            except Exception:
                                pass
                        try:
                            context.close()
                        except Exception:
                            pass

                    actions = 0
                    while actions < int(self.settings["max_actions_per_page"]):
                        if self.stop_event.is_set():
                            raise InterruptedError("测试已由用户暂停")
                        row = self.storage.claim_frontier(page_id, int(self.settings["retry_limit"]))
                        if not row:
                            break
                        self._execute_frontier(browser, page_row, row)
                        actions += 1
                    if actions >= int(self.settings["max_actions_per_page"]):
                        blocked = self.storage.block_remaining_frontier(
                            page_id, f"达到每页动作上限 {self.settings['max_actions_per_page']}"
                        )
                        self.storage.add_message(
                            self.run_id,
                            "warning",
                            "coverage",
                            self.redactor.redact_text(
                                f"页面 {page_row['url']} 达到动作上限，阻塞 {blocked} 个待测控件"
                            ),
                        )
                    self.storage.finish_page(page_id, "DONE")
                    self.storage.record_qa_transition(
                        self.run_id,
                        page_id,
                        "PAGE",
                        "CLASSIFY",
                        "REPORT",
                        "页面探索完成，结果可进入报告",
                        {"actions": actions},
                    )
                    return {"page_id": page_id, "status": "DONE", "actions": actions}
                finally:
                    browser.close()
        except InterruptedError as exc:
            self.storage.finish_page(page_id, "RETRY", str(exc))
            return {"page_id": page_id, "status": "RETRY", "error": str(exc)}
        except Exception as exc:
            error = self.redactor.redact_text(f"{type(exc).__name__}: {exc}")
            classification = classify_exception(exc, {}, "open")
            if classification.category == "AUTH_BLOCKED":
                next_status = "BLOCKED"
            elif classification.retryable and int(page_row.get("attempts") or 0) < int(self.settings["retry_limit"]):
                next_status = "RETRY"
            else:
                next_status = "LOAD_FAIL"
            self.storage.finish_page(page_id, next_status, error)
            self.storage.record_qa_transition(
                self.run_id,
                page_id,
                "PAGE_FAILURE",
                "OBSERVE",
                "CLASSIFY",
                f"{classification.category}：{error}",
                {"classification": classification.to_dict(), "next_status": next_status},
            )
            self.storage.add_message(
                self.run_id,
                "error",
                "page",
                self.redactor.redact_text(
                    f"页面 {page_row['url']} 处理失败（{classification.category}）：{error}"
                ),
                {"classification": classification.to_dict()},
            )
            return {"page_id": page_id, "status": next_status, "error": error}
