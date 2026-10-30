"""PDF export preview must not leak exception text.

POST /api/document/{doc_id}/export-pdf/preview had no handler around field
loading, and closed the database session without a guard. A sidecar error, a
markdown parse error, or a session error escaped as an unhandled failure. A
close failure could also replace a successful preview or an HTTPException.
An unexpected failure now returns HTTP 502 with a fixed sentence. The
exception is logged and is not returned. A rollback failure, including one
raised again while closing the session, still returns that same 502. An
HTTPException raised inside the handler is still passed through: a missing
document and another user's document stay HTTP 404, a document with no PDF
marker stays HTTP 400, a missing source file stays HTTP 404, a missing field
sidecar stays HTTP 404, and an anonymous caller stays HTTP 403.

The preview modal shows that fixed sentence and does not append the response
body or the client exception text.
"""
import json
import logging
import os
import uuid
from pathlib import Path

os.makedirs("data", exist_ok=True)

from fastapi import FastAPI
from fastapi.testclient import TestClient

import routes.document_routes as document_routes
from core.database import Document, DocumentVersion, SessionLocal

_PARSE_SECRET = "PARSE_SECRET markdown field decoder token=do-not-return"
_SIDECAR_SECRET = "SIDECAR_SECRET field schema path=/tmp/secret-sidecar.json token=do-not-return"
_ROLLBACK_SECRET = "ROLLBACK_SECRET connection reset while rolling back pdf preview"
_UPLOAD_ID = "abcdef0123456789abcdef0123456789.pdf"
_ORIGINAL = "old body that must stay while the pdf preview is loaded"
# The route looks up SessionLocal on this module at call time. Another test
# replaces that name with a temp engine; reads and cleanup use the app engine.
_REAL_SESSION = SessionLocal
_REPO = Path(__file__).resolve().parent.parent
_FIXED_ERROR = "Failed to load preview"


class _FailingSession:
    """Session whose rollback raises, and whose close rolls back."""

    def __init__(self, inner, rollback_exc=None):
        self._inner = inner
        self._rollback_exc = rollback_exc

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


def _pdf_content(body=_ORIGINAL, include_fields=True):
    lines = [
        f'<!-- pdf_source upload_id="{_UPLOAD_ID}" -->',
        "",
        "# Field notes",
        "",
        body,
    ]
    if include_fields:
        lines.extend([
            "- **City:** Springfield <!-- field=city type=text -->",
            "- **Notes:** _(empty)_ <!-- field=notes type=text -->",
            "- **Ghost:** hello <!-- field=ghost type=text -->",
        ])
    return "\n".join(lines) + "\n"


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


def _install_failing_session(monkeypatch, rollback_exc=None):
    def _factory():
        return _FailingSession(_REAL_SESSION(), rollback_exc=rollback_exc)

    monkeypatch.setattr(document_routes, "SessionLocal", _factory)


def _install_resolver(monkeypatch, path="/tmp/owned.pdf"):
    monkeypatch.setattr(
        document_routes,
        "_resolve_user_upload_path",
        lambda *args, **kwargs: path,
    )


def _raising_parse(content):
    raise RuntimeError(_PARSE_SECRET)


def _raising_sidecar(path):
    raise RuntimeError(_SIDECAR_SECRET)


def _forbidden_parse(content):
    raise AssertionError("parse_markdown_to_values should not run")


def _forbidden_sidecar(path):
    raise AssertionError("load_field_sidecar should not run")


def _write_sidecar(tmp_path):
    pdf_path = tmp_path / "owned.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%preview\n")
    sidecar = {
        "fields": [
            {"name": "city", "label": "City", "type": "text", "options": [], "page": 1},
            {"name": "notes", "label": "Notes", "type": "text", "options": [], "page": 2},
            {"name": "unused", "label": "Unused", "type": "checkbox", "options": [], "page": 1},
        ]
    }
    # load_field_sidecar reads a list, not a wrapper object.
    (tmp_path / "owned.pdf.fields.json").write_text(
        json.dumps(sidecar["fields"]),
        encoding="utf-8",
    )
    return str(pdf_path)


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
    return _client(username).post(f"/api/document/{doc_id}/export-pdf/preview")


def _assert_fixed_502(resp):
    assert resp.status_code == 502
    assert resp.json() == {"error": _FIXED_ERROR}
    assert "detail" not in resp.json()
    assert _PARSE_SECRET not in resp.text
    assert _SIDECAR_SECRET not in resp.text
    assert _ROLLBACK_SECRET not in resp.text
    assert "PDF fill failed:" not in resp.text


def test_preview_parse_failure_hides_exception(monkeypatch, caplog):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    content = _pdf_content()
    doc_id = _add_document(owner, content)
    _install_resolver(monkeypatch)
    monkeypatch.setattr("src.pdf_form_doc.parse_markdown_to_values", _raising_parse)
    monkeypatch.setattr(
        "src.pdf_form_doc.load_field_sidecar",
        lambda path: [{"name": "city", "label": "City", "type": "text", "page": 1}],
    )
    caplog.set_level(logging.ERROR, logger="routes.document_routes")
    try:
        resp = _post(owner, doc_id)
        _assert_fixed_502(resp)
        assert _PARSE_SECRET in caplog.text
        assert _FIXED_ERROR in caplog.text
        row = _row(doc_id)
        assert row["current_content"] == content
        assert row["version_count"] == 1
        assert row["versions"] == []
    finally:
        _delete([doc_id])


def test_preview_sidecar_failure_hides_exception(monkeypatch, caplog):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    content = _pdf_content()
    doc_id = _add_document(owner, content)
    _install_resolver(monkeypatch)
    monkeypatch.setattr("src.pdf_form_doc.load_field_sidecar", _raising_sidecar)
    monkeypatch.setattr("src.pdf_form_doc.parse_markdown_to_values", _forbidden_parse)
    caplog.set_level(logging.ERROR, logger="routes.document_routes")
    try:
        resp = _post(owner, doc_id)
        _assert_fixed_502(resp)
        assert _SIDECAR_SECRET in caplog.text
        assert _row(doc_id)["current_content"] == content
        assert _row(doc_id)["versions"] == []
    finally:
        _delete([doc_id])


def test_preview_rollback_and_close_failure_stays_fixed_502(monkeypatch, caplog):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    content = _pdf_content()
    doc_id = _add_document(owner, content)
    _install_resolver(monkeypatch)
    monkeypatch.setattr("src.pdf_form_doc.parse_markdown_to_values", _raising_parse)
    monkeypatch.setattr(
        "src.pdf_form_doc.load_field_sidecar",
        lambda path: [{"name": "city", "label": "City", "type": "text", "page": 1}],
    )
    _install_failing_session(monkeypatch, rollback_exc=RuntimeError(_ROLLBACK_SECRET))
    caplog.set_level(logging.ERROR, logger="routes.document_routes")
    try:
        resp = _post(owner, doc_id)
        _assert_fixed_502(resp)
        assert _PARSE_SECRET in caplog.text
        assert _ROLLBACK_SECRET in caplog.text
        assert "PDF preview rollback failed" in caplog.text
        assert "PDF preview session close failed" in caplog.text
        assert _row(doc_id)["current_content"] == content
        assert _row(doc_id)["versions"] == []
    finally:
        _delete([doc_id])


def test_preview_missing_document_stays_404(monkeypatch):
    _pin_session(monkeypatch)
    monkeypatch.setattr("src.pdf_form_doc.parse_markdown_to_values", _forbidden_parse)
    monkeypatch.setattr("src.pdf_form_doc.load_field_sidecar", _forbidden_sidecar)
    resp = _post(f"health-{uuid.uuid4().hex}", str(uuid.uuid4()))
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Document not found"
    assert "error" not in resp.json()


def test_preview_missing_document_close_failure_stays_404(monkeypatch, caplog):
    _pin_session(monkeypatch)
    _install_failing_session(monkeypatch, rollback_exc=RuntimeError(_ROLLBACK_SECRET))
    monkeypatch.setattr("src.pdf_form_doc.parse_markdown_to_values", _forbidden_parse)
    caplog.set_level(logging.ERROR, logger="routes.document_routes")
    resp = _post(f"health-{uuid.uuid4().hex}", str(uuid.uuid4()))
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Document not found"
    assert "error" not in resp.json()
    assert _ROLLBACK_SECRET not in resp.text
    assert "PDF preview session close failed" in caplog.text


def test_preview_other_owner_stays_404(monkeypatch):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    stranger = f"health-{uuid.uuid4().hex}"
    content = _pdf_content()
    doc_id = _add_document(owner, content)
    _install_resolver(monkeypatch)
    monkeypatch.setattr("src.pdf_form_doc.parse_markdown_to_values", _forbidden_parse)
    monkeypatch.setattr("src.pdf_form_doc.load_field_sidecar", _forbidden_sidecar)
    try:
        resp = _post(stranger, doc_id)
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Document not found"
        assert _PARSE_SECRET not in resp.text
        assert _row(doc_id)["current_content"] == content
        assert _row(doc_id)["owner"] == owner
        assert _row(doc_id)["versions"] == []
    finally:
        _delete([doc_id])


def test_preview_anonymous_stays_403(monkeypatch):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    content = _pdf_content()
    doc_id = _add_document(owner, content)
    _install_resolver(monkeypatch)
    monkeypatch.setattr("src.pdf_form_doc.parse_markdown_to_values", _forbidden_parse)
    try:
        resp = _post(None, doc_id)
        assert resp.status_code == 403
        assert resp.json()["detail"] == "Authentication required"
        assert "error" not in resp.json()
        assert _row(doc_id)["current_content"] == content
    finally:
        _delete([doc_id])


def test_preview_without_pdf_marker_stays_400(monkeypatch):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    content = "plain notes without a marker\n"
    doc_id = _add_document(owner, content)
    monkeypatch.setattr("src.pdf_form_doc.parse_markdown_to_values", _forbidden_parse)
    monkeypatch.setattr("src.pdf_form_doc.load_field_sidecar", _forbidden_sidecar)
    try:
        resp = _post(owner, doc_id)
        assert resp.status_code == 400
        assert resp.json()["detail"] == "Document is not linked to a source PDF"
        assert "error" not in resp.json()
        assert _row(doc_id)["current_content"] == content
        assert _row(doc_id)["versions"] == []
    finally:
        _delete([doc_id])


def test_preview_missing_source_pdf_stays_404(monkeypatch):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    content = _pdf_content()
    doc_id = _add_document(owner, content)
    _install_resolver(monkeypatch, path=None)
    monkeypatch.setattr("src.pdf_form_doc.load_field_sidecar", _forbidden_sidecar)
    monkeypatch.setattr("src.pdf_form_doc.parse_markdown_to_values", _forbidden_parse)
    try:
        resp = _post(owner, doc_id)
        assert resp.status_code == 404
        assert resp.json()["detail"] == f"Source PDF {_UPLOAD_ID} not found in uploads"
        assert "error" not in resp.json()
        assert _row(doc_id)["current_content"] == content
        assert _row(doc_id)["versions"] == []
    finally:
        _delete([doc_id])


def test_preview_missing_sidecar_stays_404(monkeypatch, tmp_path):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    content = _pdf_content()
    doc_id = _add_document(owner, content)
    pdf_path = tmp_path / "owned.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%preview\n")
    _install_resolver(monkeypatch, path=str(pdf_path))
    monkeypatch.setattr("src.pdf_form_doc.parse_markdown_to_values", _forbidden_parse)
    try:
        resp = _post(owner, doc_id)
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Field schema sidecar missing for source PDF"
        assert "error" not in resp.json()
        assert _row(doc_id)["current_content"] == content
        assert _row(doc_id)["versions"] == []
    finally:
        _delete([doc_id])


def test_preview_success_returns_mapping_and_leaves_documents(monkeypatch, tmp_path):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    other = f"health-{uuid.uuid4().hex}"
    content = _pdf_content()
    other_content = _pdf_content("other body")
    doc_id = _add_document(owner, content)
    other_id = _add_document(other, other_content)
    _install_resolver(monkeypatch, path=_write_sidecar(tmp_path))
    try:
        resp = _post(owner, doc_id)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["doc_id"] == doc_id
        assert body["upload_id"] == _UPLOAD_ID
        assert body["total"] == 3
        assert body["filled"] == 1
        assert body["unknown_fields"] == ["ghost"]
        assert body["fields"] == [
            {
                "name": "city",
                "label": "City",
                "type": "text",
                "options": [],
                "page": 1,
                "value": "Springfield",
            },
            {
                "name": "notes",
                "label": "Notes",
                "type": "text",
                "options": [],
                "page": 2,
                "value": "",
            },
        ]
        assert _PARSE_SECRET not in resp.text
        assert _SIDECAR_SECRET not in resp.text
        row = _row(doc_id)
        assert row["current_content"] == content
        assert row["version_count"] == 1
        assert row["versions"] == []
        other_row = _row(other_id)
        assert other_row["current_content"] == other_content
        assert other_row["owner"] == other
        assert other_row["versions"] == []
    finally:
        _delete([doc_id, other_id])


def test_preview_close_failure_on_success_still_returns_mapping(monkeypatch, tmp_path, caplog):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    content = _pdf_content()
    doc_id = _add_document(owner, content)
    _install_resolver(monkeypatch, path=_write_sidecar(tmp_path))
    _install_failing_session(monkeypatch, rollback_exc=RuntimeError(_ROLLBACK_SECRET))
    caplog.set_level(logging.ERROR, logger="routes.document_routes")
    try:
        resp = _post(owner, doc_id)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["filled"] == 1
        assert body["fields"][0]["value"] == "Springfield"
        assert _ROLLBACK_SECRET not in resp.text
        assert "PDF preview session close failed" in caplog.text
        assert _row(doc_id)["current_content"] == content
        assert _row(doc_id)["versions"] == []
    finally:
        _delete([doc_id])


def test_preview_client_does_not_append_response_text():
    source = (_REPO / "static/js/document.js").read_text(encoding="utf-8")
    start = source.index("async function _openExportPdfModal(")
    end = source.index("  // Tracks which form-backed docs", start)
    block = source[start:end]
    fetch_at = block.index("export-pdf/preview")
    summary_at = block.index("pdf-export-summary", fetch_at)
    preview_load = block[fetch_at:summary_at]
    assert "res.text()" not in preview_load
    assert "e.message" not in preview_load
    assert "statusText" not in preview_load
    assert "Failed to load preview" in preview_load
    catch_at = block.rindex("pdf-export-body').innerHTML")
    catch_block = block[catch_at:]
    assert "Failed to load preview" in catch_block
    assert "e.message" not in catch_block
    assert "${" not in catch_block
    assert "res.text()" not in block
    # The overlay download still posts /export-pdf. That handler is not this bug.
    assert "r.text()" in block


def test_preview_route_does_not_interpolate_exception():
    source = (_REPO / "routes/document_routes.py").read_text(encoding="utf-8")
    start = source.index("async def export_pdf_preview(")
    end = source.index("async def render_pages(", start)
    body = source[start:end]
    assert "HTTPException(500" not in body
    assert "str(e)" not in body
    assert 'document_error("Failed to load preview")' in body
    assert "PDF preview rollback failed" in body
    assert "PDF preview session close failed" in body
    assert "Failed to load preview:" not in body
    assert "except HTTPException:" in body
