"""POST /api/session/{id}/edit-message must not return exception text.

An unexpected failure is HTTP 502 {"error": "Failed to edit message"}.
The exception is logged and is not returned. An HTTPException raised inside
the handler is still passed through. A missing in-memory session stays
HTTP 404 "Session not found". A missing msg_id or a null content stays
HTTP 400 "msg_id and content are required", and that check runs before
get_session. A missing row stays HTTP 404 "Message not found". An empty
string is still saved. Success stays {"status": "ok"}.

The handler opens a database session after get_session and closes it in
finally. There is no rollback. Ownership is still checked before the try.
Truncate, add-message, history read, delete-messages, and mark-stopped
were not changed. The chat client does not read the body.
"""
import json
import logging
import os
import uuid
from datetime import datetime
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite:///./data/app.db")
os.makedirs("data", exist_ok=True)

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import routes.history_routes as history_routes
from core.database import ChatMessage as DbChatMessage, Session as DbSession, SessionLocal
from core.models import ChatMessage

_EDIT_SECRET = "EDIT_SECRET session message edit token=do-not-return"
_KEY_SECRET = "KEY_SECRET session missing token=do-not-return"
_JSON_SECRET = "JSON_SECRET session body token=do-not-return"
_QUERY_SECRET = "QUERY_SECRET session query token=do-not-return"
_FIXED_ERROR = "Failed to edit message"
_SECRETS = (_EDIT_SECRET, _KEY_SECRET, _JSON_SECRET, _QUERY_SECRET)
_REPO = Path(__file__).resolve().parent.parent


class _MemSession:
    def __init__(self, history):
        self.history = list(history)


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


def _add_message(session_id, content, when, meta=None):
    db = SessionLocal()
    try:
        msg_id = str(uuid.uuid4())
        db.add(DbChatMessage(
            id=msg_id,
            session_id=session_id,
            role="assistant",
            content=content,
            meta_data=None if meta is None else json.dumps(meta),
            timestamp=when,
        ))
        db.commit()
        return msg_id
    finally:
        db.close()


def _stored_message(msg_id):
    db = SessionLocal()
    try:
        row = db.query(DbChatMessage).filter(DbChatMessage.id == msg_id).one()
        meta = json.loads(row.meta_data) if row.meta_data else None
        return row.content, meta, row.session_id
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


def _edit(username, session_id, manager, body=None, raw=None):
    client = _client(username, manager)
    url = f"/api/session/{session_id}/edit-message"
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
    assert "Session message edit failed" not in resp.text
    _assert_no_secrets(resp)


def _memory(msg_id, content, extra=None):
    meta = {"_db_id": msg_id, "keep": "yes"}
    if extra:
        meta.update(extra)
    return ChatMessage(role="assistant", content=content, metadata=meta)


def test_edit_updates_db_and_matching_memory():
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project", message_count=4)
    when = datetime(2026, 1, 1, 0, 0, 0)
    msg_id = _add_message(session_id, "old answer", when, meta={"keep": "yes"})
    other_id = _add_message(session_id, "leave this", when, meta={"keep": "other"})
    matched = _memory(msg_id, "old answer")
    untouched = ChatMessage(role="user", content="question", metadata={"keep": "user"})
    memory = _MemSession([untouched, matched])
    manager = _Manager(session=memory)
    try:
        resp = _edit(owner, session_id, manager, body={"msg_id": msg_id, "content": "new answer"})
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}
        assert "error" not in resp.json()
        assert manager.calls == [session_id]
        content, meta, stored_session = _stored_message(msg_id)
        assert content == "new answer"
        assert meta == {"keep": "yes", "edited": True}
        assert stored_session == session_id
        other_content, other_meta, _ = _stored_message(other_id)
        assert other_content == "leave this"
        assert other_meta == {"keep": "other"}
        assert matched.content == "new answer"
        assert matched.metadata == {"_db_id": msg_id, "keep": "yes", "edited": True}
        assert untouched.content == "question"
        assert untouched.metadata == {"keep": "user"}
        assert _stored_count(session_id) == 4
        _assert_no_secrets(resp)
    finally:
        _delete_rows([session_id])


def test_edit_updates_dict_memory():
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project")
    msg_id = _add_message(session_id, "old", datetime(2026, 1, 1, 0, 0, 0))
    matched = {"role": "assistant", "content": "old", "metadata": {"_db_id": msg_id}}
    memory = _MemSession([matched])
    manager = _Manager(session=memory)
    try:
        resp = _edit(owner, session_id, manager, body={"msg_id": msg_id, "content": "rewritten"})
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}
        assert manager.calls == [session_id]
        content, meta, _ = _stored_message(msg_id)
        assert content == "rewritten"
        assert meta == {"edited": True}
        assert matched["content"] == "rewritten"
        assert matched["metadata"]["edited"] is True
        assert matched["metadata"]["_db_id"] == msg_id
        _assert_no_secrets(resp)
    finally:
        _delete_rows([session_id])


def test_empty_content_is_saved():
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project")
    msg_id = _add_message(session_id, "old", datetime(2026, 1, 1, 0, 0, 0))
    matched = _memory(msg_id, "old")
    manager = _Manager(session=_MemSession([matched]))
    try:
        resp = _edit(owner, session_id, manager, body={"msg_id": msg_id, "content": ""})
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}
        content, meta, _ = _stored_message(msg_id)
        assert content == ""
        assert meta == {"edited": True}
        assert matched.content == ""
        assert manager.calls == [session_id]
        _assert_no_secrets(resp)
    finally:
        _delete_rows([session_id])


def test_unmatched_memory_still_updates_db():
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project", message_count=2)
    msg_id = _add_message(session_id, "old", datetime(2026, 1, 1, 0, 0, 0))
    other = _memory("different-id", "stay")
    manager = _Manager(session=_MemSession([other]))
    try:
        resp = _edit(owner, session_id, manager, body={"msg_id": msg_id, "content": "saved"})
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}
        content, meta, _ = _stored_message(msg_id)
        assert content == "saved"
        assert meta == {"edited": True}
        assert other.content == "stay"
        assert "edited" not in other.metadata
        assert _stored_count(session_id) == 2
        assert manager.calls == [session_id]
        _assert_no_secrets(resp)
    finally:
        _delete_rows([session_id])


def test_missing_message_is_404():
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project")
    kept_id = _add_message(session_id, "kept", datetime(2026, 1, 1, 0, 0, 0))
    matched = _memory(kept_id, "kept")
    manager = _Manager(session=_MemSession([matched]))
    try:
        resp = _edit(owner, session_id, manager, body={"msg_id": "missing-id", "content": "nope"})
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Message not found"
        assert "error" not in resp.json()
        assert manager.calls == [session_id]
        content, meta, _ = _stored_message(kept_id)
        assert content == "kept"
        assert meta is None
        assert matched.content == "kept"
        _assert_no_secrets(resp)
    finally:
        _delete_rows([session_id])


def test_foreign_message_is_not_edited():
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project")
    other_id = _add_session(owner, name="Theirs")
    foreign_id = _add_message(other_id, "theirs", datetime(2026, 1, 1, 0, 0, 0), meta={"keep": "foreign"})
    manager = _Manager(session=_MemSession([_memory(foreign_id, "theirs")]))
    try:
        resp = _edit(owner, session_id, manager, body={"msg_id": foreign_id, "content": "stolen"})
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Message not found"
        assert "error" not in resp.json()
        assert manager.calls == [session_id]
        content, meta, stored_session = _stored_message(foreign_id)
        assert content == "theirs"
        assert meta == {"keep": "foreign"}
        assert stored_session == other_id
        _assert_no_secrets(resp)
    finally:
        _delete_rows([session_id, other_id])


def test_missing_msg_id_is_400():
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project")
    kept_id = _add_message(session_id, "kept", datetime(2026, 1, 1, 0, 0, 0))
    manager = _Manager()
    try:
        resp = _edit(owner, session_id, manager, body={"content": "new"})
        assert resp.status_code == 400
        assert resp.json()["detail"] == "msg_id and content are required"
        assert "error" not in resp.json()
        assert manager.calls == []
        content, meta, _ = _stored_message(kept_id)
        assert content == "kept"
        assert meta is None
        _assert_no_secrets(resp)
    finally:
        _delete_rows([session_id])


def test_null_content_is_400():
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project")
    kept_id = _add_message(session_id, "kept", datetime(2026, 1, 1, 0, 0, 0))
    manager = _Manager()
    try:
        resp = _edit(owner, session_id, manager, body={"msg_id": kept_id, "content": None})
        assert resp.status_code == 400
        assert resp.json()["detail"] == "msg_id and content are required"
        assert "error" not in resp.json()
        assert manager.calls == []
        content, _, _ = _stored_message(kept_id)
        assert content == "kept"
        _assert_no_secrets(resp)
    finally:
        _delete_rows([session_id])


def test_keyerror_hides_message(caplog):
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project")
    kept_id = _add_message(session_id, "kept", datetime(2026, 1, 1, 0, 0, 0))
    manager = _Manager(exc=KeyError(_KEY_SECRET))
    caplog.set_level(logging.ERROR, logger="routes.history_routes")
    try:
        resp = _edit(owner, session_id, manager, body={"msg_id": kept_id, "content": "new"})
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Session not found"
        assert "error" not in resp.json()
        assert manager.calls == [session_id]
        content, meta, _ = _stored_message(kept_id)
        assert content == "kept"
        assert meta is None
        _assert_no_secrets(resp)
        assert "Session message edit failed" not in caplog.text
        assert _FIXED_ERROR not in caplog.text
    finally:
        _delete_rows([session_id])


def test_edit_failure_hides_exception(caplog):
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project")
    kept_id = _add_message(session_id, "kept", datetime(2026, 1, 1, 0, 0, 0))
    manager = _Manager(exc=RuntimeError(_EDIT_SECRET))
    caplog.set_level(logging.ERROR, logger="routes.history_routes")
    try:
        resp = _edit(owner, session_id, manager, body={"msg_id": kept_id, "content": "new"})
        _assert_fixed(resp)
        assert manager.calls == [session_id]
        content, meta, _ = _stored_message(kept_id)
        assert content == "kept"
        assert meta is None
        assert _EDIT_SECRET in caplog.text
        assert f"Session message edit failed for {session_id}" in caplog.text
    finally:
        _delete_rows([session_id])


def test_query_failure_hides_exception_and_does_not_rollback(caplog, monkeypatch):
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project", message_count=4)
    kept_id = _add_message(session_id, "kept", datetime(2026, 1, 1, 0, 0, 0))
    matched = _memory(kept_id, "kept")
    manager = _Manager(session=_MemSession([matched]))
    failed = _FailSession(RuntimeError(_QUERY_SECRET))
    monkeypatch.setattr(history_routes, "SessionLocal", lambda: failed)
    caplog.set_level(logging.ERROR, logger="routes.history_routes")
    try:
        resp = _edit(owner, session_id, manager, body={"msg_id": kept_id, "content": "new"})
        _assert_fixed(resp)
        assert manager.calls == [session_id]
        assert failed.closed is True
        assert failed.rollbacks == 0
        content, meta, _ = _stored_message(kept_id)
        assert content == "kept"
        assert meta is None
        assert matched.content == "kept"
        assert "edited" not in matched.metadata
        assert _stored_count(session_id) == 4
        assert _QUERY_SECRET in caplog.text
        assert f"Session message edit failed for {session_id}" in caplog.text
    finally:
        _delete_rows([session_id])


def test_invalid_json_hides_exception(caplog):
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project")
    manager = _Manager()
    caplog.set_level(logging.ERROR, logger="routes.history_routes")
    try:
        resp = _edit(
            owner,
            session_id,
            manager,
            raw=f"{_JSON_SECRET} not-json".encode(),
        )
        _assert_fixed(resp)
        assert manager.calls == []
        assert f"Session message edit failed for {session_id}" in caplog.text
    finally:
        _delete_rows([session_id])


def test_non_object_body_hides_exception(caplog):
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project")
    manager = _Manager()
    caplog.set_level(logging.ERROR, logger="routes.history_routes")
    try:
        resp = _edit(owner, session_id, manager, body=["not", "an", "object"])
        _assert_fixed(resp)
        assert manager.calls == []
        assert "'list' object has no attribute 'get'" not in resp.text
        assert f"Session message edit failed for {session_id}" in caplog.text
    finally:
        _delete_rows([session_id])


def test_http_exception_is_reraised(caplog, monkeypatch):
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project")
    kept_id = _add_message(session_id, "kept", datetime(2026, 1, 1, 0, 0, 0))
    matched = _memory(kept_id, "kept")
    manager = _Manager(session=_MemSession([matched]))
    failed = _FailSession(HTTPException(status_code=409, detail="Session message edit refused"))
    monkeypatch.setattr(history_routes, "SessionLocal", lambda: failed)
    caplog.set_level(logging.ERROR, logger="routes.history_routes")
    try:
        resp = _edit(owner, session_id, manager, body={"msg_id": kept_id, "content": "new"})
        assert resp.status_code == 409
        assert resp.json()["detail"] == "Session message edit refused"
        assert "error" not in resp.json()
        assert manager.calls == [session_id]
        assert failed.closed is True
        assert failed.rollbacks == 0
        content, meta, _ = _stored_message(kept_id)
        assert content == "kept"
        assert meta is None
        assert matched.content == "kept"
        _assert_no_secrets(resp)
        assert "Session message edit failed" not in caplog.text
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
        other = _edit(owner, theirs, manager, body={"msg_id": "x", "content": "new"})
        blank = _edit(owner, legacy, manager, body={"msg_id": "x", "content": "new"})
        gone = _edit(owner, missing, manager, body={"msg_id": "x", "content": "new"})
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
        resp = _edit(None, session_id, manager, body={"msg_id": "x", "content": "new"})
        assert resp.status_code == 403
        assert resp.json()["detail"] == "Authentication required"
        assert "error" not in resp.json()
        assert manager.calls == []
        _assert_no_secrets(resp)
    finally:
        _delete_rows([session_id])


def test_edit_keeps_the_fixed_sentence_and_leaves_neighbors():
    history = (_REPO / "routes/history_routes.py").read_text(encoding="utf-8")
    assert "def history_error(message: str, status_code: int = 502, **extra):" in history
    assert history.count("def history_error(") == 1
    history_start = history.index("    async def get_session_history(")
    truncate_start = history.index("    async def truncate_session(")
    add_start = history.index("    async def add_message(")
    delete_start = history.index("    async def delete_messages(")
    edit_start = history.index("    async def edit_message(")
    mark_start = history.index("    async def mark_stopped(")
    update_start = history.index("    async def update_last_meta(")
    loaded = history[history_start:truncate_start]
    truncate = history[truncate_start:add_start]
    added = history[add_start:delete_start]
    deleted = history[delete_start:edit_start]
    edited = history[edit_start:mark_start]
    marked = history[mark_start:update_start]

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

    assert "history_error" not in deleted
    assert "except HTTPException:" not in deleted
    assert 'logger.error(f"Delete messages error {session_id}: {e}")' in deleted
    assert "raise HTTPException(500, str(e))" in deleted
    assert 'HTTPException(404, "Session not found")' in deleted
    assert 'return {"status": "ok", "deleted": deleted}' in deleted
    assert 'return {"status": "ok", "deleted": 0}' in deleted
    assert "db.close()" in deleted
    assert "SessionLocal" in deleted
    assert "db.rollback()" not in deleted
    owner_at = deleted.index("_verify_session_owner(request, session_id)")
    try_at = deleted.index("try:")
    assert owner_at < try_at

    owner_at = edited.index("_verify_session_owner(request, session_id)")
    try_at = edited.index("try:")
    assert owner_at < try_at
    assert "except HTTPException:" in edited
    assert 'history_error("Failed to edit message")' in edited
    assert 'logger.exception("Session message edit failed for %s", session_id)' in edited
    assert 'HTTPException(404, "Session not found")' in edited
    assert 'raise HTTPException(400, "msg_id and content are required")' in edited
    assert 'raise HTTPException(404, "Message not found")' in edited
    assert 'return {"status": "ok"}' in edited
    assert "db.close()" in edited
    assert "SessionLocal" in edited
    assert "{e}" not in edited
    assert "str(e)" not in edited
    assert "HTTPException(500" not in edited
    assert "db.rollback()" not in edited
    assert "save_sessions()" not in edited
    assert history.count('history_error("Failed to edit message")') == 1
    assert 'logger.error(f"Edit message error {session_id}: {e}")' not in edited

    assert "history_error" not in marked
    assert "except HTTPException:" not in marked
    assert 'logger.error(f"Mark stopped error {session_id}: {e}")' in marked
    assert "raise HTTPException(500, str(e))" in marked
    assert 'HTTPException(404, "Session not found")' in marked
    assert 'return {"status": "ok"}' in marked
    assert "db.close()" in marked
    assert "SessionLocal" in marked
    assert "db.rollback()" not in marked
    assert "save_sessions()" in marked
    owner_at = marked.index("_verify_session_owner(request, session_id)")
    try_at = marked.index("try:")
    assert owner_at < try_at

    chat = (_REPO / "static/js/chat.js").read_text(encoding="utf-8")
    edit_ai_start = chat.index("  export async function editAIMessage(")
    rewrite_start = chat.index("  export async function rewriteWith(")
    edit_client = chat[edit_ai_start:rewrite_start]
    assert "method: 'POST'" in edit_client
    assert "api/session/${sessionId}/edit-message" in edit_client
    assert "res.ok" in edit_client
    assert "Server error " in edit_client
    assert "Edit failed: " in edit_client
    assert ".json(" not in edit_client
    assert ".text(" not in edit_client
    assert "mark-stopped" not in edit_client
    assert chat.count("mark-stopped failed:") == 2
    assert ".json(" not in chat[chat.index("mark-stopped"):chat.index("mark-stopped") + 180]

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
