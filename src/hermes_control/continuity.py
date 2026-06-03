"""Read-only Hermes continuity inventory helpers.

This module intentionally returns metadata and counts only. It must not return raw
Hermes memory file contents or transcript content; import/sync flows can build on
this once privacy and provenance rules are explicit.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any


_MEMORY_FILENAMES = ("MEMORY.md", "USER.md")


def default_hermes_home() -> Path:
    """Resolve the Hermes home directory without inspecting private content."""
    env_home = os.environ.get("HERMES_HOME")
    if env_home:
        return Path(env_home).expanduser()
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "hermes"
    return Path.home() / "AppData" / "Local" / "hermes"


def _safe_stat(path: Path) -> dict[str, Any]:
    try:
        stat = path.stat()
    except OSError:
        return {"exists": False, "size_bytes": 0}
    return {"exists": path.exists(), "size_bytes": stat.st_size}


def _relative(path: Path, base: Path) -> str:
    try:
        return path.relative_to(base).as_posix()
    except ValueError:
        return path.as_posix()


def _file_entry(path: Path, base: Path) -> dict[str, Any]:
    stat = _safe_stat(path)
    return {
        "name": path.name,
        "relative_path": _relative(path, base),
        "exists": stat["exists"],
        "size_bytes": stat["size_bytes"],
    }


def _query_count_map(cur: sqlite3.Cursor, table: str, column: str) -> dict[str, int]:
    try:
        rows = cur.execute(
            f"SELECT {column}, COUNT(*) FROM {table} GROUP BY {column} ORDER BY {column}"
        ).fetchall()
    except sqlite3.Error:
        return {}
    return {str(key): int(count) for key, count in rows if key is not None}


def _query_single_int(cur: sqlite3.Cursor, sql: str) -> int:
    try:
        row = cur.execute(sql).fetchone()
    except sqlite3.Error:
        return 0
    return int(row[0] or 0) if row else 0


def _state_db_inventory(state_db: Path, base: Path) -> dict[str, Any]:
    stat = _safe_stat(state_db)
    result: dict[str, Any] = {
        "relative_path": _relative(state_db, base),
        "exists": stat["exists"],
        "size_bytes": stat["size_bytes"],
        "session_count": 0,
        "message_count": 0,
        "source_counts": {},
        "role_counts": {},
        "latest_session_started_at": None,
        "errors": [],
    }
    if not state_db.exists():
        return result

    try:
        con = sqlite3.connect(f"file:{state_db.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        result["errors"].append(f"open_failed: {exc.__class__.__name__}")
        return result

    try:
        cur = con.cursor()
        result["session_count"] = _query_single_int(cur, "SELECT COUNT(*) FROM sessions")
        result["message_count"] = _query_single_int(cur, "SELECT COUNT(*) FROM messages")
        result["source_counts"] = _query_count_map(cur, "sessions", "source")
        result["role_counts"] = _query_count_map(cur, "messages", "role")
        try:
            row = cur.execute("SELECT MAX(started_at) FROM sessions").fetchone()
            result["latest_session_started_at"] = row[0] if row else None
        except sqlite3.Error:
            result["latest_session_started_at"] = None
    finally:
        con.close()
    return result


def build_continuity_inventory(hermes_home: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Build a read-only inventory of Hermes continuity sources.

    The return value is intentionally JSON-serializable and contains no raw
    memory/transcript content.
    """
    base = Path(hermes_home).expanduser() if hermes_home is not None else default_hermes_home()
    exists = base.exists()
    memory_dir = base / "memories"
    profile_dir = base / "user-profile"
    skills_dir = base / "skills"
    profiles_dir = base / "profiles"

    memory_files = [_file_entry(memory_dir / name, base) for name in _MEMORY_FILENAMES]
    profile_markdown = sorted(profile_dir.rglob("*.md")) if profile_dir.exists() else []
    skill_files = sorted(skills_dir.rglob("SKILL.md")) if skills_dir.exists() else []
    profile_names = sorted(path.name for path in profiles_dir.iterdir() if path.is_dir()) if profiles_dir.exists() else []

    privacy_warnings = [
        "Inventory is read-only and returns counts/metadata only; memory and transcript contents are not returned.",
        "Raw Hermes sessions should stay inactive until an explicit import/retrieval policy is chosen.",
    ]
    if not exists:
        privacy_warnings.append("Hermes home was not found at the resolved path.")
    if profile_names:
        privacy_warnings.append("Additional Hermes profiles were detected; import/sync should preserve profile privacy boundaries.")

    return {
        "hermes_home": str(base),
        "exists": exists,
        "content_returned": False,
        "state_db": _state_db_inventory(base / "state.db", base),
        "memory_files": memory_files,
        "profile_markdown_count": len(profile_markdown),
        "profile_markdown_files": [_file_entry(path, base) for path in profile_markdown],
        "skill_count": len(skill_files),
        "profile_names": profile_names,
        "privacy_warnings": privacy_warnings,
        "recommended_next_step": "Create a provenance-preserving continuity export/import manifest before activating any imported memories.",
    }
