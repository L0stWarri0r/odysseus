"""PDF import must not return upload exception text.

POST /api/documents/import-pdf used to raise HTTP 500 with
"Upload failed: {exception}". save_upload can include paths and filenames
in that message. An unexpected save failure now returns HTTP 502 with a
fixed sentence. The exception is logged and is not returned. An
HTTPException from save_upload (empty file, type, rate limit) is not
rewritten. A missing session stays HTTP 404. Another user's session stays
HTTP 403. A missing upload handler stays HTTP 500 "Upload handler not
configured" and does not call save_upload. Success stays the _doc_to_dict
payload with no error key.

The library file import throws a fixed "PDF import failed" and does not
read detail or error from the body.
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
from core.database import Session as DbSession
from routes.document_helpers import _doc_to_dict

_SAVE_SECRET = "SAVE_SECRET upload path=/data/uploads/secret-report.pdf token=pdf-import"
_PRIVATE = "leaked-pdf-body"
# The route looks up SessionLocal on this module at call time. Another test
# replaces that name with a temp engine; reads and cleanup use the app engine.
_REAL_SESSION = SessionLocal
_REPO = Path(__file__).resolve().parent.parent


class _UploadHandler:
    def __init__(self, exc=None, meta=None):
        self.calls = []
        self.exc = exc
        self.meta = meta or {"id": "upload-health", "original_name": "Report.pdf"}

    def save_upload(self, upload, client_ip, owner=None):
        self.calls.append({
            "filename": getattr(upload, "filename", None),
            "client_ip": client_ip,
            "owner": owner,
        })
        if self.exc is not None:
            raise self.exc
        return self.meta


def _client(username, handler):
    app = FastAPI()

    @app.middleware("http")
    async def _stamp_user(request, call_next):
        if username is not None:
            request.state.current_user = username
        return await call_next(request)

    app.include_router(document_routes.setup_document_routes(None, handler))
    return TestClient(app)


def _real_session():
    return _REAL_SESSION()


def _add_session(owner, name="Health chat"):
    db = _real_session()
    try:
        session_id = uuid.uuid4().hex
        db.add(DbSession(
            id=session_id,
            name=name,
            endpoint_url="http://127.0.0.1:9",
            model="health-model",
            owner=owner,
        ))
        db.commit()
        return session_id
    finally:
        db.close()


def _delete(doc_ids, session_ids):
    db = _real_session()
    try:
        if doc_ids:
            db.query(Document).filter(Document.id.in_(doc_ids)).delete(synchronize_session=False)
        if session_ids:
            db.query(DbSession).filter(DbSession.id.in_(session_ids)).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


def _pdf():
    return {"file": ("Report.pdf", b"%PDF-1.4\n", "application/pdf")}


def _pin_session(monkeypatch):
    monkeypatch.setattr(document_routes, "SessionLocal", _REAL_SESSION)


def test_import_pdf_save_failure_hides_exception(monkeypatch, caplog):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    handler = _UploadHandler(exc=RuntimeError(_SAVE_SECRET))
    caplog.set_level(logging.ERROR, logger="routes.document_routes")
    resp = _client(owner, handler).post("/api/documents/import-pdf", files=_pdf())
    assert resp.status_code == 502
    assert resp.json() == {"error": "PDF import failed"}
    assert "detail" not in resp.json()
    assert _SAVE_SECRET not in resp.text
    assert "Upload failed" not in resp.text
    assert _SAVE_SECRET in caplog.text
    assert len(handler.calls) == 1
    assert handler.calls[0]["owner"] == owner


def test_import_pdf_http_exception_is_not_rewritten(monkeypatch):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    handler = _UploadHandler(exc=HTTPException(400, "File is empty"))
    resp = _client(owner, handler).post("/api/documents/import-pdf", files=_pdf())
    assert resp.status_code == 400
    assert resp.json() == {"detail": "File is empty"}
    assert "PDF import failed" not in resp.text
    assert len(handler.calls) == 1


def test_import_pdf_missing_session_stays_404(monkeypatch):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    handler = _UploadHandler(exc=RuntimeError(_SAVE_SECRET))
    missing = uuid.uuid4().hex
    resp = _client(owner, handler).post(
        "/api/documents/import-pdf",
        files=_pdf(),
        data={"session_id": missing},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Session not found"
    assert _SAVE_SECRET not in resp.text
    assert handler.calls == []


def test_import_pdf_other_users_session_stays_403(monkeypatch):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    other = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(other)
    handler = _UploadHandler(exc=RuntimeError(_SAVE_SECRET))
    try:
        resp = _client(owner, handler).post(
            "/api/documents/import-pdf",
            files=_pdf(),
            data={"session_id": session_id},
        )
        assert resp.status_code == 403
        assert resp.json()["detail"] == "Cannot import into another user's session"
        assert _SAVE_SECRET not in resp.text
        assert handler.calls == []
    finally:
        _delete([], [session_id])


def test_import_pdf_missing_handler_does_not_save(monkeypatch):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    resp = _client(owner, None).post("/api/documents/import-pdf", files=_pdf())
    assert resp.status_code == 500
    assert resp.json() == {"detail": "Upload handler not configured"}
    assert "PDF import failed" not in resp.text


def test_import_pdf_success_keeps_doc_shape(monkeypatch):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner)
    created = []
    handler = _UploadHandler(meta={"id": "upload-health", "original_name": "Report.pdf"})

    def _fake_plain(session_id, upload_id, title, body_text=None):
        db = _real_session()
        try:
            doc_id = uuid.uuid4().hex
            db.add(Document(
                id=doc_id,
                session_id=session_id,
                title=title,
                language="markdown",
                current_content=body_text or "",
                version_count=1,
                is_active=True,
                owner=None,
            ))
            db.commit()
            created.append(doc_id)
            return doc_id
        finally:
            db.close()

    monkeypatch.setattr(document_routes, "_resolve_user_upload_path", lambda *args, **kwargs: "/tmp/health.pdf")
    monkeypatch.setattr("src.pdf_forms.has_form_fields", lambda path: False)
    monkeypatch.setattr("src.document_processor._process_pdf", lambda path: _PRIVATE)
    monkeypatch.setattr("src.document_processor.strip_pdf_content_marker", lambda text: text)

    import src.pdf_form_doc as pdf_form_doc
    monkeypatch.setattr(pdf_form_doc, "create_plain_pdf_document", _fake_plain)

    try:
        library = _client(owner, handler).post("/api/documents/import-pdf", files=_pdf())
        assert library.status_code == 200
        body = library.json()
        assert "error" not in body
        assert _SAVE_SECRET not in library.text
        db = _real_session()
        try:
            doc = db.query(Document).filter(Document.id == body["id"]).one()
            assert doc.owner == owner
            assert doc.title == "Report"
            assert doc.session_id is None
            assert doc.current_content == _PRIVATE
            assert body == _doc_to_dict(doc)
        finally:
            db.close()

        owned = _client(owner, handler).post(
            "/api/documents/import-pdf",
            files=_pdf(),
            data={"session_id": session_id},
        )
        assert owned.status_code == 200
        owned_body = owned.json()
        assert "error" not in owned_body
        assert owned_body["session_id"] == session_id
        assert owned_body["title"] == "Report"
        assert owned_body["current_content"] == _PRIVATE
        db = _real_session()
        try:
            doc = db.query(Document).filter(Document.id == owned_body["id"]).one()
            assert doc.owner == owner
            assert owned_body == _doc_to_dict(doc)
        finally:
            db.close()
        assert len(handler.calls) == 2
    finally:
        _delete(created, [session_id])


def test_library_pdf_import_toast_ignores_response_body():
    source = (_REPO / "static/js/documentLibrary.js").read_text(encoding="utf-8")
    start = source.index("const res = await fetch(`${API_BASE}/api/documents/import-pdf`")
    end = source.index("imported++;", start)
    block = source[start:end]
    assert "if (!res.ok)" in block
    assert "throw new Error('PDF import failed')" in block
    assert ".json()" not in block
    assert "detail" not in block
    assert "error" not in block


def test_editor_pdf_import_does_not_read_error_body():
    source = (_REPO / "static/js/document.js").read_text(encoding="utf-8")
    start = source.index("const r = await fetch(`${API_BASE}/api/documents/import-pdf`")
    end = source.index("docId = j.doc_id", start)
    block = source[start:end]
    assert "if (!r.ok) throw new Error('PDF import failed');" in block
    assert block.index("PDF import failed") < block.index("await r.json()")
