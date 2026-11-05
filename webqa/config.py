from __future__ import annotations

import copy
from pathlib import Path
from urllib.parse import urlsplit

import yaml


DEFAULTS: dict = {
    "version": 1,
    "target": "https://staging.evomap.work",
    "scope": {
        "same_host": True,
        "max_page_depth": 3,
        "desktop_only": True,
        "viewport": {"width": 1440, "height": 900},
    },
    "accounts": {"mode": "guest", "storage_state": None},
    "test_data": {"prefix": "webqa-run-"},
    "prohibited": ["payment", "delete", "publish", "oauth"],
    "exclude": ["ai_answer_quality"],
    "language": "zh-CN",
    "traversal": {
        "strategy": "layered",
        "parallel_pages": 3,
        "max_pages": 500,
        "max_states_per_page": 40,
        "max_controls_per_state": 400,
        "max_actions_per_page": 1200,
        "action_timeout_ms": 8000,
        "navigation_timeout_ms": 30000,
        "settle_ms": 900,
        "retry_limit": 2,
        "new_state_controls": "front",
        "duplicate_links": "record_only",
        "child_page_check": "record_only",
        "history_scope": "run",
    },
    "pagepatrol": {
        "enabled": True,
        "executable": r"D:\KimiData\kimi\Workspaces\测试开发\pagepatrol-venv\Scripts\pagepatrol.exe",
        "viewport": "desktop",
        "open_report": True,
        "timeout_seconds": 180,
    },
    "output": {
        "directory": "webqa-output",
        "database": "webqa.sqlite3",
        "report_markdown": "WebQA测试报告.md",
        "report_html": "WebQA测试报告.html",
    },
}


def _merge(base: dict, override: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(path: str | Path) -> tuple[dict, Path]:
    config_path = Path(path).resolve()
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    config = _merge(DEFAULTS, raw)
    validate_config(config)
    output = config["output"]
    output_dir = Path(output["directory"])
    if not output_dir.is_absolute():
        output_dir = config_path.parent / output_dir
    output_dir = output_dir.resolve()
    output["directory"] = str(output_dir)
    database = Path(output["database"])
    if not database.is_absolute():
        database = output_dir / database
    output["database"] = str(database.resolve())
    return config, config_path


def validate_config(config: dict) -> None:
    target = str(config.get("target") or "")
    parts = urlsplit(target)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError(f"target 必须是完整的 http(s) URL：{target!r}")
    scope = config["scope"]
    depth = int(scope["max_page_depth"])
    if not 0 <= depth <= 20:
        raise ValueError("scope.max_page_depth 必须在 0 到 20 之间")
    traversal = config["traversal"]
    workers = int(traversal["parallel_pages"])
    if not 1 <= workers <= 8:
        raise ValueError("traversal.parallel_pages 必须在 1 到 8 之间")
    if traversal["strategy"] != "layered":
        raise ValueError("当前版本仅支持 layered 分层遍历")
    if traversal["child_page_check"] != "record_only":
        raise ValueError("当前版本要求 child_page_check=record_only")


def apply_overrides(
    config: dict,
    *,
    target: str | None = None,
    max_depth: int | None = None,
    parallel_pages: int | None = None,
) -> None:
    if target is not None:
        config["target"] = target
    if max_depth is not None:
        config["scope"]["max_page_depth"] = max_depth
    if parallel_pages is not None:
        config["traversal"]["parallel_pages"] = parallel_pages
    validate_config(config)
