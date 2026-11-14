"""POST /api/session/{id}/delete-messages must not return exception text.

An unexpected failure is HTTP 502 {"error": "Failed to delete messages"}.
The exception is logged and is not returned. An HTTPException raised inside
the handler is still passed through. A missing in-memory session stays
HTTP 404 "Session not found". Deleting by id or by index still returns
{"status": "ok", "deleted": deleted}. A body with neither msg_ids nor
indices still returns {"status": "ok", "deleted": 0}, and that path still
calls get_session first, so a missing in-memory session on an empty body
stays 404.

The handler opens a database session after get_session and closes it in
finally. There is no rollback. Ownership is still checked before the try.
Truncate, add-message, history read, and edit-message were not changed.
The chat client does not read the body.
"""
import logging
import os
import uuid
from datetime import datetime, timedelta
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite:///./data/app.db")
os.makedirs("data", exist_ok=True)

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import routes.history_routes as history_routes
from core.database import ChatMessage as DbChatMessage, Session as DbSession, SessionLocal
from core.models import ChatMessage

_DELETE_SECRET = "DELETE_SECRET session message delete token=do-not-return"
_KEY_SECRET = "KEY_SECRET session missing token=do-not-return"
_JSON_SECRET = "JSON_SECRET session body token=do-not-return"
_QUERY_SECRET = "QUERY_SECRET session query token=do-not-return"
_FIXED_ERROR = "Failed to delete messages"
_SECRETS = (_DELETE_SECRET, _KEY_SECRET, _JSON_SECRET, _QUERY_SECRET)
_REPO = Path(__file__).resolve().parent.parent


class _MemSession:
    def __init__(self, history):
        self.history = list(history)
        self.message_count = len(self.history)


class _Manager:
    def __init__(self, session=None, exc=None):
        self.session = session if session is not None else _MemSession([])
        self.exc = exc
        self.calls = []

    def get_session(self, session_id):
        self.calls.append(session_id)
        if self.exc is not None:
            raise self.exc
        return self.session


class _FailSession:
    def __init__(self, exc):
        self.exc = exc
        self.closed = False
        self.rollbacks = 0

    def query(self, *args, **kwargs):
        raise self.exc

    def close(self):
        self.closed = True

    def rollback(self):
        self.rollbacks += 1

    def commit(self):
        raise AssertionError("commit should not run")


def _client(username, manager):
    app = FastAPI()

    @app.middleware("http")
    async def _stamp_user(request, call_next):
        if username is not None:
            request.state.current_user = username
        return await call_next(request)

    app.include_router(history_routes.setup_history_routes(manager))
    return TestClient(app)


def _add_session(owner, name="Project", message_count=0):
    db = SessionLocal()
    try:
        session_id = str(uuid.uuid4())
        db.add(DbSession(
            id=session_id,
            name=name,
            endpoint_url="http://127.0.0.1:9",
            model="test-model",
            owner=owner,
            message_count=message_count,
        ))
        db.commit()
        return session_id
    finally:
        db.close()


def _add_message(session_id, content, when):
    db = SessionLocal()
    try:
        msg_id = str(uuid.uuid4())
        db.add(DbChatMessage(
            id=msg_id,
            session_id=session_id,
            role="user",
            content=content,
            timestamp=when,
        ))
        db.commit()
        return msg_id
    finally:
        db.close()


def _message_ids(session_id):
    db = SessionLocal()
    try:
        rows = (
            db.query(DbChatMessage)
            .filter(DbChatMessage.session_id == session_id)
            .order_by(DbChatMessage.timestamp)
            .all()
        )
        return [row.id for row in rows]
    finally:
        db.close()


def _stored_count(session_id):
    db = SessionLocal()
    try:
        row = db.query(DbSession).filter(DbSession.id == session_id).one()
        return row.message_count
    finally:
        db.close()


def _delete_rows(session_ids):
    db = SessionLocal()
    try:
        if session_ids:
            db.query(DbChatMessage).filter(
                DbChatMessage.session_id.in_(list(session_ids))
            ).delete(synchronize_session=False)
            db.query(DbSession).filter(DbSession.id.in_(list(session_ids))).delete(
                synchronize_session=False
            )
        db.commit()
    finally:
        db.close()


def _delete(username, session_id, manager, body=None, raw=None):
    client = _client(username, manager)
    url = f"/api/session/{session_id}/delete-messages"
    if raw is not None:
        return client.post(
            url,
            content=raw,
            headers={"Content-Type": "application/json"},
        )
    return client.post(url, json={} if body is None else body)


def _assert_no_secrets(resp):
    for secret in _SECRETS:
        assert secret not in resp.text


def _assert_fixed(resp):
    assert resp.status_code == 502
    assert resp.json() == {"error": _FIXED_ERROR}
    assert "detail" not in resp.json()
    assert "Session message delete failed" not in resp.text
    _assert_no_secrets(resp)


def _memory(msg_id, content):
    return ChatMessage(role="user", content=content, metadata={"_db_id": msg_id})


def test_delete_by_ids_removes_only_matching_rows():
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project", message_count=5)
    other_id = _add_session(owner, name="Theirs", message_count=1)
    start = datetime(2026, 1, 1, 0, 0, 0)
    kept_id = _add_message(session_id, "kept", start)
    gone_id = _add_message(session_id, "gone", start + timedelta(seconds=1))
    foreign_id = _add_message(other_id, "foreign", start)
    memory = _MemSession([_memory(gone_id, "gone"), _memory(kept_id, "kept")])
    manager = _Manager(session=memory)
    try:
        resp = _delete(owner, session_id, manager, body={"msg_ids": [gone_id, foreign_id]})
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok", "deleted": 1}
        assert "error" not in resp.json()
        assert manager.calls == [session_id]
        assert _message_ids(session_id) == [kept_id]
        assert _message_ids(other_id) == [foreign_id]
        assert [msg.metadata["_db_id"] for msg in memory.history] == [kept_id]
        assert memory.message_count == 1
        assert _stored_count(session_id) == 1
        _assert_no_secrets(resp)
    finally:
        _delete_rows([session_id, other_id])


def test_unmatched_ids_report_zero_and_still_commit():
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project", message_count=9)
    kept_id = _add_message(session_id, "kept", datetime(2026, 1, 1, 0, 0, 0))
    memory = _MemSession([_memory(kept_id, "kept")])
    manager = _Manager(session=memory)
    try:
        resp = _delete(owner, session_id, manager, body={"msg_ids": ["missing-id"]})
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok", "deleted": 0}
        assert "error" not in resp.json()
        assert manager.calls == [session_id]
        assert _message_ids(session_id) == [kept_id]
        assert len(memory.history) == 1
        assert memory.message_count == 1
        assert _stored_count(session_id) == 1
        _assert_no_secrets(resp)
    finally:
        _delete_rows([session_id])


def test_delete_by_indices_removes_high_index_first():
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project", message_count=2)
    start = datetime(2026, 1, 1, 0, 0, 0)
    first_id = _add_message(session_id, "first", start)
    second_id = _add_message(session_id, "second", start + timedelta(seconds=1))
    memory = _MemSession([
        _memory(first_id, "first"),
        _memory(second_id, "second"),
    ])
    manager = _Manager(session=memory)
    try:
        resp = _delete(owner, session_id, manager, body={"indices": [0, 1]})
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok", "deleted": 2}
        assert "error" not in resp.json()
        assert manager.calls == [session_id]
        assert _message_ids(session_id) == []
        assert memory.history == []
        assert memory.message_count == 0
        assert _stored_count(session_id) == 0
        _assert_no_secrets(resp)
    finally:
        _delete_rows([session_id])


def test_neither_ids_nor_indices_deletes_nothing():
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project", message_count=9)
    kept_id = _add_message(session_id, "kept", datetime(2026, 1, 1, 0, 0, 0))
    memory = _MemSession([_memory(kept_id, "kept")])
    manager = _Manager(session=memory)
    try:
        resp = _delete(owner, session_id, manager, body={})
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok", "deleted": 0}
        assert "error" not in resp.json()
        assert manager.calls == [session_id]
        assert _message_ids(session_id) == [kept_id]
        assert len(memory.history) == 1
        assert memory.message_count == 1
        assert _stored_count(session_id) == 9
        _assert_no_secrets(resp)
    finally:
        _delete_rows([session_id])


def test_empty_body_missing_session_is_404(caplog):
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project")
    manager = _Manager(exc=KeyError(_KEY_SECRET))
    caplog.set_level(logging.ERROR, logger="routes.history_routes")
    try:
        resp = _delete(owner, session_id, manager, body={})
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Session not found"
        assert "error" not in resp.json()
        assert manager.calls == [session_id]
        _assert_no_secrets(resp)
        assert "Session message delete failed" not in caplog.text
        assert _FIXED_ERROR not in caplog.text
    finally:
        _delete_rows([session_id])


def test_keyerror_hides_message(caplog):
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project")
    kept_id = _add_message(session_id, "kept", datetime(2026, 1, 1, 0, 0, 0))
    manager = _Manager(exc=KeyError(_KEY_SECRET))
    caplog.set_level(logging.ERROR, logger="routes.history_routes")
    try:
        resp = _delete(owner, session_id, manager, body={"msg_ids": [kept_id]})
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Session not found"
        assert "error" not in resp.json()
        assert manager.calls == [session_id]
        assert _message_ids(session_id) == [kept_id]
        _assert_no_secrets(resp)
        assert "Session message delete failed" not in caplog.text
        assert _FIXED_ERROR not in caplog.text
    finally:
        _delete_rows([session_id])


def test_delete_failure_hides_exception(caplog):
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project")
    kept_id = _add_message(session_id, "kept", datetime(2026, 1, 1, 0, 0, 0))
    manager = _Manager(exc=RuntimeError(_DELETE_SECRET))
    caplog.set_level(logging.ERROR, logger="routes.history_routes")
    try:
        resp = _delete(owner, session_id, manager, body={"msg_ids": [kept_id]})
        _assert_fixed(resp)
        assert manager.calls == [session_id]
        assert _message_ids(session_id) == [kept_id]
        assert _DELETE_SECRET in caplog.text
        assert f"Session message delete failed for {session_id}" in caplog.text
    finally:
        _delete_rows([session_id])


def test_query_failure_hides_exception_and_does_not_rollback(caplog, monkeypatch):
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project", message_count=4)
    kept_id = _add_message(session_id, "kept", datetime(2026, 1, 1, 0, 0, 0))
    memory = _MemSession([_memory(kept_id, "kept")])
    manager = _Manager(session=memory)
    failed = _FailSession(RuntimeError(_QUERY_SECRET))
    monkeypatch.setattr(history_routes, "SessionLocal", lambda: failed)
    caplog.set_level(logging.ERROR, logger="routes.history_routes")
    try:
        resp = _delete(owner, session_id, manager, body={"msg_ids": [kept_id]})
        _assert_fixed(resp)
        assert manager.calls == [session_id]
        assert failed.closed is True
        assert failed.rollbacks == 0
        assert _message_ids(session_id) == [kept_id]
        assert len(memory.history) == 1
        assert _stored_count(session_id) == 4
        assert _QUERY_SECRET in caplog.text
        assert f"Session message delete failed for {session_id}" in caplog.text
    finally:
        _delete_rows([session_id])


def test_invalid_json_hides_exception(caplog):
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project")
    manager = _Manager()
    caplog.set_level(logging.ERROR, logger="routes.history_routes")
    try:
        resp = _delete(
            owner,
            session_id,
            manager,
            raw=f"{_JSON_SECRET} not-json".encode(),
        )
        _assert_fixed(resp)
        assert manager.calls == []
        assert f"Session message delete failed for {session_id}" in caplog.text
    finally:
        _delete_rows([session_id])


def test_non_object_body_hides_exception(caplog):
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project")
    manager = _Manager()
    caplog.set_level(logging.ERROR, logger="routes.history_routes")
    try:
        resp = _delete(owner, session_id, manager, body=["not", "an", "object"])
        _assert_fixed(resp)
        assert manager.calls == []
        assert "'list' object has no attribute 'get'" not in resp.text
        assert f"Session message delete failed for {session_id}" in caplog.text
    finally:
        _delete_rows([session_id])


def test_http_exception_is_reraised(caplog, monkeypatch):
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project")
    kept_id = _add_message(session_id, "kept", datetime(2026, 1, 1, 0, 0, 0))
    memory = _MemSession([_memory(kept_id, "kept")])
    manager = _Manager(session=memory)
    failed = _FailSession(HTTPException(status_code=409, detail="Session message delete refused"))
    monkeypatch.setattr(history_routes, "SessionLocal", lambda: failed)
    caplog.set_level(logging.ERROR, logger="routes.history_routes")
    try:
        resp = _delete(owner, session_id, manager, body={"msg_ids": [kept_id]})
        assert resp.status_code == 409
        assert resp.json()["detail"] == "Session message delete refused"
        assert "error" not in resp.json()
        assert manager.calls == [session_id]
        assert failed.closed is True
        assert failed.rollbacks == 0
        assert _message_ids(session_id) == [kept_id]
        _assert_no_secrets(resp)
        assert "Session message delete failed" not in caplog.text
        assert _FIXED_ERROR not in caplog.text
    finally:
        _delete_rows([session_id])


def test_other_owner_null_owner_and_missing_row_stay_owner_404():
    owner = f"health-{uuid.uuid4().hex}"
    stranger = f"health-{uuid.uuid4().hex}"
    theirs = _add_session(stranger, name="Theirs")
    legacy = _add_session(None, name="Legacy")
    missing = str(uuid.uuid4())
    manager = _Manager()
    try:
        other = _delete(owner, theirs, manager, body={"msg_ids": ["x"]})
        blank = _delete(owner, legacy, manager, body={"msg_ids": ["x"]})
        gone = _delete(owner, missing, manager, body={"msg_ids": ["x"]})
        assert other.status_code == 404
        assert other.json()["detail"] == f"Session {theirs} not found"
        assert blank.status_code == 404
        assert blank.json()["detail"] == f"Session {legacy} not found"
        assert gone.status_code == 404
        assert gone.json()["detail"] == f"Session {missing} not found"
        assert "error" not in other.json()
        assert "error" not in blank.json()
        assert "error" not in gone.json()
        assert manager.calls == []
        _assert_no_secrets(other)
        _assert_no_secrets(blank)
        _assert_no_secrets(gone)
    finally:
        _delete_rows([theirs, legacy])


def test_anonymous_is_403():
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project")
    manager = _Manager()
    try:
        resp = _delete(None, session_id, manager, body={"msg_ids": ["x"]})
        assert resp.status_code == 403
        assert resp.json()["detail"] == "Authentication required"
        assert "error" not in resp.json()
        assert manager.calls == []
        _assert_no_secrets(resp)
    finally:
        _delete_rows([session_id])


def test_delete_keeps_the_fixed_sentence_and_leaves_neighbors():
    history = (_REPO / "routes/history_routes.py").read_text(encoding="utf-8")
    assert "def history_error(message: str, status_code: int = 502, **extra):" in history
    assert history.count("def history_error(") == 1
    history_start = history.index("    async def get_session_history(")
    truncate_start = history.index("    async def truncate_session(")
    add_start = history.index("    async def add_message(")
    delete_start = history.index("    async def delete_messages(")
    edit_start = history.index("    async def edit_message(")
    mark_start = history.index("    async def mark_stopped(")
    loaded = history[history_start:truncate_start]
    truncate = history[truncate_start:add_start]
    added = history[add_start:delete_start]
    deleted = history[delete_start:edit_start]
    edited = history[edit_start:mark_start]

    assert "history_error" not in loaded
    assert 'logger.error(f"DB fallback failed for {session_id}: {e}")' in loaded
    assert "SessionLocal" in loaded

    assert "history_error" not in truncate
    assert "except HTTPException:" not in truncate
    assert 'logger.error(f"Truncate error {session_id}: {e}")' in truncate
    assert "raise HTTPException(500, str(e))" in truncate
    assert 'HTTPException(404, "Session not found")' in truncate
    assert 'return {"status": "ok", "kept": keep_count, "truncated": result}' in truncate
    assert "db.rollback()" not in truncate
    assert "SessionLocal" not in truncate
    owner_at = truncate.index("_verify_session_owner(request, session_id)")
    try_at = truncate.index("try:")
    assert owner_at < try_at

    assert "history_error" not in added
    assert "except HTTPException:" not in added
    assert 'raise HTTPException(400, "content is required")' in added
    assert "except KeyError:" in added
    assert 'raise HTTPException(404, "Session not found")' in added
    assert 'return {"status": "ok"}' in added
    assert "{e}" not in added
    assert "str(e)" not in added

    owner_at = deleted.index("_verify_session_owner(request, session_id)")
    try_at = deleted.index("try:")
    assert owner_at < try_at
    assert "except HTTPException:" in deleted
    assert 'history_error("Failed to delete messages")' in deleted
    assert 'logger.exception("Session message delete failed for %s", session_id)' in deleted
    assert 'HTTPException(404, "Session not found")' in deleted
    assert 'return {"status": "ok", "deleted": deleted}' in deleted
    assert 'return {"status": "ok", "deleted": 0}' in deleted
    assert "db.close()" in deleted
    assert "SessionLocal" in deleted
    assert "{e}" not in deleted
    assert "str(e)" not in deleted
    assert "HTTPException(500" not in deleted
    assert "db.rollback()" not in deleted
    assert history.count('history_error("Failed to delete messages")') == 1
    assert 'logger.error(f"Delete messages error {session_id}: {e}")' not in deleted

    assert "history_error" not in edited
    assert "except HTTPException:" in edited
    assert 'logger.error(f"Edit message error {session_id}: {e}")' in edited
    assert "raise HTTPException(500, str(e))" in edited
    assert 'raise HTTPException(400, "msg_id and content are required")' in edited
    assert 'raise HTTPException(404, "Message not found")' in edited
    assert 'raise HTTPException(404, "Session not found")' in edited
    assert 'return {"status": "ok"}' in edited
    assert "db.rollback()" not in edited
    assert "db.close()" in edited

    chat = (_REPO / "static/js/chat.js").read_text(encoding="utf-8")
    delete_start = chat.index("  export async function deleteMessage(")
    edit_ai_start = chat.index("  export async function editAIMessage(")
    delete_client = chat[delete_start:edit_ai_start]
    assert "method: 'POST'" in delete_client
    assert "api/session/${sessionId}/delete-messages" in delete_client
    assert "res.ok" in delete_client
    assert "Server error " in delete_client
    assert "Delete failed: " in delete_client
    assert ".json(" not in delete_client
    assert ".text(" not in delete_client

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
