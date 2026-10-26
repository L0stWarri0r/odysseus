"""PDF text re-extraction must not return exception text.

POST /api/document/{doc_id}/extract-pdf-text used to raise HTTP 500 with
"Extraction failed: {exception}" when pypdf raised. A failed commit can
include SQL and bound values in that message. _process_pdf also swallows
its own errors and returns "[PDF processing failed: {exception}]", which
the route used to save into the document. An unexpected failure now
returns HTTP 502 with a fixed sentence. The exception is logged and is
not returned. A processor failure banner is not saved. A rollback
failure, including one raised again while closing the session, still
returns that same 502. An HTTPException raised inside the handler is not
rewritten: a missing document and another user's document stay HTTP 404,
a document with no PDF marker stays HTTP 400, a missing source file stays
HTTP 404, and an anonymous caller stays HTTP 403.

The editor already returns when the response is not ok, before it reads
the body, so it does not toast detail or the exception text.
"""
import logging
import os
import uuid
from pathlib import Path

os.makedirs("data", exist_ok=True)

from fastapi import FastAPI
from fastapi.testclient import TestClient

import routes.document_routes as document_routes
from core.database import Document, DocumentVersion, SessionLocal

_COMMIT_SECRET = "COMMIT_SECRET sql=UPDATE documents SET current_content=secret"
_ROLLBACK_SECRET = "ROLLBACK_SECRET connection reset while rolling back pdf extract"
_EXTRACT_SECRET = "EXTRACT_SECRET parser blew up reading /tmp/secret.pdf"
_BANNER_SECRET = "BANNER_SECRET pypdf encrypted trailer token=do-not-store"
_UPLOAD_ID = "abcdef0123456789abcdef0123456789.pdf"
_ORIGINAL = "old body that must stay until extraction succeeds"
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


def _pdf_content(body=_ORIGINAL):
    return (
        f'<!-- pdf_source upload_id="{_UPLOAD_ID}" -->\n\n'
        "# Field notes\n\n"
        f"{body}\n"
    )


def _client(username):
    app = FastAPI()

    @app.middleware("http")
    async def _stamp_user(request, call_next):
        if username is not None:
            request.state.current_user = username
        return await call_next(request)

    app.include_router(document_routes.setup_document_routes(None, object()))
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


def _install_resolver(monkeypatch, path="/tmp/owned.pdf"):
    monkeypatch.setattr(
        document_routes,
        "_resolve_user_upload_path",
        lambda *args, **kwargs: path,
    )


def _install_pdf(monkeypatch, fn):
    monkeypatch.setattr("src.document_processor._process_pdf", fn)


def _ok_pdf(path):
    return "\n\n[PDF content]:\n\n[Page 1 text]:\nHello from the scanned page"


def _empty_pdf(path):
    return "\n\n[PDF content]:"


def _raising_pdf(path):
    raise RuntimeError(_EXTRACT_SECRET)


def _banner_pdf(path):
    return f"\n\n[PDF processing failed: {_BANNER_SECRET}]"


def _forbidden_pdf(path):
    raise AssertionError("PDF processor should not run")


def _add_document(owner, content, title="Field notes"):
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
            is_active=True,
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
            db.query(DocumentVersion).filter(
                DocumentVersion.document_id.in_(doc_ids)
            ).delete(synchronize_session=False)
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
        versions = (
            db.query(DocumentVersion)
            .filter(DocumentVersion.document_id == doc_id)
            .all()
        )
        return {
            "title": doc.title,
            "current_content": doc.current_content,
            "version_count": doc.version_count,
            "owner": doc.owner,
            "versions": [
                {
                    "version_number": v.version_number,
                    "source": v.source,
                    "summary": v.summary,
                    "content": v.content,
                }
                for v in versions
            ],
        }
    finally:
        db.close()


def _post(username, doc_id):
    return _client(username).post(f"/api/document/{doc_id}/extract-pdf-text")


def _assert_fixed_502(resp):
    assert resp.status_code == 502
    assert resp.json() == {"error": "Failed to extract PDF text"}
    assert "detail" not in resp.json()
    assert _COMMIT_SECRET not in resp.text
    assert _ROLLBACK_SECRET not in resp.text
    assert _EXTRACT_SECRET not in resp.text
    assert _BANNER_SECRET not in resp.text
    assert "Extraction failed:" not in resp.text


def test_extract_commit_failure_hides_exception(monkeypatch, caplog):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    doc_id = _add_document(owner, _pdf_content())
    _install_resolver(monkeypatch)
    _install_pdf(monkeypatch, _ok_pdf)
    _install_failing_session(monkeypatch, commit_exc=RuntimeError(_COMMIT_SECRET))
    caplog.set_level(logging.ERROR, logger="routes.document_routes")
    try:
        resp = _post(owner, doc_id)
        _assert_fixed_502(resp)
        assert _COMMIT_SECRET in caplog.text
        assert "Failed to extract PDF text" in caplog.text
        row = _row(doc_id)
        assert row["current_content"] == _pdf_content()
        assert row["version_count"] == 1
        assert row["versions"] == []
    finally:
        _delete([doc_id])


def test_extract_rollback_and_close_failure_stays_fixed_502(monkeypatch, caplog):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    doc_id = _add_document(owner, _pdf_content())
    _install_resolver(monkeypatch)
    _install_pdf(monkeypatch, _ok_pdf)
    _install_failing_session(
        monkeypatch,
        commit_exc=RuntimeError(_COMMIT_SECRET),
        rollback_exc=RuntimeError(_ROLLBACK_SECRET),
    )
    caplog.set_level(logging.ERROR, logger="routes.document_routes")
    try:
        resp = _post(owner, doc_id)
        _assert_fixed_502(resp)
        assert _ROLLBACK_SECRET in caplog.text
        assert "PDF text extraction rollback failed" in caplog.text
        assert "PDF text extraction session close failed" in caplog.text
        assert _row(doc_id)["current_content"] == _pdf_content()
        assert _row(doc_id)["versions"] == []
    finally:
        _delete([doc_id])


def test_extract_processor_exception_hides_exception(monkeypatch, caplog):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    doc_id = _add_document(owner, _pdf_content())
    _install_resolver(monkeypatch)
    _install_pdf(monkeypatch, _raising_pdf)
    caplog.set_level(logging.ERROR, logger="routes.document_routes")
    try:
        resp = _post(owner, doc_id)
        _assert_fixed_502(resp)
        assert _EXTRACT_SECRET in caplog.text
        assert _row(doc_id)["current_content"] == _pdf_content()
        assert _row(doc_id)["versions"] == []
    finally:
        _delete([doc_id])


def test_extract_processor_failure_banner_is_not_stored(monkeypatch, caplog):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    doc_id = _add_document(owner, _pdf_content())
    _install_resolver(monkeypatch)
    _install_pdf(monkeypatch, _banner_pdf)
    caplog.set_level(logging.ERROR, logger="routes.document_routes")
    try:
        resp = _post(owner, doc_id)
        _assert_fixed_502(resp)
        assert _BANNER_SECRET in caplog.text
        row = _row(doc_id)
        assert _BANNER_SECRET not in row["current_content"]
        assert row["current_content"] == _pdf_content()
        assert row["versions"] == []
    finally:
        _delete([doc_id])


def test_extract_missing_document_stays_404(monkeypatch):
    _pin_session(monkeypatch)
    _install_pdf(monkeypatch, _forbidden_pdf)
    resp = _post(f"health-{uuid.uuid4().hex}", str(uuid.uuid4()))
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Document not found"
    assert "error" not in resp.json()


def test_extract_other_owner_stays_404(monkeypatch):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    stranger = f"health-{uuid.uuid4().hex}"
    doc_id = _add_document(owner, _pdf_content())
    _install_resolver(monkeypatch)
    _install_pdf(monkeypatch, _forbidden_pdf)
    try:
        resp = _post(stranger, doc_id)
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Document not found"
        assert _EXTRACT_SECRET not in resp.text
        assert _row(doc_id)["current_content"] == _pdf_content()
        assert _row(doc_id)["owner"] == owner
        assert _row(doc_id)["versions"] == []
    finally:
        _delete([doc_id])


def test_extract_anonymous_stays_403(monkeypatch):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    doc_id = _add_document(owner, _pdf_content())
    _install_resolver(monkeypatch)
    _install_pdf(monkeypatch, _forbidden_pdf)
    try:
        resp = _post(None, doc_id)
        assert resp.status_code == 403
        assert resp.json()["detail"] == "Authentication required"
        assert "error" not in resp.json()
        assert _row(doc_id)["current_content"] == _pdf_content()
    finally:
        _delete([doc_id])


def test_extract_without_pdf_marker_stays_400(monkeypatch):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    doc_id = _add_document(owner, "plain notes without a marker\n")
    _install_pdf(monkeypatch, _forbidden_pdf)
    try:
        resp = _post(owner, doc_id)
        assert resp.status_code == 400
        assert "pdf_source" in resp.json()["detail"]
        assert "error" not in resp.json()
        assert _row(doc_id)["current_content"] == "plain notes without a marker\n"
        assert _row(doc_id)["versions"] == []
    finally:
        _delete([doc_id])


def test_extract_missing_source_pdf_stays_404(monkeypatch):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    doc_id = _add_document(owner, _pdf_content())
    _install_resolver(monkeypatch, path=None)
    _install_pdf(monkeypatch, _forbidden_pdf)
    try:
        resp = _post(owner, doc_id)
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Source PDF could not be located"
        assert "error" not in resp.json()
        assert _row(doc_id)["current_content"] == _pdf_content()
        assert _row(doc_id)["versions"] == []
    finally:
        _delete([doc_id])


def test_extract_success_updates_caller_only(monkeypatch):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    other = f"health-{uuid.uuid4().hex}"
    doc_id = _add_document(owner, _pdf_content())
    other_id = _add_document(other, _pdf_content("other body"))
    _install_resolver(monkeypatch)
    _install_pdf(monkeypatch, _ok_pdf)
    try:
        resp = _post(owner, doc_id)
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["id"] == doc_id
        assert body["extracted"] is True
        assert body["chars"] > 0
        assert "error" not in body
        assert _EXTRACT_SECRET not in resp.text
        row = _row(doc_id)
        assert "Hello from the scanned page" in row["current_content"]
        assert _ORIGINAL not in row["current_content"]
        assert f'upload_id="{_UPLOAD_ID}"' in row["current_content"]
        assert row["current_content"].startswith("<!-- pdf_source")
        assert "# Field notes" in row["current_content"]
        assert row["version_count"] == 2
        assert len(row["versions"]) == 1
        assert row["versions"][0]["source"] == "ocr"
        assert row["versions"][0]["summary"] == "PDF text re-extracted (OCR)"
        assert row["versions"][0]["version_number"] == 2
        assert "Hello from the scanned page" in row["versions"][0]["content"]
        other_row = _row(other_id)
        assert other_row["current_content"] == _pdf_content("other body")
        assert other_row["version_count"] == 1
        assert other_row["versions"] == []
    finally:
        _delete([doc_id, other_id])


def test_extract_empty_text_leaves_document(monkeypatch):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    doc_id = _add_document(owner, _pdf_content())
    _install_resolver(monkeypatch)
    _install_pdf(monkeypatch, _empty_pdf)
    try:
        resp = _post(owner, doc_id)
        assert resp.status_code == 200
        assert resp.json() == {
            "ok": True,
            "id": doc_id,
            "extracted": False,
            "reason": "No readable content",
        }
        assert _row(doc_id)["current_content"] == _pdf_content()
        assert _row(doc_id)["version_count"] == 1
        assert _row(doc_id)["versions"] == []
    finally:
        _delete([doc_id])


def test_extract_client_does_not_read_error_body():
    source = (_REPO / "static/js/document.js").read_text(encoding="utf-8")
    start = source.index("api/document/${docId}/extract-pdf-text")
    end = source.index("const mdToolbar = document.getElementById('doc-md-toolbar');", start)
    block = source[start:end]
    ok_at = block.index("if (!r.ok) return;")
    json_at = block.index("r.json()")
    assert ok_at < json_at
    assert "detail" not in block
    assert "showToast" not in block
    assert "Extraction failed" not in block


def test_extract_route_does_not_interpolate_exception():
    source = (_REPO / "routes/document_routes.py").read_text(encoding="utf-8")
    start = source.index("async def extract_pdf_text")
    end = source.index("async def documents_export_zip", start)
    body = source[start:end]
    assert "Extraction failed:" not in body
    assert "str(e)" not in body
    assert "{e}" not in body
    assert 'document_error("Failed to extract PDF text")' in body
