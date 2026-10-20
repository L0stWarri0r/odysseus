"""Saving a document must not return database exception text.

PUT /api/document/{doc_id} used to raise HTTP 500 with
"Failed to update document: {exception}". A failed commit can include SQL
and bound values in that message. An unexpected failure now returns HTTP
502 with a fixed sentence. The exception is logged and is not returned. A
rollback failure, including one raised again while closing the session,
still returns that same 502. An HTTPException raised inside the handler is
not rewritten. A missing document stays HTTP 404. Another user's document
stays HTTP 404 from the existing owner check. Identical content still
returns the current document and does not write a version. Success stays
the _doc_to_dict payload with no error key.

saveDocument checks res.ok before reading the body, so a JSON error is not
treated as a saved document. The toast stays the fixed sentence.
"""
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.makedirs("data", exist_ok=True)

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import routes.document_routes as document_routes
from core.database import Document, DocumentVersion, SessionLocal
from routes.document_helpers import _doc_to_dict

_COMMIT_SECRET = "COMMIT_SECRET sql=UPDATE documents content=secret-notes token=update-doc"
_ROLLBACK_SECRET = "ROLLBACK_SECRET connection reset while rolling back document update"
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


def _add_document(owner, content="original notes", version_count=1):
    db = _real_session()
    try:
        doc_id = str(uuid.uuid4())
        db.add(Document(
            id=doc_id,
            title=f"health-update-{uuid.uuid4().hex}",
            language="markdown",
            current_content=content,
            version_count=version_count,
            is_active=True,
            owner=owner,
        ))
        db.add(DocumentVersion(
            id=str(uuid.uuid4()),
            document_id=doc_id,
            version_number=1,
            content=content,
            summary="Initial version",
            source="ai",
            created_at=datetime.now(timezone.utc) - timedelta(hours=2),
        ))
        db.commit()
        return doc_id
    finally:
        db.close()


def _delete(doc_ids):
    db = _real_session()
    try:
        if doc_ids:
            db.query(DocumentVersion).filter(
                DocumentVersion.document_id.in_(doc_ids)
            ).delete(synchronize_session=False)
            db.query(Document).filter(Document.id.in_(doc_ids)).delete(synchronize_session=False)
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


def _version_count(doc_id):
    db = _real_session()
    try:
        return db.query(DocumentVersion).filter(
            DocumentVersion.document_id == doc_id
        ).count()
    finally:
        db.close()


def test_update_document_commit_failure_hides_exception(monkeypatch, caplog):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    doc_id = _add_document(owner)
    _install_failing_session(monkeypatch, commit_exc=RuntimeError(_COMMIT_SECRET))
    caplog.set_level(logging.ERROR, logger="routes.document_routes")
    try:
        resp = _client(owner).put(
            f"/api/document/{doc_id}",
            json={"content": "edited notes"},
        )
        assert resp.status_code == 502
        assert resp.json() == {"error": "Failed to update document"}
        assert "detail" not in resp.json()
        assert _COMMIT_SECRET not in resp.text
        assert "Failed to update document:" not in resp.text
        assert _COMMIT_SECRET in caplog.text
        doc, versions = _load(doc_id)
        assert doc.current_content == "original notes"
        assert doc.version_count == 1
        assert len(versions) == 1
        assert versions[0].content == "original notes"
    finally:
        _delete([doc_id])


def test_update_document_rollback_failure_stays_fixed_502(monkeypatch, caplog):
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
        resp = _client(owner).put(
            f"/api/document/{doc_id}",
            json={"content": "edited notes"},
        )
        assert resp.status_code == 502
        assert resp.json() == {"error": "Failed to update document"}
        assert "detail" not in resp.json()
        assert _COMMIT_SECRET not in resp.text
        assert _ROLLBACK_SECRET not in resp.text
        assert _COMMIT_SECRET in caplog.text
        assert _ROLLBACK_SECRET in caplog.text
        assert "Document update rollback failed" in caplog.text
        assert "Document update session close failed" in caplog.text
        doc, versions = _load(doc_id)
        assert doc.current_content == "original notes"
        assert doc.version_count == 1
        assert len(versions) == 1
    finally:
        _delete([doc_id])


def test_update_document_http_exception_is_not_rewritten(monkeypatch):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    doc_id = _add_document(owner)

    def _reject(*args, **kwargs):
        raise HTTPException(400, "Document PDF marker references an upload you do not own")

    monkeypatch.setattr(document_routes, "_assert_pdf_marker_upload_owned", _reject)
    try:
        resp = _client(owner).put(
            f"/api/document/{doc_id}",
            json={"content": "pdf marker"},
        )
        assert resp.status_code == 400
        assert resp.json() == {"detail": "Document PDF marker references an upload you do not own"}
        assert resp.json().get("error") != "Failed to update document"
        doc, versions = _load(doc_id)
        assert doc.current_content == "original notes"
        assert len(versions) == 1
    finally:
        _delete([doc_id])


def test_update_document_missing_document_stays_404(monkeypatch):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    missing = str(uuid.uuid4())
    resp = _client(owner).put(
        f"/api/document/{missing}",
        json={"content": "edited notes"},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Document not found"
    assert "Failed to update document" not in resp.text
    assert _COMMIT_SECRET not in resp.text
    assert _version_count(missing) == 0


def test_update_document_other_users_document_stays_404(monkeypatch):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    other = f"health-{uuid.uuid4().hex}"
    doc_id = _add_document(other)
    try:
        resp = _client(owner).put(
            f"/api/document/{doc_id}",
            json={"content": "edited notes"},
        )
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Document not found"
        assert "Failed to update document" not in resp.text
        doc, versions = _load(doc_id)
        assert doc.current_content == "original notes"
        assert doc.owner == other
        assert len(versions) == 1
    finally:
        _delete([doc_id])


def test_update_document_anonymous_stays_403(monkeypatch):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    doc_id = _add_document(owner)
    try:
        resp = _client(None).put(
            f"/api/document/{doc_id}",
            json={"content": "edited notes"},
        )
        assert resp.status_code == 403
        assert resp.json()["detail"] == "Authentication required"
        assert "Failed to update document" not in resp.text
        doc, _versions = _load(doc_id)
        assert doc.current_content == "original notes"
    finally:
        _delete([doc_id])


def test_update_document_identical_content_does_not_version(monkeypatch):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    doc_id = _add_document(owner, content="same notes")
    _install_failing_session(monkeypatch, commit_exc=RuntimeError(_COMMIT_SECRET))
    try:
        resp = _client(owner).put(
            f"/api/document/{doc_id}",
            json={"content": "same notes"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "error" not in body
        assert "detail" not in body
        assert _COMMIT_SECRET not in resp.text
        doc, versions = _load(doc_id)
        assert doc.current_content == "same notes"
        assert doc.version_count == 1
        assert len(versions) == 1
        assert body == _doc_to_dict(doc)
    finally:
        _delete([doc_id])


def test_update_document_success_keeps_doc_shape(monkeypatch):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    doc_id = _add_document(owner, content="original notes")
    try:
        resp = _client(owner).put(
            f"/api/document/{doc_id}",
            json={"content": "edited notes", "summary": "Manual edit"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "error" not in body
        assert "detail" not in body
        assert _COMMIT_SECRET not in resp.text
        doc, versions = _load(doc_id)
        assert doc.owner == owner
        assert doc.current_content == "edited notes"
        assert doc.version_count == 2
        assert body == _doc_to_dict(doc)
        assert len(versions) == 2
        assert versions[0].content == "original notes"
        assert versions[0].version_number == 1
        assert versions[1].content == "edited notes"
        assert versions[1].version_number == 2
        assert versions[1].source == "user"
        assert versions[1].summary == "Manual edit"
    finally:
        _delete([doc_id])


def test_save_document_client_checks_ok_before_json():
    source = (_REPO / "static/js/document.js").read_text(encoding="utf-8")
    start = source.index("export async function saveDocument({ silent = false } = {})")
    end = source.index("/** Export/download the active document */", start)
    block = source[start:end]
    guard = "if (!res.ok) throw new Error('Failed to save document');"
    assert guard in block
    assert block.index(guard) < block.index("const doc = await res.json();")
    assert block.index("const doc = await res.json();") < block.index("uiModule.showToast('Document saved')")
    failure = block[block.index(guard):block.index("const doc = await res.json();")]
    assert ".json()" not in failure
    assert "detail" not in failure
    assert "error" not in failure
    assert "showError('Failed to save document')" in block
    assert "showToast('Document saved')" in block
    assert "e.message" not in block
    assert "detail" not in block
