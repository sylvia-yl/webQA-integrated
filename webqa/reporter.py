from __future__ import annotations

import html
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .safety import Redactor
from .storage import Storage
from .utils import canonical_url, json_load, now_iso, path_label, same_host


def _md(value: object) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", "<br>")


def _status_cn(value: str) -> str:
    return {
        "PENDING": "待处理",
        "RUNNING": "处理中",
        "PAUSED": "已暂停",
        "INTERRUPTED": "被下一次续跑接管",
        "READY": "可继续",
        "RETRY": "待重试",
        "DONE": "已完成",
        "COMPLETED": "运行完成",
        "LOAD_FAIL": "加载失败",
        "TECH_PASS": "技术通过",
        "TECH_PASS_WITH_WARNINGS": "技术通过（有提示）",
        "STATE_DISCOVERED": "发现新状态",
        "SHARED_CONTROL_REUSED": "共享控件已核验并复用",
        "LINK_RECORDED": "子页面链接已记录（未进入）",
        "LINK_RECORDED_AFTER_CLICK": "点击后识别子页面（未继续测试）",
        "DUPLICATE_LINK": "重复链接（未再点击）",
        "DUPLICATE_AFTER_CLICK": "点击后识别为重复链接",
        "BOUNDARY": "深度边界（未点击）",
        "BOUNDARY_AFTER_CLICK": "点击后识别为深度边界",
        "OUT_OF_SCOPE": "范围外",
        "SKIPPED_RISK": "风险跳过",
        "FORM_PLANNED": "已生成表单用例",
        "FORM_EXECUTED": "表单用例通过",
        "FORM_EXECUTED_WITH_WARNINGS": "表单用例有未确认项",
        "FORM_CONTRACT_FAILED": "表单约束失败",
        "LIST_SAMPLE_PASS": "列表样本通过",
        "PAGINATION_SAMPLE_PASS": "分页样本通过",
        "PAGINATION_INFERRED": "分页模式推导代表页",
        "NEEDS_SPEC": "业务规则未确认",
        "UNCONFIRMED": "自动化未确认",
        "MANUAL_REVIEW": "需要人工确认",
        "STATE_REPLAY_GAP": "状态重放覆盖缺口（非缺陷）",
        "NOT_ACTIONABLE": "非当前可操作控件（已排除）",
        "DYNAMIC_DISABLED": "动态禁用控件",
        "NEEDS_RETEST": "策略修正后需重测",
        "SUSPECTED": "疑似问题（未确认缺陷）",
        "BLOCKED": "阻塞",
        "OBSERVED": "中间观测记录",
    }.get(value, value)


def _friendly_label(label: str | None, href: str | None, section: str | None) -> str:
    value = re.sub(r"\s+", " ", label or "").strip()
    if value and not value.startswith("未命名"):
        return value
    if href:
        parts = urlsplit(href)
        path_parts = [unquote(part).replace("-", " ") for part in parts.path.split("/") if part]
        target = unquote(parts.fragment).replace("-", " ") if parts.fragment else (path_parts[-1] if path_parts else parts.hostname or href)
        return f"{section} → {target}" if section else f"链接：{target}"
    kind = "按钮" if "button" in value.lower() else "控件"
    return f"未提供可访问名称的{kind}" + (f"（{section}）" if section else "")


def _effective_action(action: dict) -> dict:
    value = dict(action)
    value["display_label"] = _friendly_label(value.get("label"), value.get("href"), value.get("section"))
    descriptor = json_load(value.get("descriptor_json"), {})
    legacy_icons = {
        "AI 助手": "机器人",
        "切换到浅色主题": "太阳",
        "切换到深色主题": "月亮",
    }
    if descriptor.get("icon_only") or value["display_label"] in legacy_icons:
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
        raw_icon = str(descriptor.get("icon_name") or "").lower()
        icon_name = icon_names.get(raw_icon, raw_icon.replace("-", " ") if raw_icon else "")
        if not icon_name:
            icon_name = legacy_icons.get(value["display_label"], "")
        control_kind = "按钮" if descriptor.get("role") == "button" or descriptor.get("tag") == "button" else "链接"
        visual = f"{icon_name}图标{control_kind}" if icon_name else f"图标{control_kind}"
        if "图标按钮" not in value["display_label"] and "图标链接" not in value["display_label"]:
            value["display_label"] = f"{value['display_label']}（{visual}）"
    value["effective_status"] = value.get("status")
    value["display_outcome"] = value.get("outcome") or ""
    if value.get("status") == "SKIPPED_RISK" and value.get("href"):
        value["effective_status"] = "NEEDS_RETEST"
        value["display_outcome"] = "旧版风险规则误把普通导航文案当成持久化操作；已修正规则，本项需要重新点击验证"
        return value
    if value.get("status") != "UNCONFIRMED":
        return value
    evidence = json_load(value.get("evidence_json"), {})
    error = str(evidence.get("error") or "")
    if "控件在重放到目标状态后不可见" in error:
        value["effective_status"] = "STATE_REPLAY_GAP"
        value["display_outcome"] = "重新执行点击路径后控件未再次出现；属于自动化覆盖缺口，不是产品缺陷"
    elif "intercepts pointer events" in error:
        value["effective_status"] = "NOT_ACTIONABLE"
        value["display_outcome"] = "控件被当前活动元素遮挡或属于非活动轮播项；已排除，不是产品缺陷"
    elif "element is not enabled" in error:
        value["effective_status"] = "DYNAMIC_DISABLED"
        value["display_outcome"] = "控件在执行时处于禁用态，未强制点击"
    elif (
        not error
        and value.get("before_fingerprint")
        and value.get("after_fingerprint")
        and value["before_fingerprint"] != value["after_fingerprint"]
    ):
        value["effective_status"] = "TECH_PASS"
        if "主题" in value["display_label"]:
            value["display_outcome"] = "主题切换已经生效；旧检测器因页面返回已登记过的主题状态而误判为无反应"
        else:
            value["display_outcome"] = "点击后页面成功切换到已登记过的状态；旧检测器误判为无反应"
    elif not error and ("未检测到" in value["display_outcome"] or "没有可稳定观察" in value["display_outcome"]):
        value["effective_status"] = "MANUAL_REVIEW"
        value["display_outcome"] = "点击完成，但没有可稳定观察的页面变化；需要人工确认预期效果"
    return value


def _action_path_text(action: dict) -> str:
    path = json_load(action.get("full_path_json"), [])
    if path and "未命名" in str(path[-1].get("label") or ""):
        path = [dict(item) if isinstance(item, dict) else item for item in path]
        path[-1]["label"] = f"点击「{action.get('display_label') or '名称缺失控件'}」"
    return path_label(path)


def _inline(text: str) -> str:
    escaped = html.escape(text, quote=False)
    escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
    escaped = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', escaped)
    return escaped


def markdown_to_html(markdown: str, title: str) -> str:
    lines = markdown.splitlines()
    output: list[str] = []
    in_list = False
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            if in_list:
                output.append("</ul>")
                in_list = False
            index += 1
            continue
        if line.startswith("#"):
            if in_list:
                output.append("</ul>")
                in_list = False
            level = min(len(line) - len(line.lstrip("#")), 4)
            output.append(f"<h{level}>{_inline(line[level:].strip())}</h{level}>")
            index += 1
            continue
        if line.startswith("- "):
            if not in_list:
                output.append("<ul>")
                in_list = True
            output.append(f"<li>{_inline(line[2:])}</li>")
            index += 1
            continue
        if "|" in line and index + 1 < len(lines) and re.match(r"^\s*\|?\s*:?-+", lines[index + 1]):
            if in_list:
                output.append("</ul>")
                in_list = False
            header = [cell.strip() for cell in line.strip().strip("|").split("|")]
            index += 2
            rows: list[list[str]] = []
            while index < len(lines) and "|" in lines[index] and lines[index].strip():
                rows.append([cell.strip() for cell in lines[index].strip().strip("|").split("|")])
                index += 1
            output.append("<div class=table-wrap><table><thead><tr>" + "".join(f"<th>{_inline(c)}</th>" for c in header) + "</tr></thead><tbody>")
            for row in rows:
                output.append("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in row) + "</tr>")
            output.append("</tbody></table></div>")
            continue
        if in_list:
            output.append("</ul>")
            in_list = False
        output.append(f"<p>{_inline(line)}</p>")
        index += 1
    if in_list:
        output.append("</ul>")
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title><style>
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;max-width:1500px;margin:0 auto;padding:30px;color:#1d2530;background:#f5f7fa;line-height:1.55}}
h1,h2,h3{{color:#13213c}} h1{{border-bottom:3px solid #3559e0;padding-bottom:12px}}
p,li{{max-width:1200px}} code{{background:#eef1f6;padding:2px 5px;border-radius:4px}}
.table-wrap{{overflow:auto;margin:12px 0 24px}} table{{border-collapse:collapse;width:100%;background:white;font-size:14px}}
th,td{{border:1px solid #dbe1ea;padding:8px 10px;text-align:left;vertical-align:top}} th{{background:#eaf0ff;position:sticky;top:0}}
a{{color:#234dcc}} ul{{background:white;padding:12px 30px;border-radius:8px}}
</style></head><body>{''.join(output)}</body></html>"""


class Reporter:
    def __init__(self, storage: Storage, run_id: str, config: dict, output_dir: Path):
        self.storage = storage
        self.run_id = run_id
        self.config = config
        self.output_dir = output_dir
        self.redactor = Redactor(config.get("safety") or {})
        self.storage.rebase_output_references(config["output"]["directory"])

    def final_actions(self) -> list[dict]:
        rows = self.storage.rows(
            """SELECT a.*,c.href,c.section,c.descriptor_json,f.attempts
               FROM actions a
               LEFT JOIN controls c ON c.id=a.control_id
               LEFT JOIN frontier f ON f.id=a.frontier_id
               WHERE a.run_id=? AND a.status!='OBSERVED'
                 AND a.id=(SELECT MAX(a2.id) FROM actions a2
                           WHERE a2.frontier_id=a.frontier_id AND a2.status!='OBSERVED')
               ORDER BY a.page_id,a.id""",
            (self.run_id,),
        )
        return [_effective_action(row) for row in rows]

    def summary(self) -> dict:
        run = self.storage.run(self.run_id)
        pages = self.storage.rows("SELECT status,COUNT(*) n FROM page_nodes WHERE run_id=? GROUP BY status", (self.run_id,))
        action_attempts = self.storage.rows("SELECT status,COUNT(*) n FROM actions WHERE run_id=? GROUP BY status", (self.run_id,))
        frontier = self.storage.rows("SELECT status,COUNT(*) n FROM frontier WHERE run_id=? GROUP BY status", (self.run_id,))
        final_results = Counter(action["effective_status"] for action in self.final_actions())
        visited_links = self.storage.scalar("SELECT COUNT(*) FROM link_visits WHERE run_id=?", (self.run_id,))
        edge_targets = self.storage.rows("SELECT DISTINCT target_url FROM page_edges WHERE run_id=?", (self.run_id,))
        recorded_same_host_links = sum(1 for row in edge_targets if same_host(run["target"], row["target_url"]))
        semantic_evaluations = self.storage.rows(
            "SELECT status,COUNT(*) n FROM semantic_evaluations WHERE run_id=? GROUP BY status",
            (self.run_id,),
        )
        semantic_findings = self.storage.rows(
            """SELECT sf.classification,COUNT(*) n FROM semantic_findings sf
               JOIN semantic_evaluations se ON se.id=sf.evaluation_id
               WHERE se.run_id=? GROUP BY sf.classification""",
            (self.run_id,),
        )
        contract_failures = self.storage.scalar(
            "SELECT COUNT(*) FROM contract_results WHERE run_id=? AND status='FAIL'",
            (self.run_id,),
        )
        external_findings = self.storage.scalar(
            "SELECT COUNT(*) FROM external_findings WHERE run_id=?", (self.run_id,)
        )
        invariant_failures = self.storage.scalar(
            "SELECT COUNT(*) FROM invariant_results WHERE run_id=? AND status='FAIL'",
            (self.run_id,),
        )
        business_flows = self.storage.rows(
            "SELECT status,COUNT(*) n FROM business_flow_results WHERE run_id=? GROUP BY status",
            (self.run_id,),
        )
        list_sampling = self.storage.rows(
            """SELECT sample_kind,SUM(selected_count) selected,SUM(skipped_count) skipped
               FROM list_sampling_results WHERE run_id=? GROUP BY sample_kind""",
            (self.run_id,),
        )
        return {
            "run": run,
            "pages": {row["status"]: row["n"] for row in pages},
            "actions": dict(final_results),
            "action_attempts": {row["status"]: row["n"] for row in action_attempts},
            "frontier": {row["status"]: row["n"] for row in frontier},
            "states": self.storage.scalar("SELECT COUNT(*) FROM state_nodes WHERE run_id=?", (self.run_id,)),
            "control_occurrences": self.storage.scalar("SELECT COUNT(*) FROM controls WHERE run_id=?", (self.run_id,)),
            "unique_controls": self.storage.scalar("SELECT COUNT(*) FROM frontier WHERE run_id=?", (self.run_id,)),
            "links": max(int(visited_links or 0), recorded_same_host_links),
            "semantic_evaluations": {row["status"]: row["n"] for row in semantic_evaluations},
            "semantic_findings": {row["classification"]: row["n"] for row in semantic_findings},
            "qa_transitions": self.storage.scalar(
                "SELECT COUNT(*) FROM qa_state_transitions WHERE run_id=?", (self.run_id,)
            ),
            "contract_failures": contract_failures,
            "external_findings": external_findings,
            "invariant_failures": invariant_failures,
            "business_flows": {row["status"]: row["n"] for row in business_flows},
            "list_sampling": {
                row["sample_kind"]: {
                    "selected": int(row["selected"] or 0),
                    "skipped": int(row["skipped"] or 0),
                }
                for row in list_sampling
            },
        }

    def _artifact_reference(self, artifact: dict) -> tuple[str | None, str]:
        """Return a portable link contained by the current run directory."""
        run_root = self.output_dir.resolve()
        raw = Path(artifact["path"])
        label = raw.name or "未命名证据"
        candidate = (run_root / raw).resolve() if not raw.is_absolute() else raw.resolve()
        try:
            relative = candidate.relative_to(run_root)
        except ValueError:
            parts = list(raw.parts)
            try:
                run_index = next(index for index, part in enumerate(parts) if part == self.run_id)
            except StopIteration:
                return None, label
            candidate = run_root.joinpath(*parts[run_index + 1 :]).resolve()
            try:
                relative = candidate.relative_to(run_root)
            except ValueError:
                return None, label
        if not candidate.exists():
            return None, label
        kind = str(artifact.get("kind") or "")
        try:
            if kind == "pagepatrol_json":
                content = json.loads(candidate.read_text(encoding="utf-8"))
                candidate.write_text(
                    json.dumps(self.redactor.redact_value(content), ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            elif kind == "pagepatrol_html":
                candidate.write_text(
                    self.redactor.redact_text(candidate.read_text(encoding="utf-8", errors="replace")),
                    encoding="utf-8",
                )
        except (OSError, json.JSONDecodeError) as exc:
            self.storage.add_message(
                self.run_id,
                "warning",
                "redaction",
                self.redactor.redact_text(f"证据 {label} 脱敏失败：{type(exc).__name__}: {exc}"),
            )
        normalized = str(candidate)
        if normalized != artifact["path"]:
            self.storage.update_artifact_path(int(artifact["id"]), normalized)
        return relative.as_posix(), label

    def build_markdown(self) -> str:
        run = self.storage.run(self.run_id)
        run_config = json_load(run.get("config_json"), self.config)
        child_page_policy = (run_config.get("traversal") or {}).get("child_page_check", "load_only")
        deferred_strategy = child_page_policy == "record_only"
        root_only = int(run.get("max_depth") or 0) == 0
        pages = self.storage.rows(
            """SELECT p.*,
                      (SELECT COUNT(*) FROM state_nodes s WHERE s.page_id=p.id) state_count,
                      (SELECT COUNT(*) FROM frontier f WHERE f.page_id=p.id) control_count
               FROM page_nodes p WHERE p.run_id=? ORDER BY p.depth,p.id""",
            (self.run_id,),
        )
        actions = self.final_actions()
        edges = self.storage.rows(
            """SELECT e.*, p.depth source_depth, p.url source_url, tp.depth target_depth
               FROM page_edges e JOIN page_nodes p ON p.id=e.source_page_id
               LEFT JOIN page_nodes tp ON tp.id=e.target_page_id
               WHERE e.run_id=? ORDER BY p.depth,e.id""",
            (self.run_id,),
        )
        states = self.storage.rows(
            "SELECT * FROM state_nodes WHERE run_id=? ORDER BY page_id,id", (self.run_id,)
        )
        artifacts = self.storage.rows("SELECT * FROM artifacts WHERE run_id=? ORDER BY page_id,id", (self.run_id,))
        messages = self.storage.rows("SELECT * FROM messages WHERE run_id=? ORDER BY id", (self.run_id,))
        test_cases = self.storage.rows("SELECT * FROM test_cases WHERE run_id=? ORDER BY page_id,id", (self.run_id,))
        bugs = self.storage.rows("SELECT * FROM bugs WHERE run_id=? ORDER BY id", (self.run_id,))
        reproductions = self.storage.rows(
            """SELECT r.* FROM reproductions r JOIN bugs b ON b.id=r.bug_id
               WHERE b.run_id=? ORDER BY r.bug_id,r.attempt""",
            (self.run_id,),
        )
        semantic_evaluations = self.storage.rows(
            "SELECT * FROM semantic_evaluations WHERE run_id=? ORDER BY id", (self.run_id,)
        )
        semantic_findings = self.storage.rows(
            """SELECT sf.*,se.page_id,se.phase,se.observed_url,se.page_type,se.coverage
               FROM semantic_findings sf JOIN semantic_evaluations se ON se.id=sf.evaluation_id
               WHERE se.run_id=? ORDER BY sf.id""",
            (self.run_id,),
        )
        qa_transitions = self.storage.rows(
            "SELECT * FROM qa_state_transitions WHERE run_id=? ORDER BY id", (self.run_id,)
        )
        contract_failures = self.storage.rows(
            """SELECT cr.*,p.canonical_url FROM contract_results cr
               JOIN page_nodes p ON p.id=cr.page_id
               WHERE cr.run_id=? AND cr.status IN ('FAIL','UNCONFIRMED')
               ORDER BY cr.page_id,cr.id""",
            (self.run_id,),
        )
        external_findings = self.storage.rows(
            """SELECT ef.*,p.canonical_url FROM external_findings ef
               JOIN page_nodes p ON p.id=ef.page_id
               WHERE ef.run_id=? ORDER BY ef.page_id,ef.id""",
            (self.run_id,),
        )
        invariant_findings = self.storage.rows(
            """SELECT ir.*,p.canonical_url,c.label control_label FROM invariant_results ir
               JOIN page_nodes p ON p.id=ir.page_id
               LEFT JOIN controls c ON c.id=ir.control_id
               WHERE ir.run_id=? AND ir.status IN ('FAIL','UNCONFIRMED')
               ORDER BY ir.page_id,ir.id""",
            (self.run_id,),
        )
        business_flow_results = self.storage.rows(
            "SELECT * FROM business_flow_results WHERE run_id=? ORDER BY id", (self.run_id,)
        )
        form_findings = self.storage.rows(
            """SELECT tc.*,p.canonical_url,c.label control_label
               FROM test_cases tc JOIN page_nodes p ON p.id=tc.page_id
               LEFT JOIN controls c ON c.id=tc.control_id
               WHERE tc.run_id=? AND tc.status IN ('FAIL','UNCONFIRMED')
               ORDER BY tc.page_id,tc.id""",
            (self.run_id,),
        )
        list_sampling = self.storage.rows(
            """SELECT ls.*,p.canonical_url FROM list_sampling_results ls
               JOIN page_nodes p ON p.id=ls.page_id
               WHERE ls.run_id=? ORDER BY ls.page_id,ls.id""",
            (self.run_id,),
        )
        rounds = self.storage.rows(
            "SELECT * FROM run_rounds WHERE run_id=? ORDER BY round_no", (self.run_id,)
        )
        summary = self.summary()
        page_status = Counter({key: int(value) for key, value in summary["pages"].items()})
        action_status = Counter({key: int(value) for key, value in summary["actions"].items()})
        frontier_status = Counter({key: int(value) for key, value in summary["frontier"].items()})
        deferred_targets = {
            canonical_url(run["target"], edge["target_url"])
            for edge in edges
            if edge.get("target_page_id") is None and same_host(run["target"], edge["target_url"])
        }
        md: list[str] = ["# WebQA 状态图测试报告", ""]
        md.extend(
            [
                f"- 运行 ID：`{self.run_id}`",
                f"- 目标：{run['target']}",
                f"- 测试模式：{str((run_config.get('testing') or {}).get('profile') or 'full').upper()}",
                f"- 状态：{_status_cn(run['status'])}",
                f"- 实例开始时间：{run['created_at']}",
                f"- 实例最后更新时间：{run['updated_at']}",
                (
                    "- 页面范围：只测试起始页面；同域子页面链接只记录，不点击、不进入"
                    if root_only
                    else f"- 页面范围：已从主页解锁并累计测试到第 {run['max_depth']} 层"
                ),
                f"- 当前批次层级：{run['current_depth']}",
                f"- 生成时间：{now_iso()}",
                "- 判定原则：报告按控件最终结果去重；自动重试只保留最后一次结论，不重复计数。",
                "- 状态重放失败、非活动轮播项和动态禁用控件单列为覆盖信息，不计入产品缺陷或自动化未确认。",
                (
                    "- 子页面策略：当前页只收集下一层链接；达到解锁深度后，下一轮复用本实例并将其提升为标准页面，不重测已完成页面。"
                    if deferred_strategy
                    else "- 子页面策略：当前层仅通过真实点击确认子页面正确加载，下一层批次才检查其内容和控件。"
                ),
                "",
                "## 覆盖总账",
                "",
                f"- 页面节点：{len(pages)}；页面状态：{len(states)}",
                f"- 可见控件出现次数：{summary['control_occurrences']}；页面内去重后的待测控件：{summary['unique_controls']}",
                f"- 已记录的同域目标链接：{summary['links']}；等待更深轮次解锁：{len(deferred_targets)}；表单边界用例：{len(test_cases)}",
                f"- 语义观察：{len(semantic_evaluations)} 次；语义发现：{len(semantic_findings)} 项；"
                f"分类：{'；'.join(f'{k} {v}' for k,v in summary['semantic_findings'].items()) or '无'}",
                f"- QA 状态转换：{len(qa_transitions)} 条；主链：理解需求 → Oracle → 前置条件 → 执行 → 观察 → 分类 → 复现 → 报告",
                f"- 自动测试发现：契约 {len(contract_failures)}；PagePatrol 明显缺陷 {len(external_findings)}；"
                f"表单 {len(form_findings)}；不变量 {len(invariant_findings)}；业务流程 {len(business_flow_results)}",
                f"- 列表采样：已执行 {sum(int(item['selected_count']) for item in list_sampling)} 个代表控件；"
                f"跳过 {sum(int(item['skipped_count']) for item in list_sampling)} 个重复控件；"
                "分页按首页/中间/末页代表点验证",
                f"- 页面状态分布：{'；'.join(f'{_status_cn(k)} {v}' for k,v in page_status.items()) or '无'}",
                f"- 控件最终结果：{'；'.join(f'{_status_cn(k)} {v}' for k,v in action_status.items()) or '无'}",
                "",
                "## 结果口径",
                "",
                "| 类别 | 数量 | 解释 |",
                "|---|---:|---|",
            ]
        )
        for key, count in sorted(action_status.items()):
            md.append(f"| {_status_cn(key)} | {count} | {key} |")
        confirmed_bugs = [bug for bug in bugs if bug["status"] == "CONFIRMED"]
        if rounds:
            round_modes = {"NEW": "新建实例", "RESUME": "断点续跑", "EXPAND": "提高深度接续"}
            md.extend(
                [
                    f"| 已确认缺陷 | {len(confirmed_bugs)} | 仅统计两个全新 BrowserContext 中均稳定复现的问题 |",
                    "",
                    "## 运行轮次与深度接续",
                    "",
                    "| 轮次 | 类型 | 深度变化 | 从历史链接提升页面 | 状态 | 开始时间 | 完成时间 |",
                    "|---:|---|---:|---:|---|---|---|",
                ]
            )
            for item in rounds:
                md.append(
                    f"| {item['round_no']} | {round_modes.get(item['mode'], item['mode'])} | "
                    f"{item['from_depth']} → {item['to_depth']} | {item['activated_pages']} | "
                    f"{_status_cn(item['status'])} | {item['started_at']} | {item.get('finished_at') or '-'} |"
                )
            md.append("")
        else:
            md.append(f"| 已确认缺陷 | {len(confirmed_bugs)} | 仅统计两个全新 BrowserContext 中均稳定复现的问题 |")
        md.extend(["", "## 可追溯 QA 状态机", ""])
        md.append("`UNDERSTAND_REQUIREMENT → BUILD_ORACLE → PREPARE_PRECONDITIONS → EXECUTE → OBSERVE → CLASSIFY → REPRODUCE → REPORT`")
        md.append("")
        md.extend(
            [
                "| 页面 ID | 阶段 | 状态转换 | 原因 |",
                "|---:|---|---|---|",
            ]
        )
        for item in qa_transitions:
            transition = f"{item.get('from_state') or 'START'} → {item['to_state']}"
            md.append(
                f"| {item['page_id']} | {_md(item['phase'])} | {_md(transition)} | {_md(item['reason'])} |"
            )
        md.extend(["", "## 页面理解与语义 Oracle", ""])
        if not semantic_evaluations:
            md.append("- 当前运行没有语义观察记录。")
        else:
            md.extend(
                [
                    "| 页面 | 阶段 | 页面类型 | 语义状态 | 覆盖 |",
                    "|---|---|---|---|---|",
                ]
            )
            for item in semantic_evaluations:
                md.append(
                    f"| {_md(item['observed_url'])} | {_md(item['phase'])} | {_md(item['page_type'])} | "
                    f"{_md(item['status'])} | {_md(item['coverage'])} |"
                )
            md.append("")
            if semantic_findings:
                md.extend(
                    [
                        "| 分类 | 状态 | 置信度/严重度 | Oracle 来源 | 预期 | 实际 |",
                        "|---|---|---|---|---|---|",
                    ]
                )
                for finding in semantic_findings:
                    md.append(
                        f"| {_md(finding['classification'])} | {_md(finding['status'])} | "
                        f"{_md(finding['confidence'])} / {_md(finding['severity'])} | "
                        f"{_md(finding['oracle_source'])} | {_md(finding['expected'])} | {_md(finding['actual'])} |"
                    )
        if contract_failures:
            md.extend(
                [
                    "",
                    "## API/UI 契约异常",
                    "",
                    "| 类型 | 页面 | 规则 | 严重度 | 问题 | 预期 | 实际 |",
                    "|---|---|---|---|---|---|---|",
                ]
            )
            for item in contract_failures:
                md.append(
                    f"| {_md(item['contract_type'])} | {_md(item['canonical_url'])} | "
                    f"{_md(item['rule_key'])} | {_md(item['severity'])} | {_md(item['title'])} | "
                    f"{_md(item['expected'])} | {_md(item['actual'])} |"
                )
        if list_sampling:
            md.extend(
                [
                    "",
                    "## 列表与分页采样",
                    "",
                    "重复列表不会逐项点击。冒烟模式默认打开 1 个代表项目，全量模式默认打开首页和末页各 1 个；分页验证首页、可识别的中间代表页和末页。",
                    "",
                    "| 页面 | 类型 | 策略 | 当前页/末页 | 可见项 | 执行 | 跳过重复控件 | 样本 |",
                    "|---|---|---|---|---:|---:|---:|---|",
                ]
            )
            for item in list_sampling:
                context = json_load(item.get("page_context_json"), {})
                selected = json_load(item.get("selected_json"), [])
                samples = "；".join(
                    str(value.get("label") or value.get("href") or value.get("page") or "-")
                    for value in selected
                ) or "-"
                md.append(
                    f"| {_md(item['canonical_url'])} | {_md(item['sample_kind'])} | "
                    f"{_md(item['strategy'])} | {_md(context.get('current') or '-')} / "
                    f"{_md(context.get('last') or '-')} | {item['visible_items']} | "
                    f"{item['selected_count']} | {item['skipped_count']} | {_md(samples)} |"
                )
        if external_findings:
            md.extend(
                [
                    "",
                    "## PagePatrol 明显缺陷",
                    "",
                    "正常结果、warning 和 info 不在此汇总；完整信息仍保留在 PagePatrol 原报告。",
                    "",
                    "| 页面 | 检查 | 类别 | 严重度 | 问题 | 元素 | 原报告 |",
                    "|---|---|---|---|---|---|---|",
                ]
            )
            for item in external_findings:
                report_label = "-"
                if item.get("report_path"):
                    report_file = Path(item["report_path"])
                    try:
                        relative = report_file.resolve().relative_to(self.output_dir.resolve())
                        if report_file.exists():
                            report_label = f"[打开]({relative.as_posix()})"
                    except ValueError:
                        pass
                md.append(
                    f"| {_md(item['canonical_url'])} | {_md(item['check_key'])} | "
                    f"{_md(item['category'])} | {_md(item['severity'])} | {_md(item['title'])} | "
                    f"{_md(item.get('selector') or '-')} | {report_label} |"
                )
        if form_findings:
            md.extend(
                [
                    "",
                    "## 等价类与边界值异常",
                    "",
                    "| 页面 | 字段 | 用例 | 状态 | 预期 | 实际 |",
                    "|---|---|---|---|---|---|",
                ]
            )
            for item in form_findings:
                result = json_load(item.get("result_json"), {})
                actual = result.get("error") or result.get("validity") or result
                md.append(
                    f"| {_md(item['canonical_url'])} | {_md(item.get('control_label') or item['control_id'])} | "
                    f"{_md(item['case_type'])} | {_md(item['status'])} | {_md(item['expected'])} | {_md(actual)} |"
                )
        if invariant_findings:
            md.extend(
                [
                    "",
                    "## 幂等、可逆与蜕变关系异常",
                    "",
                    "| 页面 | 控件 | 关系 | 来源/可信度 | 状态 | 预期 | 实际 |",
                    "|---|---|---|---|---|---|---|",
                ]
            )
            for item in invariant_findings:
                md.append(
                    f"| {_md(item['canonical_url'])} | {_md(item.get('control_label') or '-')} | "
                    f"{_md(item['invariant_type'])} | {_md(item['source'])} / {_md(item['confidence'])} | "
                    f"{_md(item['status'])} | {_md(item['expected'])} | {_md(item['actual'])} |"
                )
        if business_flow_results:
            md.extend(
                [
                    "",
                    "## Given/When/Then 业务流程",
                    "",
                    "| 流程 | 角色 | 模式 | 状态 | 结果 |",
                    "|---|---|---|---|---|",
                ]
            )
            for item in business_flow_results:
                result = json_load(item.get("result_json"), {})
                failures = [
                    step.get("error")
                    for step in result.get("steps") or []
                    if step.get("error")
                ]
                md.append(
                    f"| {_md(item['title'])} | {_md(item['role'])} | {_md(item['profile'])} | "
                    f"{_md(item['status'])} | {_md('；'.join(failures) if failures else '全部断言通过')} |"
                )
        md.extend(
            [
                "",
                "## 分层页面批次",
                "",
                "| 层级 | 页面 | 状态 | 状态数 | 去重控件数 | 到达路径 |",
                "|---:|---|---|---:|---:|---|",
            ]
        )
        for page in pages:
            path = path_label(json_load(page.get("arrival_path_json"), []))
            md.append(
                f"| {page['depth']} | {_md(page.get('title') or page['canonical_url'])}<br>{_md(page['canonical_url'])} | "
                f"{_status_cn(page['status'])} | {page['state_count']} | {page['control_count']} | {_md(path)} |"
            )

        grouped_edges: dict[int, dict[tuple, dict]] = defaultdict(dict)
        for edge in edges:
            depth = int(edge["source_depth"])
            key = (edge["source_url"], edge["target_url"], edge["status"], edge.get("target_depth"))
            if key not in grouped_edges[depth]:
                grouped_edges[depth][key] = dict(edge, occurrences=1)
            else:
                grouped_edges[depth][key]["occurrences"] += 1
        md.extend(["", "## 链接去重与下一批次", ""])
        for depth in sorted(grouped_edges):
            md.extend(
                [
                    f"### 第 {depth} 层发现的链接",
                    "",
                    "| 来源页 | 目标 | 处理结果 | 出现次数 | 下一层 | 发现历程 |",
                    "|---|---|---|---:|---:|---|",
                ]
            )
            for edge in grouped_edges[depth].values():
                next_depth = edge.get("target_depth")
                if next_depth is None and same_host(run["target"], edge["target_url"]):
                    next_depth = int(edge["source_depth"]) + 1
                md.append(
                    f"| {_md(edge['source_url'])} | {_md(edge['target_url'])} | {_status_cn(edge['status'])} | "
                    f"{edge['occurrences']} | {next_depth if next_depth is not None else '-'} | "
                    f"{_md(path_label(json_load(edge.get('path_json'), [])))} |"
                )
            md.append("")

        states_by_page: dict[int, list[dict]] = defaultdict(list)
        for state in states:
            states_by_page[int(state["page_id"])].append(state)
        md.extend(["## 页面状态图", ""])
        for page in pages:
            md.append(f"### 第 {page['depth']} 层 · {page.get('title') or page['canonical_url']}")
            md.append("")
            md.append(f"- 页面：{page['canonical_url']}")
            md.append(f"- 到达历程：{path_label(json_load(page.get('arrival_path_json'), []))}")
            md.append("")
            md.append("| 状态 ID | 指纹 | 页面内动作路径 |")
            md.append("|---:|---|---|")
            for state in states_by_page.get(int(page["id"]), []):
                action_path = json_load(state.get("action_path_json"), [])
                md.append(f"| {state['id']} | `{state['fingerprint'][:12]}` | {_md(path_label(action_path))} |")
            md.append("")

        actions_by_page: dict[int, list[dict]] = defaultdict(list)
        for action in actions:
            if action["effective_status"] not in {"STATE_REPLAY_GAP", "NOT_ACTIONABLE"}:
                actions_by_page[int(action["page_id"])].append(action)
        md.extend(["## 控件执行明细", ""])
        for page in pages:
            page_actions = actions_by_page.get(int(page["id"]), [])
            if not page_actions:
                continue
            md.append(f"### 第 {page['depth']} 层 · {page.get('title') or page['canonical_url']}")
            md.append("")
            md.append("| 控件 | 结果 | 观察 | 完整点击历程 |")
            md.append("|---|---|---|---|")
            for action in page_actions:
                md.append(
                    f"| {_md(action.get('display_label'))} | {_status_cn(action['effective_status'])} | {_md(action['display_outcome'])} | "
                    f"{_md(_action_path_text(action))} |"
                )
            md.append("")

        md.extend(["## 覆盖缺口与待确认项", ""])
        gaps = [
            action
            for action in actions
            if action["effective_status"] in {"UNCONFIRMED", "SUSPECTED", "NEEDS_SPEC", "BLOCKED", "MANUAL_REVIEW", "STATE_REPLAY_GAP", "NOT_ACTIONABLE", "NEEDS_RETEST"}
        ]
        if not gaps and not any(page["status"] in {"LOAD_FAIL", "BLOCKED"} for page in pages):
            md.append("- 当前已处理范围内没有未确认项。")
        else:
            grouped_gaps: dict[str, list[dict]] = defaultdict(list)
            for action in gaps:
                grouped_gaps[action["effective_status"]].append(action)
            for status, items in grouped_gaps.items():
                md.append(f"### {_status_cn(status)} · {len(items)} 项")
                md.append("")
                unique_examples: list[dict] = []
                seen_labels: set[str] = set()
                for action in items:
                    if action["display_label"] in seen_labels:
                        continue
                    seen_labels.add(action["display_label"])
                    unique_examples.append(action)
                for action in unique_examples[:10]:
                    md.append(
                        f"- {action['display_label']}：{action['display_outcome']}；路径："
                        f"{_action_path_text(action)}"
                    )
                if len(unique_examples) > 10:
                    md.append(f"- 其余 {len(unique_examples) - 10} 个不同控件已保留在 SQLite 审计数据中。")
                md.append("")
            for page in pages:
                if page["status"] in {"LOAD_FAIL", "BLOCKED"}:
                    md.append(f"- [{_status_cn(page['status'])}] {page['canonical_url']}：{page.get('load_error') or '未提供原因'}")
        md.extend(["", "## 缺陷独立复现", ""])
        if not bugs:
            md.append("- 当前没有触发独立复现器的疑似脚本或内容语义异常。")
        else:
            reproductions_by_bug: dict[int, list[dict]] = defaultdict(list)
            for item in reproductions:
                reproductions_by_bug[int(item["bug_id"])].append(item)
            for bug in bugs:
                attempts = reproductions_by_bug.get(int(bug["id"]), [])
                replayed = sum(int(item["reproduced"]) for item in attempts)
                evidence = json_load(bug.get("first_evidence_json"), {})
                md.append(
                    f"### BUG-{bug['id']:04d} [{bug['status']}] {bug['title']}"
                )
                md.append("")
                md.append(f"- 严重度：{bug.get('severity') or 'unknown'}")
                if evidence.get("classification"):
                    md.append(f"- 异常分类：{evidence['classification']}")
                if evidence.get("preconditions"):
                    md.append(f"- 前置条件：{_md(evidence['preconditions'])}")
                md.append(f"- 最小路径：{path_label(json_load(bug.get('shortest_path_json'), []))}")
                if evidence.get("reproduction_steps"):
                    md.append(f"- 复现步骤：{' → '.join(str(step) for step in evidence['reproduction_steps'])}")
                if evidence.get("expected"):
                    md.append(f"- 预期：{_md(evidence['expected'])}")
                if evidence.get("actual"):
                    md.append(f"- 实际：{_md(evidence['actual'])}")
                if evidence.get("impact"):
                    md.append(f"- 影响：{_md(evidence['impact'])}")
                if evidence.get("confidence"):
                    md.append(f"- 可信度：{_md(evidence['confidence'])}")
                md.append(f"- 独立复现：{replayed}/{len(attempts)}；结论：{bug.get('triage') or '待复现'}")
                md.append("")
        md.extend(["", "## 业务流程线索", ""])
        md.append("以下点击历程用于后续语义 Planner 推断业务流程；它们是证据，不是业务真值。")
        md.append("")
        for page in pages:
            md.append(f"- 第 {page['depth']} 层 `{page['canonical_url']}`：{path_label(json_load(page.get('arrival_path_json'), []))}")

        if artifacts:
            md.extend(["", "## 本地证据", ""])
            for artifact in artifacts:
                link, label = self._artifact_reference(artifact)
                prefix = f"- {artifact['kind']}（页面 {artifact.get('page_id') or '-'}）"
                if link:
                    md.append(f"{prefix}：[{label}]({link})")
                else:
                    md.append(f"{prefix}：`{label}`（文件缺失或不属于当前运行目录）")
        warnings = [message for message in messages if message["level"] in {"warning", "error"}]
        if warnings:
            md.extend(["", "## 运行消息", ""])
            for message in warnings:
                md.append(f"- [{message['level']}] {message['message']}")
        md.append("")
        return self.redactor.redact_text("\n".join(md))

    def write(self) -> tuple[Path, Path]:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        md_name = self.config["output"].get("report_markdown") or "WebQA测试报告.md"
        html_name = self.config["output"].get("report_html") or "WebQA测试报告.html"
        md_path = self.output_dir / md_name
        html_path = self.output_dir / html_name
        markdown = self.build_markdown()
        md_path.write_text(markdown, encoding="utf-8")
        html_path.write_text(markdown_to_html(markdown, "WebQA 状态图测试报告"), encoding="utf-8")
        summary = self.summary()
        rounds = self.storage.rows(
            "SELECT * FROM run_rounds WHERE run_id=? ORDER BY round_no", (self.run_id,)
        )
        manifest_path = self.output_dir / "run-manifest.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "run_id": self.run_id,
                    "target": self.redactor.redact_url(summary["run"]["target"]),
                    "started_at": summary["run"]["created_at"],
                    "updated_at": summary["run"]["updated_at"],
                    "completed_at": summary["run"].get("completed_at"),
                    "status": summary["run"]["status"],
                    "max_depth": summary["run"]["max_depth"],
                    "rounds": rounds,
                    "summary": {key: value for key, value in summary.items() if key != "run"},
                    "reports": {"markdown": str(md_path.resolve()), "html": str(html_path.resolve())},
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        self._write_archive_index()
        return md_path, html_path

    def _write_archive_index(self) -> tuple[Path, Path]:
        archive_root = Path(self.config["output"]["directory"])
        archive_root.mkdir(parents=True, exist_ok=True)
        runs = self.storage.rows("SELECT * FROM runs ORDER BY created_at DESC")
        md_name = self.config["output"].get("report_markdown") or "WebQA测试报告.md"
        html_name = self.config["output"].get("report_html") or "WebQA测试报告.html"
        lines = [
            "# WebQA 运行归档索引",
            "",
            f"- 固定归档目录：`{(archive_root / 'runs').resolve()}`",
            "- 同一实例断点续跑或提高深度时沿用原目录和历史结果；只有新实例才创建新的时间目录。",
            "",
            "| 开始时间 | 最后更新 | 运行 ID | 目标 | 状态 | 深度 | 报告 |",
            "|---|---|---|---|---|---:|---|",
        ]
        for run in runs:
            relative = Path("runs") / run["id"]
            lines.append(
                f"| {run['created_at']} | {run['updated_at']} | `{run['id']}` | {_md(run['target'])} | "
                f"{_status_cn(run['status'])} | {run['max_depth']} | "
                f"[Markdown]({(relative / md_name).as_posix()}) / [HTML]({(relative / html_name).as_posix()}) / "
                f"[Manifest]({(relative / 'run-manifest.json').as_posix()}) |"
            )
        markdown = self.redactor.redact_text("\n".join(lines) + "\n")
        md_path = archive_root / "运行归档索引.md"
        html_path = archive_root / "运行归档索引.html"
        md_path.write_text(markdown, encoding="utf-8")
        html_path.write_text(markdown_to_html(markdown, "WebQA 运行归档索引"), encoding="utf-8")
        return md_path, html_path
