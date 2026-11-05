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
        "LINK_RECORDED": "子页面链接已记录（未进入）",
        "LINK_RECORDED_AFTER_CLICK": "点击后识别子页面（未继续测试）",
        "DUPLICATE_LINK": "重复链接（未再点击）",
        "DUPLICATE_AFTER_CLICK": "点击后识别为重复链接",
        "BOUNDARY": "深度边界（未点击）",
        "BOUNDARY_AFTER_CLICK": "点击后识别为深度边界",
        "OUT_OF_SCOPE": "范围外",
        "SKIPPED_RISK": "风险跳过",
        "FORM_PLANNED": "已生成表单用例",
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
            md.append("- 当前没有触发独立复现器的疑似页面脚本异常。")
        else:
            reproductions_by_bug: dict[int, list[dict]] = defaultdict(list)
            for item in reproductions:
                reproductions_by_bug[int(item["bug_id"])].append(item)
            for bug in bugs:
                attempts = reproductions_by_bug.get(int(bug["id"]), [])
                replayed = sum(int(item["reproduced"]) for item in attempts)
                md.append(
                    f"- BUG-{bug['id']:04d} [{bug['status']}] {bug['title']}；独立复现 {replayed}/{len(attempts)}；"
                    f"结论：{bug.get('triage') or '待复现'}；路径：{path_label(json_load(bug.get('shortest_path_json'), []))}"
                )
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
