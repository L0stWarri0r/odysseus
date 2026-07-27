"""Application health assessment — 2026-07-27.

Covers NEW findings not addressed by open PRs #1–#10:
1. require_admin must not trust X-Odysseus-Internal-Token without loopback stamp
2. manage_research is owner-scoped (list/read/delete)
3. task auto-name resolves endpoint from the caller's sessions only
4. rename rejects reserved usernames before rewriting owner rows
5. task scheduler endpoint lookups are owner-filtered
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException


# ── 1. require_admin: no header-direct escalation ───────────────────────────


def test_require_admin_rejects_internal_token_header_without_stamp(monkeypatch):
    """A matching X-Odysseus-Internal-Token alone must NOT grant admin.

    AuthMiddleware stamps current_user=internal-tool only after a trusted
    loopback check. require_admin must rely on that stamp, not the raw header.
    """
    monkeypatch.setenv("AUTH_ENABLED", "true")
    from core import middleware as mw

    class _Auth:
        is_configured = True

        def is_admin(self, user):
            return False

    req = SimpleNamespace(
        state=SimpleNamespace(current_user="alice"),
        headers={mw.INTERNAL_TOOL_HEADER: mw.INTERNAL_TOOL_TOKEN},
        app=SimpleNamespace(state=SimpleNamespace(auth_manager=_Auth())),
    )
    with pytest.raises(HTTPException) as exc:
        mw.require_admin(req)
    assert exc.value.status_code == 403


def test_require_admin_allows_stamped_internal_tool(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    from core import middleware as mw

    req = SimpleNamespace(
        state=SimpleNamespace(current_user="internal-tool"),
        headers={},
        app=SimpleNamespace(state=SimpleNamespace(auth_manager=None)),
    )
    assert mw.require_admin(req) is None


# ── 2. manage_research owner scope ──────────────────────────────────────────


@pytest.fixture
def research_library(tmp_path, monkeypatch):
    data_dir = tmp_path / "data" / "deep_research"
    data_dir.mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    return data_dir


def _write_report(data_dir: Path, rid: str, owner: str, query: str) -> None:
    (data_dir / f"{rid}.json").write_text(
        json.dumps({
            "query": query,
            "owner": owner,
            "result": f"Report body for {query}",
            "sources": [{"title": "Example", "url": "https://example.com"}],
            "completed_at": 100,
        }),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_manage_research_list_is_owner_scoped(research_library):
    from src.tool_implementations import do_manage_research

    _write_report(research_library, "rp-alice", "alice", "alice query")
    _write_report(research_library, "rp-bob", "bob", "bob query")

    res = await do_manage_research(json.dumps({"action": "list"}), owner="alice")
    out = res.get("output", "")
    assert "alice query" in out
    assert "bob query" not in out
    assert res.get("exit_code") == 0


@pytest.mark.asyncio
async def test_manage_research_read_blocks_cross_owner(research_library):
    from src.tool_implementations import do_manage_research

    _write_report(research_library, "rp-bob", "bob", "secret bob research")
    res = await do_manage_research(
        json.dumps({"action": "read", "id": "rp-bob"}),
        owner="alice",
    )
    assert "error" in res
    assert "not found" in res["error"].lower()
    assert "secret bob" not in res.get("output", "")


@pytest.mark.asyncio
async def test_manage_research_delete_blocks_cross_owner(research_library):
    from src.tool_implementations import do_manage_research

    _write_report(research_library, "rp-bob", "bob", "bob query")
    res = await do_manage_research(
        json.dumps({"action": "delete", "id": "rp-bob"}),
        owner="alice",
    )
    assert "error" in res
    assert (research_library / "rp-bob.json").exists()


# ── 3. task auto-name owner scope ───────────────────────────────────────────


def test_generate_task_name_filters_sessions_by_owner():
    """Static check: _generate_task_name must filter DbSession.owner when set."""
    src = Path("routes/task_routes.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_generate_task_name":
            body = ast.unparse(node)
            assert "DbSession.owner == owner" in body or "owner ==" in body
            assert "owner: str" in body or "owner=" in ast.unparse(node.args)
            found = True
            break
    assert found, "_generate_task_name not found"


def test_create_task_passes_user_to_generate_task_name():
    src = Path("routes/task_routes.py").read_text(encoding="utf-8")
    assert "_generate_task_name(req.prompt, owner=user" in src


# ── 4. rename reserved usernames before DB rewrite ──────────────────────────


def test_rename_route_rejects_reserved_before_owner_rewrite():
    src = Path("routes/auth_routes.py").read_text(encoding="utf-8")
    rename_idx = src.index("async def rename_user")
    reserved_idx = src.index("RESERVED_USERNAMES", rename_idx)
    owner_update_idx = src.index('update({"owner": new_username}', rename_idx)
    assert reserved_idx < owner_update_idx, (
        "Reserved-username check must run before owner-column rewrites"
    )


# ── 5. task scheduler endpoint ownership ────────────────────────────────────


def test_task_scheduler_endpoint_lookups_use_owner_filter():
    src = Path("src/task_scheduler.py").read_text(encoding="utf-8")
    assert "owner_filter(q, ModelEndpoint, task.owner" in src
    assert src.count("owner_filter(q, ModelEndpoint, task.owner") >= 2
    assert "resolve_utility_fallback_candidates(owner=task.owner" in src


# ── 6. tmux script/log permissions ──────────────────────────────────────────


def test_tmux_wrapper_uses_owner_only_permissions():
    src = Path("routes/shell_routes.py").read_text(encoding="utf-8")
    assert "chmod(0o700)" in src
    assert "chmod(0o755)" not in src
    assert "_ensure_tmux_log_dir" in src
