"""Regression tests for the 2026-07-29 application health assessment.

Covers NEW fixes on this branch (not re-landing the full #9/#10/#11 suites):
- Hermes continuity inventory + preflight require admin
- Continuity inventory bounds rglob and redacts outside-base paths
- merge-last-assistant DB delete matches in-memory (no collateral wipe)
- Service worker activate only prunes odysseus-* caches
- Task scheduler seeds sessions via _db_to_session_meta (correct arity)
- Email MCP account cache is TTL-based
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from unittest.mock import MagicMock

from fastapi import FastAPI
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
    assert ok.json()["decision"] == "allow"


# --- Continuity inventory safety -------------------------------------------

def test_continuity_relative_redacts_outside_base(tmp_path):
    base = tmp_path / "hermes"
    base.mkdir()
    outside = tmp_path / "elsewhere" / "secret.md"
    outside.parent.mkdir()
    outside.write_text("x", encoding="utf-8")
    rel = _relative(outside, base)
    assert str(outside) not in rel
    assert rel.startswith("<outside-base>/")
    assert rel.endswith("secret.md")


def test_bounded_rglob_caps_file_listing(tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    for i in range(_MAX_INVENTORY_FILES + 25):
        d = root / f"skill-{i}"
        d.mkdir()
        (d / "SKILL.md").write_text("x", encoding="utf-8")
    found = _bounded_rglob(root, "SKILL.md")
    assert len(found) == _MAX_INVENTORY_FILES

    inventory = build_continuity_inventory(tmp_path)
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
