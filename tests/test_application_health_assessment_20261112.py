"""DELETE /api/editor-drafts/{id} must not return exception text.

An unexpected failure is HTTP 502 {"error": "Failed to delete draft"}. The
exception is logged and is not returned. A rollback failure is logged and
does not replace that 502 or a 404. An HTTPException raised inside the
handler is still passed through. Success stays {"status": "deleted", "id"}.
Missing and unowned drafts stay HTTP 404 "Draft not found".

Create and update already use fixed sentences. The gallery and editor
clients await DELETE and do not read the body. They were not edited.
"""
import logging
import os
import uuid
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite:///./data/app.db")
os.makedirs("data", exist_ok=True)

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import routes.editor_draft_routes as draft_routes
from core.database import EditorDraft, SessionLocal

_COMMIT_SECRET = "COMMIT_SECRET draft delete token=do-not-return"
_QUERY_SECRET = "QUERY_SECRET draft lookup token=do-not-return"
_ROLLBACK_SECRET = "ROLLBACK_SECRET draft rollback token=do-not-return"
_FIXED_ERROR = "Failed to delete draft"
_SECRETS = (_COMMIT_SECRET, _QUERY_SECRET, _ROLLBACK_SECRET)
_REAL_SESSION = SessionLocal
_REPO = Path(__file__).resolve().parent.parent


class _FailingSession:
    """Session whose commit or rollback can raise. Close still closes the inner session."""

    def __init__(self, inner, commit_exc=None, rollback_exc=None):
        self._inner = inner
        self._commit_exc = commit_exc
        self._rollback_exc = rollback_exc

    def commit(self):
        if self._commit_exc is not None:
            raise self._commit_exc
        return self._inner.commit()

    def rollback(self):
        if self._rollback_exc is not None:
            raise self._rollback_exc
        return self._inner.rollback()

    def close(self):
        self._inner.close()

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _QueryFailSession(_FailingSession):
    def query(self, *args, **kwargs):
        raise RuntimeError(_QUERY_SECRET)


class _HttpFailSession(_FailingSession):
    def query(self, *args, **kwargs):
        raise HTTPException(status_code=409, detail="Draft delete refused")


def _client(username):
    app = FastAPI()

    @app.middleware("http")
    async def _stamp_user(request, call_next):
        if username is not None:
            request.state.current_user = username
        return await call_next(request)

    app.include_router(draft_routes.setup_editor_draft_routes())
    return TestClient(app)


def _install_failing_session(
    monkeypatch,
    commit_exc=None,
    rollback_exc=None,
    query_fails=False,
    http_fails=False,
):
    def _factory():
        inner = _REAL_SESSION()
        if query_fails:
            cls = _QueryFailSession
        elif http_fails:
            cls = _HttpFailSession
        else:
            cls = _FailingSession
        return cls(inner, commit_exc=commit_exc, rollback_exc=rollback_exc)

    monkeypatch.setattr(draft_routes, "SessionLocal", _factory)


def _add_draft(owner, name="Project", active=True):
    db = _REAL_SESSION()
    try:
        draft_id = str(uuid.uuid4())
        db.add(EditorDraft(
            id=draft_id,
            owner=owner,
            name=name,
            payload='{"layers": []}',
            is_active=active,
        ))
        db.commit()
        return draft_id
    finally:
        db.close()


def _row(draft_id):
    db = _REAL_SESSION()
    try:
        return db.query(EditorDraft).filter(EditorDraft.id == draft_id).first()
    finally:
        db.close()


def _delete_rows(draft_ids):
    db = _REAL_SESSION()
    try:
        if draft_ids:
            db.query(EditorDraft).filter(EditorDraft.id.in_(list(draft_ids))).delete(
                synchronize_session=False
            )
        db.commit()
    finally:
        db.close()


def _delete(username, draft_id):
    return _client(username).delete(f"/api/editor-drafts/{draft_id}")


def _list(username):
    return _client(username).get("/api/editor-drafts")


def _assert_no_secrets(resp):
    for secret in _SECRETS:
        assert secret not in resp.text


def _assert_fixed(resp):
    assert resp.status_code == 502
    assert resp.json() == {"error": _FIXED_ERROR}
    assert "detail" not in resp.json()
    assert "Editor draft delete failed" not in resp.text
    assert "Editor draft delete rollback failed" not in resp.text
    _assert_no_secrets(resp)


def test_delete_soft_deletes_owned_draft():
    owner = f"health-{uuid.uuid4().hex}"
    draft_id = _add_draft(owner, name="Project")
    try:
        resp = _delete(owner, draft_id)
        assert resp.status_code == 200
        assert resp.json() == {"status": "deleted", "id": draft_id}
        assert "error" not in resp.json()
        row = _row(draft_id)
        assert row is not None
        assert row.is_active is False
        listed = _list(owner)
        assert listed.status_code == 200
        assert draft_id not in [item["id"] for item in listed.json()["drafts"]]
        again = _delete(owner, draft_id)
        assert again.status_code == 200
        assert again.json() == {"status": "deleted", "id": draft_id}
        _assert_no_secrets(resp)
    finally:
        _delete_rows([draft_id])


def test_missing_draft_is_404():
    owner = f"health-{uuid.uuid4().hex}"
    missing = str(uuid.uuid4())
    resp = _delete(owner, missing)
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Draft not found"
    assert "error" not in resp.json()
    _assert_no_secrets(resp)


def test_other_owner_and_null_owner_stay_404():
    owner = f"health-{uuid.uuid4().hex}"
    stranger = f"health-{uuid.uuid4().hex}"
    owned = _add_draft(stranger, name="Theirs")
    legacy = _add_draft(None, name="Legacy")
    try:
        other = _delete(owner, owned)
        blank = _delete(owner, legacy)
        assert other.status_code == 404
        assert other.json()["detail"] == "Draft not found"
        assert blank.status_code == 404
        assert blank.json()["detail"] == "Draft not found"
        assert _row(owned).is_active is True
        assert _row(legacy).is_active is True
        _assert_no_secrets(other)
        _assert_no_secrets(blank)
    finally:
        _delete_rows([owned, legacy])


def test_anonymous_delete_still_removes_any_draft():
    owner = f"health-{uuid.uuid4().hex}"
    draft_id = _add_draft(owner, name="Shared")
    try:
        resp = _delete(None, draft_id)
        assert resp.status_code == 200
        assert resp.json() == {"status": "deleted", "id": draft_id}
        assert _row(draft_id).is_active is False
        _assert_no_secrets(resp)
    finally:
        _delete_rows([draft_id])


def test_commit_failure_hides_exception(monkeypatch, caplog):
    owner = f"health-{uuid.uuid4().hex}"
    draft_id = _add_draft(owner, name="Project")
    _install_failing_session(monkeypatch, commit_exc=RuntimeError(_COMMIT_SECRET))
    caplog.set_level(logging.ERROR, logger="routes.editor_draft_routes")
    try:
        resp = _delete(owner, draft_id)
        _assert_fixed(resp)
        assert _COMMIT_SECRET in caplog.text
        assert "Editor draft delete failed" in caplog.text
        assert "Editor draft delete rollback failed" not in caplog.text
        assert _row(draft_id).is_active is True
    finally:
        _delete_rows([draft_id])


def test_query_failure_hides_exception(monkeypatch, caplog):
    owner = f"health-{uuid.uuid4().hex}"
    draft_id = _add_draft(owner, name="Project")
    _install_failing_session(monkeypatch, query_fails=True)
    caplog.set_level(logging.ERROR, logger="routes.editor_draft_routes")
    try:
        resp = _delete(owner, draft_id)
        _assert_fixed(resp)
        assert _QUERY_SECRET in caplog.text
        assert "Editor draft delete failed" in caplog.text
        assert "Editor draft delete rollback failed" not in caplog.text
        assert _row(draft_id).is_active is True
    finally:
        _delete_rows([draft_id])


def test_query_and_rollback_failure_stays_fixed_502(monkeypatch, caplog):
    owner = f"health-{uuid.uuid4().hex}"
    draft_id = _add_draft(owner, name="Project")
    _install_failing_session(
        monkeypatch,
        rollback_exc=RuntimeError(_ROLLBACK_SECRET),
        query_fails=True,
    )
    caplog.set_level(logging.ERROR, logger="routes.editor_draft_routes")
    try:
        resp = _delete(owner, draft_id)
        _assert_fixed(resp)
        assert _QUERY_SECRET in caplog.text
        assert _ROLLBACK_SECRET in caplog.text
        assert "Editor draft delete failed" in caplog.text
        assert "Editor draft delete rollback failed" in caplog.text
        assert _row(draft_id).is_active is True
    finally:
        _delete_rows([draft_id])


def test_missing_draft_rollback_failure_stays_404(monkeypatch, caplog):
    owner = f"health-{uuid.uuid4().hex}"
    _install_failing_session(monkeypatch, rollback_exc=RuntimeError(_ROLLBACK_SECRET))
    caplog.set_level(logging.ERROR, logger="routes.editor_draft_routes")
    resp = _delete(owner, str(uuid.uuid4()))
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Draft not found"
    assert "error" not in resp.json()
    _assert_no_secrets(resp)
    assert _ROLLBACK_SECRET in caplog.text
    assert "Editor draft delete rollback failed" in caplog.text
    assert "Editor draft delete failed" not in caplog.text
    assert _FIXED_ERROR not in caplog.text


def test_http_exception_is_reraised(monkeypatch, caplog):
    _install_failing_session(monkeypatch, http_fails=True)
    caplog.set_level(logging.ERROR, logger="routes.editor_draft_routes")
    resp = _delete(f"health-{uuid.uuid4().hex}", str(uuid.uuid4()))
    assert resp.status_code == 409
    assert resp.json()["detail"] == "Draft delete refused"
    assert "error" not in resp.json()
    _assert_no_secrets(resp)
    assert "Editor draft delete failed" not in caplog.text
    assert "Editor draft delete rollback failed" not in caplog.text
    assert _FIXED_ERROR not in caplog.text


def test_http_exception_rollback_failure_stays_409(monkeypatch, caplog):
    _install_failing_session(
        monkeypatch,
        rollback_exc=RuntimeError(_ROLLBACK_SECRET),
        http_fails=True,
    )
    caplog.set_level(logging.ERROR, logger="routes.editor_draft_routes")
    resp = _delete(f"health-{uuid.uuid4().hex}", str(uuid.uuid4()))
    assert resp.status_code == 409
    assert resp.json()["detail"] == "Draft delete refused"
    assert "error" not in resp.json()
    _assert_no_secrets(resp)
    assert _ROLLBACK_SECRET in caplog.text
    assert "Editor draft delete rollback failed" in caplog.text
    assert "Editor draft delete failed" not in caplog.text
    assert _FIXED_ERROR not in caplog.text


def test_delete_keeps_the_fixed_sentence_and_leaves_create_and_update():
    source = (_REPO / "routes/editor_draft_routes.py").read_text(encoding="utf-8")
    assert "def draft_error(message: str, status_code: int = 502, **extra):" in source
    assert source.count("def draft_error(") == 1
    create_start = source.index("    async def create_draft(")
    update_start = source.index("    async def update_draft(")
    delete_start = source.index("    async def delete_draft(")
    end = source.index("\n    return router", delete_start)
    create = source[create_start:update_start]
    update = source[update_start:delete_start]
    deleted = source[delete_start:end]

    assert "draft_error" not in create
    assert 'raise HTTPException(500, "Could not save draft")' in create
    assert 'logger.warning(f"editor-draft create failed: {e}")' in create
    assert "except HTTPException:" not in create

    assert "draft_error" not in update
    assert "except HTTPException:" in update
    assert 'raise HTTPException(500, "Could not update draft")' in update
    assert 'logger.warning(f"editor-draft update failed: {e}")' in update
    assert "Editor draft delete" not in update

    assert "except HTTPException:" in deleted
    assert 'draft_error("Failed to delete draft")' in deleted
    assert 'logger.exception("Editor draft delete failed")' in deleted
    assert deleted.count('logger.exception("Editor draft delete rollback failed")') == 2
    assert 'HTTPException(404, "Draft not found")' in deleted
    assert 'return {"status": "deleted", "id": draft_id}' in deleted
    assert "{e}" not in deleted
    assert "str(e)" not in deleted
    assert "HTTPException(500" not in deleted
    assert source.count('draft_error("Failed to delete draft")') == 1

    gallery = (_REPO / "static/js/gallery.js").read_text(encoding="utf-8")
    single_start = gallery.index("grid.querySelectorAll('.gallery-editor-draft-delete')")
    single_end = gallery.index("function _draftsSyncBulkBar(", single_start)
    single = gallery[single_start:single_end]
    assert "method: 'DELETE'" in single
    assert ".json(" not in single
    assert ".text(" not in single
    assert ".ok" not in single

    bulk_start = gallery.index("gallery-editor-drafts-bulk-delete')?.addEventListener")
    bulk_end = gallery.index("function _humanRelativeDate(", bulk_start)
    bulk = gallery[bulk_start:bulk_end]
    assert "Promise.allSettled" in bulk
    assert "method: 'DELETE'" in bulk
    assert ".json(" not in bulk
    assert ".text(" not in bulk
    assert ".ok" not in bulk

    editor = (_REPO / "static/js/galleryEditor.js").read_text(encoding="utf-8")
    clear_start = editor.index("async function _clearDraftServer(")
    clear_end = editor.index("function _restoreDraft(", clear_start)
    clear = editor[clear_start:clear_end]
    assert "method: 'DELETE'" in clear
    assert ".json(" not in clear
    assert ".text(" not in clear
    assert ".ok" not in clear

    skills = (_REPO / "routes/skills_routes.py").read_text(encoding="utf-8")
    assert 'return {"builtin": [], "count": 0, "error": str(e)}' in skills
    assert "raise HTTPException(500, str(e))" in skills
    assert "def skill_error(" not in skills
    assert "draft_error" not in skills

    upload = (_REPO / "routes/upload_routes.py").read_text(encoding="utf-8")
    assert 'raise HTTPException(500, f"Vision analysis failed: {e}")' in upload
    assert "def vision_error(" not in upload
    assert "draft_error" not in upload

    docs = (_REPO / "routes/document_routes.py").read_text(encoding="utf-8")
    fill_start = docs.index("    async def ai_fill_annotations(")
    render_start = docs.index("    async def render_pdf(", fill_start)
    fill = docs[fill_start:render_start]
    assert "import fitz" in fill
    assert "No vision model available: {e}" in fill
    assert "document_error" not in fill
    assert "draft_error" not in fill

    history = (_REPO / "routes/history_routes.py").read_text(encoding="utf-8")
    truncate_start = history.index("    async def truncate_session(")
    add_start = history.index("    async def add_message(", truncate_start)
    truncate = history[truncate_start:add_start]
    assert 'raise HTTPException(500, str(e))' in truncate
    assert "draft_error" not in truncate
