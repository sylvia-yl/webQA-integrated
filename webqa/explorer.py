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
from .safety import (
    ActionRisk,
    Redactor,
    classify_action,
    classify_exception,
    decide_recovery,
)
from .storage import Storage
from .utils import canonical_url, json_load, same_host, sha


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
        snap, controls = snapshot(page, self.target, int(self.settings["max_controls_per_state"]))
        return self.redactor.redact_value(snap), self.redactor.redact_value(controls)

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
        return self.storage.record_action(
            self.run_id,
            int(row["page_id"]),
            int(row["state_id"]),
            int(row["control_id"]),
            int(row["id"]),
            status,
            action_type,
            self.redactor.redact_text(descriptor.get("label") or "未命名控件"),
            row.get("state_fingerprint"),
            evidence.pop("after_fingerprint", None),
            self.redactor.redact_text(outcome),
            self.redactor.redact_value(row["full_path"]),
            evidence,
        )

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

    def _link_load_result(self, page, log: EventLog) -> dict:
        loading_wait_timed_out = False
        try:
            page.wait_for_function(
                """() => {
                  const visible = el => {
                    const r=el.getBoundingClientRect(),s=getComputedStyle(el);
                    return r.width>0 && r.height>0 && s.display!=='none' && s.visibility!=='hidden' && Number(s.opacity)!==0;
                  };
                  const selector='[aria-busy="true"],[data-loading="true"],[data-state="loading"],.skeleton,[class*="skeleton"],[class*="spinner"],[class~="loading"]';
                  return document.readyState==='complete' && !Array.from(document.querySelectorAll(selector)).some(visible);
                }""",
                timeout=8000,
            )
        except Exception:
            loading_wait_timed_out = True
        try:
            content = page.evaluate(
                """() => {
                  const compact=v=>(v||'').replace(/\s+/g,' ').trim();
                  const visible=el=>{const r=el.getBoundingClientRect(),s=getComputedStyle(el);return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden'&&Number(s.opacity)!==0};
                  const loadingSelector='[aria-busy="true"],[data-loading="true"],[data-state="loading"],.skeleton,[class*="skeleton"],[class*="spinner"],[class~="loading"]';
                  const broken=Array.from(document.images).filter(img=>visible(img)&&img.complete&&img.naturalWidth===0).map(img=>img.currentSrc||img.src).slice(0,10);
                  return {
                    ready_state:document.readyState,
                    body_text:compact(document.body?.innerText||''),
                    main_text:compact(document.querySelector('main')?.innerText||''),
                    h1:compact(document.querySelector('h1')?.innerText||''),
                    alerts:Array.from(document.querySelectorAll('[role="alert"]')).filter(visible).map(x=>compact(x.innerText)).filter(Boolean).slice(0,10),
                    loading:Array.from(document.querySelectorAll(loadingSelector)).filter(visible).map(x=>compact(x.innerText)||x.className||x.tagName).slice(0,10),
                    broken_images:broken
                  };
                }"""
            )
        except Exception:
            content = {"body_text": "", "main_text": "", "h1": "", "alerts": [], "loading": [], "broken_images": [], "ready_state": "unknown"}
        body_chars = len(content.get("body_text") or "")
        status = None
        for item in reversed(log.responses):
            if canonical_url(self.target, item["url"]) == canonical_url(self.target, page.url):
                status = item["status"]
                break
        title = page.title() if not page.is_closed() else ""
        error_surface = " ".join([title, content.get("h1") or ""] + list(content.get("alerts") or []))
        error_page_re = re.compile(
            r"(^|\b)(404|500|not found|page not found|internal server error|application error|server error)(\b|$)|"
            r"页面不存在|找不到页面|服务器错误|内部错误|加载失败|出错了|发生错误",
            re.I,
        )
        error_page = bool(error_page_re.search(error_surface))
        reasons: list[str] = []
        if status is not None and status >= 400:
            reasons.append(f"文档 HTTP {status}")
        if body_chars < 50:
            reasons.append(f"可见正文过少（{body_chars} 字符）")
        if error_page:
            reasons.append(f"标题、H1 或错误提示疑似错误页：{error_surface[:240]}")
        if loading_wait_timed_out or content.get("loading"):
            reasons.append("等待数据加载完成超时或仍存在可见加载占位")
        if content.get("broken_images"):
            reasons.append(f"存在 {len(content['broken_images'])} 个可见破图")
        if log.page_errors:
            reasons.append(f"页面脚本异常：{log.page_errors[0]}")
        if log.response_errors:
            reasons.append(f"文档/API 错误响应：{log.response_errors[0]}")
        return {
            "final_url": page.url,
            "http_status": status,
            "title": title,
            "body_chars": body_chars,
            "h1": content.get("h1") or "",
            "main_chars": len(content.get("main_text") or ""),
            "ready_state": content.get("ready_state"),
            "loading_indicators": content.get("loading") or [],
            "broken_images": content.get("broken_images") or [],
            "error_page": error_page,
            "response_errors": log.response_errors[:10],
            "loaded": not reasons,
            "load_check_reasons": reasons,
        }

    def _click_link(self, page, context, locator, descriptor: dict, log: EventLog) -> tuple[object, dict]:
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
            result = self._link_load_result(destination_page, active_log)
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

        destination_page, result = self._click_link(page, context, locator, descriptor, log)
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
                    load_result = self._link_load_result(page, log)
                    self.storage.finish_link(int(visit["id"]), "CONFIRMED", **load_result)
                    arrival = list(row["full_path"]) + [{"label": f"已到达：{load_result.get('title') or after_canon}", "url": after_canon}]
                    target_page_id, _ = self._schedule_destination(page_row, row, after_canon, arrival)
                    status, outcome = "TECH_PASS", f"无 href 控件完成页面跳转：{after_canon}；留到下一层深入测试"
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
        changes = observable_delta(before, after)
        if clipboard_after and clipboard_after != clipboard_before:
            changes.append(f"剪贴板写入 {len(clipboard_after)} 个字符")
        if log.page_errors:
            status = "SUSPECTED"
            outcome = f"点击期间出现页面脚本异常，列为疑似问题等待独立复现：{log.page_errors[0]}"
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
                        body_chars = int(page.evaluate("() => (document.body?.innerText || '').trim().length"))
                        http_status = response.status if response else None
                        if http_status is not None and http_status >= 400:
                            raise RuntimeError(f"页面返回 HTTP {http_status}")
                        if body_chars == 0:
                            raise RuntimeError("页面没有可见文本内容")
                        self.storage.update_page(
                            page_id,
                            title=self.redactor.redact_text(page.title()),
                            http_status=http_status,
                            final_url=self.redactor.redact_url(page.url),
                            content_summary=f"可见文本 {body_chars} 字符",
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
