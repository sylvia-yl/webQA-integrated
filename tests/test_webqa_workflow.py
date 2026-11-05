from __future__ import annotations

import copy
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from webqa.config import DEFAULTS
from webqa.progress import ProgressTracker
from webqa.reporter import Reporter
from webqa.runner import LayeredRunner
from webqa.storage import Storage


HOME = b"""<!doctype html><html lang='en' class='dark'><head><title>Home</title>
<style>#menu{display:none}.hidden{display:none}</style></head><body>
<h1>Home</h1>
<button id='toggle' aria-expanded='false' onclick="const m=document.querySelector('#menu');const open=m.style.display==='block';m.style.display=open?'none':'block';this.setAttribute('aria-expanded',String(!open))">Resources</button>
<button id='theme' aria-label='Switch to light theme' onclick="const light=document.documentElement.classList.toggle('light');document.documentElement.classList.toggle('dark',!light);this.setAttribute('aria-label',light?'Switch to dark theme':'Switch to light theme')"><svg data-lucide='sun'></svg></button>
<button id='boom' onclick="throw new Error('stable test error')">Broken action</button>
<nav id='menu'><a href='/child'>Menu child</a></nav>
<a href='/child'>Child</a><a href='/child'>Child</a>
<a class='hidden' href='/hidden'>Invisible child</a>
</body></html>"""

CHILD = b"""<!doctype html><html lang='en'><head><title>Child</title></head><body>
<h1>Child loaded</h1><button onclick="document.body.insertAdjacentHTML('beforeend','<p>new content</p>')">Details</button>
<a href='/grandchild'>Grandchild</a>
</body></html>"""

GRANDCHILD = b"""<!doctype html><html lang='en'><head><title>Grandchild</title></head><body>
<h1>Grandchild loaded</h1>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/child"):
            body = CHILD
        elif self.path.startswith("/grandchild"):
            body = GRANDCHILD
        else:
            body = HOME
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


class WorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_layered_state_queue_and_link_dedupe(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = copy.deepcopy(DEFAULTS)
            config["target"] = f"http://127.0.0.1:{self.server.server_port}/"
            config["scope"]["max_page_depth"] = 0
            config["traversal"].update(
                {
                    "parallel_pages": 2,
                    "max_states_per_page": 8,
                    "max_controls_per_state": 50,
                    "max_actions_per_page": 30,
                    "settle_ms": 80,
                    "retry_limit": 1,
                }
            )
            config["pagepatrol"]["enabled"] = False
            config["output"]["directory"] = str(root / "output")
            config["output"]["database"] = str(root / "output" / "webqa.sqlite3")
            storage = Storage(config["output"]["database"])
            run_id, _ = storage.create_or_resume_run(config, new_run=True)
            self.assertRegex(run_id, r"^\d{8}-\d{6}-[0-9a-f]{6}$")
            run_dir = root / "output" / "runs" / run_id
            result = LayeredRunner(
                storage, run_id, config, run_dir, pagepatrol_enabled=False
            ).run(one_layer=True)

            self.assertEqual([0], [layer["depth"] for layer in result["layers"]])
            pages = storage.rows("SELECT * FROM page_nodes WHERE run_id=? ORDER BY depth", (run_id,))
            self.assertEqual(1, len(pages))
            self.assertEqual("DONE", pages[0]["status"])

            labels = [
                row["label"]
                for row in storage.rows("SELECT label FROM controls WHERE run_id=?", (run_id,))
            ]
            self.assertNotIn("Invisible child", labels)
            self.assertIn("Menu child", labels)
            self.assertGreaterEqual(
                storage.scalar("SELECT COUNT(*) FROM state_nodes WHERE run_id=?", (run_id,)), 2
            )
            self.assertEqual(
                1, storage.scalar("SELECT COUNT(*) FROM link_visits WHERE run_id=?", (run_id,))
            )
            statuses = {
                row["status"]
                for row in storage.rows("SELECT status FROM actions WHERE run_id=?", (run_id,))
            }
            self.assertIn("STATE_DISCOVERED", statuses)
            self.assertIn("TECH_PASS", statuses)
            self.assertIn("LINK_RECORDED", statuses)
            theme_actions = storage.rows(
                "SELECT status,outcome FROM actions WHERE run_id=? AND label LIKE 'Switch to % theme%' AND status!='OBSERVED' ORDER BY id",
                (run_id,),
            )
            self.assertEqual(2, len(theme_actions))
            self.assertEqual("TECH_PASS", theme_actions[-1]["status"])
            self.assertIn("主题外观发生变化", theme_actions[-1]["outcome"])
            bugs = storage.rows("SELECT * FROM bugs WHERE run_id=?", (run_id,))
            self.assertEqual(1, len(bugs))
            self.assertEqual("CONFIRMED", bugs[0]["status"])
            self.assertEqual(
                2,
                storage.scalar(
                    "SELECT COUNT(*) FROM reproductions WHERE bug_id=? AND reproduced=1",
                    (bugs[0]["id"],),
                ),
            )

            root_attempts = int(pages[0]["attempts"])
            first_round = storage.latest_round(run_id)
            self.assertEqual("NEW", first_round["mode"])
            self.assertEqual("COMPLETED", first_round["status"])

            # Increase the depth on the same target. The completed depth-0 run is
            # reused, its root is not rerun, and the recorded child becomes a
            # standard depth-1 page job.
            config["scope"]["max_page_depth"] = 1
            resumed_id, created = storage.create_or_resume_run(config, new_run=False)
            self.assertEqual(run_id, resumed_id)
            self.assertFalse(created)
            second_round = storage.latest_round(run_id)
            self.assertEqual("EXPAND", second_round["mode"])
            self.assertEqual(0, second_round["from_depth"])
            self.assertEqual(1, second_round["to_depth"])
            self.assertEqual(1, second_round["activated_pages"])
            pages = storage.rows("SELECT * FROM page_nodes WHERE run_id=? ORDER BY depth", (run_id,))
            self.assertEqual(2, len(pages))
            self.assertEqual("DONE", pages[0]["status"])
            self.assertEqual("PENDING", pages[1]["status"])
            self.assertIn("/child", pages[1]["canonical_url"])

            resumed = LayeredRunner(
                storage, run_id, config, run_dir, pagepatrol_enabled=False
            ).run()
            self.assertEqual([1], [layer["depth"] for layer in resumed["layers"]])
            pages = storage.rows("SELECT * FROM page_nodes WHERE run_id=? ORDER BY depth", (run_id,))
            self.assertEqual(root_attempts, int(pages[0]["attempts"]))
            self.assertEqual("DONE", pages[1]["status"])
            self.assertEqual("Child", pages[1]["title"])
            self.assertEqual(
                0,
                storage.scalar(
                    "SELECT COUNT(*) FROM page_nodes WHERE run_id=? AND canonical_url LIKE '%/grandchild'",
                    (run_id,),
                ),
            )
            self.assertEqual(2, storage.scalar("SELECT COUNT(*) FROM run_rounds WHERE run_id=?", (run_id,)))

            md_path, html_path = Reporter(storage, run_id, config, run_dir).write()
            self.assertTrue(md_path.exists())
            self.assertTrue(html_path.exists())
            self.assertTrue((run_dir / "run-manifest.json").exists())
            self.assertTrue((root / "output" / "运行归档索引.md").exists())
            tracker = ProgressTracker(storage, run_id, run_dir, interval=1)
            snapshot = tracker.snapshot()
            tracker.write(snapshot)
            self.assertEqual(run_id, snapshot["run_id"])
            self.assertTrue((run_dir / "live-status.json").exists())
            self.assertTrue((run_dir / "live-status.md").exists())
            report = md_path.read_text(encoding="utf-8")
            self.assertIn("提高深度接续", report)
            self.assertIn("Menu child", report)
            self.assertIn("Grandchild", report)
            self.assertIn("等待更深轮次解锁：1", report)
            storage.close_thread_connection()

    def test_running_items_are_recovered_for_resume(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = copy.deepcopy(DEFAULTS)
            config["target"] = f"http://127.0.0.1:{self.server.server_port}/"
            config["output"]["database"] = str(root / "resume.sqlite3")
            storage = Storage(config["output"]["database"])
            run_id, _ = storage.create_or_resume_run(config, new_run=True)
            page = storage.pending_pages(run_id, 0)[0]
            self.assertTrue(storage.claim_page(page["id"]))
            state_id, created = storage.add_state(
                run_id,
                page["id"],
                {
                    "fingerprint": "state-a",
                    "url": page["url"],
                    "title": "Home",
                    "visible_text_hash": "text-a",
                },
                [],
                None,
                10,
            )
            self.assertTrue(created)
            storage.add_controls(
                run_id,
                page["id"],
                state_id,
                [
                    {
                        "control_key": "control-a",
                        "semantic_key": "semantic-a",
                        "behavior_key": "behavior-a",
                        "role": "button",
                        "tag": "button",
                        "label": "Open",
                        "href": "",
                        "section": "main",
                        "selector": "#open",
                    }
                ],
                0,
                [{"label": "初始页面"}],
            )
            frontier = storage.claim_frontier(page["id"], 2)
            self.assertIsNotNone(frontier)
            reserved, _ = storage.reserve_link(
                run_id,
                f"http://127.0.0.1:{self.server.server_port}/child",
                page["id"],
                state_id,
                frontier["control_id"],
                frontier["full_path"],
            )
            self.assertTrue(reserved)

            storage.pause_run(run_id)
            self.assertEqual("PAUSED", storage.run(run_id)["status"])
            self.assertEqual("RETRY", storage.page(page["id"])["status"])
            self.assertEqual(
                "RETRY",
                storage.rows("SELECT status FROM frontier WHERE id=?", (frontier["id"],))[0]["status"],
            )
            self.assertEqual(
                "RETRY",
                storage.rows("SELECT status FROM link_visits WHERE run_id=?", (run_id,))[0]["status"],
            )
            storage.close_thread_connection()


if __name__ == "__main__":
    unittest.main()
