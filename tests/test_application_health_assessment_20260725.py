"""Regressions for the 2026-07-25 application health assessment."""

from __future__ import annotations

import ast
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException


ROOT = Path(__file__).resolve().parents[1]


def test_require_privilege_fails_closed_on_lookup_error(monkeypatch):
    from src import auth_helpers
    from src.auth_helpers import require_privilege

    class _Boom:
        def get_privileges(self, user):
            raise RuntimeError("corrupt auth store")

    monkeypatch.setattr(auth_helpers, "require_user", lambda request: "alice")
    req = types.SimpleNamespace(
        app=types.SimpleNamespace(state=types.SimpleNamespace(auth_manager=_Boom()))
    )
    with pytest.raises(HTTPException) as exc:
        require_privilege(req, "can_use_research")
    assert exc.value.status_code == 403


def test_require_privilege_fails_closed_on_nondict(monkeypatch):
    from src import auth_helpers
    from src.auth_helpers import require_privilege

    class _Mgr:
        def get_privileges(self, user):
            return ["can_use_research"]

    monkeypatch.setattr(auth_helpers, "require_user", lambda request: "alice")
    req = types.SimpleNamespace(
        app=types.SimpleNamespace(state=types.SimpleNamespace(auth_manager=_Mgr()))
    )
    with pytest.raises(HTTPException) as exc:
        require_privilege(req, "can_use_research")
    assert exc.value.status_code == 403


def test_require_privilege_fails_closed_on_missing_key(monkeypatch):
    from src import auth_helpers
    from src.auth_helpers import require_privilege

    class _Mgr:
        def get_privileges(self, user):
            return {"can_use_documents": True}

    monkeypatch.setattr(auth_helpers, "require_user", lambda request: "alice")
    req = types.SimpleNamespace(
        app=types.SimpleNamespace(state=types.SimpleNamespace(auth_manager=_Mgr()))
    )
    with pytest.raises(HTTPException) as exc:
        require_privilege(req, "can_use_research")
    assert exc.value.status_code == 403


def test_require_privilege_still_allows_explicit_true(monkeypatch):
    from src import auth_helpers
    from src.auth_helpers import require_privilege

    class _Mgr:
        def get_privileges(self, user):
            return {"can_use_research": True}

    monkeypatch.setattr(auth_helpers, "require_user", lambda request: "alice")
    req = types.SimpleNamespace(
        app=types.SimpleNamespace(state=types.SimpleNamespace(auth_manager=_Mgr()))
    )
    assert require_privilege(req, "can_use_research") == "alice"


def test_shell_ssh_rejects_silent_host_key_disable():
    src = (ROOT / "routes" / "shell_routes.py").read_text(encoding="utf-8")
    assert "StrictHostKeyChecking=no" not in src
    assert "StrictHostKeyChecking=accept-new" in src
    hw = (ROOT / "services" / "hwfit" / "hardware.py").read_text(encoding="utf-8")
    assert "StrictHostKeyChecking=no" not in hw
    assert "StrictHostKeyChecking=accept-new" in hw


def test_task_name_generator_scopes_session_endpoint_to_owner():
    src = (ROOT / "routes" / "task_routes.py").read_text(encoding="utf-8")
    assert "async def _generate_task_name(prompt: str, owner: Optional[str] = None)" in src
    assert "q.filter(DbSession.owner == owner)" in src
    assert "name = await _generate_task_name(req.prompt, owner=user)" in src


def test_task_clear_cache_refuses_unscoped_global_wipe():
    src = (ROOT / "routes" / "task_routes.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "clear_task_cache":
            body = ast.get_source_segment(src, node) or ""
            assert "PRAGMA table_info" in body
            assert "Tables without an owner column cannot be safely scoped" in body
            assert 'child.stem not in account_ids' in body
            # Must not unconditionally DELETE FROM {table} when user is set.
            assert "refuse the global wipe" in body
            found = True
    assert found


def test_incognito_ghost_purge_is_owner_scoped():
    src = (ROOT / "routes" / "session_routes.py").read_text(encoding="utf-8")
    assert 'DbSession.name.in_(("Nobody", "Incognito"))' in src
    assert "_ghosts_q = _ghosts_q.filter(DbSession.owner == user)" in src


def test_document_session_null_owner_denied_for_authenticated():
    src = (ROOT / "routes" / "document_routes.py").read_text(encoding="utf-8")
    assert "if user and (not session.owner or session.owner != user):" in src
    assert "if user and (not sess.owner or sess.owner != user):" in src
    # Legacy fail-open pattern must not remain on create/import/list.
    assert "if user and session.owner and session.owner != user:" not in src
    assert "if user and sess.owner and sess.owner != user:" not in src


def test_chat_fallback_endpoint_is_owner_scoped():
    src = (ROOT / "routes" / "chat_helpers.py").read_text(encoding="utf-8")
    fn_start = src.index("def try_fallback_endpoint")
    fn_end = src.index("\ndef ", fn_start + 1)
    body = src[fn_start:fn_end]
    assert "owner = getattr(sess, \"owner\", None)" in body
    assert "owner_filter(q, ModelEndpoint, owner)" in body


def test_model_context_local_endpoint_rejects_hostname_prefix_spoof():
    from src.model_context import _is_local_endpoint

    assert _is_local_endpoint("http://127.0.0.1:8080/v1") is True
    assert _is_local_endpoint("http://10.0.0.5:8080/v1") is True
    assert _is_local_endpoint("http://192.168.1.10/v1") is True
    assert _is_local_endpoint("http://100.64.1.5:8080/v1") is True
    assert _is_local_endpoint("http://10.evil.com/v1") is False
    assert _is_local_endpoint("http://192.168.evil.com/v1") is False
    assert _is_local_endpoint("https://api.openai.com/v1") is False
