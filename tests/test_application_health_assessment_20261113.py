"""POST /api/session/{id}/truncate must not return exception text.

An unexpected failure is HTTP 502 {"error": "Failed to truncate session"}.
The exception is logged and is not returned. An HTTPException raised inside
the handler is still passed through. A missing in-memory session stays
HTTP 404 "Session not found". Success stays
{"status": "ok", "kept": keep_count, "truncated": result}.

There is no database session in this handler, so there is no rollback.
Ownership is still checked before the try. Add-message and history read
were not changed. The chat and slash-command clients do not read the body.
"""
import logging
import os
import uuid
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite:///./data/app.db")
os.makedirs("data", exist_ok=True)

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import routes.history_routes as history_routes
from core.database import Session as DbSession, SessionLocal

_TRUNCATE_SECRET = "TRUNCATE_SECRET session truncate token=do-not-return"
_KEY_SECRET = "KEY_SECRET session missing token=do-not-return"
_JSON_SECRET = "JSON_SECRET session body token=do-not-return"
_FIXED_ERROR = "Failed to truncate session"
_SECRETS = (_TRUNCATE_SECRET, _KEY_SECRET, _JSON_SECRET)
_REPO = Path(__file__).resolve().parent.parent


class _Manager:
    def __init__(self, result=True, exc=None):
        self.result = result
        self.exc = exc
        self.calls = []

    def truncate_messages(self, session_id, keep_count):
        self.calls.append((session_id, keep_count))
        if self.exc is not None:
            raise self.exc
        return self.result


def _client(username, manager):
    app = FastAPI()

    @app.middleware("http")
    async def _stamp_user(request, call_next):
        if username is not None:
            request.state.current_user = username
        return await call_next(request)

    app.include_router(history_routes.setup_history_routes(manager))
    return TestClient(app)


def _add_session(owner, name="Project"):
    db = SessionLocal()
    try:
        session_id = str(uuid.uuid4())
        db.add(DbSession(
            id=session_id,
            name=name,
            endpoint_url="http://127.0.0.1:9",
            model="test-model",
            owner=owner,
        ))
        db.commit()
        return session_id
    finally:
        db.close()


def _delete_rows(session_ids):
    db = SessionLocal()
    try:
        if session_ids:
            db.query(DbSession).filter(DbSession.id.in_(list(session_ids))).delete(
                synchronize_session=False
            )
        db.commit()
    finally:
        db.close()


def _truncate(username, session_id, manager, body=None, raw=None):
    client = _client(username, manager)
    url = f"/api/session/{session_id}/truncate"
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
    assert "Session truncate failed" not in resp.text
    _assert_no_secrets(resp)


def test_truncate_returns_keep_count_and_result():
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project")
    manager = _Manager(result=True)
    try:
        resp = _truncate(owner, session_id, manager, body={"keep_count": 2})
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok", "kept": 2, "truncated": True}
        assert "error" not in resp.json()
        assert manager.calls == [(session_id, 2)]
        _assert_no_secrets(resp)
    finally:
        _delete_rows([session_id])


def test_missing_keep_count_defaults_to_zero():
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project")
    manager = _Manager(result=True)
    try:
        resp = _truncate(owner, session_id, manager, body={})
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok", "kept": 0, "truncated": True}
        assert manager.calls == [(session_id, 0)]
        _assert_no_secrets(resp)
    finally:
        _delete_rows([session_id])


def test_false_result_stays_success():
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project")
    manager = _Manager(result=False)
    try:
        resp = _truncate(owner, session_id, manager, body={"keep_count": 4})
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok", "kept": 4, "truncated": False}
        assert "error" not in resp.json()
        assert manager.calls == [(session_id, 4)]
        _assert_no_secrets(resp)
    finally:
        _delete_rows([session_id])


def test_missing_in_memory_session_is_404(caplog):
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project")
    manager = _Manager(exc=KeyError(_KEY_SECRET))
    caplog.set_level(logging.ERROR, logger="routes.history_routes")
    try:
        resp = _truncate(owner, session_id, manager, body={"keep_count": 1})
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Session not found"
        assert "error" not in resp.json()
        assert manager.calls == [(session_id, 1)]
        _assert_no_secrets(resp)
        assert "Session truncate failed" not in caplog.text
        assert _FIXED_ERROR not in caplog.text
    finally:
        _delete_rows([session_id])


def test_truncate_failure_hides_exception(caplog):
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project")
    manager = _Manager(exc=RuntimeError(_TRUNCATE_SECRET))
    caplog.set_level(logging.ERROR, logger="routes.history_routes")
    try:
        resp = _truncate(owner, session_id, manager, body={"keep_count": 2})
        _assert_fixed(resp)
        assert manager.calls == [(session_id, 2)]
        assert _TRUNCATE_SECRET in caplog.text
        assert f"Session truncate failed for {session_id}" in caplog.text
    finally:
        _delete_rows([session_id])


def test_invalid_json_hides_exception(caplog):
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project")
    manager = _Manager(result=True)
    caplog.set_level(logging.ERROR, logger="routes.history_routes")
    try:
        resp = _truncate(
            owner,
            session_id,
            manager,
            raw=f"{_JSON_SECRET} not-json".encode(),
        )
        _assert_fixed(resp)
        assert manager.calls == []
        assert f"Session truncate failed for {session_id}" in caplog.text
    finally:
        _delete_rows([session_id])


def test_non_object_body_hides_exception(caplog):
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project")
    manager = _Manager(result=True)
    caplog.set_level(logging.ERROR, logger="routes.history_routes")
    try:
        resp = _truncate(owner, session_id, manager, body=["not", "an", "object"])
        _assert_fixed(resp)
        assert manager.calls == []
        assert "'list' object has no attribute 'get'" not in resp.text
        assert f"Session truncate failed for {session_id}" in caplog.text
    finally:
        _delete_rows([session_id])


def test_http_exception_is_reraised(caplog):
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project")
    manager = _Manager(exc=HTTPException(status_code=409, detail="Session truncate refused"))
    caplog.set_level(logging.ERROR, logger="routes.history_routes")
    try:
        resp = _truncate(owner, session_id, manager, body={"keep_count": 2})
        assert resp.status_code == 409
        assert resp.json()["detail"] == "Session truncate refused"
        assert "error" not in resp.json()
        assert manager.calls == [(session_id, 2)]
        _assert_no_secrets(resp)
        assert "Session truncate failed" not in caplog.text
        assert _FIXED_ERROR not in caplog.text
    finally:
        _delete_rows([session_id])


def test_other_owner_null_owner_and_missing_row_stay_owner_404():
    owner = f"health-{uuid.uuid4().hex}"
    stranger = f"health-{uuid.uuid4().hex}"
    theirs = _add_session(stranger, name="Theirs")
    legacy = _add_session(None, name="Legacy")
    missing = str(uuid.uuid4())
    manager = _Manager(result=True)
    try:
        other = _truncate(owner, theirs, manager, body={"keep_count": 1})
        blank = _truncate(owner, legacy, manager, body={"keep_count": 1})
        gone = _truncate(owner, missing, manager, body={"keep_count": 1})
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
    manager = _Manager(result=True)
    try:
        resp = _truncate(None, session_id, manager, body={"keep_count": 1})
        assert resp.status_code == 403
        assert resp.json()["detail"] == "Authentication required"
        assert "error" not in resp.json()
        assert manager.calls == []
        _assert_no_secrets(resp)
    finally:
        _delete_rows([session_id])


def test_truncate_keeps_the_fixed_sentence_and_leaves_neighbors():
    history = (_REPO / "routes/history_routes.py").read_text(encoding="utf-8")
    assert "def history_error(message: str, status_code: int = 502, **extra):" in history
    assert history.count("def history_error(") == 1
    history_start = history.index("    async def get_session_history(")
    truncate_start = history.index("    async def truncate_session(")
    add_start = history.index("    async def add_message(")
    delete_start = history.index("    async def delete_messages(")
    edit_start = history.index("    async def edit_message(")
    loaded = history[history_start:truncate_start]
    truncate = history[truncate_start:add_start]
    added = history[add_start:delete_start]
    deleted = history[delete_start:edit_start]

    assert "history_error" not in loaded
    assert 'logger.error(f"DB fallback failed for {session_id}: {e}")' in loaded
    assert "SessionLocal" in loaded

    assert loaded.count("_verify_session_owner(request, session_id)") == 1
    owner_at = truncate.index("_verify_session_owner(request, session_id)")
    try_at = truncate.index("try:")
    assert owner_at < try_at
    assert "except HTTPException:" in truncate
    assert 'history_error("Failed to truncate session")' in truncate
    assert 'logger.exception("Session truncate failed for %s", session_id)' in truncate
    assert 'HTTPException(404, "Session not found")' in truncate
    assert 'return {"status": "ok", "kept": keep_count, "truncated": result}' in truncate
    assert "{e}" not in truncate
    assert "str(e)" not in truncate
    assert "HTTPException(500" not in truncate
    assert "rollback" not in truncate
    assert "SessionLocal" not in truncate
    assert history.count('history_error("Failed to truncate session")') == 1

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
    assert "db.rollback()" not in deleted
    assert "db.close()" in deleted

    chat = (_REPO / "static/js/chat.js").read_text(encoding="utf-8")
    edit_start = chat.index("  export async function editUserMessage(")
    resend_start = chat.index("  export async function resendUserMessage(")
    regen_start = chat.index("  export async function regenerateFrom(")
    regen_end = chat.index("  function _attachVariantNav(", regen_start)
    edit = chat[edit_start:resend_start]
    resend = chat[resend_start:regen_start]
    regen = chat[regen_start:regen_end]
    for slice_ in (edit, resend, regen):
        assert "api/session/${sessionId}/truncate" in slice_
        assert ".json(" not in slice_
        assert ".text(" not in slice_
        assert ".ok" not in slice_
    assert "Edit failed: " in edit
    assert "err.message" in edit
    assert "Resend failed: " in resend
    assert "err.message" in resend
    assert "Regenerate failed: " in regen
    assert "err.message" in regen

    slash = (_REPO / "static/js/slashCommands.js").read_text(encoding="utf-8")
    slash_start = slash.index("async function _cmdSessionTruncate(")
    slash_end = slash.index("async function _cmdSessionList(", slash_start)
    command = slash[slash_start:slash_end]
    assert "api/session/${ctx.sid}/truncate" in command
    assert "res.ok" in command
    assert "Truncate failed" in command
    assert ".json(" not in command
    assert ".text(" not in command

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
