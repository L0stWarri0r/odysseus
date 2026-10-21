"""Document metadata updates must not return database exception text.

PATCH /api/document/{doc_id} used to raise HTTP 500 with the raw exception
string. A failed commit can include SQL and bound values in that message.
An unexpected failure now returns HTTP 502 with a fixed sentence. The
exception is logged and is not returned. A rollback failure, including one
raised again while closing the session, still returns that same 502. An
HTTPException raised inside the handler is not rewritten. A missing
document stays HTTP 404. Another user's document stays HTTP 404 from the
existing owner check. An anonymous caller stays HTTP 403. Success stays
the _doc_to_dict payload and does not write a version.

The active-document pointer is cleared only after an unlink commit
succeeds. A failed unlink leaves the pointer and the session link in place.

updateTitle and updateLanguage apply the new title or language only when
the response is ok. The unlink toast is shown only when the response is
ok. None of those paths read the error body.
"""
import logging
import os
import uuid
from pathlib import Path

os.makedirs("data", exist_ok=True)

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import routes.document_routes as document_routes
from core.database import Document, DocumentVersion, SessionLocal
from core.database import Session as DbSession
from routes.document_helpers import _doc_to_dict
from src.tool_implementations import get_active_document, set_active_document

_COMMIT_SECRET = "COMMIT_SECRET sql=UPDATE documents title=secret-title token=patch-doc"
_ROLLBACK_SECRET = "ROLLBACK_SECRET connection reset while rolling back document metadata"
# The route looks up SessionLocal on this module at call time. Another test
# replaces that name with a temp engine; reads and cleanup use the app engine.
_REAL_SESSION = SessionLocal
_REPO = Path(__file__).resolve().parent.parent


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


def _add_session(owner):
    db = _real_session()
    try:
        sid = str(uuid.uuid4())
        db.add(DbSession(
            id=sid,
            name="health-session",
            endpoint_url="http://127.0.0.1:9",
            model="test",
            owner=owner,
        ))
        db.commit()
        return sid
    finally:
        db.close()


def _add_document(owner, title="Original title", language="markdown", session_id=None):
    db = _real_session()
    try:
        doc_id = str(uuid.uuid4())
        db.add(Document(
            id=doc_id,
            session_id=session_id,
            title=title,
            language=language,
            current_content="original notes",
            version_count=1,
            is_active=True,
            owner=owner,
        ))
        db.add(DocumentVersion(
            id=str(uuid.uuid4()),
            document_id=doc_id,
            version_number=1,
            content="original notes",
            summary="Initial version",
            source="ai",
        ))
        db.commit()
        return doc_id
    finally:
        db.close()


def _delete(doc_ids, session_ids=None):
    db = _real_session()
    try:
        if doc_ids:
            db.query(DocumentVersion).filter(
                DocumentVersion.document_id.in_(doc_ids)
            ).delete(synchronize_session=False)
            db.query(Document).filter(Document.id.in_(doc_ids)).delete(synchronize_session=False)
        if session_ids:
            db.query(DbSession).filter(DbSession.id.in_(session_ids)).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


def _load(doc_id):
    db = _real_session()
    try:
        doc = db.query(Document).filter(Document.id == doc_id).one()
        versions = db.query(DocumentVersion).filter(
            DocumentVersion.document_id == doc_id
        ).order_by(DocumentVersion.version_number.asc()).all()
        return doc, versions
    finally:
        db.close()


def _js(start_marker, end_marker):
    source = (_REPO / "static/js/document.js").read_text(encoding="utf-8")
    start = source.index(start_marker)
    end = source.index(end_marker, start)
    return source[start:end]


def test_patch_document_commit_failure_hides_exception(monkeypatch, caplog):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    doc_id = _add_document(owner)
    set_active_document(doc_id)
    _install_failing_session(monkeypatch, commit_exc=RuntimeError(_COMMIT_SECRET))
    caplog.set_level(logging.ERROR, logger="routes.document_routes")
    try:
        resp = _client(owner).patch(
            f"/api/document/{doc_id}",
            json={"title": "Renamed", "language": "python"},
        )
        assert resp.status_code == 502
        assert resp.json() == {"error": "Failed to update document metadata"}
        assert "detail" not in resp.json()
        assert _COMMIT_SECRET not in resp.text
        assert "Failed to update document:" not in resp.text
        assert _COMMIT_SECRET in caplog.text
        assert get_active_document() == doc_id
        doc, versions = _load(doc_id)
        assert doc.title == "Original title"
        assert doc.language == "markdown"
        assert doc.current_content == "original notes"
        assert doc.version_count == 1
        assert len(versions) == 1
    finally:
        _delete([doc_id])


def test_patch_document_rollback_failure_stays_fixed_502(monkeypatch, caplog):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    doc_id = _add_document(owner)
    _install_failing_session(
        monkeypatch,
        commit_exc=RuntimeError(_COMMIT_SECRET),
        rollback_exc=RuntimeError(_ROLLBACK_SECRET),
    )
    caplog.set_level(logging.ERROR, logger="routes.document_routes")
    try:
        resp = _client(owner).patch(
            f"/api/document/{doc_id}",
            json={"title": "Renamed"},
        )
        assert resp.status_code == 502
        assert resp.json() == {"error": "Failed to update document metadata"}
        assert "detail" not in resp.json()
        assert _COMMIT_SECRET not in resp.text
        assert _ROLLBACK_SECRET not in resp.text
        assert _COMMIT_SECRET in caplog.text
        assert _ROLLBACK_SECRET in caplog.text
        assert "Document metadata update rollback failed" in caplog.text
        assert "Document metadata update session close failed" in caplog.text
        doc, versions = _load(doc_id)
        assert doc.title == "Original title"
        assert doc.language == "markdown"
        assert len(versions) == 1
    finally:
        _delete([doc_id])


def test_patch_unlink_commit_failure_keeps_link_and_pointer(monkeypatch, caplog):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    sid = _add_session(owner)
    doc_id = _add_document(owner, session_id=sid)
    set_active_document(doc_id)
    _install_failing_session(monkeypatch, commit_exc=RuntimeError(_COMMIT_SECRET))
    caplog.set_level(logging.ERROR, logger="routes.document_routes")
    try:
        resp = _client(owner).patch(
            f"/api/document/{doc_id}",
            json={"session_id": ""},
        )
        assert resp.status_code == 502
        assert resp.json() == {"error": "Failed to update document metadata"}
        assert _COMMIT_SECRET not in resp.text
        assert get_active_document() == doc_id
        doc, versions = _load(doc_id)
        assert doc.session_id == sid
        assert doc.title == "Original title"
        assert len(versions) == 1
    finally:
        _delete([doc_id], [sid])


def test_patch_document_http_exception_is_not_rewritten(monkeypatch):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    doc_id = _add_document(owner)

    def _reject(*args, **kwargs):
        raise HTTPException(400, "Document metadata rejected")

    monkeypatch.setattr(document_routes, "_verify_doc_owner", _reject)
    try:
        resp = _client(owner).patch(
            f"/api/document/{doc_id}",
            json={"title": "Renamed"},
        )
        assert resp.status_code == 400
        assert resp.json() == {"detail": "Document metadata rejected"}
        assert resp.json().get("error") != "Failed to update document metadata"
        doc, _versions = _load(doc_id)
        assert doc.title == "Original title"
    finally:
        _delete([doc_id])


def test_patch_document_missing_document_stays_404(monkeypatch):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    missing = str(uuid.uuid4())
    resp = _client(owner).patch(
        f"/api/document/{missing}",
        json={"title": "Renamed"},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Document not found"
    assert "Failed to update document metadata" not in resp.text
    assert _COMMIT_SECRET not in resp.text


def test_patch_document_other_users_document_stays_404(monkeypatch):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    other = f"health-{uuid.uuid4().hex}"
    doc_id = _add_document(other)
    try:
        resp = _client(owner).patch(
            f"/api/document/{doc_id}",
            json={"title": "Renamed", "language": "python"},
        )
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Document not found"
        assert "Failed to update document metadata" not in resp.text
        doc, versions = _load(doc_id)
        assert doc.title == "Original title"
        assert doc.language == "markdown"
        assert doc.owner == other
        assert len(versions) == 1
    finally:
        _delete([doc_id])


def test_patch_document_anonymous_stays_403(monkeypatch):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    doc_id = _add_document(owner)
    try:
        resp = _client(None).patch(
            f"/api/document/{doc_id}",
            json={"title": "Renamed"},
        )
        assert resp.status_code == 403
        assert resp.json()["detail"] == "Authentication required"
        assert "Failed to update document metadata" not in resp.text
        doc, _versions = _load(doc_id)
        assert doc.title == "Original title"
    finally:
        _delete([doc_id])


def test_patch_document_success_keeps_doc_shape(monkeypatch):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    sid = _add_session(owner)
    doc_id = _add_document(owner, session_id=sid)
    set_active_document(doc_id)
    try:
        resp = _client(owner).patch(
            f"/api/document/{doc_id}",
            json={"title": "Renamed", "language": "python"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "error" not in body
        assert "detail" not in body
        assert _COMMIT_SECRET not in resp.text
        doc, versions = _load(doc_id)
        assert doc.owner == owner
        assert doc.title == "Renamed"
        assert doc.language == "python"
        assert doc.session_id == sid
        assert doc.current_content == "original notes"
        assert doc.version_count == 1
        assert body == _doc_to_dict(doc)
        assert len(versions) == 1
        assert versions[0].content == "original notes"
        assert get_active_document() == doc_id
    finally:
        _delete([doc_id], [sid])


def test_patch_unlink_success_clears_pointer_after_commit(monkeypatch):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    sid = _add_session(owner)
    doc_id = _add_document(owner, session_id=sid)
    set_active_document(doc_id)
    try:
        resp = _client(owner).patch(
            f"/api/document/{doc_id}",
            json={"session_id": ""},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "error" not in body
        doc, versions = _load(doc_id)
        assert doc.session_id is None
        assert doc.title == "Original title"
        assert body == _doc_to_dict(doc)
        assert len(versions) == 1
        assert get_active_document() is None
    finally:
        _delete([doc_id], [sid])


def test_update_title_client_checks_ok_before_applying_title():
    block = _js(
        "async function updateTitle(overrideDocId, overrideTitle)",
        "/** Auto-detect title from content",
    )
    guard = "if (!res.ok) throw new Error('Failed to update document metadata');"
    assert guard in block
    assert block.index(guard) < block.index("docs.get(docId).title = title")
    assert ".json()" not in block
    assert "detail" not in block
    assert "e.message" not in block
    assert "showToast" not in block
    assert "showError" not in block
    assert "console.error('Failed to update title')" in block
    commit = _js("function commitEdit()", "function cancelEdit()")
    assert "if (!newTitle)" in commit
    pending = commit[commit.index("if (!newTitle)"):commit.index("updateTitle(docId, newTitle)")]
    assert "doc.title = newTitle" in pending
    after = commit[commit.index("updateTitle(docId, newTitle)"):]
    assert "doc.title" not in after
    auto = _js("function autoTitleFromContent(content, docId)", "async function updateLanguage()")
    assert "updateTitle(id, title)" in auto
    assert "titleInput.value" not in auto


def test_update_language_client_checks_ok_before_applying_language():
    block = _js("async function updateLanguage()", "/** Clear all tab state")
    guard = "if (!res.ok) throw new Error('Failed to update document metadata');"
    assert guard in block
    assert block.index(guard) < block.index("doc.language = next")
    catch = block[block.index("} catch"):]
    assert "select.value = doc.language || ''" in catch
    assert "doc.language = next" not in catch
    assert ".json()" not in block
    assert "detail" not in block
    assert "e.message" not in block
    assert "showToast" not in block
    assert "showError" not in block
    assert "console.error('Failed to update language')" in block
    detect = _js("function attemptAutoDetect()", "// ---- Selection-based AI editing ----")
    assert "updateLanguage()" in detect
    assert "doc.language =" not in detect


def test_unlink_client_checks_ok_before_success_toast():
    block = _js("function _detachDocFromSession(", "async function closeTab(")
    assert "if (!res.ok)" in block
    assert block.index("if (!res.ok)") < block.index("Document unlinked from session")
    assert "console.error('Failed to unlink document')" in block
    assert ".json()" not in block
    assert "detail" not in block
    assert "e.message" not in block
