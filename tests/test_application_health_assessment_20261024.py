"""Document tidy must not return database exception text.

POST /api/documents/tidy used to raise HTTP 500 with "Tidy failed: {exception}".
A failed commit can include SQL and bound values in that message. An unexpected
failure now returns HTTP 502 with a fixed sentence. The exception is logged and
is not returned. A rollback failure, including one raised again while closing
the session, still returns that same 502. An HTTPException raised inside the
handler is not rewritten. Another user's documents stay in place. An anonymous
caller still matches no rows and deletes nothing. Success still fixes an
Untitled title, removes that caller's empty documents, and leaves another
owner's rows alone. A title of "Untitled" is junk and is removed; an empty
title on a long document is renamed from the content.

The library Tidy button checks res.ok before reading the body. A non-ok
response shows the same fixed sentence and returns before "Already tidy" and
before the optional AI tidy request. The catch does not log or toast the
exception object.
"""
import logging
import os
import uuid
from pathlib import Path

os.makedirs("data", exist_ok=True)

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import routes.document_routes as document_routes
from core.database import Document, SessionLocal

_COMMIT_SECRET = "COMMIT_SECRET sql=DELETE FROM documents title=Untitled token=tidy-doc"
_ROLLBACK_SECRET = "ROLLBACK_SECRET connection reset while rolling back document tidy"
# The route looks up SessionLocal on this module at call time. Another test
# replaces that name with a temp engine; reads and cleanup use the app engine.
_REAL_SESSION = SessionLocal
_REPO = Path(__file__).resolve().parent.parent

_KEEP_BODY = (
    "# Trip notes\n\n"
    "Pack the camera, the notebook, and the train tickets before Friday morning."
)
_KEPT_BODY = (
    "# Field report\n\n"
    "The north platform was clear, and the evening train left on time with the samples."
)


class _FailingSession:
    """Session whose commit or rollback raises, and whose close rolls back."""

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
        try:
            self.rollback()
        finally:
            self._inner.close()

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _client(username):
    app = FastAPI()

    @app.middleware("http")
    async def _stamp_user(request, call_next):
        if username is not None:
            request.state.current_user = username
        return await call_next(request)

    app.include_router(document_routes.setup_document_routes(None, None))
    return TestClient(app)


def _real_session():
    return _REAL_SESSION()


def _pin_session(monkeypatch):
    monkeypatch.setattr(document_routes, "SessionLocal", _REAL_SESSION)


def _install_failing_session(monkeypatch, commit_exc=None, rollback_exc=None):
    def _factory():
        return _FailingSession(
            _REAL_SESSION(),
            commit_exc=commit_exc,
            rollback_exc=rollback_exc,
        )

    monkeypatch.setattr(document_routes, "SessionLocal", _factory)


def _add_document(owner, title, content, is_active=True):
    db = _real_session()
    try:
        doc_id = str(uuid.uuid4())
        db.add(Document(
            id=doc_id,
            session_id=None,
            title=title,
            language="markdown",
            current_content=content,
            version_count=1,
            is_active=is_active,
            archived=False,
            owner=owner,
        ))
        db.commit()
        return doc_id
    finally:
        db.close()


def _delete(doc_ids):
    db = _real_session()
    try:
        if doc_ids:
            db.query(Document).filter(Document.id.in_(doc_ids)).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


def _row(doc_id):
    db = _real_session()
    try:
        doc = db.query(Document).filter(Document.id == doc_id).first()
        if doc is None:
            return None
        return {
            "title": doc.title,
            "current_content": doc.current_content,
            "is_active": doc.is_active,
            "owner": doc.owner,
            "archived": bool(doc.archived),
        }
    finally:
        db.close()


def _js(path, start_marker, end_marker):
    source = (_REPO / path).read_text(encoding="utf-8")
    start = source.index(start_marker)
    end = source.index(end_marker, start)
    return source[start:end]


def test_tidy_commit_failure_hides_exception(monkeypatch, caplog):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    empty_id = _add_document(owner, "Blank", "")
    untitled_id = _add_document(owner, "Untitled", _KEEP_BODY)
    _install_failing_session(monkeypatch, commit_exc=RuntimeError(_COMMIT_SECRET))
    caplog.set_level(logging.ERROR, logger="routes.document_routes")
    try:
        resp = _client(owner).post("/api/documents/tidy")
        assert resp.status_code == 502
        assert resp.json() == {"error": "Failed to tidy documents"}
        assert "detail" not in resp.json()
        assert _COMMIT_SECRET not in resp.text
        assert "Tidy failed:" not in resp.text
        assert _COMMIT_SECRET in caplog.text
        assert "Failed to tidy documents" in caplog.text
        assert _row(empty_id)["title"] == "Blank"
        assert _row(empty_id)["current_content"] == ""
        assert _row(untitled_id)["title"] == "Untitled"
        assert _row(untitled_id)["current_content"] == _KEEP_BODY
    finally:
        _delete([empty_id, untitled_id])


def test_tidy_rollback_failure_stays_fixed_502(monkeypatch, caplog):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    empty_id = _add_document(owner, "Blank", "")
    _install_failing_session(
        monkeypatch,
        commit_exc=RuntimeError(_COMMIT_SECRET),
        rollback_exc=RuntimeError(_ROLLBACK_SECRET),
    )
    caplog.set_level(logging.ERROR, logger="routes.document_routes")
    try:
        resp = _client(owner).post("/api/documents/tidy")
        assert resp.status_code == 502
        assert resp.json() == {"error": "Failed to tidy documents"}
        assert "detail" not in resp.json()
        assert _COMMIT_SECRET not in resp.text
        assert _ROLLBACK_SECRET not in resp.text
        assert _COMMIT_SECRET in caplog.text
        assert _ROLLBACK_SECRET in caplog.text
        assert "Document tidy rollback failed" in caplog.text
        assert "Document tidy session close failed" in caplog.text
        assert _row(empty_id)["current_content"] == ""
        assert _row(empty_id)["owner"] == owner
    finally:
        _delete([empty_id])


def test_tidy_http_exception_is_not_rewritten(monkeypatch):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    empty_id = _add_document(owner, "Blank", "")

    def _reject(*args, **kwargs):
        raise HTTPException(400, "Document tidy rejected")

    monkeypatch.setattr(document_routes, "_owner_session_filter", _reject)
    try:
        resp = _client(owner).post("/api/documents/tidy")
        assert resp.status_code == 400
        assert resp.json() == {"detail": "Document tidy rejected"}
        assert resp.json().get("error") != "Failed to tidy documents"
        assert _row(empty_id)["title"] == "Blank"
        assert _row(empty_id)["current_content"] == ""
    finally:
        _delete([empty_id])


def test_tidy_anonymous_deletes_nothing(monkeypatch):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    empty_id = _add_document(owner, "Blank", "")
    try:
        resp = _client(None).post("/api/documents/tidy")
        assert resp.status_code == 200
        assert resp.json() == {
            "fixed_titles": 0,
            "deleted": 0,
            "message": "Fixed 0 titles, removed 0 empty documents",
        }
        assert "error" not in resp.json()
        assert _row(empty_id)["owner"] == owner
        assert _row(empty_id)["current_content"] == ""
    finally:
        _delete([empty_id])


def test_tidy_success_fixes_title_and_skips_other_owner(monkeypatch):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    other = f"health-{uuid.uuid4().hex}"
    empty_id = _add_document(owner, "Blank", "")
    inactive_id = _add_document(owner, "Old blank", "", is_active=False)
    # "Untitled" is a junk title and is removed. An empty title on a real
    # document is the case the handler renames from the content.
    untitled_id = _add_document(owner, "", _KEEP_BODY)
    kept_id = _add_document(owner, "Field report", _KEPT_BODY)
    other_empty_id = _add_document(other, "Blank", "")
    other_inactive_id = _add_document(other, "Old blank", "", is_active=False)
    ids = [empty_id, inactive_id, untitled_id, kept_id, other_empty_id, other_inactive_id]
    try:
        resp = _client(owner).post("/api/documents/tidy")
        assert resp.status_code == 200
        assert resp.json() == {
            "fixed_titles": 1,
            "deleted": 2,
            "message": "Fixed 1 title, removed 2 empty documents",
        }
        assert "error" not in resp.json()
        assert _row(empty_id) is None
        assert _row(inactive_id) is None
        assert _row(untitled_id) == {
            "title": "Trip notes",
            "current_content": _KEEP_BODY,
            "is_active": True,
            "owner": owner,
            "archived": False,
        }
        assert _row(kept_id)["title"] == "Field report"
        assert _row(kept_id)["current_content"] == _KEPT_BODY
        assert _row(other_empty_id)["owner"] == other
        assert _row(other_empty_id)["current_content"] == ""
        assert _row(other_inactive_id)["is_active"] is False
        assert _row(other_inactive_id)["current_content"] == ""
    finally:
        _delete(ids)


def test_tidy_handler_hides_exception_text():
    source = (_REPO / "routes/document_routes.py").read_text(encoding="utf-8")
    start = source.index("async def tidy_documents")
    end = source.index("async def ai_tidy_documents", start)
    block = source[start:end]
    assert "except HTTPException:" in block
    assert 'document_error("Failed to tidy documents")' in block
    assert "str(e)" not in block
    assert "Tidy failed:" not in block
    assert "HTTPException(500" not in block


def test_tidy_button_does_not_claim_already_tidy_on_failure():
    block = _js(
        "static/js/documentLibrary.js",
        "const tidyBtn = document.getElementById('doclib-tidy-btn');",
        "// Select mode",
    )
    ok_at = block.index("if (!res1.ok)")
    return_at = block.index("return;", ok_at)
    json_at = block.index("await res1.json()")
    already_at = block.index("Already tidy")
    ai_at = block.index("/api/documents/ai-tidy")
    assert ok_at < return_at < json_at < ai_at < already_at
    assert "console.error('Failed to tidy documents')" in block
    assert "showToast('Failed to tidy documents')" in block
    assert block.count("showToast('Failed to tidy documents')") == 2
    assert block.count("console.error('Failed to tidy documents')") == 2
    assert "console.error('Document tidy failed:', e)" not in block
    assert "catch (e)" not in block
    assert "detail" not in block
    assert "e.message" not in block
    assert "showToast('Tidy failed')" not in block
