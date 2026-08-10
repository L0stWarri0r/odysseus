"""Regression tests for the 2026-08-10 health assessment.

Covers:
  * require_admin header-direct bypass requires trusted loopback
  * require_privilege fails closed on lookup errors / corrupt privileges
  * API token chat scope allowlist (global, not route-local)
  * TTS clear-cache is admin-gated; synthesize/transcribe require_user
  * serve_generated_image fail-closed + effective_user ownership
  * SSH uses StrictHostKeyChecking=accept-new (not no)
  * email_summaries / email_ai_replies keyed by (message_id, owner)
  * SessionManager hot-set eviction when over soft cap
"""

from __future__ import annotations

import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException


# --- require_admin header-direct -------------------------------------------

def test_require_admin_header_bypass_requires_loopback(monkeypatch):
    from core import middleware as mw

    monkeypatch.setattr(mw, "INTERNAL_TOOL_TOKEN", "secret-token")
    monkeypatch.delenv("AUTH_ENABLED", raising=False)

    class _Mgr:
        is_configured = True

        def is_admin(self, user):
            return False

    def _req(host: str, hdr: str | None):
        state = types.SimpleNamespace(current_user="bob")
        app = types.SimpleNamespace(state=types.SimpleNamespace(auth_manager=_Mgr()))
        headers = {mw.INTERNAL_TOOL_HEADER: hdr} if hdr else {}
        client = types.SimpleNamespace(host=host)
        return types.SimpleNamespace(state=state, app=app, headers=headers, client=client)

    # Matching token from remote host must NOT escalate.
    with pytest.raises(HTTPException) as exc:
        mw.require_admin(_req("203.0.113.9", "secret-token"))
    assert exc.value.status_code == 403

    # Matching token from trusted loopback is allowed.
    assert mw.require_admin(_req("127.0.0.1", "secret-token")) is None

    # Proxy-forwarded loopback (tunnel) must still be rejected.
    proxied = _req("127.0.0.1", "secret-token")
    proxied.headers = {
        mw.INTERNAL_TOOL_HEADER: "secret-token",
        "x-forwarded-for": "203.0.113.9",
    }
    with pytest.raises(HTTPException) as exc2:
        mw.require_admin(proxied)
    assert exc2.value.status_code == 403


# --- require_privilege fail-closed -----------------------------------------

def test_require_privilege_fails_closed_on_exception(monkeypatch):
    from src import auth_helpers

    class _Boom:
        def get_privileges(self, user):
            raise RuntimeError("corrupt auth.json")

    monkeypatch.setattr(auth_helpers, "require_user", lambda request: "bob")
    req = types.SimpleNamespace(
        app=types.SimpleNamespace(state=types.SimpleNamespace(auth_manager=_Boom()))
    )
    with pytest.raises(HTTPException) as exc:
        auth_helpers.require_privilege(req, "can_use_research")
    assert exc.value.status_code == 403


def test_require_privilege_fails_closed_on_nondict(monkeypatch):
    from src import auth_helpers

    class _Mgr:
        def get_privileges(self, user):
            return ["not", "a", "dict"]

    monkeypatch.setattr(auth_helpers, "require_user", lambda request: "bob")
    req = types.SimpleNamespace(
        app=types.SimpleNamespace(state=types.SimpleNamespace(auth_manager=_Mgr()))
    )
    with pytest.raises(HTTPException) as exc:
        auth_helpers.require_privilege(req, "can_use_research")
    assert exc.value.status_code == 403


# --- API token scope allowlist ---------------------------------------------

def test_api_token_chat_scope_allowlist():
    from core.middleware import api_token_path_allowed

    assert api_token_path_allowed("/api/v1/chat", ["chat"]) is True
    assert api_token_path_allowed("/api/companion/ping", ["chat"]) is True
    assert api_token_path_allowed("/api/companion/models", ["chat"]) is True
    assert api_token_path_allowed("/api/generated-image/abc123.png", ["chat"]) is True
    # Previously open to any chat-scoped bearer via synthetic "api" user:
    assert api_token_path_allowed("/api/research/start", ["chat"]) is False
    assert api_token_path_allowed("/api/gallery/library", ["chat"]) is False
    assert api_token_path_allowed("/api/tts/synthesize", ["chat"]) is False
    assert api_token_path_allowed("/api/shell/exec", ["chat"]) is False
    assert api_token_path_allowed("/api/email/list", ["chat"]) is False
    # Broad scopes still pass.
    assert api_token_path_allowed("/api/research/start", ["admin"]) is True
    assert api_token_path_allowed("/api/gallery/library", ["*"]) is True


# --- TTS / STT route gates -------------------------------------------------

def test_tts_stt_routes_require_auth_gates():
    tts = Path("routes/tts_routes.py").read_text(encoding="utf-8")
    stt = Path("routes/stt_routes.py").read_text(encoding="utf-8")
    assert "require_user(http_request)" in tts
    assert "require_admin(http_request)" in tts
    assert "clear_tts_cache" in tts
    assert tts.index("require_admin(http_request)") < tts.index("tts_service.clear_cache()")
    assert "require_user(http_request)" in stt


# --- serve_generated_image -------------------------------------------------

def test_serve_generated_image_fail_closed_and_effective_user():
    src = Path("app.py").read_text(encoding="utf-8")
    # Ownership must use effective_user (API token owner), not get_current_user.
    assert "effective_user(request)" in src
    assert "serve_generated_image" in src
    # Fail closed on lookup errors — no bare `except Exception: pass`.
    start = src.index("async def serve_generated_image")
    end = src.index("\n# =========", start)
    body = src[start:end]
    assert "raise HTTPException(status_code=404" in body
    assert "except Exception:\n        pass" not in body
    assert "_auth_disabled()" in body


# --- SSH host key checking -------------------------------------------------

def test_ssh_uses_accept_new_not_disabled():
    files = [
        "routes/shell_routes.py",
        "routes/cookbook_routes.py",
        "services/hwfit/hardware.py",
        "src/tool_implementations.py",
    ]
    for f in files:
        text = Path(f).read_text(encoding="utf-8")
        assert "StrictHostKeyChecking=no" not in text, f
        assert "StrictHostKeyChecking=accept-new" in text, f


# --- email cache owner columns ---------------------------------------------

def test_email_cache_tables_are_owner_keyed():
    helpers = Path("routes/email_helpers.py").read_text(encoding="utf-8")
    assert "PRIMARY KEY (message_id, owner)" in helpers
    assert "email_summaries__new" in helpers
    assert "email_ai_replies__new" in helpers
    routes = Path("routes/email_routes.py").read_text(encoding="utf-8")
    assert "FROM email_summaries WHERE owner = ?" in routes or "AND owner = ?" in routes
    assert "INSERT OR REPLACE INTO email_summaries" in routes
    assert "owner," in routes[routes.index("INSERT OR REPLACE INTO email_summaries"):
                              routes.index("INSERT OR REPLACE INTO email_summaries") + 200]
    pollers = Path("routes/email_pollers.py").read_text(encoding="utf-8")
    assert "WHERE owner = ?" in pollers
    assert "account_owner or \"\"" in pollers or "account_owner or ''" in pollers


# --- SessionManager hot-set eviction ---------------------------------------

def test_session_manager_evicts_over_soft_cap(tmp_path, monkeypatch):
    # Avoid real DB during eviction unit test.
    import core.session_manager as sm

    mgr = sm.SessionManager.__new__(sm.SessionManager)
    mgr.sessions = {}
    mgr._lock = __import__("threading").RLock()
    mgr._max_hot = 5

    for i in range(8):
        sess = MagicMock()
        sess.history = [] if i < 6 else ["x"]
        mgr.sessions[f"s{i}"] = sess

    mgr._evict_if_needed(keep_id="s7")
    assert len(mgr.sessions) <= 5
    assert "s7" in mgr.sessions


def test_shell_require_admin_fails_closed_when_unconfigured(monkeypatch):
    from routes.shell_routes import _require_admin

    monkeypatch.delenv("AUTH_ENABLED", raising=False)

    class _Req:
        state = types.SimpleNamespace(current_user=None)
        app = types.SimpleNamespace(state=types.SimpleNamespace(auth_manager=None))

    with pytest.raises(HTTPException) as exc:
        _require_admin(_Req())
    assert exc.value.status_code == 403
