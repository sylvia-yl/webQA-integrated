from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import Counter
from pathlib import Path

from .storage import Storage
from .utils import canonical_url, json_load, now_iso, path_label, same_host


TERMINAL_RUN_STATES = {"COMPLETED", "FAILED", "PAUSED", "READY"}


def _counts(rows: list[dict]) -> dict[str, int]:
    return {str(row["status"]): int(row["n"]) for row in rows}


class ProgressTracker:
    def __init__(self, storage: Storage, run_id: str, output_dir: Path, interval: float = 5.0):
        self.storage = storage
        self.run_id = run_id
        self.output_dir = output_dir
        self.interval = max(1.0, float(interval))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_digest = ""

    def snapshot(self) -> dict:
        run = self.storage.run(self.run_id)
        page_rows = self.storage.rows(
            "SELECT depth,status,COUNT(*) n FROM page_nodes WHERE run_id=? GROUP BY depth,status ORDER BY depth,status",
            (self.run_id,),
        )
        frontier_rows = self.storage.rows(
            "SELECT status,COUNT(*) n FROM frontier WHERE run_id=? GROUP BY status", (self.run_id,)
        )
        action_rows = self.storage.rows(
            "SELECT status,COUNT(*) n FROM actions WHERE run_id=? AND status!='OBSERVED' GROUP BY status",
            (self.run_id,),
        )
        latest_actions = self.storage.rows(
            """SELECT a.created_at,a.status,a.label,a.outcome,a.full_path_json,p.depth,p.url
               FROM actions a JOIN page_nodes p ON p.id=a.page_id
               WHERE a.run_id=? AND a.status!='OBSERVED' ORDER BY a.id DESC LIMIT 5""",
            (self.run_id,),
        )
        latest_messages = self.storage.rows(
            "SELECT created_at,level,category,message FROM messages WHERE run_id=? ORDER BY id DESC LIMIT 5",
            (self.run_id,),
        )
        running_pages = self.storage.rows(
            "SELECT id,depth,url,title FROM page_nodes WHERE run_id=? AND status='RUNNING' ORDER BY depth,id",
            (self.run_id,),
        )
        edge_rows = self.storage.rows(
            "SELECT target_url,target_page_id FROM page_edges WHERE run_id=?", (self.run_id,)
        )
        deferred_targets = {
            canonical_url(run["target"], row["target_url"])
            for row in edge_rows
            if row.get("target_page_id") is None and same_host(run["target"], row["target_url"])
        }
        pages_by_depth: dict[str, dict[str, int]] = {}
        for row in page_rows:
            pages_by_depth.setdefault(str(row["depth"]), {})[row["status"]] = int(row["n"])
        return {
            "generated_at": now_iso(),
            "run_id": self.run_id,
            "target": run["target"],
            "run_status": run["status"],
            "started_at": run["created_at"],
            "updated_at": run["updated_at"],
            "current_depth": int(run["current_depth"]),
            "max_depth": int(run["max_depth"]),
            "current_round": self.storage.latest_round(self.run_id),
            "pages_by_depth": pages_by_depth,
            "running_pages": running_pages,
            "state_count": self.storage.scalar("SELECT COUNT(*) FROM state_nodes WHERE run_id=?", (self.run_id,)),
            "control_occurrences": self.storage.scalar("SELECT COUNT(*) FROM controls WHERE run_id=?", (self.run_id,)),
            "frontier": _counts(frontier_rows),
            "actions": _counts(action_rows),
            "link_visits": self.storage.scalar("SELECT COUNT(*) FROM link_visits WHERE run_id=?", (self.run_id,)),
            "deferred_pages": len(deferred_targets),
            "confirmed_bugs": self.storage.scalar(
                "SELECT COUNT(*) FROM bugs WHERE run_id=? AND status='CONFIRMED'", (self.run_id,)
            ),
            "latest_actions": [
                {
                    **{key: value for key, value in row.items() if key != "full_path_json"},
                    "path": path_label(json_load(row.get("full_path_json"), [])),
                }
                for row in latest_actions
            ],
            "latest_messages": latest_messages,
        }

    @staticmethod
    def one_line(snapshot: dict) -> str:
        page_totals = Counter()
        for statuses in snapshot["pages_by_depth"].values():
            page_totals.update(statuses)
        frontier = snapshot["frontier"]
        finished_controls = sum(
            count for status, count in frontier.items() if status not in {"PENDING", "RUNNING", "RETRY"}
        )
        pending_controls = sum(frontier.get(key, 0) for key in ("PENDING", "RUNNING", "RETRY"))
        return (
            f"[{snapshot['generated_at'][11:19]}] {snapshot['run_status']} | "
            f"轮次{(snapshot.get('current_round') or {}).get('round_no', '-')} | "
            f"层 {snapshot['current_depth']}/{snapshot['max_depth']} | "
            f"页面 完成{page_totals.get('DONE', 0)} 运行{page_totals.get('RUNNING', 0)} "
            f"待处理{page_totals.get('PENDING', 0) + page_totals.get('RETRY', 0)} | "
            f"状态{snapshot['state_count']} | 控件 完成{finished_controls} 待处理{pending_controls} | "
            f"链接{snapshot['link_visits']} 待解锁{snapshot['deferred_pages']} | 确认缺陷{snapshot['confirmed_bugs']}"
        )

    @staticmethod
    def markdown(snapshot: dict) -> str:
        lines = [
            "# WebQA 实时状态",
            "",
            f"- 运行 ID：`{snapshot['run_id']}`",
            f"- 目标：{snapshot['target']}",
            f"- 状态：{snapshot['run_status']}",
            f"- 开始时间：{snapshot['started_at']}",
            f"- 状态时间：{snapshot['generated_at']}",
            f"- 当前层：{snapshot['current_depth']} / {snapshot['max_depth']}",
            f"- 当前轮次：{(snapshot.get('current_round') or {}).get('round_no', '-')}；"
            f"类型：{(snapshot.get('current_round') or {}).get('mode', '-')}",
            f"- 页面状态数：{snapshot['state_count']}；控件出现次数：{snapshot['control_occurrences']}",
            f"- 已记录链接：{snapshot['link_visits']}；等待更深轮次解锁页面：{snapshot['deferred_pages']}；"
            f"确认缺陷：{snapshot['confirmed_bugs']}",
            "",
            "## 分层页面",
            "",
            "| 层级 | 状态分布 |",
            "|---:|---|",
        ]
        for depth, statuses in snapshot["pages_by_depth"].items():
            lines.append(f"| {depth} | {'；'.join(f'{key} {value}' for key, value in statuses.items())} |")
        lines.extend(["", "## 控件队列", ""])
        lines.append("- " + ("；".join(f"{key} {value}" for key, value in snapshot["frontier"].items()) or "尚未建立"))
        lines.extend(["", "## 最新动作", ""])
        if snapshot["latest_actions"]:
            for action in snapshot["latest_actions"]:
                lines.append(
                    f"- [{action['created_at']}] 第 {action['depth']} 层 · {action['label']} · "
                    f"{action['status']}：{action['outcome']}；路径：{action['path']}"
                )
        else:
            lines.append("- 尚无动作。")
        lines.extend(["", "## 最新运行消息", ""])
        for message in snapshot["latest_messages"]:
            lines.append(f"- [{message['created_at']}] [{message['category']}] {message['message']}")
        return "\n".join(lines) + "\n"

    def write(self, snapshot: dict | None = None) -> tuple[Path, Path]:
        snapshot = snapshot or self.snapshot()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        json_path = self.output_dir / "live-status.json"
        md_path = self.output_dir / "live-status.md"
        json_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
        md_path.write_text(self.markdown(snapshot), encoding="utf-8")
        return json_path, md_path

    def emit(self, *, force: bool = False) -> dict:
        snapshot = self.snapshot()
        digest_source = json.dumps(
            {
                "status": snapshot["run_status"],
                "depth": snapshot["current_depth"],
                "round": snapshot.get("current_round"),
                "pages": snapshot["pages_by_depth"],
                "states": snapshot["state_count"],
                "frontier": snapshot["frontier"],
                "links": snapshot["link_visits"],
                "deferred": snapshot["deferred_pages"],
                "latest": snapshot["latest_actions"][:1],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        digest = hashlib.sha256(digest_source.encode("utf-8")).hexdigest()
        self.write(snapshot)
        if force or digest != self._last_digest:
            print(self.one_line(snapshot), flush=True)
            if snapshot["latest_actions"]:
                action = snapshot["latest_actions"][0]
                print(
                    f"  [最新动作] 第 {action['depth']} 层 · {action['label']} · {action['status']}：{action['outcome']}",
                    flush=True,
                )
            elif snapshot["latest_messages"]:
                print(f"  [当前步骤] {snapshot['latest_messages'][0]['message']}", flush=True)
            self._last_digest = digest
        return snapshot

    def _loop(self) -> None:
        try:
            while not self._stop.wait(self.interval):
                self.emit()
        finally:
            self.storage.close_thread_connection()

    def start(self) -> None:
        self.emit(force=True)
        self._thread = threading.Thread(target=self._loop, name="webqa-progress", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=self.interval + 2)
        self.emit(force=True)

    def watch(self) -> None:
        try:
            while True:
                snapshot = self.emit(force=True)
                if snapshot["run_status"] in TERMINAL_RUN_STATES:
                    break
                time.sleep(self.interval)
        finally:
            self.storage.close_thread_connection()
