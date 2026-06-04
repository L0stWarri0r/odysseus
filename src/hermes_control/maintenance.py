"""Read-only Odysseus maintenance/status helpers.

The status payload is intentionally metadata-only. It reports repository and PWA
freshness state for the local personal fork without returning user content,
secrets, transcripts, or file contents.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Callable, Sequence

from src.hermes_control.continuity import default_hermes_home

Runner = Callable[[Sequence[str], Path], str]

_AHEAD_RE = re.compile(r"ahead (\d+)")
_BEHIND_RE = re.compile(r"behind (\d+)")
_CACHE_NAME_RE = re.compile(r"odysseus-[A-Za-z0-9_.-]+")


def default_odysseus_repo() -> Path:
    """Resolve the local Odysseus checkout path."""
    env_repo = os.environ.get("ODYSSEUS_REPO")
    if env_repo:
        return Path(env_repo).expanduser()
    return Path.home() / "odysseus"


def default_intake_script() -> Path:
    """Resolve the Hermes daily intake script path."""
    return default_hermes_home() / "scripts" / "odysseus_daily_main_update.sh"


def _run_command(args: Sequence[str], cwd: Path) -> str:
    completed = subprocess.run(
        list(args),
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        raise RuntimeError(stderr or f"command failed: {' '.join(args)}")
    return (completed.stdout or "").strip()


def _safe_run(runner: Runner, args: Sequence[str], cwd: Path) -> tuple[str | None, str | None]:
    try:
        return runner(args, cwd).strip(), None
    except Exception as exc:  # noqa: BLE001 - status endpoint should degrade safely
        return None, exc.__class__.__name__


def _extract_count(pattern: re.Pattern[str], text: str | None) -> int:
    if not text:
        return 0
    match = pattern.search(text)
    return int(match.group(1)) if match else 0


def _detect_cache_name(sw_path: Path) -> str | None:
    try:
        text = sw_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    match = _CACHE_NAME_RE.search(text)
    return match.group(0) if match else None


def build_maintenance_status(
    repo_path: str | os.PathLike[str] | None = None,
    intake_script: str | os.PathLike[str] | None = None,
    runner: Runner = _run_command,
) -> dict[str, object]:
    """Return a read-only System/Maintenance status payload.

    No user content is returned. Git commands are read-only and run without a
    shell. File inspection is limited to presence plus the Odysseus service
    worker cache-name marker.
    """
    repo = Path(repo_path).expanduser() if repo_path is not None else default_odysseus_repo()
    script = Path(intake_script).expanduser() if intake_script is not None else default_intake_script()
    static_dir = repo / "static"
    sw_path = static_dir / "sw.js"
    reset_path = static_dir / "sw-reset.html"

    repo_exists = repo.exists()
    repo_info: dict[str, object] = {
        "path": str(repo),
        "exists": repo_exists,
        "branch": None,
        "dirty": None,
        "ahead": 0,
        "behind": 0,
        "head": None,
        "head_subject": None,
        "upstream_main": None,
        "errors": [],
    }

    status = "missing_repo"
    if repo_exists:
        top, err = _safe_run(runner, ("git", "rev-parse", "--show-toplevel"), repo)
        if err:
            repo_info["errors"].append(f"git_root_failed: {err}")
            status = "git_unavailable"
        else:
            status = "ok"
            repo_info["root"] = top
            branch, err = _safe_run(runner, ("git", "branch", "--show-current"), repo)
            if err:
                repo_info["errors"].append(f"branch_failed: {err}")
            else:
                repo_info["branch"] = branch or None

            porcelain, err = _safe_run(runner, ("git", "status", "--porcelain"), repo)
            if err:
                repo_info["errors"].append(f"dirty_check_failed: {err}")
            else:
                repo_info["dirty"] = bool(porcelain)

            short_status, err = _safe_run(runner, ("git", "status", "--short", "--branch"), repo)
            if err:
                repo_info["errors"].append(f"ahead_behind_failed: {err}")
            else:
                repo_info["ahead"] = _extract_count(_AHEAD_RE, short_status)
                repo_info["behind"] = _extract_count(_BEHIND_RE, short_status)
                repo_info["short_status"] = short_status.splitlines()[0] if short_status else ""

            head, err = _safe_run(runner, ("git", "rev-parse", "--short", "HEAD"), repo)
            if err:
                repo_info["errors"].append(f"head_failed: {err}")
            else:
                repo_info["head"] = head

            subject, err = _safe_run(runner, ("git", "log", "-1", "--pretty=%s"), repo)
            if err:
                repo_info["errors"].append(f"head_subject_failed: {err}")
            else:
                repo_info["head_subject"] = subject

            upstream, err = _safe_run(runner, ("git", "rev-parse", "--short", "upstream/main"), repo)
            if err:
                repo_info["errors"].append(f"upstream_main_failed: {err}")
            else:
                repo_info["upstream_main"] = upstream

    pwa = {
        "service_worker_exists": sw_path.exists(),
        "service_worker_path": "static/sw.js",
        "service_worker_cache_name": _detect_cache_name(sw_path),
        "reset_page_exists": reset_path.exists(),
        "reset_url": "/static/sw-reset.html",
        "cache_prefix": "odysseus-",
        "reset_scope": "Odysseus service worker and odysseus-* Cache Storage only",
    }

    automation = {
        "daily_intake_script_exists": script.exists(),
        "daily_intake_schedule": "0 13 * * *",
        "daily_intake_behavior": "fetch upstream/main, skip dirty/wrong branch, merge or cherry-pick compatible updates, never push",
    }

    return {
        "status": status,
        "content_returned": False,
        "repo": repo_info,
        "pwa": pwa,
        "automation": automation,
        "privacy_note": "System/Maintenance status is metadata-only; no chat, memory, transcript, token, or credential contents are returned.",
    }
