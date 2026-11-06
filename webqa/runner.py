from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import threading

from .explorer import PageExplorer
from .reporter import Reporter
from .scenarios import ScenarioRunner
from .storage import Storage


class LayeredRunner:
    def __init__(
        self,
        storage: Storage,
        run_id: str,
        config: dict,
        output_dir: Path,
        *,
        headed: bool = False,
        pagepatrol_enabled: bool = True,
    ):
        self.storage = storage
        self.run_id = run_id
        self.config = config
        self.output_dir = output_dir
        self.headed = headed
        self.pagepatrol_enabled = pagepatrol_enabled
        self.stop_event = threading.Event()

    def _worker(self, page_id: int) -> dict:
        explorer = PageExplorer(
            self.storage,
            self.run_id,
            self.config,
            self.output_dir,
            headed=self.headed,
            pagepatrol_enabled=self.pagepatrol_enabled,
            stop_event=self.stop_event,
        )
        try:
            return explorer.process_page(page_id)
        finally:
            self.storage.close_thread_connection()

    def run(self, *, one_layer: bool = False) -> dict:
        max_depth = int(self.config["scope"]["max_page_depth"])
        workers = int(self.config["traversal"]["parallel_pages"])
        self.storage.set_run_status(self.run_id, "RUNNING")
        processed_layers: list[dict] = []
        while True:
            self.storage.promote_discovered_pages(self.run_id, max_depth)
            depths = self.storage.pending_depths(self.run_id, max_depth)
            if not depths:
                break
            depth = depths[0]
            pages = self.storage.pending_pages(self.run_id, depth)
            if not pages:
                break
            self.storage.set_run_status(self.run_id, "RUNNING", current_depth=depth)
            self.storage.add_message(
                self.run_id,
                "info",
                "batch",
                f"开始第 {depth} 层批次：{len(pages)} 个去重页面，最多 {min(workers, len(pages))} 路并行",
            )
            results: list[dict] = []
            pool = ThreadPoolExecutor(max_workers=min(workers, len(pages)), thread_name_prefix=f"webqa-d{depth}")
            futures = {pool.submit(self._worker, int(page["id"])): page for page in pages}
            try:
                for future in as_completed(futures):
                    page = futures[future]
                    try:
                        result = future.result()
                    except Exception as exc:
                        result = {"page_id": page["id"], "status": "LOAD_FAIL", "error": str(exc)}
                        self.storage.finish_page(int(page["id"]), "LOAD_FAIL", str(exc))
                    results.append(result)
                    print(f"[第 {depth} 层] {page['url']} -> {result['status']}", flush=True)
            except KeyboardInterrupt:
                self.stop_event.set()
                for future in futures:
                    future.cancel()
                raise
            finally:
                pool.shutdown(wait=True, cancel_futures=True)
            processed_layers.append({"depth": depth, "pages": len(pages), "results": results})
            # Materialize newly discovered links before deciding whether a
            # one-layer invocation is complete. Otherwise an eligible next
            # layer could remain only in the link ledger and be mistaken for
            # a fully completed run.
            self.storage.promote_discovered_pages(self.run_id, max_depth)
            Reporter(self.storage, self.run_id, self.config, self.output_dir).write()
            if one_layer:
                self.storage.set_run_status(self.run_id, "READY", current_depth=depth)
                break

        remaining = self.storage.pending_depths(self.run_id, max_depth)
        business_flows: list[dict] = []
        if not remaining:
            business_flows = ScenarioRunner(
                self.storage,
                self.run_id,
                self.config,
                self.output_dir,
            ).run_all()
            self.storage.set_run_status(self.run_id, "COMPLETED", current_depth=max_depth)
        Reporter(self.storage, self.run_id, self.config, self.output_dir).write()
        return {
            "run_id": self.run_id,
            "profile": self.config.get("testing", {}).get("profile", "full"),
            "layers": processed_layers,
            "business_flows": business_flows,
            "remaining_depths": remaining,
        }
