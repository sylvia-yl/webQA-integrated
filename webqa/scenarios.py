from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urljoin

from .safety import Redactor, classify_action
from .storage import Storage


class ScenarioRunner:
    def __init__(self, storage: Storage, run_id: str, config: dict, output_dir: Path):
        self.storage = storage
        self.run_id = run_id
        self.config = config
        self.output_dir = output_dir
        self.redactor = Redactor(config.get("safety") or {})

    def _context(self, browser, role: str):
        args = {
            "viewport": self.config["scope"]["viewport"],
            "locale": self.config.get("language") or "zh-CN",
            "accept_downloads": False,
        }
        accounts = self.config.get("accounts") or {}
        storage_state = None
        if role == str(accounts.get("mode") or "guest"):
            storage_state = accounts.get("storage_state")
        roles = accounts.get("roles") or {}
        if role in roles:
            role_value = roles[role]
            storage_state = role_value.get("storage_state") if isinstance(role_value, dict) else role_value
        if role != "guest" and not storage_state:
            raise RuntimeError(f"业务流程角色 {role!r} 没有配置 storage_state")
        if storage_state:
            state_path = Path(str(storage_state)).resolve()
            if not state_path.exists():
                raise RuntimeError(f"业务流程角色 {role!r} 的 storage_state 不存在：{state_path}")
            args["storage_state"] = str(state_path)
        context = browser.new_context(**args)
        context.set_default_timeout(int(self.config["traversal"]["action_timeout_ms"]))
        return context

    @staticmethod
    def _locator(page, spec):
        if isinstance(spec, str):
            return page.locator(spec).first
        if spec.get("selector"):
            return page.locator(str(spec["selector"])).first
        if spec.get("role"):
            return page.get_by_role(
                str(spec["role"]),
                name=spec.get("name"),
                exact=bool(spec.get("exact", True)),
            ).first
        if spec.get("text"):
            return page.get_by_text(str(spec["text"]), exact=bool(spec.get("exact", True))).first
        raise ValueError(f"步骤缺少 selector/role/text：{spec}")

    def _assert(self, page, spec: dict) -> dict:
        if spec.get("url_regex") and not re.search(str(spec["url_regex"]), page.url, re.I):
            raise AssertionError(f"URL {page.url!r} 不匹配 {spec['url_regex']!r}")
        if spec.get("title_regex") and not re.search(str(spec["title_regex"]), page.title(), re.I):
            raise AssertionError(f"标题 {page.title()!r} 不匹配 {spec['title_regex']!r}")
        if spec.get("text") is not None:
            text = str(spec["text"])
            if page.get_by_text(text, exact=bool(spec.get("exact", False))).count() == 0:
                raise AssertionError(f"页面缺少文案 {text!r}")
        if spec.get("not_text") is not None:
            text = str(spec["not_text"])
            if page.get_by_text(text, exact=bool(spec.get("exact", False))).count() > 0:
                raise AssertionError(f"页面出现禁止文案 {text!r}")
        if spec.get("selector"):
            count = page.locator(str(spec["selector"])).count()
            expected_count = spec.get("count")
            if expected_count is not None and count != int(expected_count):
                raise AssertionError(f"{spec['selector']!r} 数量 {count} != {expected_count}")
            if expected_count is None and count == 0:
                raise AssertionError(f"找不到 {spec['selector']!r}")
        if spec.get("role"):
            locator = page.get_by_role(str(spec["role"]), name=spec.get("name"))
            if locator.count() == 0:
                raise AssertionError(f"找不到 role={spec['role']!r}, name={spec.get('name')!r}")
        return {"status": "PASS", "assertion": spec}

    def _step(self, page, step: dict, flow: dict) -> dict:
        if "open" in step:
            target = urljoin(self.config["target"], str(step["open"]))
            response = page.goto(
                target,
                wait_until="domcontentloaded",
                timeout=int(self.config["traversal"]["navigation_timeout_ms"]),
            )
            page.wait_for_timeout(int(self.config["traversal"]["settle_ms"]))
            if response and response.status >= 400:
                raise AssertionError(f"打开 {target} 返回 HTTP {response.status}")
            return {"status": "PASS", "open": self.redactor.redact_url(target)}
        if "assert" in step:
            return self._assert(page, dict(step["assert"] or {}))
        if "wait_ms" in step:
            delay = min(10000, max(0, int(step["wait_ms"])))
            page.wait_for_timeout(delay)
            return {"status": "PASS", "wait_ms": delay}
        for action in ("click", "fill", "select", "check", "clear"):
            if action not in step:
                continue
            raw = step[action]
            spec = dict(raw) if isinstance(raw, dict) else {"selector": raw}
            locator = self._locator(page, spec)
            if not locator.is_visible(timeout=1500):
                raise AssertionError(f"{action} 的目标控件不可见：{spec}")
            if action == "click":
                descriptor = {
                    "tag": "button",
                    "role": spec.get("role") or "button",
                    "label": spec.get("name") or spec.get("text") or spec.get("selector") or "",
                }
                risk = classify_action(
                    descriptor,
                    self.config.get("safety") or {},
                    self.config.get("prohibited") or [],
                )
                if risk.level in {"STATEFUL", "DESTRUCTIVE"} and not (
                    flow.get("authorized_stateful")
                    and self.config.get("safety", {}).get("allow_stateful_actions")
                ):
                    raise RuntimeError(f"业务流程未获持久化动作授权：{risk.reason}")
                if not risk.allowed:
                    raise RuntimeError(f"业务流程动作被风险策略拒绝：{risk.reason}")
                locator.click()
            elif action == "fill":
                locator.fill(str(spec.get("value") or ""))
            elif action == "select":
                locator.select_option(str(spec.get("value") or ""))
            elif action == "check":
                locator.set_checked(bool(spec.get("value", True)))
            elif action == "clear":
                locator.fill("")
            page.wait_for_timeout(int(self.config["traversal"]["settle_ms"]))
            return {"status": "PASS", action: self.redactor.redact_value(spec)}
        raise ValueError(f"不支持的业务流程步骤：{step}")

    def run_all(self) -> list[dict]:
        flows = self.config.get("business_flows") or []
        profile = str(self.config.get("testing", {}).get("profile") or "full")
        profile_settings = (self.config.get("testing", {}).get("profiles") or {}).get(profile) or {}
        if not profile_settings.get("run_business_flows", True) or not flows:
            return []
        from playwright.sync_api import sync_playwright

        summaries: list[dict] = []
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                for index, flow in enumerate(flows):
                    flow_key = str(flow.get("key") or f"business-flow-{index + 1}")
                    profiles = {str(item) for item in flow.get("profiles") or ["smoke", "full"]}
                    if profile not in profiles or self.storage.business_flow_result(self.run_id, flow_key):
                        continue
                    role = str(flow.get("role") or "guest")
                    title = str(flow.get("title") or flow_key)
                    given = list(flow.get("given") or [])
                    when = list(flow.get("when") or [])
                    then = list(flow.get("then") or [])
                    context = None
                    page = None
                    requests: list[dict] = []
                    step_results: list[dict] = []
                    status = "PASS"
                    screenshot_path: Path | None = None
                    try:
                        context = self._context(browser, role)
                        page = context.new_page()
                        page.on(
                            "request",
                            lambda request: requests.append(
                                self.redactor.redact_value(
                                    {"method": request.method, "url": request.url}
                                )
                            ),
                        )
                        for phase, steps in (("given", given), ("when", when), ("then", then)):
                            for step in steps:
                                result = self._step(page, dict(step), flow)
                                step_results.append({"phase": phase, **result})
                        forbidden = {
                            str(item).upper()
                            for item in flow.get("forbidden_side_effects")
                            or (["POST", "PUT", "PATCH", "DELETE"] if not flow.get("authorized_stateful") else [])
                        }
                        violations = [request for request in requests if request.get("method") in forbidden]
                        if violations:
                            raise AssertionError(f"检测到禁止副作用请求：{violations[:5]}")
                    except Exception as exc:
                        status = "FAIL" if isinstance(exc, AssertionError) else "UNCONFIRMED"
                        step_results.append(
                            {
                                "phase": "execution",
                                "status": status,
                                "error": self.redactor.redact_text(f"{type(exc).__name__}: {exc}"),
                            }
                        )
                        if page and self.config.get("safety", {}).get("capture_screenshots", True):
                            screen_dir = self.output_dir / "business-flows"
                            screen_dir.mkdir(parents=True, exist_ok=True)
                            screenshot_path = screen_dir / f"{flow_key}.png"
                            try:
                                page.screenshot(
                                    path=str(screenshot_path),
                                    full_page=True,
                                    mask=[page.locator("input,textarea,[contenteditable='true']")],
                                    mask_color="#111111",
                                )
                            except Exception:
                                screenshot_path = None
                    finally:
                        if context:
                            context.close()
                    result = {
                        "steps": step_results,
                        "requests": requests[-100:],
                    }
                    self.storage.add_business_flow_result(
                        self.run_id,
                        flow_key,
                        profile,
                        role,
                        status,
                        title,
                        given,
                        when,
                        then,
                        self.redactor.redact_value(result),
                        str(screenshot_path) if screenshot_path else None,
                    )
                    if screenshot_path:
                        self.storage.add_artifact(
                            self.run_id,
                            "business_flow_failure",
                            str(screenshot_path),
                            metadata={"flow_key": flow_key, "status": status},
                        )
                    summaries.append({"flow_key": flow_key, "status": status, "title": title})
            finally:
                browser.close()
        return summaries
