from __future__ import annotations

import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator
from urllib.parse import urlsplit

from .utils import canonical_url, json_load, json_text, now_iso


SCHEMA = r"""
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    target TEXT NOT NULL,
    host TEXT NOT NULL,
    status TEXT NOT NULL,
    current_depth INTEGER NOT NULL DEFAULT 0,
    max_depth INTEGER NOT NULL,
    config_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS run_rounds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(id),
    round_no INTEGER NOT NULL,
    mode TEXT NOT NULL,
    from_depth INTEGER NOT NULL,
    to_depth INTEGER NOT NULL,
    status TEXT NOT NULL,
    activated_pages INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    UNIQUE(run_id, round_no)
);
CREATE INDEX IF NOT EXISTS idx_run_rounds_run ON run_rounds(run_id, round_no);

CREATE TABLE IF NOT EXISTS page_nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(id),
    url TEXT NOT NULL,
    canonical_url TEXT NOT NULL,
    depth INTEGER NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    title TEXT,
    http_status INTEGER,
    final_url TEXT,
    content_summary TEXT,
    load_error TEXT,
    discovered_from_page_id INTEGER,
    discovered_by_control_id INTEGER,
    arrival_path_json TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(run_id, canonical_url)
);
CREATE INDEX IF NOT EXISTS idx_pages_run_depth_status
    ON page_nodes(run_id, depth, status);

CREATE TABLE IF NOT EXISTS state_nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(id),
    page_id INTEGER NOT NULL REFERENCES page_nodes(id),
    fingerprint TEXT NOT NULL,
    status TEXT NOT NULL,
    url TEXT NOT NULL,
    title TEXT,
    visible_text_hash TEXT,
    snapshot_json TEXT NOT NULL,
    action_path_json TEXT NOT NULL,
    discovered_by_action_id INTEGER,
    created_at TEXT NOT NULL,
    UNIQUE(page_id, fingerprint)
);
CREATE INDEX IF NOT EXISTS idx_states_page ON state_nodes(page_id, id);

CREATE TABLE IF NOT EXISTS controls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(id),
    page_id INTEGER NOT NULL REFERENCES page_nodes(id),
    state_id INTEGER NOT NULL REFERENCES state_nodes(id),
    control_key TEXT NOT NULL,
    semantic_key TEXT NOT NULL,
    behavior_key TEXT NOT NULL,
    role TEXT,
    tag TEXT,
    label TEXT,
    href TEXT,
    section TEXT,
    selector TEXT,
    descriptor_json TEXT NOT NULL,
    discovered_at TEXT NOT NULL,
    UNIQUE(state_id, control_key)
);
CREATE INDEX IF NOT EXISTS idx_controls_page_behavior
    ON controls(page_id, behavior_key);

CREATE TABLE IF NOT EXISTS frontier (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(id),
    page_id INTEGER NOT NULL REFERENCES page_nodes(id),
    state_id INTEGER NOT NULL REFERENCES state_nodes(id),
    control_id INTEGER NOT NULL REFERENCES controls(id),
    behavior_key TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    lease_started_at TEXT,
    path_json TEXT NOT NULL,
    result_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(page_id, behavior_key)
);
CREATE INDEX IF NOT EXISTS idx_frontier_claim
    ON frontier(page_id, status, priority DESC, id);

CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(id),
    page_id INTEGER NOT NULL REFERENCES page_nodes(id),
    state_id INTEGER REFERENCES state_nodes(id),
    control_id INTEGER REFERENCES controls(id),
    frontier_id INTEGER REFERENCES frontier(id),
    status TEXT NOT NULL,
    action_type TEXT NOT NULL,
    label TEXT,
    before_fingerprint TEXT,
    after_fingerprint TEXT,
    outcome TEXT NOT NULL,
    full_path_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_actions_page ON actions(page_id, id);

CREATE TABLE IF NOT EXISTS link_visits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(id),
    target_url TEXT NOT NULL,
    source_page_id INTEGER NOT NULL REFERENCES page_nodes(id),
    source_state_id INTEGER REFERENCES state_nodes(id),
    source_control_id INTEGER REFERENCES controls(id),
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 1,
    final_url TEXT,
    http_status INTEGER,
    title TEXT,
    body_chars INTEGER,
    error TEXT,
    path_json TEXT NOT NULL,
    first_clicked_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(run_id, target_url)
);

CREATE TABLE IF NOT EXISTS page_edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(id),
    source_page_id INTEGER NOT NULL REFERENCES page_nodes(id),
    source_state_id INTEGER REFERENCES state_nodes(id),
    source_control_id INTEGER REFERENCES controls(id),
    target_url TEXT NOT NULL,
    target_page_id INTEGER REFERENCES page_nodes(id),
    status TEXT NOT NULL,
    duplicate_visit_id INTEGER REFERENCES link_visits(id),
    path_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_edges_run_target ON page_edges(run_id, target_url);

CREATE TABLE IF NOT EXISTS test_cases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(id),
    page_id INTEGER NOT NULL REFERENCES page_nodes(id),
    state_id INTEGER REFERENCES state_nodes(id),
    control_id INTEGER REFERENCES controls(id),
    source TEXT NOT NULL,
    case_type TEXT NOT NULL,
    input_json TEXT NOT NULL,
    expected TEXT NOT NULL,
    status TEXT NOT NULL,
    result_json TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(id),
    page_id INTEGER REFERENCES page_nodes(id),
    action_id INTEGER REFERENCES actions(id),
    kind TEXT NOT NULL,
    path TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bugs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(id),
    page_id INTEGER NOT NULL REFERENCES page_nodes(id),
    action_id INTEGER REFERENCES actions(id),
    fingerprint TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'unknown',
    triage TEXT,
    shortest_path_json TEXT NOT NULL,
    first_evidence_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(run_id, fingerprint)
);

CREATE TABLE IF NOT EXISTS reproductions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bug_id INTEGER NOT NULL REFERENCES bugs(id),
    attempt INTEGER NOT NULL,
    reproduced INTEGER NOT NULL,
    status TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    trace_path TEXT,
    screenshot_path TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(bug_id, attempt)
);

CREATE TABLE IF NOT EXISTS business_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    host TEXT NOT NULL,
    rule_key TEXT NOT NULL,
    rule_text TEXT NOT NULL,
    status TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(host, rule_key)
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(id),
    level TEXT NOT NULL,
    category TEXT NOT NULL,
    message TEXT NOT NULL,
    data_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


class Storage:
    def __init__(self, path: str | Path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        conn = self.connection()
        conn.executescript(SCHEMA)
        conn.execute("INSERT OR REPLACE INTO schema_meta(key,value) VALUES('version','2')")
        conn.commit()

    def connection(self) -> sqlite3.Connection:
        conn = getattr(self._local, "connection", None)
        if conn is None:
            conn = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
            self._local.connection = conn
        return conn

    def close_thread_connection(self) -> None:
        conn = getattr(self._local, "connection", None)
        if conn is not None:
            try:
                conn.close()
            finally:
                self._local.connection = None

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self.connection()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def create_or_resume_run(self, config: dict, *, new_run: bool = False) -> tuple[str, bool]:
        target = canonical_url(config["target"], config["target"])
        host = urlsplit(target).hostname or ""
        requested_depth = int(config["scope"]["max_page_depth"])
        now = now_iso()
        with self.transaction() as conn:
            row = None
            if not new_run:
                latest = conn.execute(
                    "SELECT * FROM runs WHERE target=? ORDER BY created_at DESC LIMIT 1",
                    (target,),
                ).fetchone()
                if latest:
                    previous_depth = int(latest["max_depth"])
                    if requested_depth < previous_depth:
                        raise ValueError(
                            f"续跑不能把深度从 {previous_depth} 降到 {requested_depth}；"
                            "如需较浅范围，请新建实例"
                        )
                    if latest["status"] in {"READY", "RUNNING", "PAUSED", "NEEDS_INPUT"}:
                        row = latest
                    elif latest["status"] == "COMPLETED" and requested_depth > previous_depth:
                        row = latest
                    elif latest["status"] == "COMPLETED":
                        raise ValueError(
                            f"该实例已完成至第 {previous_depth} 层；"
                            "请提高层数接续，或选择新建实例执行回归"
                        )
            if row:
                run_id = row["id"]
                previous_depth = int(row["max_depth"])
                conn.execute(
                    """UPDATE runs SET max_depth=?, config_json=?, status='READY',
                       completed_at=NULL, updated_at=? WHERE id=?""",
                    (requested_depth, json_text(config), now, run_id),
                )
                created = False
                mode = "EXPAND" if requested_depth > previous_depth else "RESUME"
            else:
                # The directory name is also the human-auditable archive key. Keep the
                # exact local start time in it; the suffix prevents same-second clashes.
                started = datetime.fromisoformat(now)
                run_id = f"{started.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
                conn.execute(
                    """INSERT INTO runs(id,target,host,status,current_depth,max_depth,config_json,created_at,updated_at)
                       VALUES(?,?,?,'READY',0,?,?,?,?)""",
                    (run_id, target, host, requested_depth, json_text(config), now, now),
                )
                created = True
                previous_depth = 0
                mode = "NEW"
            conn.execute(
                "UPDATE run_rounds SET status='INTERRUPTED', finished_at=? WHERE run_id=? AND status='RUNNING'",
                (now, run_id),
            )
            round_no = int(
                conn.execute(
                    "SELECT COALESCE(MAX(round_no),0)+1 FROM run_rounds WHERE run_id=?", (run_id,)
                ).fetchone()[0]
            )
            conn.execute(
                """INSERT INTO run_rounds(run_id,round_no,mode,from_depth,to_depth,status,started_at)
                   VALUES(?,?,?,?,?,'RUNNING',?)""",
                (run_id, round_no, mode, previous_depth, requested_depth, now),
            )
        if created:
            self.add_page(run_id, target, 0, [{"label": "初始页面", "url": target}], None, None)
        self.recover_interrupted(run_id)
        self.promote_discovered_pages(run_id, requested_depth)
        return run_id, created

    def recover_interrupted(self, run_id: str) -> None:
        now = now_iso()
        with self.transaction() as conn:
            conn.execute(
                "UPDATE page_nodes SET status='RETRY', updated_at=? WHERE run_id=? AND status='RUNNING'",
                (now, run_id),
            )
            conn.execute(
                "UPDATE frontier SET status='RETRY', updated_at=? WHERE run_id=? AND status='RUNNING'",
                (now, run_id),
            )
            conn.execute(
                "UPDATE link_visits SET status='RETRY', updated_at=? WHERE run_id=? AND status='RUNNING'",
                (now, run_id),
            )

    def pause_run(self, run_id: str) -> None:
        """Pause without turning browser shutdown side effects into product failures."""
        now = now_iso()
        with self.transaction() as conn:
            conn.execute(
                """UPDATE page_nodes SET status='RETRY', updated_at=?
                   WHERE run_id=? AND (
                       status='RUNNING' OR
                       (status='LOAD_FAIL' AND (
                           load_error LIKE '%TargetClosedError%' OR
                           load_error LIKE '%connection closed%'
                       ))
                   )""",
                (now, run_id),
            )
            conn.execute(
                """UPDATE frontier SET status='RETRY',
                   attempts=CASE WHEN attempts>0 THEN attempts-1 ELSE 0 END,
                   updated_at=? WHERE run_id=? AND (
                       status='RUNNING' OR result_json LIKE '%Connection closed%' OR
                       result_json LIKE '%connection closed%'
                   )""",
                (now, run_id),
            )
            conn.execute(
                """UPDATE link_visits SET status='RETRY',
                   attempts=CASE WHEN attempts>0 THEN attempts-1 ELSE 0 END,
                   updated_at=? WHERE run_id=? AND status='RUNNING'""",
                (now, run_id),
            )
            conn.execute(
                "UPDATE runs SET status='PAUSED', updated_at=? WHERE id=?",
                (now, run_id),
            )
            conn.execute(
                """UPDATE run_rounds SET status='PAUSED', finished_at=?
                   WHERE id=(SELECT id FROM run_rounds WHERE run_id=? AND status='RUNNING'
                            ORDER BY round_no DESC LIMIT 1)""",
                (now, run_id),
            )

    def run(self, run_id: str) -> dict:
        row = self.connection().execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            raise KeyError(f"找不到运行：{run_id}")
        return dict(row)

    def latest_run(self, target: str | None = None) -> dict | None:
        if target:
            row = self.connection().execute(
                "SELECT * FROM runs WHERE target=? ORDER BY created_at DESC LIMIT 1", (target,)
            ).fetchone()
        else:
            row = self.connection().execute("SELECT * FROM runs ORDER BY created_at DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def latest_round(self, run_id: str) -> dict | None:
        row = self.connection().execute(
            "SELECT * FROM run_rounds WHERE run_id=? ORDER BY round_no DESC LIMIT 1", (run_id,)
        ).fetchone()
        return dict(row) if row else None

    def set_run_status(self, run_id: str, status: str, current_depth: int | None = None) -> None:
        now = now_iso()
        completed = now if status == "COMPLETED" else None
        with self.transaction() as conn:
            if current_depth is None:
                conn.execute(
                    "UPDATE runs SET status=?, updated_at=?, completed_at=COALESCE(?,completed_at) WHERE id=?",
                    (status, now, completed, run_id),
                )
            else:
                conn.execute(
                    """UPDATE runs SET status=?, current_depth=?, updated_at=?,
                       completed_at=COALESCE(?,completed_at) WHERE id=?""",
                    (status, current_depth, now, completed, run_id),
                )
            if status in {"COMPLETED", "READY", "FAILED"}:
                round_statuses = "('RUNNING','READY')" if status == "COMPLETED" else "('RUNNING')"
                conn.execute(
                    f"""UPDATE run_rounds SET status=?, finished_at=?
                        WHERE id=(SELECT id FROM run_rounds WHERE run_id=? AND status IN {round_statuses}
                                  ORDER BY round_no DESC LIMIT 1)""",
                    (status, now, run_id),
                )

    def add_page(
        self,
        run_id: str,
        url: str,
        depth: int,
        arrival_path: list,
        discovered_from_page_id: int | None,
        discovered_by_control_id: int | None,
    ) -> tuple[int, bool]:
        run = self.run(run_id)
        canon = canonical_url(run["target"], url)
        now = now_iso()
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM page_nodes WHERE run_id=? AND canonical_url=?", (run_id, canon)
            ).fetchone()
            if row:
                if depth < row["depth"]:
                    conn.execute(
                        """UPDATE page_nodes SET depth=?, arrival_path_json=?, discovered_from_page_id=?,
                           discovered_by_control_id=?, updated_at=? WHERE id=?""",
                        (
                            depth,
                            json_text(arrival_path),
                            discovered_from_page_id,
                            discovered_by_control_id,
                            now,
                            row["id"],
                        ),
                    )
                return int(row["id"]), False
            cur = conn.execute(
                """INSERT INTO page_nodes(run_id,url,canonical_url,depth,status,discovered_from_page_id,
                   discovered_by_control_id,arrival_path_json,created_at,updated_at)
                   VALUES(?,?,?,?, 'PENDING', ?,?,?,?,?)""",
                (
                    run_id,
                    url,
                    canon,
                    depth,
                    discovered_from_page_id,
                    discovered_by_control_id,
                    json_text(arrival_path),
                    now,
                    now,
                ),
            )
            return int(cur.lastrowid), True

    def promote_discovered_pages(self, run_id: str, max_depth: int) -> list[dict]:
        """Turn previously recorded same-host links into deduplicated page jobs.

        The shallowest recorded source wins. This is idempotent, so it is safe to
        call before every layer and again after an interrupted expansion.
        """
        max_depth = int(max_depth)
        now = now_iso()
        promoted: list[dict] = []
        with self.transaction() as conn:
            run = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if not run:
                raise KeyError(f"找不到运行：{run_id}")
            config = json_load(run["config_json"], {})
            max_pages = int((config.get("traversal") or {}).get("max_pages", 500))
            rows = conn.execute(
                """SELECT e.*,p.depth AS source_depth
                   FROM page_edges e JOIN page_nodes p ON p.id=e.source_page_id
                   WHERE e.run_id=? AND p.depth<? ORDER BY p.depth,e.id""",
                (run_id, max_depth),
            ).fetchall()
            candidates: dict[str, dict] = {}
            for raw in rows:
                item = dict(raw)
                target = canonical_url(run["target"], item["target_url"])
                if (urlsplit(target).hostname or "").lower() != str(run["host"]).lower():
                    continue
                target_depth = int(item["source_depth"]) + 1
                if target_depth > max_depth:
                    continue
                path = json_load(item.get("path_json"), [])
                score = (target_depth, len(path), int(item["id"]))
                current = candidates.get(target)
                if current is None:
                    candidates[target] = {
                        "target": target,
                        "target_depth": target_depth,
                        "path": path,
                        "source_page_id": int(item["source_page_id"]),
                        "source_control_id": item.get("source_control_id"),
                        "score": score,
                        "edge_ids": [int(item["id"])],
                    }
                else:
                    current["edge_ids"].append(int(item["id"]))
                    if score < current["score"]:
                        current.update(
                            target_depth=target_depth,
                            path=path,
                            source_page_id=int(item["source_page_id"]),
                            source_control_id=item.get("source_control_id"),
                            score=score,
                        )

            page_count = int(
                conn.execute("SELECT COUNT(*) FROM page_nodes WHERE run_id=?", (run_id,)).fetchone()[0]
            )
            for item in sorted(candidates.values(), key=lambda value: value["score"]):
                existing = conn.execute(
                    "SELECT * FROM page_nodes WHERE run_id=? AND canonical_url=?",
                    (run_id, item["target"]),
                ).fetchone()
                if existing:
                    page_id = int(existing["id"])
                    if int(item["target_depth"]) < int(existing["depth"]):
                        conn.execute(
                            """UPDATE page_nodes SET depth=?,arrival_path_json=?,discovered_from_page_id=?,
                               discovered_by_control_id=?,updated_at=? WHERE id=?""",
                            (
                                item["target_depth"],
                                json_text(item["path"]),
                                item["source_page_id"],
                                item["source_control_id"],
                                now,
                                page_id,
                            ),
                        )
                elif page_count < max_pages:
                    cur = conn.execute(
                        """INSERT INTO page_nodes(run_id,url,canonical_url,depth,status,discovered_from_page_id,
                           discovered_by_control_id,arrival_path_json,created_at,updated_at)
                           VALUES(?,?,?,?, 'PENDING', ?,?,?,?,?)""",
                        (
                            run_id,
                            item["target"],
                            item["target"],
                            item["target_depth"],
                            item["source_page_id"],
                            item["source_control_id"],
                            json_text(item["path"]),
                            now,
                            now,
                        ),
                    )
                    page_id = int(cur.lastrowid)
                    page_count += 1
                    promoted.append(
                        {
                            "page_id": page_id,
                            "url": item["target"],
                            "depth": item["target_depth"],
                        }
                    )
                else:
                    continue
                placeholders = ",".join("?" for _ in item["edge_ids"])
                conn.execute(
                    f"UPDATE page_edges SET target_page_id=? WHERE id IN ({placeholders})",
                    (page_id, *item["edge_ids"]),
                )
                conn.execute(
                    """UPDATE link_visits SET status='PROMOTED',updated_at=?
                       WHERE run_id=? AND target_url=? AND status IN ('RECORDED','RETRY')""",
                    (now, run_id, item["target"]),
                )
            if promoted:
                conn.execute(
                    """UPDATE run_rounds SET activated_pages=activated_pages+?
                       WHERE id=(SELECT id FROM run_rounds WHERE run_id=? AND status='RUNNING'
                                ORDER BY round_no DESC LIMIT 1)""",
                    (len(promoted), run_id),
                )
                conn.execute(
                    """INSERT INTO messages(run_id,level,category,message,data_json,created_at)
                       VALUES(?, 'info', 'depth_expansion', ?, ?, ?)""",
                    (
                        run_id,
                        f"深度已解锁到第 {max_depth} 层；从上一轮链接总账提升 {len(promoted)} 个标准页面",
                        json_text({"max_depth": max_depth, "pages": promoted}),
                        now,
                    ),
                )
        return promoted

    def pending_depths(self, run_id: str, max_depth: int) -> list[int]:
        rows = self.connection().execute(
            """SELECT DISTINCT depth FROM page_nodes
               WHERE run_id=? AND depth<=? AND status IN ('PENDING','RETRY') ORDER BY depth""",
            (run_id, max_depth),
        ).fetchall()
        return [int(row[0]) for row in rows]

    def pending_pages(self, run_id: str, depth: int) -> list[dict]:
        rows = self.connection().execute(
            """SELECT * FROM page_nodes WHERE run_id=? AND depth=?
               AND status IN ('PENDING','RETRY') ORDER BY id""",
            (run_id, depth),
        ).fetchall()
        return [dict(row) for row in rows]

    def claim_page(self, page_id: int) -> bool:
        with self.transaction() as conn:
            cur = conn.execute(
                """UPDATE page_nodes SET status='RUNNING', attempts=attempts+1, started_at=?, updated_at=?
                   WHERE id=? AND status IN ('PENDING','RETRY')""",
                (now_iso(), now_iso(), page_id),
            )
            return cur.rowcount == 1

    def update_page(self, page_id: int, **values) -> None:
        allowed = {"title", "http_status", "final_url", "content_summary", "load_error"}
        fields = [(key, value) for key, value in values.items() if key in allowed]
        if not fields:
            return
        sql = ",".join(f"{key}=?" for key, _ in fields)
        params = [value for _, value in fields] + [now_iso(), page_id]
        with self.transaction() as conn:
            conn.execute(f"UPDATE page_nodes SET {sql}, updated_at=? WHERE id=?", params)

    def finish_page(self, page_id: int, status: str, error: str | None = None) -> None:
        now = now_iso()
        with self.transaction() as conn:
            conn.execute(
                """UPDATE page_nodes SET status=?, load_error=COALESCE(?,load_error),
                   finished_at=?, updated_at=? WHERE id=?""",
                (status, error, now, now, page_id),
            )

    def page(self, page_id: int) -> dict:
        row = self.connection().execute("SELECT * FROM page_nodes WHERE id=?", (page_id,)).fetchone()
        if not row:
            raise KeyError(page_id)
        return dict(row)

    def state_count(self, page_id: int) -> int:
        return int(
            self.connection().execute("SELECT COUNT(*) FROM state_nodes WHERE page_id=?", (page_id,)).fetchone()[0]
        )

    def add_state(
        self,
        run_id: str,
        page_id: int,
        snapshot: dict,
        action_path: list,
        discovered_by_action_id: int | None,
        max_states: int,
    ) -> tuple[int | None, bool]:
        fingerprint = snapshot["fingerprint"]
        now = now_iso()
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT id FROM state_nodes WHERE page_id=? AND fingerprint=?", (page_id, fingerprint)
            ).fetchone()
            if row:
                return int(row["id"]), False
            count = conn.execute("SELECT COUNT(*) FROM state_nodes WHERE page_id=?", (page_id,)).fetchone()[0]
            if int(count) >= max_states:
                return None, False
            cur = conn.execute(
                """INSERT INTO state_nodes(run_id,page_id,fingerprint,status,url,title,visible_text_hash,
                   snapshot_json,action_path_json,discovered_by_action_id,created_at)
                   VALUES(?,?,?,'DISCOVERED',?,?,?,?,?,?,?)""",
                (
                    run_id,
                    page_id,
                    fingerprint,
                    snapshot.get("url") or "",
                    snapshot.get("title") or "",
                    snapshot.get("visible_text_hash") or "",
                    json_text(snapshot),
                    json_text(action_path),
                    discovered_by_action_id,
                    now,
                ),
            )
            return int(cur.lastrowid), True

    def state(self, state_id: int) -> dict:
        row = self.connection().execute("SELECT * FROM state_nodes WHERE id=?", (state_id,)).fetchone()
        if not row:
            raise KeyError(state_id)
        value = dict(row)
        value["action_path"] = json_load(value.get("action_path_json"), [])
        value["snapshot"] = json_load(value.get("snapshot_json"), {})
        return value

    def add_controls(
        self,
        run_id: str,
        page_id: int,
        state_id: int,
        controls: list[dict],
        base_priority: int,
        full_path: list,
    ) -> tuple[int, int]:
        now = now_iso()
        inserted = 0
        queued = 0
        with self.transaction() as conn:
            for offset, item in enumerate(controls):
                cur = conn.execute(
                    """INSERT OR IGNORE INTO controls(run_id,page_id,state_id,control_key,semantic_key,
                       behavior_key,role,tag,label,href,section,selector,descriptor_json,discovered_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        run_id,
                        page_id,
                        state_id,
                        item["control_key"],
                        item["semantic_key"],
                        item["behavior_key"],
                        item.get("role"),
                        item.get("tag"),
                        item.get("label"),
                        item.get("href"),
                        item.get("section"),
                        item.get("selector"),
                        json_text(item),
                        now,
                    ),
                )
                if cur.rowcount != 1:
                    continue
                inserted += 1
                control_id = int(cur.lastrowid)
                existing = conn.execute(
                    "SELECT id FROM frontier WHERE page_id=? AND behavior_key=?",
                    (page_id, item["behavior_key"]),
                ).fetchone()
                if existing:
                    continue
                priority = base_priority - offset
                path = list(full_path) + [{"label": f"点击「{item.get('label') or '未命名控件'}」"}]
                conn.execute(
                    """INSERT INTO frontier(run_id,page_id,state_id,control_id,behavior_key,priority,status,
                       path_json,created_at,updated_at) VALUES(?,?,?,?,?,?,'PENDING',?,?,?)""",
                    (run_id, page_id, state_id, control_id, item["behavior_key"], priority, json_text(path), now, now),
                )
                queued += 1
        return inserted, queued

    def claim_frontier(self, page_id: int, retry_limit: int) -> dict | None:
        with self.transaction() as conn:
            row = conn.execute(
                """SELECT f.*, c.descriptor_json, c.label, c.href, c.selector,
                          s.action_path_json, s.fingerprint AS state_fingerprint
                   FROM frontier f
                   JOIN controls c ON c.id=f.control_id
                   JOIN state_nodes s ON s.id=f.state_id
                   WHERE f.page_id=? AND f.status IN ('PENDING','RETRY') AND f.attempts<?
                   ORDER BY f.priority DESC, f.id LIMIT 1""",
                (page_id, retry_limit),
            ).fetchone()
            if not row:
                return None
            now = now_iso()
            cur = conn.execute(
                """UPDATE frontier SET status='RUNNING', attempts=attempts+1,
                   lease_started_at=?, updated_at=? WHERE id=? AND status IN ('PENDING','RETRY')""",
                (now, now, row["id"]),
            )
            if cur.rowcount != 1:
                return None
            value = dict(row)
            value["attempts"] = int(value["attempts"]) + 1
            value["descriptor"] = json_load(value.pop("descriptor_json"), {})
            value["action_path"] = json_load(value.pop("action_path_json"), [])
            value["full_path"] = json_load(value.get("path_json"), [])
            return value

    def finish_frontier(self, frontier_id: int, status: str, result: dict) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE frontier SET status=?, result_json=?, updated_at=? WHERE id=?",
                (status, json_text(result), now_iso(), frontier_id),
            )

    def retry_frontier(self, frontier_id: int, result: dict, retry_limit: int) -> str:
        with self.transaction() as conn:
            row = conn.execute("SELECT attempts FROM frontier WHERE id=?", (frontier_id,)).fetchone()
            status = "RETRY" if row and int(row["attempts"]) < retry_limit else "UNCONFIRMED"
            conn.execute(
                "UPDATE frontier SET status=?, result_json=?, updated_at=? WHERE id=?",
                (status, json_text(result), now_iso(), frontier_id),
            )
            return status

    def record_action(
        self,
        run_id: str,
        page_id: int,
        state_id: int | None,
        control_id: int | None,
        frontier_id: int | None,
        status: str,
        action_type: str,
        label: str,
        before_fingerprint: str | None,
        after_fingerprint: str | None,
        outcome: str,
        full_path: list,
        evidence: dict,
    ) -> int:
        with self.transaction() as conn:
            cur = conn.execute(
                """INSERT INTO actions(run_id,page_id,state_id,control_id,frontier_id,status,action_type,
                   label,before_fingerprint,after_fingerprint,outcome,full_path_json,evidence_json,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id,
                    page_id,
                    state_id,
                    control_id,
                    frontier_id,
                    status,
                    action_type,
                    label,
                    before_fingerprint,
                    after_fingerprint,
                    outcome,
                    json_text(full_path),
                    json_text(evidence),
                    now_iso(),
                ),
            )
            return int(cur.lastrowid)

    def reserve_link(
        self,
        run_id: str,
        target_url: str,
        page_id: int,
        state_id: int,
        control_id: int,
        path: list,
    ) -> tuple[bool, dict]:
        now = now_iso()
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM link_visits WHERE run_id=? AND target_url=?", (run_id, target_url)
            ).fetchone()
            if row:
                if row["status"] == "RETRY":
                    conn.execute(
                        "UPDATE link_visits SET status='RUNNING', attempts=attempts+1, updated_at=? WHERE id=?",
                        (now, row["id"]),
                    )
                    value = dict(row)
                    value["status"] = "RUNNING"
                    value["attempts"] = int(value["attempts"]) + 1
                    return True, value
                return False, dict(row)
            cur = conn.execute(
                """INSERT INTO link_visits(run_id,target_url,source_page_id,source_state_id,source_control_id,
                   status,path_json,first_clicked_at,updated_at) VALUES(?,?,?,?,?,'RUNNING',?,?,?)""",
                (run_id, target_url, page_id, state_id, control_id, json_text(path), now, now),
            )
            value = dict(
                id=int(cur.lastrowid),
                run_id=run_id,
                target_url=target_url,
                status="RUNNING",
                attempts=1,
            )
            return True, value

    def finish_link(self, visit_id: int, status: str, **values) -> None:
        allowed = {"final_url", "http_status", "title", "body_chars", "error"}
        fields = [(key, value) for key, value in values.items() if key in allowed]
        sql = ",".join(["status=?"] + [f"{key}=?" for key, _ in fields] + ["updated_at=?"])
        params = [status] + [value for _, value in fields] + [now_iso(), visit_id]
        with self.transaction() as conn:
            conn.execute(f"UPDATE link_visits SET {sql} WHERE id=?", params)

    def add_edge(
        self,
        run_id: str,
        page_id: int,
        state_id: int | None,
        control_id: int | None,
        target_url: str,
        status: str,
        path: list,
        target_page_id: int | None = None,
        duplicate_visit_id: int | None = None,
    ) -> int:
        with self.transaction() as conn:
            cur = conn.execute(
                """INSERT INTO page_edges(run_id,source_page_id,source_state_id,source_control_id,target_url,
                   target_page_id,status,duplicate_visit_id,path_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id,
                    page_id,
                    state_id,
                    control_id,
                    target_url,
                    target_page_id,
                    status,
                    duplicate_visit_id,
                    json_text(path),
                    now_iso(),
                ),
            )
            return int(cur.lastrowid)

    def add_artifact(
        self,
        run_id: str,
        kind: str,
        path: str,
        page_id: int | None = None,
        action_id: int | None = None,
        metadata: dict | None = None,
    ) -> int:
        with self.transaction() as conn:
            cur = conn.execute(
                """INSERT INTO artifacts(run_id,page_id,action_id,kind,path,metadata_json,created_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (run_id, page_id, action_id, kind, path, json_text(metadata or {}), now_iso()),
            )
            return int(cur.lastrowid)

    def add_test_cases(
        self,
        run_id: str,
        page_id: int,
        state_id: int,
        control_id: int,
        cases: list[dict],
    ) -> int:
        now = now_iso()
        with self.transaction() as conn:
            for case in cases:
                conn.execute(
                    """INSERT INTO test_cases(run_id,page_id,state_id,control_id,source,case_type,
                       input_json,expected,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (
                        run_id,
                        page_id,
                        state_id,
                        control_id,
                        "html_constraints",
                        case["case_type"],
                        json_text({"value": case.get("value")}),
                        case["expected"],
                        "PLANNED",
                        now,
                    ),
                )
        return len(cases)

    def create_bug_candidate(
        self,
        run_id: str,
        page_id: int,
        action_id: int,
        fingerprint: str,
        title: str,
        path: list,
        evidence: dict,
    ) -> int:
        now = now_iso()
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT id FROM bugs WHERE run_id=? AND fingerprint=?", (run_id, fingerprint)
            ).fetchone()
            if row:
                return int(row["id"])
            cur = conn.execute(
                """INSERT INTO bugs(run_id,page_id,action_id,fingerprint,title,status,shortest_path_json,
                   first_evidence_json,created_at,updated_at) VALUES(?,?,?,?,?,'SUSPECTED',?,?,?,?)""",
                (run_id, page_id, action_id, fingerprint, title, json_text(path), json_text(evidence), now, now),
            )
            return int(cur.lastrowid)

    def add_reproduction(
        self,
        bug_id: int,
        attempt: int,
        reproduced: bool,
        status: str,
        evidence: dict,
        trace_path: str | None,
        screenshot_path: str | None,
    ) -> int:
        with self.transaction() as conn:
            cur = conn.execute(
                """INSERT OR REPLACE INTO reproductions(bug_id,attempt,reproduced,status,evidence_json,
                   trace_path,screenshot_path,created_at) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    bug_id,
                    attempt,
                    1 if reproduced else 0,
                    status,
                    json_text(evidence),
                    trace_path,
                    screenshot_path,
                    now_iso(),
                ),
            )
            return int(cur.lastrowid)

    def finish_bug(self, bug_id: int, status: str, triage: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE bugs SET status=?, triage=?, updated_at=? WHERE id=?",
                (status, triage, now_iso(), bug_id),
            )

    def block_remaining_frontier(self, page_id: int, reason: str) -> int:
        with self.transaction() as conn:
            cur = conn.execute(
                """UPDATE frontier SET status='BLOCKED', result_json=?, updated_at=?
                   WHERE page_id=? AND status IN ('PENDING','RETRY')""",
                (json_text({"reason": reason}), now_iso(), page_id),
            )
            return int(cur.rowcount)

    def add_message(self, run_id: str, level: str, category: str, message: str, data: dict | None = None) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO messages(run_id,level,category,message,data_json,created_at) VALUES(?,?,?,?,?,?)",
                (run_id, level, category, message, json_text(data or {}), now_iso()),
            )

    def rows(self, sql: str, params: tuple = ()) -> list[dict]:
        return [dict(row) for row in self.connection().execute(sql, params).fetchall()]

    def scalar(self, sql: str, params: tuple = ()) -> int:
        return int(self.connection().execute(sql, params).fetchone()[0])

