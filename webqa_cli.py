from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path

from webqa.config import apply_overrides, load_config
from webqa.progress import ProgressTracker
from webqa.reporter import Reporter
from webqa.storage import Storage


ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "webqa.yaml"


def ensure_playwright_runtime(config: dict) -> None:
    if importlib.util.find_spec("playwright") is not None:
        return
    executable = Path(config["pagepatrol"]["executable"])
    runtime = executable.with_name("python.exe")
    if not runtime.exists():
        raise RuntimeError(
            "当前 Python 环境没有 Playwright，且未找到 PagePatrol 虚拟环境的 python.exe。"
        )
    current = Path(sys.executable).resolve()
    if current == runtime.resolve():
        raise RuntimeError(f"{runtime} 中没有可用的 Playwright")
    os.execv(str(runtime), [str(runtime), str(Path(__file__).resolve()), *sys.argv[1:]])


def resolve_run(storage: Storage, config: dict, run_id: str | None) -> dict:
    if run_id:
        return storage.run(run_id)
    run = storage.latest_run()
    if not run:
        raise RuntimeError("还没有可用的运行记录，请先执行 run")
    return run


def run_output_dir(config: dict, run_id: str) -> Path:
    return Path(config["output"]["directory"]) / "runs" / run_id


def command_run(args) -> int:
    config, _ = load_config(args.config)
    apply_overrides(
        config,
        target=args.target,
        max_depth=args.max_depth,
        parallel_pages=args.parallel_pages,
    )
    if args.no_open:
        config["pagepatrol"]["open_report"] = False
    if args.no_pagepatrol:
        config["pagepatrol"]["enabled"] = False
    ensure_playwright_runtime(config)
    storage = Storage(config["output"]["database"])
    run_id, created = storage.create_or_resume_run(config, new_run=args.new_run)
    output_dir = run_output_dir(config, run_id)
    output_dir.mkdir(parents=True, exist_ok=True)
    print(f"[运行] {'新建' if created else '续跑'} {run_id}", flush=True)
    round_info = storage.latest_round(run_id) or {}
    round_modes = {"NEW": "新建实例", "RESUME": "断点续跑", "EXPAND": "提高深度接续"}
    print(
        f"[轮次] 第 {round_info.get('round_no', '-')} 轮 · "
        f"{round_modes.get(round_info.get('mode'), round_info.get('mode', '-'))} · "
        f"深度 {round_info.get('from_depth', '-')} → {round_info.get('to_depth', '-')} · "
        f"从历史结果提升 {round_info.get('activated_pages', 0)} 个页面",
        flush=True,
    )
    print(f"[数据库] {storage.path}", flush=True)
    from webqa.runner import LayeredRunner

    tracker = ProgressTracker(storage, run_id, output_dir, interval=args.progress_interval)
    tracker.start()
    try:
        result = LayeredRunner(
            storage,
            run_id,
            config,
            output_dir,
            headed=args.headed,
            pagepatrol_enabled=not args.no_pagepatrol,
        ).run(one_layer=args.one_layer)
    except KeyboardInterrupt:
        storage.pause_run(run_id)
        Reporter(storage, run_id, config, output_dir).write()
        raise
    finally:
        tracker.stop()
    md_path, html_path = Reporter(storage, run_id, config, output_dir).write()
    print(f"[报告] {md_path}", flush=True)
    print(f"[报告] {html_path}", flush=True)
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    return 0


def command_status(args) -> int:
    config, _ = load_config(args.config)
    storage = Storage(config["output"]["database"])
    run = resolve_run(storage, config, args.run_id)
    output_dir = run_output_dir(config, run["id"])
    if args.watch:
        ProgressTracker(storage, run["id"], output_dir, interval=args.interval).watch()
    else:
        tracker = ProgressTracker(storage, run["id"], output_dir, interval=args.interval)
        snapshot = tracker.snapshot()
        tracker.write(snapshot)
        print(json.dumps(snapshot, ensure_ascii=False, indent=2))
    return 0


def command_report(args) -> int:
    config, _ = load_config(args.config)
    storage = Storage(config["output"]["database"])
    run = resolve_run(storage, config, args.run_id)
    output_dir = run_output_dir(config, run["id"])
    md_path, html_path = Reporter(storage, run["id"], config, output_dir).write()
    print(md_path)
    print(html_path)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="WebQA 状态图驱动的分层网站测试 Agent")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="Brief YAML 路径")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="新建、断点续跑或提高深度接续测试")
    run.add_argument("--target", help="临时覆盖 Brief 中的目标 URL")
    run.add_argument("--new-run", action="store_true", help="开启新的独立运行；链接去重历史随新运行重置")
    run.add_argument("--one-layer", action="store_true", help="只处理当前最浅的一层，完成后停在可继续状态")
    run.add_argument("--max-depth", type=int, help="解锁到指定层；续跑时可递增但不可降低")
    run.add_argument("--parallel-pages", type=int, help="临时覆盖同层并行页面数（1-8）")
    run.add_argument("--headed", action="store_true", help="显示 Playwright 浏览器窗口")
    run.add_argument("--no-pagepatrol", action="store_true", help="本次不执行 PagePatrol")
    run.add_argument("--no-open", action="store_true", help="PagePatrol 完成后不自动打开子报告")
    run.add_argument("--progress-interval", type=float, default=5.0, help="运行进度输出间隔秒数")
    run.set_defaults(func=command_run)

    status = subparsers.add_parser("status", help="查看当前运行和覆盖队列")
    status.add_argument("--run-id")
    status.add_argument("--watch", action="store_true", help="持续跟踪直到运行完成")
    status.add_argument("--interval", type=float, default=5.0, help="跟踪刷新间隔秒数")
    status.set_defaults(func=command_status)

    report = subparsers.add_parser("report", help="从 SQLite 重新生成中文报告")
    report.add_argument("--run-id")
    report.set_defaults(func=command_report)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("\n[中断] 当前事务已经保存；再次运行相同命令会从控件级队列继续。", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"[错误] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
