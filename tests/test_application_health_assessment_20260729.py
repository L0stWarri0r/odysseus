"""Regression tests for the 2026-07-29 application health assessment.

Covers NEW fixes on this branch (not re-landing the full #9/#10/#11 suites):
- Hermes continuity inventory + preflight require admin
- Continuity inventory bounds rglob and redacts outside-base paths
- merge-last-assistant DB delete matches in-memory (no collateral wipe)
- Service worker activate only prunes odysseus-* caches
- Task scheduler seeds sessions via _db_to_session_meta (correct arity)
- Email MCP account cache is TTL-based
- manage_notes prefix lookup is owner-scoped / null-owner fail-closed
- manage_tasks mutation gates fail closed on null owner
- manage_research tool owner-scopes list/read/delete
- Compose uploads are owner-scoped; email account null-owner denied
- Reserved rename rejected before owner-column rewrite
- do_edit_image stamps internal auth + owner headers
"""

from __future__ import annotations

import ast
import asyncio
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from routes.hermes_routes import setup_hermes_routes
from src.hermes_control.continuity import (
    _MAX_INVENTORY_FILES,
    _bounded_rglob,
    _relative,
    build_continuity_inventory,
)


ROOT = Path(__file__).resolve().parents[1]


class FakeAuthManager:
    is_configured = True

    def is_admin(self, username):
        return username == "admin"


def _client_with_user(username=None):
    app = FastAPI()
    app.state.auth_manager = FakeAuthManager()

    @app.middleware("http")
    async def _stamp_user(request, call_next):
        if username is not None:
            request.state.current_user = username
        return await call_next(request)

    app.include_router(setup_hermes_routes())
    return TestClient(app)


# --- Hermes admin gates ----------------------------------------------------

def test_hermes_continuity_inventory_requires_admin(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    denied = _client_with_user("alice").get("/api/hermes/continuity/inventory")
    assert denied.status_code == 403

    anon = _client_with_user(None).get("/api/hermes/continuity/inventory")
    assert anon.status_code == 403

    ok = _client_with_user("admin").get("/api/hermes/continuity/inventory")
    assert ok.status_code == 200
    assert ok.json()["content_returned"] is False


def test_hermes_preflight_requires_admin():
    payload = {
        "message": "hello",
        "session_id": "s1",
        "endpoint_url": "https://api.openai.com/v1",
        "model": "cloud-model",
    }
    denied = _client_with_user("alice").post("/api/hermes/preflight", json=payload)
    assert denied.status_code == 403

    ok = _client_with_user("admin").post("/api/hermes/preflight", json=payload)
    assert ok.status_code == 200
    assert "decision" in ok.json() or "allowed" in ok.json() or isinstance(ok.json(), dict)


# --- Continuity inventory bounds -------------------------------------------

def test_relative_redacts_outside_base(tmp_path):
    base = tmp_path / "hermes"
    base.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("x", encoding="utf-8")
    assert _relative(outside, base).startswith("<outside-base>/")


def test_bounded_rglob_caps_file_listing(tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    for i in range(_MAX_INVENTORY_FILES + 25):
        d = root / f"skill-{i}"
        d.mkdir()
        (d / "SKILL.md").write_text("x", encoding="utf-8")
    found = _bounded_rglob(root, "SKILL.md")
    assert len(found) == _MAX_INVENTORY_FILES

    # skills live under hermes_home/skills — place them correctly
    hermes = tmp_path / "h"
    skills = hermes / "skills"
    skills.mkdir(parents=True)
    for i in range(_MAX_INVENTORY_FILES + 5):
        d = skills / f"s{i}"
        d.mkdir()
        (d / "SKILL.md").write_text("x", encoding="utf-8")
    inventory = build_continuity_inventory(hermes)
    assert inventory["skill_count"] == _MAX_INVENTORY_FILES
    assert any("capped" in w.lower() for w in inventory["privacy_warnings"])


# --- merge-last-assistant DB delete ----------------------------------------

def test_merge_last_assistant_db_delete_matches_memory_logic():
    src = (ROOT / "routes" / "history_routes.py").read_text(encoding="utf-8")
    # Executable wipe-all loop must be gone; comment may still mention it.
    assert "for di in range(db_idx2, db_idx1, -1):" not in src
    assert "previous response was interrupted" in src
    assert "to_delete = [db2]" in src
    assert "to_delete.append(between)" in src


# --- Service worker scoped prune -------------------------------------------

def test_sw_activate_only_prunes_odysseus_caches():
    sw = (ROOT / "static" / "sw.js").read_text(encoding="utf-8")
    assert "k.startsWith('odysseus-')" in sw
    assert "keys.filter(k => k !== CACHE_NAME)" not in sw
    assert "odysseus-v328" in sw or "scoped-cache-prune" in sw


# --- Task scheduler session seed arity -------------------------------------

def test_task_scheduler_uses_db_to_session_meta():
    src = (ROOT / "src" / "task_scheduler.py").read_text(encoding="utf-8")
    assert src.count("_db_to_session_meta(sess)") >= 3
    assert "_db_to_session(sess)" not in src


def test_db_to_session_guards_corrupt_metadata():
    src = (ROOT / "core" / "session_manager.py").read_text(encoding="utf-8")
    assert "except (json.JSONDecodeError, TypeError)" in src
    assert src.count("json.loads(db_msg.meta_data)") >= 2


# --- Email MCP account cache TTL -------------------------------------------

def test_email_mcp_account_cache_is_ttl_based():
    src = (ROOT / "mcp_servers" / "email_server.py").read_text(encoding="utf-8")
    assert "EMAIL_ACCOUNT_CACHE_TTL" in src
    assert "time.monotonic()" in src
    assert "clear_account_cache" in src
    tree = ast.parse(src)
    # Ensure we no longer assign bare cfg into the cache forever
    assigns = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
    ]
    forever = False
    for node in assigns:
        for target in node.targets:
            if (
                isinstance(target, ast.Subscript)
                and isinstance(target.value, ast.Name)
                and target.value.id == "_ACCOUNT_CACHE"
                and isinstance(node.value, ast.Name)
                and node.value.id == "cfg"
            ):
                forever = True
    assert forever is False


# --- archive_inactive syncs in-memory --------------------------------------

def test_archive_inactive_sessions_updates_memory_cache():
    src = (ROOT / "src" / "cleanup_service.py").read_text(encoding="utf-8")
    assert "cached.archived = True" in src
    assert "session_manager" in src


# --- manage_notes owner-scoped prefix lookup -------------------------------

class _Col:
    def __init__(self, key):
        self.key = key

    def startswith(self, prefix):
        return SimpleNamespace(left=self, right=SimpleNamespace(value=prefix))

    def __eq__(self, other):
        return SimpleNamespace(left=self, right=SimpleNamespace(value=other))


def _owner_aware_note_db(notes):
    class OwnerAwareQuery:
        def __init__(self):
            self._rows = list(notes)

        def filter(self, *exprs):
            for expr in exprs:
                left = getattr(expr, "left", None)
                right = getattr(expr, "right", None)
                key = getattr(left, "key", None)
                if key == "owner":
                    want = getattr(right, "value", right)
                    self._rows = [n for n in self._rows if n.owner == want]
                elif key == "id":
                    prefix = getattr(right, "value", None)
                    if isinstance(prefix, str):
                        self._rows = [n for n in self._rows if n.id.startswith(prefix)]
            return self

        def first(self):
            return self._rows[0] if self._rows else None

    class OwnerAwareDB:
        def query(self, model):
            return OwnerAwareQuery()

        def commit(self):
            pass

        def delete(self, obj):
            notes[:] = [n for n in notes if n is not obj]

        def close(self):
            pass

    fake_core_db = types.ModuleType("core.database")
    fake_core_db.SessionLocal = lambda: OwnerAwareDB()
    fake_core_db.Note = SimpleNamespace(
        id=_Col("id"),
        owner=_Col("owner"),
        archived=_Col("archived"),
        pinned=_Col("pinned"),
        updated_at=_Col("updated_at"),
        label=_Col("label"),
    )
    return fake_core_db


def test_manage_notes_find_note_filters_by_owner():
    src = (ROOT / "src" / "tool_implementations.py").read_text(encoding="utf-8")
    assert "def _find_note(note_id: str):" in src
    assert "q.filter(Note.owner == owner)" in src
    # Old fail-open null-owner gate must be gone from update/delete/toggle.
    assert "note.owner and note.owner != owner" not in src


def test_manage_notes_prefix_cannot_hit_other_users_note(monkeypatch):
    from src import tool_implementations

    bob_note = SimpleNamespace(
        id="abc12345-bob", owner="bob", title="Bob secret", content=None,
        note_type="note", color=None, label=None, items=None,
        pinned=False, archived=False, due_date=None,
    )
    alice_note = SimpleNamespace(
        id="abc99999-alice", owner="alice", title="Alice", content=None,
        note_type="note", color=None, label=None, items=None,
        pinned=False, archived=False, due_date=None,
    )
    notes = [bob_note, alice_note]  # bob first — unscoped startswith would pick bob

    fake_sa_attrs = types.ModuleType("sqlalchemy.orm.attributes")
    fake_sa_attrs.flag_modified = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "sqlalchemy.orm.attributes", fake_sa_attrs)
    monkeypatch.setitem(sys.modules, "core.database", _owner_aware_note_db(notes))

    result = asyncio.run(
        tool_implementations.do_manage_notes(
            json.dumps({"action": "delete", "id": "abc"}),
            owner="alice",
        )
    )
    assert result.get("exit_code") == 0
    assert "Alice" in result.get("response", "")
    assert bob_note in notes  # bob untouched
    assert alice_note not in notes


def test_manage_notes_null_owner_note_denied(monkeypatch):
    from src import tool_implementations

    orphan = SimpleNamespace(
        id="orphan01-null", owner=None, title="Orphan", content=None,
        note_type="note", color=None, label=None, items=None,
        pinned=False, archived=False, due_date=None,
    )
    notes = [orphan]

    fake_sa_attrs = types.ModuleType("sqlalchemy.orm.attributes")
    fake_sa_attrs.flag_modified = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "sqlalchemy.orm.attributes", fake_sa_attrs)
    monkeypatch.setitem(sys.modules, "core.database", _owner_aware_note_db(notes))

    result = asyncio.run(
        tool_implementations.do_manage_notes(
            json.dumps({"action": "delete", "id": "orphan01"}),
            owner="alice",
        )
    )
    assert result.get("exit_code") == 1
    assert "not found" in (result.get("error") or "").lower()
    assert orphan in notes


# --- manage_tasks null-owner fail-closed -----------------------------------

def test_manage_tasks_null_owner_fail_closed_source():
    src = (ROOT / "src" / "tool_implementations.py").read_text(encoding="utf-8")
    # Old fail-open pattern must be gone from manage_tasks mutations.
    assert "if owner and task.owner and task.owner != owner:" not in src
    assert "if owner is not None and task.owner != owner:" in src


def test_manage_tasks_denies_null_owner_task(monkeypatch):
    from src import tool_implementations

    orphan = SimpleNamespace(
        id="task-1", owner=None, name="Orphan task", status="active",
        schedule=None, scheduled_time=None, scheduled_day=None,
        trigger_type="schedule",
    )

    class FakeQuery:
        def filter(self, *a, **k):
            return self

        def first(self):
            return orphan

    class FakeDB:
        def query(self, *a, **k):
            return FakeQuery()

        def close(self):
            pass

    fake_core_db = types.ModuleType("core.database")
    fake_core_db.SessionLocal = lambda: FakeDB()
    fake_core_db.ScheduledTask = MagicMock()
    monkeypatch.setitem(sys.modules, "core.database", fake_core_db)

    fake_sched = types.ModuleType("src.task_scheduler")
    fake_sched.compute_next_run = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "src.task_scheduler", fake_sched)

    result = asyncio.run(
        tool_implementations.do_manage_tasks(
            json.dumps({"action": "delete", "task_id": "task-1"}),
            owner="alice",
        )
    )
    assert result.get("exit_code") == 1
    assert "access denied" in (result.get("error") or "").lower()


# --- manage_research owner scope -------------------------------------------

def test_manage_research_owner_scopes_list_read_delete(tmp_path, monkeypatch):
    from src import tool_implementations

    monkeypatch.chdir(tmp_path)
    data_dir = tmp_path / "data" / "deep_research"
    data_dir.mkdir(parents=True)
    (data_dir / "alice1.json").write_text(
        json.dumps({"owner": "alice", "query": "Alice Q", "sources": [], "completed_at": 2}),
        encoding="utf-8",
    )
    (data_dir / "bob1.json").write_text(
        json.dumps({"owner": "bob", "query": "Bob secret", "result": "SECRET", "sources": [], "completed_at": 3}),
        encoding="utf-8",
    )
    (data_dir / "legacy.json").write_text(
        json.dumps({"query": "Legacy", "result": "LEGACY", "sources": [], "completed_at": 1}),
        encoding="utf-8",
    )

    listed = asyncio.run(
        tool_implementations.do_manage_research(
            json.dumps({"action": "list"}),
            owner="alice",
        )
    )
    assert "Alice Q" in listed.get("output", "")
    assert "Bob secret" not in listed.get("output", "")
    assert "Legacy" not in listed.get("output", "")

    denied = asyncio.run(
        tool_implementations.do_manage_research(
            json.dumps({"action": "read", "id": "bob1"}),
            owner="alice",
        )
    )
    assert "not found" in (denied.get("error") or "").lower()
    assert "SECRET" not in json.dumps(denied)

    del_denied = asyncio.run(
        tool_implementations.do_manage_research(
            json.dumps({"action": "delete", "id": "bob1"}),
            owner="alice",
        )
    )
    assert "not found" in (del_denied.get("error") or "").lower()
    assert (data_dir / "bob1.json").exists()


# --- compose-upload owner isolation ----------------------------------------

def test_compose_upload_path_is_owner_scoped(tmp_path, monkeypatch):
    from routes import email_helpers

    monkeypatch.setattr(email_helpers, "COMPOSE_UPLOADS_DIR", tmp_path / "_compose")
    email_helpers.COMPOSE_UPLOADS_DIR.mkdir(parents=True)

    alice_path = email_helpers._compose_upload_path("alice", "tok_file.pdf")
    bob_path = email_helpers._compose_upload_path("bob", "tok_file.pdf")
    assert alice_path != bob_path
    assert alice_path.parent.name == "alice"
    assert bob_path.parent.name == "bob"

    # Traversal in token collapses to basename under owner dir
    nasty = email_helpers._compose_upload_path("alice", "../etc/passwd")
    assert nasty.name == "passwd"
    assert nasty.parent.name == "alice"


def test_compose_attach_cannot_read_other_owners_file(tmp_path, monkeypatch):
    from email.mime.multipart import MIMEMultipart
    from routes import email_helpers

    monkeypatch.setattr(email_helpers, "COMPOSE_UPLOADS_DIR", tmp_path / "_compose")
    email_helpers.COMPOSE_UPLOADS_DIR.mkdir(parents=True)

    token = "aabbccdd_secret.pdf"
    bob_file = email_helpers._compose_upload_path("bob", token)
    bob_file.parent.mkdir(parents=True, exist_ok=True)
    bob_file.write_bytes(b"bob-private")

    outer = MIMEMultipart("mixed")
    email_helpers._attach_compose_uploads(outer, [token], owner="alice")
    raw = outer.as_bytes()
    assert b"bob-private" not in raw


def test_assert_owns_account_denies_null_owner(monkeypatch):
    from routes import email_helpers

    class FakeQuery:
        def filter(self, *a, **k):
            return self

        def first(self):
            return SimpleNamespace(id="acct-1", owner=None)

    class FakeDB:
        def query(self, *a, **k):
            return FakeQuery()

        def close(self):
            pass

    fake_db_mod = types.ModuleType("core.database")
    fake_db_mod.SessionLocal = lambda: FakeDB()
    fake_db_mod.EmailAccount = MagicMock()
    monkeypatch.setitem(sys.modules, "core.database", fake_db_mod)

    with pytest.raises(HTTPException) as exc:
        email_helpers._assert_owns_account("acct-1", "alice")
    assert exc.value.status_code == 404


# --- reserved rename pre-check ---------------------------------------------

def test_rename_rejects_reserved_before_owner_rewrite():
    src = (ROOT / "routes" / "auth_routes.py").read_text(encoding="utf-8")
    reserved_idx = src.index("if new_username in RESERVED_USERNAMES:")
    rewrite_idx = src.index('update({"owner": new_username}')
    assert reserved_idx < rewrite_idx


# --- do_edit_image auth headers --------------------------------------------

def test_do_edit_image_uses_internal_headers():
    src = (ROOT / "src" / "tool_implementations.py").read_text(encoding="utf-8")
    start = src.index("async def do_edit_image")
    end = src.index("async def do_manage_research", start)
    body = src[start:end]
    assert "_internal_headers(owner=owner)" in body
    assert "_COOKBOOK_BASE" in body
    assert 'post(f"http://localhost:7000/api/gallery/{action}", json=payload)' not in body
