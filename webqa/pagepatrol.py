from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from .utils import sha


def _slug(url: str, page_id: int) -> str:
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", url).strip("-")[:50] or "home"
    return f"{page_id:04d}-{base}-{sha(url, 8)}"


def run_pagepatrol(config: dict, output_dir: Path, url: str, page_id: int) -> dict:
    settings = config["pagepatrol"]
    if not settings.get("enabled", True):
        return {"status": "DISABLED"}
    executable = Path(settings["executable"])
    if not executable.exists():
        return {"status": "UNAVAILABLE", "error": f"找不到 PagePatrol：{executable}"}
    report_dir = output_dir / "pagepatrol" / _slug(url, page_id)
    report_dir.mkdir(parents=True, exist_ok=True)
    report_json = report_dir / "report.json"
    report_html = report_dir / "report.html"
    if report_json.exists():
        return {
            "status": "DONE",
            "report_json": str(report_json),
            "report_html": str(report_html) if report_html.exists() else None,
            "reused": True,
        }
    cmd = [
        str(executable),
        "check",
        url,
        "--viewport",
        str(settings.get("viewport") or "desktop"),
        "--out",
        str(report_dir),
    ]
    if settings.get("open_report"):
        cmd.append("--open")
    try:
        completed = subprocess.run(
            cmd,
            cwd=str(output_dir.parent),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=int(settings.get("timeout_seconds") or 180),
        )
    except subprocess.TimeoutExpired:
        return {"status": "UNCONFIRMED", "error": "PagePatrol 执行超时", "command": subprocess.list2cmdline(cmd)}
    except Exception as exc:
        return {"status": "UNCONFIRMED", "error": f"{type(exc).__name__}: {exc}", "command": subprocess.list2cmdline(cmd)}
    result = {
        "status": "DONE" if report_json.exists() else "UNCONFIRMED",
        "exit_code": completed.returncode,
        "report_json": str(report_json) if report_json.exists() else None,
        "report_html": str(report_html) if report_html.exists() else None,
        "stdout_tail": completed.stdout[-2000:],
        "stderr_tail": completed.stderr[-1200:],
        "command": subprocess.list2cmdline(cmd),
    }
    if report_json.exists():
        try:
            raw = json.loads(report_json.read_text(encoding="utf-8"))
            findings = raw.get("findings") or []
            result["finding_counts"] = {
                level: sum(1 for item in findings if str(item.get("severity", "")).lower() == level)
                for level in ("error", "warning", "info")
            }
        except Exception as exc:
            result["parse_error"] = str(exc)
    return result
