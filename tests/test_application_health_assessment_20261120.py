"""GET /api/conversations/topics must not return exception text.

An unexpected failure is HTTP 502 {"error": "Failed to analyze topics"}.
The exception is logged and is not returned. An HTTPException raised inside
the handler is still passed through. A KeyError from analyze_topics is that
502, not a 404. require_user stays outside the try. An anonymous TestClient
(host testclient) stays HTTP 401 "Not authenticated" and does not call
analyze_topics. Success returns the analyzer dict unchanged. owner is
`user or None`. The handler does not read a body, does not open
SessionLocal, and does not roll back. Fork, merge, metadata update,
mark-stopped, edit, delete, truncate, history read, and both compact
handlers were not changed. Clients were not changed.
"""
import logging
import os
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite:///./data/app.db")
os.makedirs("data", exist_ok=True)

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import routes.history_routes as history_routes

_TOPIC_SECRET = "TOPIC_SECRET topic analysis token=do-not-return"
_KEY_SECRET = "KEY_SECRET session missing token=do-not-return"
_FIXED_ERROR = "Failed to analyze topics"
_LOG = "Topic analysis failed"
_REFUSED = "Topic analysis refused"
_REPO = Path(__file__).resolve().parent.parent


class _RaisingSessions(dict):
    def items(self):
        raise RuntimeError(_TOPIC_SECRET)


class _Manager:
    def __init__(self, sessions=None):
        self.sessions = {} if sessions is None else sessions
        self.saves = 0
        self.hydrated = []

    def get_session(self, session_id):
        self.hydrated.append(session_id)
        self.sessions[session_id]["history"] = [
            {"role": "user", "content": _KEY_SECRET}
        ]
        raise KeyError(_KEY_SECRET)

    def save_sessions(self):
        self.saves += 1
        raise RuntimeError("save_sessions should not run")


@pytest.fixture(autouse=True)
def _guard_history_sessionlocal(monkeypatch):
    state = {"opens": 0}

    def tracking():
        state["opens"] += 1
        raise RuntimeError("history SessionLocal opened")

    monkeypatch.setattr(history_routes, "SessionLocal", tracking)
    yield state
    assert state["opens"] == 0


def _client(username, manager):
    app = FastAPI()

    @app.middleware("http")
    async def _stamp_user(request, call_next):
        if username is not None:
            request.state.current_user = username
        return await call_next(request)

    app.include_router(history_routes.setup_history_routes(manager))
    return TestClient(app)


def _get(username, manager):
    return _client(username, manager).get("/api/conversations/topics")


def _assert_no_secrets(resp):
    assert _TOPIC_SECRET not in resp.text
    assert _KEY_SECRET not in resp.text
    assert _LOG not in resp.text


def _assert_fixed(resp):
    assert resp.status_code == 502
    assert resp.json() == {"error": _FIXED_ERROR}
    assert "detail" not in resp.json()
    assert "Session not found" not in resp.text
    _assert_no_secrets(resp)


def test_anonymous_testclient_stays_401(caplog, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("LOCALHOST_BYPASS", "false")
    caplog.set_level(logging.ERROR, logger="routes.history_routes")
    seen = {"host": None, "calls": 0}

    def boom(session_manager, owner=None):
        seen["calls"] += 1
        raise RuntimeError(_TOPIC_SECRET)

    monkeypatch.setattr(history_routes, "analyze_topics", boom)

    app = FastAPI()

    @app.middleware("http")
    async def _record(request, call_next):
        seen["host"] = request.client.host if request.client else None
        return await call_next(request)

    manager = _Manager()
    app.include_router(history_routes.setup_history_routes(manager))
    resp = TestClient(app).get("/api/conversations/topics")

    assert seen["host"] == "testclient"
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Not authenticated"
    assert "error" not in resp.json()
    assert seen["calls"] == 0
    assert manager.hydrated == []
    assert manager.saves == 0
    assert _LOG not in caplog.text
    assert _FIXED_ERROR not in caplog.text
    _assert_no_secrets(resp)


def test_blank_user_passes_owner_none_and_does_not_scan(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    calls = []
    real = history_routes.analyze_topics

    def wrapped(session_manager, owner=None):
        calls.append(owner)
        return real(session_manager, owner=owner)

    monkeypatch.setattr(history_routes, "analyze_topics", wrapped)
    manager = _Manager(sessions=_RaisingSessions({
        "s-alice": {
            "owner": "alice",
            "name": "Project",
            "archived": False,
            "history": [{"role": "user", "content": "python code"}],
        }
    }))
    resp = _get(None, manager)

    assert calls == [None]
    assert resp.status_code == 200
    assert resp.json() == {"topics": [], "total_topics": 0}
    assert "error" not in resp.json()
    assert "status" not in resp.json()
    assert manager.hydrated == []
    assert manager.saves == 0
    _assert_no_secrets(resp)


def test_success_returns_analyzer_dict_unchanged(monkeypatch):
    payload = {
        "topics": [{
            "topic": "Work",
            "frequency": 2,
            "examples": [{"snippet": "ship the report"}],
            "session_count": 1,
        }],
        "total_topics": 1,
        "extra": "kept",
    }
    calls = []

    def fake(session_manager, owner=None):
        calls.append(owner)
        session_manager.sessions["touched"] = True
        return payload

    monkeypatch.setattr(history_routes, "analyze_topics", fake)
    manager = _Manager()
    resp = _get("alice", manager)

    assert calls == ["alice"]
    assert resp.status_code == 200
    assert resp.json() == payload
    assert manager.sessions["touched"] is True
    assert manager.hydrated == []
    assert manager.saves == 0
    _assert_no_secrets(resp)


def test_runtime_error_hides_exception_and_keeps_hydration(caplog, monkeypatch):
    caplog.set_level(logging.ERROR, logger="routes.history_routes")

    def fake(session_manager, owner=None):
        session_manager.sessions["hydrated"] = {"history": [_TOPIC_SECRET]}
        raise RuntimeError(_TOPIC_SECRET)

    monkeypatch.setattr(history_routes, "analyze_topics", fake)
    manager = _Manager()
    resp = _get("alice", manager)

    _assert_fixed(resp)
    assert manager.sessions["hydrated"] == {"history": [_TOPIC_SECRET]}
    assert manager.saves == 0
    assert _TOPIC_SECRET in caplog.text
    assert f"{_LOG} for alice" in caplog.text


def test_keyerror_after_hydration_is_502_not_404(caplog):
    caplog.set_level(logging.ERROR, logger="routes.history_routes")
    manager = _Manager(sessions={
        "s1": {
            "owner": "alice",
            "archived": False,
            "name": "Project",
            "history": [],
        }
    })
    resp = _get("alice", manager)

    _assert_fixed(resp)
    assert resp.status_code != 404
    assert manager.hydrated == ["s1"]
    assert manager.sessions["s1"]["history"] == [
        {"role": "user", "content": _KEY_SECRET}
    ]
    assert manager.saves == 0
    assert _KEY_SECRET in caplog.text
    assert f"{_LOG} for alice" in caplog.text
    assert "Session not found" not in caplog.text


def test_http_exception_is_reraised(caplog, monkeypatch):
    caplog.set_level(logging.ERROR, logger="routes.history_routes")

    def fake(session_manager, owner=None):
        raise HTTPException(status_code=409, detail=_REFUSED)

    monkeypatch.setattr(history_routes, "analyze_topics", fake)
    manager = _Manager()
    resp = _get("alice", manager)

    assert resp.status_code == 409
    assert resp.json()["detail"] == _REFUSED
    assert "error" not in resp.json()
    assert _FIXED_ERROR not in resp.text
    assert _LOG not in caplog.text
    assert _FIXED_ERROR not in caplog.text
    assert manager.hydrated == []
    assert manager.saves == 0
    _assert_no_secrets(resp)


def test_topics_keeps_the_fixed_sentence_and_leaves_neighbors():
    history = (_REPO / "routes/history_routes.py").read_text(encoding="utf-8")
    assert "def history_error(message: str, status_code: int = 502, **extra):" in history
    assert history.count("def history_error(") == 1
    assert "from fastapi.responses import JSONResponse" in history
    assert 'raise HTTPException(500, f"Topic analysis failed: {e}")' not in history
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
    assert 'body.get("keep_count", 0)' in truncate

    assert "history_error" not in added
    assert "except HTTPException:" not in added
    assert 'raise HTTPException(400, "content is required")' in added
    assert "except KeyError:" in added

    assert "history_error" not in deleted
    assert "except HTTPException:" not in deleted
    assert 'logger.error(f"Delete messages error {session_id}: {e}")' in deleted
    assert "raise HTTPException(500, str(e))" in deleted

    assert "history_error" not in edited
    assert "except HTTPException:" in edited
    assert 'logger.error(f"Edit message error {session_id}: {e}")' in edited
    assert "raise HTTPException(500, str(e))" in edited

    assert "history_error" not in marked
    assert "except HTTPException:" not in marked
    assert 'logger.error(f"Mark stopped error {session_id}: {e}")' in marked
    assert "raise HTTPException(500, str(e))" in marked

    assert "history_error" not in updated
    assert "except HTTPException:" not in updated
    assert 'logger.error(f"Update last meta error {session_id}: {e}")' in updated
    assert "raise HTTPException(500, str(e))" in updated
    assert 'body.get("metadata", {})' in updated

    assert "history_error" not in merged
    assert "except HTTPException:" not in merged
    assert 'logger.error(f"Merge assistant error {session_id}: {e}")' in merged
    assert "raise HTTPException(500, str(e))" in merged
    assert 'return {"status": "ok", "merged": False}' in merged
    assert 'return {"status": "ok", "merged": True}' in merged
    assert "request.json()" in merged
    assert 'body.get("separator", "\\n\\n")' in merged
    assert "db.rollback()" not in merged
    assert merged.index("request.json()") < merged.index("get_session(")

    owner_at = forked.index("_verify_session_owner(request, session_id)")
    try_at = forked.index("try:")
    assert owner_at < try_at
    assert forked.index("request.json()") < forked.index('body.get("keep_count", 0)')
    assert forked.index('body.get("keep_count", 0)') < forked.index("sessions.get(session_id)")
    assert forked.index("sessions.get(session_id)") < forked.index('raise HTTPException(404, "Session not found")')
    assert forked.index('raise HTTPException(404, "Session not found")') < forked.index("create_session(")
    assert forked.index("create_session(") < forked.index("source.history[:keep_count]")
    assert forked.index("source.history[:keep_count]") < forked.index("add_message(")
    assert forked.index("add_message(") < forked.index('fire_event("session_created"')
    assert forked.index('fire_event("session_created"') < forked.index('"status": "ok"')
    assert 'fork_name = f"\\u2ADD {source.name}"' in forked
    assert "ChatMessage(msg.role, msg.content, msg.metadata)" in forked
    assert "getattr(source, 'owner', None)" in forked
    assert "rag=False" in forked
    assert 'logger.debug("session_created event dispatch failed", exc_info=True)' in forked
    assert "history_error" not in forked
    assert 'logger.error(f"Fork error {session_id}: {e}")' in forked
    assert "raise HTTPException(500, str(e))" in forked
    assert "get_session(" not in forked
    assert "save_sessions" not in forked
    assert "SessionLocal" not in forked
    assert "db.rollback()" not in forked
    assert "except KeyError:" not in forked
    assert forked.count("except HTTPException:") == 1
    assert forked.count("except Exception:") == 1
    assert forked.count("except Exception as e:") == 1
    http_at = forked.index("except HTTPException:")
    assert forked.index("except Exception:") < http_at
    assert forked.index("except Exception as e:") > http_at

    user_at = topics.index("require_user(request)")
    topics_try = topics.index("try:")
    assert user_at < topics_try
    assert "analyze_topics(session_manager, owner=user or None)" in topics
    assert 'history_error("Failed to analyze topics")' in topics
    assert 'logger.exception("Topic analysis failed for %s", user)' in topics
    assert "request.json()" not in topics
    assert "SessionLocal" not in topics
    assert "save_sessions" not in topics
    assert "db.rollback()" not in topics
    assert "except KeyError:" not in topics
    assert "{e}" not in topics
    assert "str(e)" not in topics
    assert "HTTPException(500" not in topics
    assert topics.count("except HTTPException:") == 1
    assert topics.count("except Exception:") == 1
    topics_http = topics.index("except HTTPException:")
    assert topics_http > topics_try
    assert topics.index("except Exception:") > topics_http
    assert topics.index('history_error("Failed to analyze topics")') > topics_http
    assert history.count('history_error("Failed to analyze topics")') == 1
    assert 'history_error("Failed to fork session")' not in history
    assert 'history_error("Failed to merge assistant messages")' not in history
    assert 'history_error("Failed to update message metadata")' not in history
    assert 'history_error("Failed to mark message stopped")' not in history

    assert "history_error" not in compact
    assert "except HTTPException:" not in compact
    assert 'logger.error(f"Manual compact error {session_id}: {e}")' in compact
    assert "raise HTTPException(500, str(e))" in compact
    assert 'HTTPException(404, "Session not found")' in compact

    session_routes = (_REPO / "routes/session_routes.py").read_text(encoding="utf-8")
    live_start = session_routes.index("    async def compact_session(")
    live_end = session_routes.index("    @router.post(\"/sessions/auto-sort\")", live_start)
    live = session_routes[live_start:live_end]
    assert 'raise HTTPException(500, "Compaction failed")' in live
    assert 'logger.error("Manual compaction failed: %s", e)' in live
    assert "str(e)" not in live
    assert "history_error" not in session_routes

    chat = (_REPO / "static/js/chat.js").read_text(encoding="utf-8")
    assert chat.count("merge-last-assistant failed:") == 1
    assert chat.count("update-last-meta (variants) failed:") == 1
    assert chat.count("update-last-meta (variantIndex) failed:") == 1
    assert chat.count("update-last-meta") == 5
    assert chat.count("mark-stopped failed:") == 2
    fork_fn = chat.index("  export async function forkFrom(")
    fork_end = chat.index("\n  export async function ", fork_fn + 10)
    fork_client = chat[fork_fn:fork_end]
    assert "api/session/${sessionId}/fork" in fork_client
    assert "await res.text()" in fork_client
    assert "res.ok" in fork_client
    assert "const data = await res.json()" in fork_client
    assert "Fork failed: " in fork_client
    assert "keep_count: keepCount" in fork_client
    assert "history_error" not in fork_client
    assert "/api/conversations/topics" not in chat

    slash = (_REPO / "static/js/slashCommands.js").read_text(encoding="utf-8")
    slash_fn = slash.index("async function _cmdSessionFork(")
    slash_end = slash.index("async function _cmdSessionTruncate(")
    slash_fork = slash[slash_fn:slash_end]
    assert "api/session/${ctx.sid}/fork" in slash_fork
    assert "slashReply('Fork failed')" in slash_fork
    assert "keep_count: keepCount" in slash_fork
    assert ".text(" not in slash_fork
    assert "history_error" not in slash_fork
    assert "/api/conversations/topics" not in slash

    skills = (_REPO / "routes/skills_routes.py").read_text(encoding="utf-8")
    assert 'return {"builtin": [], "count": 0, "error": str(e)}' in skills
    assert "raise HTTPException(500, str(e))" in skills
    assert "def skill_error(" not in skills
    assert "history_error" not in skills

    upload = (_REPO / "routes/upload_routes.py").read_text(encoding="utf-8")
    assert 'raise HTTPException(500, f"Vision analysis failed: {e}")' in upload
    assert "def vision_error(" not in upload
    assert "history_error" not in upload

    docs = (_REPO / "routes/document_routes.py").read_text(encoding="utf-8")
    fill_start = docs.index("    async def ai_fill_annotations(")
    render_start = docs.index("    async def render_pdf(", fill_start)
    fill = docs[fill_start:render_start]
    assert "import fitz" in fill
    assert "No vision model available: {e}" in fill
    assert "document_error" not in fill
    assert "history_error" not in fill

    drafts = (_REPO / "routes/editor_draft_routes.py").read_text(encoding="utf-8")
    draft_delete = drafts.index("    async def delete_draft(")
    draft_end = drafts.index("\n    return router", draft_delete)
    draft_slice = drafts[draft_delete:draft_end]
    assert "raise HTTPException(500, str(e))" in draft_slice
    assert "def draft_error(" not in drafts
    assert "history_error" not in drafts
    assert 'raise HTTPException(500, "Could not save draft")' in drafts
    assert 'logger.warning(f"editor-draft create failed: {e}")' in drafts
    assert 'raise HTTPException(500, "Could not update draft")' in drafts
    assert 'logger.warning(f"editor-draft update failed: {e}")' in drafts
