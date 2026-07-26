"""Regression coverage for the 2026-07-26 application health assessment.

Focus: reliability bugs (session catalog, incognito persistence, corrupt
meta_data, persist failure rollback) plus small security hardenings that
remain open on lost/personal-core.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.models import ChatMessage, Session, set_session_manager
from core.session_manager import SessionManager
import core.session_manager as SM


def _manager_with(sessions):
    manager = SessionManager.__new__(SessionManager)
    manager.sessions = dict(sessions)
    return manager


def _session_local(parent_row):
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = parent_row
    return MagicMock(return_value=db), db


def test_parse_message_meta_tolerates_corrupt_json():
    assert SessionManager._parse_message_meta("{not-json") == {}
    assert SessionManager._parse_message_meta(None) == {}
    assert SessionManager._parse_message_meta('{"a": 1}') == {"a": 1}
    assert SessionManager._parse_message_meta({"already": True}) == {"already": True}


def test_db_to_session_survives_corrupt_meta(monkeypatch):
    """One corrupt meta_data row must not abort loading the whole session."""
    good = SimpleNamespace(
        id="m1", role="user", content="hi", meta_data='{"ok": true}', timestamp=None
    )
    bad = SimpleNamespace(
        id="m2", role="assistant", content="yo", meta_data="{broken", timestamp=None
    )
    db_session = SimpleNamespace(
        id="sid",
        name="Chat",
        endpoint_url="http://x",
        model="m",
        rag=False,
        archived=False,
        headers={},
        owner="alice",
        is_important=False,
        message_count=2,
        messages=[good, bad],
    )
    manager = _manager_with({})
    session = manager._db_to_session(db_session, MagicMock())
    assert session is not None
    assert len(session.history) == 2
    assert session.history[0].metadata.get("ok") is True
    assert session.history[1].metadata.get("_db_id") == "m2"


def test_get_sessions_for_user_reads_beyond_hot_cache(monkeypatch):
    """Sidebar catalog must include DB sessions not present in the ≤100 cache."""
    cold = SimpleNamespace(
        id="cold-1",
        name="Old chat",
        endpoint_url="http://x",
        model="m",
        rag=False,
        archived=False,
        headers={},
        owner="alice",
        is_important=False,
        message_count=3,
    )
    db = MagicMock()
    q = db.query.return_value
    q.filter.return_value = q
    q.order_by.return_value.all.return_value = [cold]
    monkeypatch.setattr(SM, "SessionLocal", MagicMock(return_value=db))

    manager = _manager_with({})  # empty hot cache
    result = manager.get_sessions_for_user("alice")
    assert "cold-1" in result
    assert result["cold-1"].name == "Old chat"
    assert result["cold-1"].owner == "alice"


def test_incognito_add_message_skips_persist():
    calls = []

    class _FakeSM:
        def _persist_message(self, session_id, message):
            calls.append((session_id, message.content))
            return True

    set_session_manager(_FakeSM())
    try:
        sess = Session(id="s1", name="Nobody", endpoint_url="", model="")
        ok = sess.add_message(ChatMessage("user", "secret"), persist=False)
        assert ok is True
        assert len(sess.history) == 1
        assert calls == []
    finally:
        set_session_manager(None)


def test_persist_failure_rolls_back_in_memory_history(monkeypatch):
    parent = SimpleNamespace(message_count=0, last_accessed=None, last_message_at=None)
    session_local, db = _session_local(parent)
    monkeypatch.setattr(SM, "SessionLocal", session_local)
    db.commit.side_effect = RuntimeError("disk full")

    manager = _manager_with({"sid": Session(id="sid", name="c", endpoint_url="", model="")})
    set_session_manager(manager)
    try:
        sess = manager.sessions["sid"]
        msg = ChatMessage("user", "hello")
        ok = sess.add_message(msg)
        assert ok is False
        assert sess.history == []
        assert sess.message_count == 0
    finally:
        set_session_manager(None)


def test_persist_message_returns_false_when_parent_gone(monkeypatch):
    session_local, db = _session_local(None)
    monkeypatch.setattr(SM, "SessionLocal", session_local)
    manager = _manager_with({"deleted": SimpleNamespace(history=[])})
    assert manager._persist_message("deleted", ChatMessage("assistant", "x")) is False
    assert "deleted" not in manager.sessions


def test_sqlite_pragma_enables_wal_and_busy_timeout():
    src = open("core/database.py", encoding="utf-8").read()
    assert "PRAGMA journal_mode=WAL" in src
    assert "PRAGMA busy_timeout=5000" in src


def test_mermaid_uses_strict_security_level():
    src = open("static/js/markdown.js", encoding="utf-8").read()
    assert "securityLevel: 'strict'" in src
    assert "securityLevel: 'loose'" not in src


def test_incognito_ui_sends_private_mode():
    chat = open("static/js/chat.js", encoding="utf-8").read()
    compare = open("static/js/compare/stream.js", encoding="utf-8").read()
    assert "private_mode" in chat
    assert "private_mode" in compare


def test_chat_routes_map_incognito_to_private_mode():
    src = open("routes/chat_routes.py", encoding="utf-8").read()
    assert "or incognito" in src


def test_ghost_purge_is_owner_scoped():
    src = open("routes/session_routes.py", encoding="utf-8").read()
    assert "DbSession.owner == user" in src
    # Must not call delete_session after already deleting the row (double-delete).
    purge_region = src.split("Lazy purge")[1].split("user_sessions =")[0]
    assert "delete_session" not in purge_region


def test_task_scheduler_uses_meta_loader():
    src = open("src/task_scheduler.py", encoding="utf-8").read()
    assert "_db_to_session_meta(sess)" in src
    # Avoid the broken 1-arg _db_to_session(sess) TypeError path.
    assert "_db_to_session(sess)" not in src
