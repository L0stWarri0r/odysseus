"""Document version restore must not return database exception text.

POST /api/document/{doc_id}/restore/{num} used to raise HTTP 500 with the raw
exception string. A failed commit can include SQL and bound values in that
message. An unexpected failure now returns HTTP 502 with a fixed sentence.
The exception is logged and is not returned. A rollback failure, including
one raised again while closing the session, still returns that same 502. An
HTTPException raised inside the handler is not rewritten. A missing document
stays HTTP 404. A missing version stays HTTP 404. Another user's document
stays HTTP 404 from the existing owner check. An anonymous caller stays
HTTP 403. Success stays the document dict and still appends the next user
version ("Restored from v{num}").

The active-document pointer is left alone. A failed restore does not change
current content or version rows. The editor checks res.ok before reading the
body, filling the editor, or toasting success, and it does not toast detail
or the exception object.
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

_COMMIT_SECRET = "COMMIT_SECRET sql=INSERT document_versions summary=Restored token=restore-doc"
_ROLLBACK_SECRET = "ROLLBACK_SECRET connection reset while rolling back document restore"
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
    """Two versions. Current content is the second. Restore targets version 1."""
    db = _real_session()
    try:
        doc_id = str(uuid.uuid4())
        db.add(Document(
            id=doc_id,
            session_id=session_id,
            title=title,
            language=language,
            current_content="edited notes",
            version_count=2,
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
        db.add(DocumentVersion(
            id=str(uuid.uuid4()),
            document_id=doc_id,
            version_number=2,
            content="edited notes",
            summary="Manual edit",
            source="user",
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


def _snapshot(doc_id):
    db = _real_session()
    try:
        doc = db.query(Document).filter(Document.id == doc_id).one()
        payload = _doc_to_dict(doc)
        versions = [
            {
                "version_number": v.version_number,
                "content": v.content,
                "summary": v.summary,
                "source": v.source,
            }
            for v in db.query(DocumentVersion).filter(
                DocumentVersion.document_id == doc_id
            ).order_by(DocumentVersion.version_number.asc()).all()
        ]
        state = {
            "is_active": doc.is_active,
            "title": doc.title,
            "language": doc.language,
            "current_content": doc.current_content,
            "version_count": doc.version_count,
            "owner": doc.owner,
            "session_id": doc.session_id,
        }
        return state, versions, payload
    finally:
        db.close()


def _js(path, start_marker, end_marker):
    source = (_REPO / path).read_text(encoding="utf-8")
    start = source.index(start_marker)
    end = source.index(end_marker, start)
    return source[start:end]


def test_restore_version_commit_failure_hides_exception(monkeypatch, caplog):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    doc_id = _add_document(owner)
    set_active_document(doc_id)
    _install_failing_session(monkeypatch, commit_exc=RuntimeError(_COMMIT_SECRET))
    caplog.set_level(logging.ERROR, logger="routes.document_routes")
    try:
        resp = _client(owner).post(f"/api/document/{doc_id}/restore/1")
        assert resp.status_code == 502
        assert resp.json() == {"error": "Failed to restore version"}
        assert "detail" not in resp.json()
        assert _COMMIT_SECRET not in resp.text
        assert "Failed to restore version:" not in resp.text
        assert _COMMIT_SECRET in caplog.text
        assert get_active_document() == doc_id
        state, versions, _payload = _snapshot(doc_id)
        assert state["current_content"] == "edited notes"
        assert state["version_count"] == 2
        assert state["is_active"] is True
        assert state["title"] == "Original title"
        assert state["owner"] == owner
        assert versions == [
            {
                "version_number": 1,
                "content": "original notes",
                "summary": "Initial version",
                "source": "ai",
            },
            {
                "version_number": 2,
                "content": "edited notes",
                "summary": "Manual edit",
                "source": "user",
            },
        ]
    finally:
        _delete([doc_id])
        set_active_document(None)


def test_restore_version_rollback_failure_stays_fixed_502(monkeypatch, caplog):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    doc_id = _add_document(owner)
    set_active_document(doc_id)
    _install_failing_session(
        monkeypatch,
        commit_exc=RuntimeError(_COMMIT_SECRET),
        rollback_exc=RuntimeError(_ROLLBACK_SECRET),
    )
    caplog.set_level(logging.ERROR, logger="routes.document_routes")
    try:
        resp = _client(owner).post(f"/api/document/{doc_id}/restore/1")
        assert resp.status_code == 502
        assert resp.json() == {"error": "Failed to restore version"}
        assert "detail" not in resp.json()
        assert _COMMIT_SECRET not in resp.text
        assert _ROLLBACK_SECRET not in resp.text
        assert _COMMIT_SECRET in caplog.text
        assert _ROLLBACK_SECRET in caplog.text
        assert "Document restore rollback failed" in caplog.text
        assert "Document restore session close failed" in caplog.text
        assert get_active_document() == doc_id
        state, versions, _payload = _snapshot(doc_id)
        assert state["current_content"] == "edited notes"
        assert state["version_count"] == 2
        assert len(versions) == 2
    finally:
        _delete([doc_id])
        set_active_document(None)


def test_restore_version_http_exception_is_not_rewritten(monkeypatch):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    doc_id = _add_document(owner)

    def _reject(*args, **kwargs):
        raise HTTPException(400, "Document restore rejected")

    monkeypatch.setattr(document_routes, "_verify_doc_owner", _reject)
    try:
        resp = _client(owner).post(f"/api/document/{doc_id}/restore/1")
        assert resp.status_code == 400
        assert resp.json() == {"detail": "Document restore rejected"}
        assert resp.json().get("error") != "Failed to restore version"
        state, versions, _payload = _snapshot(doc_id)
        assert state["current_content"] == "edited notes"
        assert state["version_count"] == 2
        assert len(versions) == 2
    finally:
        _delete([doc_id])


def test_restore_version_missing_document_stays_404(monkeypatch):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    missing = str(uuid.uuid4())
    resp = _client(owner).post(f"/api/document/{missing}/restore/1")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Document not found"
    assert "Failed to restore version" not in resp.text
    assert _COMMIT_SECRET not in resp.text


def test_restore_version_missing_version_stays_404(monkeypatch):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    doc_id = _add_document(owner)
    set_active_document(doc_id)
    try:
        resp = _client(owner).post(f"/api/document/{doc_id}/restore/9")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Version not found"
        assert "Failed to restore version" not in resp.text
        assert get_active_document() == doc_id
        state, versions, _payload = _snapshot(doc_id)
        assert state["current_content"] == "edited notes"
        assert state["version_count"] == 2
        assert len(versions) == 2
    finally:
        _delete([doc_id])
        set_active_document(None)


def test_restore_version_other_users_document_stays_404(monkeypatch):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    other = f"health-{uuid.uuid4().hex}"
    doc_id = _add_document(other)
    set_active_document(doc_id)
    try:
        resp = _client(owner).post(f"/api/document/{doc_id}/restore/1")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Document not found"
        assert "Failed to restore version" not in resp.text
        state, versions, _payload = _snapshot(doc_id)
        assert state["current_content"] == "edited notes"
        assert state["title"] == "Original title"
        assert state["owner"] == other
        assert state["version_count"] == 2
        assert len(versions) == 2
        assert get_active_document() == doc_id
    finally:
        _delete([doc_id])
        set_active_document(None)


def test_restore_version_anonymous_stays_403(monkeypatch):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    doc_id = _add_document(owner)
    set_active_document(doc_id)
    try:
        resp = _client(None).post(f"/api/document/{doc_id}/restore/1")
        assert resp.status_code == 403
        assert resp.json()["detail"] == "Authentication required"
        assert "Failed to restore version" not in resp.text
        state, versions, _payload = _snapshot(doc_id)
        assert state["current_content"] == "edited notes"
        assert state["version_count"] == 2
        assert len(versions) == 2
        assert get_active_document() == doc_id
    finally:
        _delete([doc_id])
        set_active_document(None)


def test_restore_version_success_appends_user_version_and_keeps_pointer(monkeypatch):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    sid = _add_session(owner)
    doc_id = _add_document(owner, session_id=sid)
    set_active_document(doc_id)
    try:
        resp = _client(owner).post(f"/api/document/{doc_id}/restore/1")
        assert resp.status_code == 200
        state, versions, payload = _snapshot(doc_id)
        body = resp.json()
        assert body == payload
        assert "error" not in body
        assert "detail" not in body
        assert _COMMIT_SECRET not in resp.text
        assert state["current_content"] == "original notes"
        assert state["version_count"] == 3
        assert state["is_active"] is True
        assert state["title"] == "Original title"
        assert state["language"] == "markdown"
        assert state["owner"] == owner
        assert state["session_id"] == sid
        assert versions == [
            {
                "version_number": 1,
                "content": "original notes",
                "summary": "Initial version",
                "source": "ai",
            },
            {
                "version_number": 2,
                "content": "edited notes",
                "summary": "Manual edit",
                "source": "user",
            },
            {
                "version_number": 3,
                "content": "original notes",
                "summary": "Restored from v1",
                "source": "user",
            },
        ]
        assert get_active_document() == doc_id
    finally:
        _delete([doc_id], [sid])
        set_active_document(None)


def test_restore_other_document_leaves_active_pointer(monkeypatch):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    active_id = _add_document(owner, title="Still open")
    other_id = _add_document(owner, title="Restoring")
    set_active_document(active_id)
    try:
        resp = _client(owner).post(f"/api/document/{other_id}/restore/1")
        assert resp.status_code == 200
        assert resp.json()["id"] == other_id
        assert resp.json()["current_content"] == "original notes"
        assert resp.json()["version_count"] == 3
        assert "error" not in resp.json()
        assert get_active_document() == active_id
        active, active_versions, _active_payload = _snapshot(active_id)
        other, other_versions, _other_payload = _snapshot(other_id)
        assert active["current_content"] == "edited notes"
        assert active["version_count"] == 2
        assert len(active_versions) == 2
        assert other["current_content"] == "original notes"
        assert other["version_count"] == 3
        assert other_versions[-1]["summary"] == "Restored from v1"
    finally:
        _delete([active_id, other_id])
        set_active_document(None)


def test_restore_handler_does_not_clear_active_pointer():
    source = (_REPO / "routes/document_routes.py").read_text(encoding="utf-8")
    start = source.index("async def restore_version")
    end = source.index("async def tidy_documents", start)
    block = source[start:end]
    assert "clear_active_document" not in block
    assert 'document_error("Failed to restore version")' in block
    assert "str(e)" not in block


def test_restore_version_checks_ok_before_editor_and_toast():
    block = _js(
        "static/js/document.js",
        "async function restoreVersion(",
        "/** Update document title via PATCH */",
    )
    ok_at = block.index("if (!res.ok)")
    json_at = block.index("const doc = await res.json();")
    populate_at = block.index("populateEditor(doc)")
    toast_at = block.index("showToast(`Restored to v${num}`)")
    return_at = block.index("return;", ok_at)
    assert ok_at != -1
    assert ok_at < return_at < json_at < populate_at < toast_at
    assert "console.error('Failed to restore version')" in block
    assert "showError('Failed to restore version')" in block
    assert block.count("showError('Failed to restore version')") == 2
    assert "console.error('Failed to restore version:', e)" not in block
    assert "catch (e)" not in block
    assert "detail" not in block
    assert "e.message" not in block
