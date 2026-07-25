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
_MAX_PROFILE_MARKDOWN_FILES = 200
_MAX_SKILL_FILES = 200


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


def _resolved_or_none(path: Path) -> Path | None:
    try:
        return path.resolve(strict=False)
    except OSError:
        return None


def _is_within_base(path: Path, base: Path) -> bool:
    resolved_path = _resolved_or_none(path)
    resolved_base = _resolved_or_none(base)
    if resolved_path is None or resolved_base is None:
        return False
    try:
        resolved_path.relative_to(resolved_base)
        return True
    except ValueError:
        return False


def _relative(path: Path, base: Path) -> str | None:
    try:
        return path.relative_to(base).as_posix()
    except ValueError:
        return None


def _file_entry(path: Path, base: Path) -> dict[str, Any] | None:
    if not _is_within_base(path, base):
        return None
    relative = _relative(path, base)
    if relative is None:
        return None
    stat = _safe_stat(path)
    return {
        "name": path.name,
        "relative_path": relative,
        "exists": stat["exists"],
        "size_bytes": stat["size_bytes"],
    }


def _collect_capped(paths, base: Path, limit: int) -> tuple[list[dict[str, Any]], int, bool]:
    entries: list[dict[str, Any]] = []
    total = 0
    truncated = False
    for path in paths:
        total += 1
        if len(entries) >= limit:
            truncated = True
            continue
        entry = _file_entry(path, base)
        if entry is not None:
            entries.append(entry)
        else:
            # Outside-base / symlink escape — count but do not expose.
            truncated = truncated or False
    return entries, total, truncated or total > len(entries)


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
    relative = _relative(state_db, base) if _is_within_base(state_db, base) else None
    result: dict[str, Any] = {
        "relative_path": relative or "state.db",
        "exists": stat["exists"] if relative is not None else False,
        "size_bytes": stat["size_bytes"] if relative is not None else 0,
        "session_count": 0,
        "message_count": 0,
        "source_counts": {},
        "role_counts": {},
        "latest_session_started_at": None,
        "errors": [],
    }
    if relative is None or not state_db.exists():
        if relative is None and state_db.exists():
            result["errors"].append("outside_hermes_home")
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


def _iter_capped_rglob(root: Path, pattern: str, limit: int) -> tuple[list[Path], int, bool]:
    """Walk ``root`` for ``pattern``, stopping once ``limit`` matches are kept.

    Returns (matched_paths, total_seen_or_lower_bound, truncated).
    """
    if not root.exists():
        return [], 0, False
    matched: list[Path] = []
    total = 0
    truncated = False
    try:
        for path in root.rglob(pattern):
            total += 1
            if len(matched) < limit:
                matched.append(path)
            else:
                truncated = True
                # Keep counting a little further for honesty, but bail before
                # pathological trees turn one inventory request into a DoS.
                if total >= limit * 5:
                    break
    except OSError:
        return matched, total, truncated
    return matched, total, truncated


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

    memory_files = [
        entry
        for entry in (_file_entry(memory_dir / name, base) for name in _MEMORY_FILENAMES)
        if entry is not None
    ]
    profile_markdown, profile_seen, profile_walk_truncated = _iter_capped_rglob(
        profile_dir, "*.md", _MAX_PROFILE_MARKDOWN_FILES
    )
    skill_files, skill_seen, skills_walk_truncated = _iter_capped_rglob(
        skills_dir, "SKILL.md", _MAX_SKILL_FILES
    )
    profile_markdown_files, profile_markdown_count, profile_truncated = _collect_capped(
        profile_markdown, base, _MAX_PROFILE_MARKDOWN_FILES
    )
    _skill_entries, skill_count, skills_truncated = _collect_capped(
        skill_files, base, _MAX_SKILL_FILES
    )
    # Prefer walk totals when the walk stopped early.
    if profile_walk_truncated:
        profile_markdown_count = max(profile_markdown_count, profile_seen)
        profile_truncated = True
    if skills_walk_truncated:
        skill_count = max(skill_count, skill_seen)
        skills_truncated = True
    profile_names = sorted(path.name for path in profiles_dir.iterdir() if path.is_dir()) if profiles_dir.exists() else []

    privacy_warnings = [
        "Inventory is read-only and returns counts/metadata only; memory and transcript contents are not returned.",
        "Raw Hermes sessions should stay inactive until an explicit import/retrieval policy is chosen.",
    ]
    if not exists:
        privacy_warnings.append("Hermes home was not found at the resolved path.")
    if profile_names:
        privacy_warnings.append("Additional Hermes profiles were detected; import/sync should preserve profile privacy boundaries.")
    if profile_truncated or skills_truncated:
        privacy_warnings.append("Inventory file lists were capped; counts may exceed returned file entries.")

    return {
        "hermes_home": str(base),
        "exists": exists,
        "content_returned": False,
        "state_db": _state_db_inventory(base / "state.db", base),
        "memory_files": memory_files,
        "profile_markdown_count": profile_markdown_count,
        "profile_markdown_files": profile_markdown_files,
        "skill_count": skill_count,
        "profile_names": profile_names,
        "privacy_warnings": privacy_warnings,
        "recommended_next_step": "Create a provenance-preserving continuity export/import manifest before activating any imported memories.",
    }
