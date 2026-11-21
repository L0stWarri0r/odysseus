"""POST /api/session/{id}/compact in history_routes must not return exception text.

This is not the live route. app.py registers session_routes first, and that
compact handler already returns "Compaction failed" without exception text.
An unexpected failure of the history handler is HTTP 502
{"error": "Failed to compact session"}. The exception is logged and is not
returned. An HTTPException raised inside the second try is still passed
through. A KeyError from get_session stays HTTP 404 "Session not found".
A KeyError inside the second try is the 502, not a 404. Fewer than 6
messages stays HTTP 200 and does not open the history SessionLocal.
In-memory history is replaced before that SessionLocal call. save_sessions
stays after db.close(). There is no rollback. Topics, fork, merge, metadata
update, mark-stopped, edit, delete, truncate, and the live compact handler
were not changed. Clients were not changed.
"""
import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite:///./data/app.db")
os.makedirs("data", exist_ok=True)

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import routes.history_routes as history_routes
from core.database import ChatMessage as DbChatMessage, Session as DbSession, SessionLocal
from core.models import ChatMessage, Session

_COMPACT_SECRET = "COMPACT_SECRET session compact token=do-not-return"
_KEY_SECRET = "KEY_SECRET session missing token=do-not-return"
_FIXED_ERROR = "Failed to compact session"
_LOG = "Manual compact error"
_REFUSED = "Compaction refused"
_REPO = Path(__file__).resolve().parent.parent
_MODEL = "test-model"
_ENDPOINT = "http://127.0.0.1:9"
_SESSION_HEADERS = {"Authorization": "Bearer session"}
_UTIL_URL = "http://127.0.0.1:9/utility"
_UTIL_MODEL = "util-model"
_UTIL_HEADERS = {"Authorization": "Bearer util"}


class _Manager:
    def __init__(self, session=None, missing=False, save_exc=None):
        self.session = session
        self.missing = missing
        self.save_exc = save_exc
        self.lookups = []
        self.saves = 0

    def get_session(self, session_id):
        self.lookups.append(session_id)
        if self.missing:
            raise KeyError(_KEY_SECRET)
        return self.session

    def save_sessions(self):
        self.saves += 1
        if self.save_exc is not None:
            raise self.save_exc


def _messages(count):
    return [
        ChatMessage(role="user" if i % 2 == 0 else "assistant", content=f"c{i}")
        for i in range(count)
    ]


def _session(count):
    return Session(
        id="pending",
        name="Project",
        endpoint_url=_ENDPOINT,
        model=_MODEL,
        headers=dict(_SESSION_HEADERS),
        history=_messages(count),
    )


@pytest.fixture
def owned_ids():
    ids = []
    yield ids
    _delete_rows(ids)


def _client(username, manager):
    app = FastAPI()

    @app.middleware("http")
    async def _stamp_user(request, call_next):
        if username is not None:
            request.state.current_user = username
        return await call_next(request)

    app.include_router(history_routes.setup_history_routes(manager))
    return TestClient(app)


def _post(username, manager, session_id):
    return _client(username, manager).post(f"/api/session/{session_id}/compact")


def _block_history_db(monkeypatch):
    state = {"opens": 0}

    def tracking():
        state["opens"] += 1
        raise RuntimeError("history SessionLocal opened")

    monkeypatch.setattr(history_routes, "SessionLocal", tracking)
    return state


def _add_session(owner, name="Project", message_count=0):
    db = SessionLocal()
    try:
        session_id = str(uuid.uuid4())
        db.add(DbSession(
            id=session_id,
            name=name,
            endpoint_url=_ENDPOINT,
            model=_MODEL,
            owner=owner,
            message_count=message_count,
        ))
        db.commit()
        return session_id
    finally:
        db.close()


def _add_message(session_id, content, when, role="user"):
    db = SessionLocal()
    try:
        msg_id = str(uuid.uuid4())
        db.add(DbChatMessage(
            id=msg_id,
            session_id=session_id,
            role=role,
            content=content,
            meta_data=None,
            timestamp=when,
        ))
        db.commit()
        return msg_id
    finally:
        db.close()


def _rows(session_id):
    db = SessionLocal()
    try:
        messages = (
            db.query(DbChatMessage)
            .filter(DbChatMessage.session_id == session_id)
            .order_by(DbChatMessage.timestamp)
            .all()
        )
        stored = [(m.role, m.content, m.meta_data) for m in messages]
        row = db.query(DbSession).filter(DbSession.id == session_id).one()
        return stored, row.message_count
    finally:
        db.close()


def _delete_rows(session_ids):
    db = SessionLocal()
    try:
        if session_ids:
            db.query(DbChatMessage).filter(
                DbChatMessage.session_id.in_(list(session_ids))
            ).delete(synchronize_session=False)
            db.query(DbSession).filter(
                DbSession.id.in_(list(session_ids))
            ).delete(synchronize_session=False)
            db.commit()
    finally:
        db.close()


def _assert_no_secrets(resp):
    assert _COMPACT_SECRET not in resp.text
    assert _KEY_SECRET not in resp.text
    assert _LOG not in resp.text


def _assert_fixed(resp):
    assert resp.status_code == 502
    assert resp.json() == {"error": _FIXED_ERROR}
    assert "detail" not in resp.json()
    assert "Session not found" not in resp.text
    _assert_no_secrets(resp)


def _patch_compact(monkeypatch, llm, resolve=None, ctx_len=1000, tokens=None):
    calls = {"resolve": [], "llm": [], "ctx": [], "tokens": []}
    token_values = list(tokens or [100, 50])

    def fake_ctx(endpoint_url, model):
        calls["ctx"].append((endpoint_url, model))
        return ctx_len

    def fake_tokens(messages):
        calls["tokens"].append(messages)
        if not token_values:
            return 0
        return token_values.pop(0)

    def fake_resolve(*args, **kwargs):
        calls["resolve"].append((args, kwargs))
        if resolve is not None:
            return resolve
        return (_UTIL_URL, _UTIL_MODEL, dict(_UTIL_HEADERS))

    async def fake_llm(*args, **kwargs):
        calls["llm"].append((args, kwargs))
        if isinstance(llm, Exception):
            raise llm
        return llm

    monkeypatch.setattr("src.model_context.get_context_length", fake_ctx)
    monkeypatch.setattr("src.model_context.estimate_tokens", fake_tokens)
    monkeypatch.setattr("src.endpoint_resolver.resolve_endpoint", fake_resolve)
    monkeypatch.setattr("src.llm_core.llm_call_async", fake_llm)
    return calls


def test_anonymous_stays_403_and_does_not_load_session(caplog, monkeypatch, owned_ids):
    caplog.set_level(logging.ERROR, logger="routes.history_routes")
    state = _block_history_db(monkeypatch)
    manager = _Manager(session=_session(8))
    resp = _post(None, manager, "missing")

    assert resp.status_code == 403
    assert resp.json()["detail"] == "Authentication required"
    assert "error" not in resp.json()
    assert manager.lookups == []
    assert manager.saves == 0
    assert state["opens"] == 0
    assert _LOG not in caplog.text
    _assert_no_secrets(resp)


def test_missing_session_keyerror_stays_404(caplog, monkeypatch, owned_ids):
    caplog.set_level(logging.ERROR, logger="routes.history_routes")
    state = _block_history_db(monkeypatch)
    calls = _patch_compact(monkeypatch, llm=RuntimeError(_COMPACT_SECRET))
    session_id = _add_session("alice")
    owned_ids.append(session_id)
    manager = _Manager(missing=True)
    resp = _post("alice", manager, session_id)

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Session not found"
    assert "error" not in resp.json()
    assert manager.lookups == [session_id]
    assert manager.saves == 0
    assert state["opens"] == 0
    assert calls["resolve"] == []
    assert calls["llm"] == []
    assert calls["ctx"] == []
    assert _LOG not in caplog.text
    assert _KEY_SECRET not in caplog.text
    _assert_no_secrets(resp)


def test_short_history_stays_200_and_does_not_open_db(monkeypatch, owned_ids):
    state = _block_history_db(monkeypatch)
    calls = _patch_compact(monkeypatch, llm=RuntimeError(_COMPACT_SECRET))
    session_id = _add_session("alice")
    owned_ids.append(session_id)
    session = _session(5)
    manager = _Manager(session=session)
    resp = _post("alice", manager, session_id)

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "message": "Not enough messages to compact"}
    assert "error" not in resp.json()
    assert manager.lookups == [session_id]
    assert manager.saves == 0
    assert state["opens"] == 0
    assert calls["resolve"] == []
    assert calls["llm"] == []
    assert calls["ctx"] == []
    assert calls["tokens"] == []
    assert [m.content for m in session.history] == [f"c{i}" for i in range(5)]
    _assert_no_secrets(resp)


def test_second_try_keyerror_is_502_not_404(caplog, monkeypatch, owned_ids):
    caplog.set_level(logging.ERROR, logger="routes.history_routes")
    state = _block_history_db(monkeypatch)
    session_id = _add_session("alice")
    owned_ids.append(session_id)
    session = _session(7)
    original = list(session.history)

    def boom(endpoint_url, model):
        raise KeyError(_COMPACT_SECRET)

    monkeypatch.setattr("src.model_context.get_context_length", boom)
    manager = _Manager(session=session)
    resp = _post("alice", manager, session_id)

    _assert_fixed(resp)
    assert resp.status_code != 404
    assert manager.lookups == [session_id]
    assert manager.saves == 0
    assert state["opens"] == 0
    assert session.history == original
    assert _COMPACT_SECRET in caplog.text
    assert f"{_LOG} for {session_id}" in caplog.text
    assert "Session not found" not in caplog.text


def test_llm_runtime_error_hides_exception_and_uses_session_fallback(caplog, monkeypatch, owned_ids):
    caplog.set_level(logging.ERROR, logger="routes.history_routes")
    state = _block_history_db(monkeypatch)
    calls = _patch_compact(
        monkeypatch,
        llm=RuntimeError(_COMPACT_SECRET),
        resolve=(None, None, None),
    )
    session_id = _add_session("alice")
    owned_ids.append(session_id)
    session = _session(7)
    original = list(session.history)
    manager = _Manager(session=session)
    resp = _post("alice", manager, session_id)

    _assert_fixed(resp)
    assert manager.saves == 0
    assert state["opens"] == 0
    assert session.history == original
    assert calls["resolve"] == [(("utility",), {})]
    assert calls["ctx"] == [(_ENDPOINT, _MODEL)]
    args, kwargs = calls["llm"][0]
    assert args[0] == _ENDPOINT
    assert args[1] == _MODEL
    assert kwargs["headers"] == _SESSION_HEADERS
    assert kwargs["temperature"] == 0.2
    assert kwargs["max_tokens"] == 1024
    assert kwargs["timeout"] == 30
    assert "owner" not in kwargs
    assert _COMPACT_SECRET in caplog.text
    assert f"{_LOG} for {session_id}" in caplog.text


def test_http_exception_from_llm_is_reraised(caplog, monkeypatch, owned_ids):
    caplog.set_level(logging.ERROR, logger="routes.history_routes")
    state = _block_history_db(monkeypatch)
    calls = _patch_compact(
        monkeypatch,
        llm=HTTPException(status_code=409, detail=_REFUSED),
    )
    session_id = _add_session("alice")
    owned_ids.append(session_id)
    session = _session(7)
    original = list(session.history)
    manager = _Manager(session=session)
    resp = _post("alice", manager, session_id)

    assert resp.status_code == 409
    assert resp.json()["detail"] == _REFUSED
    assert "error" not in resp.json()
    assert _FIXED_ERROR not in resp.text
    assert _LOG not in caplog.text
    assert _FIXED_ERROR not in caplog.text
    assert manager.saves == 0
    assert state["opens"] == 0
    assert session.history == original
    assert calls["resolve"] == [(("utility",), {})]
    _assert_no_secrets(resp)


def test_sessionlocal_failure_leaves_replaced_history(caplog, monkeypatch, owned_ids):
    caplog.set_level(logging.ERROR, logger="routes.history_routes")
    state = {"opens": 0}

    def tracking():
        state["opens"] += 1
        raise RuntimeError(_COMPACT_SECRET)

    monkeypatch.setattr(history_routes, "SessionLocal", tracking)
    _patch_compact(monkeypatch, llm="dense summary")
    session_id = _add_session("alice", message_count=7)
    owned_ids.append(session_id)
    for i in range(7):
        _add_message(
            session_id,
            f"m{i}",
            datetime(2026, 11, 21, tzinfo=timezone.utc) + timedelta(seconds=i),
            role="user" if i % 2 == 0 else "assistant",
        )
    before, before_count = _rows(session_id)
    session = _session(7)
    manager = _Manager(session=session)
    resp = _post("alice", manager, session_id)

    _assert_fixed(resp)
    assert state["opens"] == 1
    assert manager.saves == 0
    assert len(session.history) == 6
    assert session.history[0].role == "system"
    assert session.history[0].metadata == {"compacted": True, "hidden": True}
    assert "dense summary" in session.history[0].content
    assert session.history[1].role == "assistant"
    assert session.message_count == 6
    assert [m.content for m in session.history[2:]] == ["c3", "c4", "c5", "c6"]
    assert _rows(session_id) == (before, before_count)
    assert before_count == 7
    assert _COMPACT_SECRET in caplog.text
    assert f"{_LOG} for {session_id}" in caplog.text


def test_save_failure_after_commit_keeps_db_changes(caplog, monkeypatch, owned_ids):
    caplog.set_level(logging.ERROR, logger="routes.history_routes")
    calls = _patch_compact(monkeypatch, llm="dense summary", tokens=[100, 50])
    session_id = _add_session("alice", message_count=7)
    owned_ids.append(session_id)
    for i in range(7):
        _add_message(
            session_id,
            f"m{i}",
            datetime(2026, 11, 21, tzinfo=timezone.utc) + timedelta(seconds=i),
            role="user" if i % 2 == 0 else "assistant",
        )
    session = _session(7)
    manager = _Manager(session=session, save_exc=RuntimeError(_COMPACT_SECRET))
    resp = _post("alice", manager, session_id)

    _assert_fixed(resp)
    assert manager.saves == 1
    stored, count = _rows(session_id)
    assert count == 6
    assert [item[1] for item in stored[:4]] == ["m3", "m4", "m5", "m6"]
    systems = [item for item in stored if item[0] == "system"]
    visible = [item for item in stored if item[1].startswith("**Conversation compacted**")]
    assert len(systems) == 1
    assert "dense summary" in systems[0][1]
    assert json.loads(systems[0][2]) == {"compacted": True, "hidden": True}
    assert len(visible) == 1
    assert visible[0][0] == "assistant"
    assert visible[0][1] == "**Conversation compacted** — 3 messages summarized, 4 kept."
    assert len(session.history) == 6
    assert session.message_count == 6
    assert calls["resolve"] == [(("utility",), {})]
    assert _COMPACT_SECRET in caplog.text
    assert f"{_LOG} for {session_id}" in caplog.text


def test_success_uses_utility_endpoint_and_keeps_tail(monkeypatch, owned_ids):
    calls = _patch_compact(monkeypatch, llm="dense summary", tokens=[200, 40])
    session_id = _add_session("alice", message_count=7)
    owned_ids.append(session_id)
    for i in range(7):
        _add_message(
            session_id,
            f"m{i}",
            datetime(2026, 11, 21, tzinfo=timezone.utc) + timedelta(seconds=i),
            role="user" if i % 2 == 0 else "assistant",
        )
    session = _session(7)
    manager = _Manager(session=session)
    resp = _post("alice", manager, session_id)

    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "status": "ok",
        "message": "Compacted: 7 msgs → 6 msgs (20.0% → 4.0%)",
        "before": 20.0,
        "after": 4.0,
    }
    assert "error" not in body
    assert manager.saves == 1
    assert manager.lookups == [session_id]
    assert calls["resolve"] == [(("utility",), {})]
    assert calls["ctx"] == [(_ENDPOINT, _MODEL)]
    args, kwargs = calls["llm"][0]
    assert args[0] == _UTIL_URL
    assert args[1] == _UTIL_MODEL
    assert kwargs["headers"] == _UTIL_HEADERS
    assert kwargs["temperature"] == 0.2
    assert kwargs["max_tokens"] == 1024
    assert kwargs["timeout"] == 30
    assert "owner" not in kwargs
    assert "USER: c0" in args[2][1]["content"]
    assert "c3" not in args[2][1]["content"]
    assert session.history[0].metadata["hidden"] is True
    assert [m.content for m in session.history[2:]] == ["c3", "c4", "c5", "c6"]
    stored, count = _rows(session_id)
    assert count == 6
    assert [item[1] for item in stored[:4]] == ["m3", "m4", "m5", "m6"]
    assert "m0" not in [item[1] for item in stored]
    assert any(item[0] == "system" and "dense summary" in item[1] for item in stored)
    _assert_no_secrets(resp)


def test_history_compact_keeps_the_fixed_sentence_and_leaves_neighbors():
    history = (_REPO / "routes/history_routes.py").read_text(encoding="utf-8")
    assert "def history_error(message: str, status_code: int = 502, **extra):" in history
    assert history.count("def history_error(") == 1
    assert "from fastapi.responses import JSONResponse" in history
    assert 'logger.error(f"Manual compact error {session_id}: {e}")' not in history
    history_start = history.index("    async def get_session_history(")
    truncate_start = history.index("    async def truncate_session(")
    add_start = history.index("    async def add_message(")
    delete_start = history.index("    async def delete_messages(")
    edit_start = history.index("    async def edit_message(")
    mark_start = history.index("    async def mark_stopped(")
    update_start = history.index("    async def update_last_meta(")
    merge_start = history.index("    async def merge_last_assistant(")
    fork_start = history.index("    async def fork_session(")
    topics_start = history.index("    async def get_conversation_topics(")
    compact_start = history.index("    async def compact_session(")
    loaded = history[history_start:truncate_start]
    truncate = history[truncate_start:add_start]
    added = history[add_start:delete_start]
    deleted = history[delete_start:edit_start]
    edited = history[edit_start:mark_start]
    marked = history[mark_start:update_start]
    updated = history[update_start:merge_start]
    merged = history[merge_start:fork_start]
    forked = history[fork_start:topics_start]
    topics = history[topics_start:compact_start]
    compact = history[compact_start:]

    assert "history_error" not in loaded
    assert 'logger.error(f"DB fallback failed for {session_id}: {e}")' in loaded

    assert "history_error" not in truncate
    assert "except HTTPException:" not in truncate
    assert 'logger.error(f"Truncate error {session_id}: {e}")' in truncate
    assert "raise HTTPException(500, str(e))" in truncate

    assert "history_error" not in deleted
    assert "except HTTPException:" not in deleted
    assert "raise HTTPException(500, str(e))" in deleted

    assert "history_error" not in edited
    assert "except HTTPException:" in edited
    assert "raise HTTPException(500, str(e))" in edited

    assert "history_error" not in marked
    assert "except HTTPException:" not in marked
    assert "raise HTTPException(500, str(e))" in marked

    assert "history_error" not in updated
    assert "except HTTPException:" not in updated
    assert "raise HTTPException(500, str(e))" in updated

    assert "history_error" not in merged
    assert "except HTTPException:" not in merged
    assert "raise HTTPException(500, str(e))" in merged

    assert "history_error" not in forked
    assert 'logger.error(f"Fork error {session_id}: {e}")' in forked
    assert "raise HTTPException(500, str(e))" in forked
    assert forked.count("except HTTPException:") == 1
    assert forked.count("except Exception:") == 1
    assert forked.count("except Exception as e:") == 1
    http_at = forked.index("except HTTPException:")
    assert forked.index("except Exception:") < http_at
    assert forked.index("except Exception as e:") > http_at

    assert "history_error" not in topics
    assert 'raise HTTPException(500, f"Topic analysis failed: {e}")' in topics
    assert "except HTTPException:" not in topics
    assert topics.index("require_user(request)") < topics.index("try:")
    assert "analyze_topics(session_manager, owner=user or None)" in topics

    assert 'history_error("Failed to compact session")' in compact
    assert 'logger.exception("Manual compact error for %s", session_id)' in compact
    assert 'HTTPException(404, "Session not found")' in compact
    assert 'return {"status": "ok", "message": "Not enough messages to compact"}' in compact
    assert "keep_count = 4" in compact
    assert 'resolve_endpoint("utility")' in compact
    assert "temperature=0.2, max_tokens=1024" in compact
    assert "timeout=30" in compact
    assert "db.rollback()" not in compact
    assert "request.json()" not in compact
    assert "str(e)" not in compact
    assert "{e}" not in compact
    assert "HTTPException(500" not in compact
    assert compact.count("except HTTPException:") == 1
    assert compact.count("except Exception:") == 1
    assert compact.count("except Exception as e:") == 0
    assert compact.count("except KeyError:") == 1
    owner_at = compact.index("_verify_session_owner(request, session_id)")
    get_at = compact.index("session = session_manager.get_session(session_id)")
    key_at = compact.index("except KeyError:")
    second_at = compact.index("try:", key_at)
    short_at = compact.index("len(session.history) < 6")
    resolve_at = compact.index('resolve_endpoint("utility")')
    assign_at = compact.index("session.history = new_history")
    local_at = compact.index("db = SessionLocal()")
    close_at = compact.index("db.close()")
    save_at = compact.index("session_manager.save_sessions()")
    refused_at = compact.index("except HTTPException:")
    generic_at = compact.index("except Exception:")
    log_at = compact.index('logger.exception("Manual compact error for %s", session_id)')
    err_at = compact.index('history_error("Failed to compact session")')
    assert owner_at < get_at < key_at < second_at < short_at
    assert short_at < resolve_at < assign_at < local_at < close_at < save_at
    assert save_at < refused_at < generic_at < log_at < err_at
    assert "except KeyError:" not in compact[second_at:]
    assert "owner=" not in compact[resolve_at:resolve_at + 40]
    assert history.count('history_error("Failed to compact session")') == 1
    assert 'history_error("Failed to analyze topics")' not in history
    assert 'history_error("Failed to fork session")' not in history
    assert "history_error" not in added

    session_routes = (_REPO / "routes/session_routes.py").read_text(encoding="utf-8")
    live_start = session_routes.index("    async def compact_session(")
    live_end = session_routes.index('    @router.post("/sessions/auto-sort")', live_start)
    live = session_routes[live_start:live_end]
    assert 'raise HTTPException(500, "Compaction failed")' in live
    assert 'logger.error("Manual compaction failed: %s", e)' in live
    assert "str(e)" not in live
    assert "history_error" not in session_routes

    chat = (_REPO / "static/js/chat.js").read_text(encoding="utf-8")
    assert chat.count("merge-last-assistant failed:") == 1
    assert chat.count("update-last-meta") == 5
    assert chat.count("mark-stopped failed:") == 2
    assert "/api/conversations/topics" not in chat

    slash = (_REPO / "static/js/slashCommands.js").read_text(encoding="utf-8")
    slash_fn = slash.index("async function _cmdCompact(")
    slash_end = slash.index("async function _cmdTts(")
    slash_compact = slash[slash_fn:slash_end]
    assert "let detail = 'Compaction failed';" in slash_compact
    assert "detail = err.detail || detail;" in slash_compact
    assert "/compact" in slash_compact
    assert "history_error" not in slash_compact

    renderer = (_REPO / "static/js/chatRenderer.js").read_text(encoding="utf-8")
    render_at = renderer.index("ctx-compact-btn")
    render_end = renderer.index("const rect = ctxRing.getBoundingClientRect();", render_at)
    render_compact = renderer[render_at:render_end]
    assert "Compaction failed. Try again later." in render_compact
    assert "Compaction failed: " in render_compact
    assert "err.message" in render_compact
    ok_at = render_compact.index("if (res.ok)")
    catch_at = render_compact.index("} catch (err)")
    assert "res.json()" not in render_compact[ok_at:catch_at].split("else", 1)[1]
    assert "history_error" not in render_compact

    skills = (_REPO / "routes/skills_routes.py").read_text(encoding="utf-8")
    assert 'return {"builtin": [], "count": 0, "error": str(e)}' in skills
    assert "def skill_error(" not in skills

    upload = (_REPO / "routes/upload_routes.py").read_text(encoding="utf-8")
    assert 'raise HTTPException(500, f"Vision analysis failed: {e}")' in upload
    assert "def vision_error(" not in upload

    drafts = (_REPO / "routes/editor_draft_routes.py").read_text(encoding="utf-8")
    assert "raise HTTPException(500, str(e))" in drafts
    assert "def draft_error(" not in drafts
