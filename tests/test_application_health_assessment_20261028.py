"""Filled-PDF download must not return exception text.

GET /api/document/{doc_id}/export-pdf used to raise HTTP 500 with
"PDF fill failed: {exception}" when fill_fields raised. A PDF library
error can include local paths and parser state in that message. An
unexpected failure now returns HTTP 502 with a fixed sentence. The
exception is logged and is not returned. A rollback failure, including
one raised again while closing the session, still returns that same 502.
An HTTPException raised inside the handler is not rewritten: a missing
document and another user's document stay HTTP 404, a document with no
PDF marker stays HTTP 400, a missing source file stays HTTP 404, and an
anonymous caller stays HTTP 403.

A stamp failure is still logged and the filled PDF is still returned.
The download button and the export overlay show "Failed to export PDF"
and do not read the response body.
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

_FILL_SECRET = "FILL_SECRET acroform widget path=/tmp/secret-form.pdf token=do-not-return"
_ROLLBACK_SECRET = "ROLLBACK_SECRET connection reset while rolling back pdf export"
_STAMP_SECRET = "STAMP_SECRET annotation png bytes=do-not-return"
_PARSE_SECRET = "PARSE_SECRET markdown field decoder token=do-not-return"
_SIDECAR_SECRET = "SIDECAR_SECRET field schema path=/tmp/secret-sidecar.json token=do-not-return"
_UPLOAD_ID = "abcdef0123456789abcdef0123456789.pdf"
_ORIGINAL = "old body that must stay while the pdf is exported"
_PDF_BYTES = b"%PDF-1.4\n%exported-form\n"
# The route looks up SessionLocal on this module at call time. Another test
# replaces that name with a temp engine; reads and cleanup use the app engine.
_REAL_SESSION = SessionLocal
_REPO = Path(__file__).resolve().parent.parent


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


def _pdf_content(body=_ORIGINAL, annotation=False):
    lines = [
        f'<!-- pdf_source upload_id="{_UPLOAD_ID}" -->',
        "",
        "# Field notes",
        "",
        f"{body}",
        "- **City:** Springfield <!-- field=city type=text -->",
    ]
    if annotation:
        lines.append(
            "- note <!-- annotation id=ann1 page=1 x=10 y=10 w=20 h=5 kind=text -->"
        )
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


def _install_fill(monkeypatch, fn):
    monkeypatch.setattr("src.pdf_forms.fill_fields", fn)


def _install_stamp(monkeypatch, fn):
    monkeypatch.setattr("src.pdf_forms.stamp_annotations", fn)


def _ok_fill(source, output, values):
    with open(output, "wb") as handle:
        handle.write(_PDF_BYTES)
    _ok_fill.seen = {"source": source, "output": output, "values": dict(values)}
    return 1


def _raising_fill(source, output, values):
    _raising_fill.seen = output
    raise RuntimeError(_FILL_SECRET)


def _forbidden_fill(source, output, values):
    raise AssertionError("fill_fields should not run")


def _raising_stamp(source, output, annotations, pngs):
    raise RuntimeError(_STAMP_SECRET)


def _raising_parse(content):
    raise RuntimeError(_PARSE_SECRET)


def _raising_sidecar(path):
    raise RuntimeError(_SIDECAR_SECRET)


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


def _get(username, doc_id):
    return _client(username).get(f"/api/document/{doc_id}/export-pdf")


def _assert_fixed_502(resp):
    assert resp.status_code == 502
    assert resp.json() == {"error": "Failed to export PDF"}
    assert "detail" not in resp.json()
    assert _FILL_SECRET not in resp.text
    assert _ROLLBACK_SECRET not in resp.text
    assert _STAMP_SECRET not in resp.text
    assert _PARSE_SECRET not in resp.text
    assert _SIDECAR_SECRET not in resp.text
    assert "PDF fill failed:" not in resp.text


def test_export_fill_failure_hides_exception(monkeypatch, caplog):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    content = _pdf_content()
    doc_id = _add_document(owner, content)
    _install_resolver(monkeypatch)
    _install_fill(monkeypatch, _raising_fill)
    caplog.set_level(logging.ERROR, logger="routes.document_routes")
    try:
        resp = _get(owner, doc_id)
        _assert_fixed_502(resp)
        assert _FILL_SECRET in caplog.text
        assert "Failed to export PDF" in caplog.text
        assert not os.path.exists(_raising_fill.seen)
        row = _row(doc_id)
        assert row["current_content"] == content
        assert row["version_count"] == 1
        assert row["versions"] == []
    finally:
        _delete([doc_id])


def test_export_rollback_and_close_failure_stays_fixed_502(monkeypatch, caplog):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    content = _pdf_content()
    doc_id = _add_document(owner, content)
    _install_resolver(monkeypatch)
    _install_fill(monkeypatch, _raising_fill)
    _install_failing_session(monkeypatch, rollback_exc=RuntimeError(_ROLLBACK_SECRET))
    caplog.set_level(logging.ERROR, logger="routes.document_routes")
    try:
        resp = _get(owner, doc_id)
        _assert_fixed_502(resp)
        assert _FILL_SECRET in caplog.text
        assert _ROLLBACK_SECRET in caplog.text
        assert "PDF export rollback failed" in caplog.text
        assert "PDF export session close failed" in caplog.text
        assert not os.path.exists(_raising_fill.seen)
        assert _row(doc_id)["current_content"] == content
        assert _row(doc_id)["versions"] == []
    finally:
        _delete([doc_id])


def test_export_parse_failure_hides_exception(monkeypatch, caplog):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    content = _pdf_content()
    doc_id = _add_document(owner, content)
    _install_resolver(monkeypatch)
    _install_fill(monkeypatch, _forbidden_fill)
    monkeypatch.setattr("src.pdf_form_doc.parse_markdown_to_values", _raising_parse)
    caplog.set_level(logging.ERROR, logger="routes.document_routes")
    try:
        resp = _get(owner, doc_id)
        _assert_fixed_502(resp)
        assert _PARSE_SECRET in caplog.text
        assert _row(doc_id)["current_content"] == content
        assert _row(doc_id)["versions"] == []
    finally:
        _delete([doc_id])


def test_export_sidecar_failure_hides_exception(monkeypatch, caplog):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    content = _pdf_content()
    doc_id = _add_document(owner, content)
    _install_resolver(monkeypatch)
    _install_fill(monkeypatch, _forbidden_fill)
    monkeypatch.setattr("src.pdf_form_doc.load_field_sidecar", _raising_sidecar)
    caplog.set_level(logging.ERROR, logger="routes.document_routes")
    try:
        resp = _get(owner, doc_id)
        _assert_fixed_502(resp)
        assert _SIDECAR_SECRET in caplog.text
        assert _row(doc_id)["current_content"] == content
        assert _row(doc_id)["versions"] == []
    finally:
        _delete([doc_id])


def test_export_missing_document_stays_404(monkeypatch):
    _pin_session(monkeypatch)
    _install_fill(monkeypatch, _forbidden_fill)
    resp = _get(f"health-{uuid.uuid4().hex}", str(uuid.uuid4()))
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Document not found"
    assert "error" not in resp.json()


def test_export_other_owner_stays_404(monkeypatch):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    stranger = f"health-{uuid.uuid4().hex}"
    content = _pdf_content()
    doc_id = _add_document(owner, content)
    _install_resolver(monkeypatch)
    _install_fill(monkeypatch, _forbidden_fill)
    try:
        resp = _get(stranger, doc_id)
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Document not found"
        assert _FILL_SECRET not in resp.text
        assert _row(doc_id)["current_content"] == content
        assert _row(doc_id)["owner"] == owner
        assert _row(doc_id)["versions"] == []
    finally:
        _delete([doc_id])


def test_export_anonymous_stays_403(monkeypatch):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    content = _pdf_content()
    doc_id = _add_document(owner, content)
    _install_resolver(monkeypatch)
    _install_fill(monkeypatch, _forbidden_fill)
    try:
        resp = _get(None, doc_id)
        assert resp.status_code == 403
        assert resp.json()["detail"] == "Authentication required"
        assert "error" not in resp.json()
        assert _row(doc_id)["current_content"] == content
    finally:
        _delete([doc_id])


def test_export_without_pdf_marker_stays_400(monkeypatch):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    content = "plain notes without a marker\n"
    doc_id = _add_document(owner, content)
    _install_fill(monkeypatch, _forbidden_fill)
    try:
        resp = _get(owner, doc_id)
        assert resp.status_code == 400
        assert resp.json()["detail"] == "Document is not linked to a source PDF"
        assert "error" not in resp.json()
        assert _row(doc_id)["current_content"] == content
        assert _row(doc_id)["versions"] == []
    finally:
        _delete([doc_id])


def test_export_missing_source_pdf_stays_404(monkeypatch):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    content = _pdf_content()
    doc_id = _add_document(owner, content)
    _install_resolver(monkeypatch, path=None)
    _install_fill(monkeypatch, _forbidden_fill)
    try:
        resp = _get(owner, doc_id)
        assert resp.status_code == 404
        assert resp.json()["detail"] == f"Source PDF {_UPLOAD_ID} not found in uploads"
        assert "error" not in resp.json()
        assert _row(doc_id)["current_content"] == content
        assert _row(doc_id)["versions"] == []
    finally:
        _delete([doc_id])


def test_export_success_returns_pdf_and_leaves_documents(monkeypatch):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    other = f"health-{uuid.uuid4().hex}"
    content = _pdf_content()
    other_content = _pdf_content("other body")
    doc_id = _add_document(owner, content)
    other_id = _add_document(other, other_content)
    _install_resolver(monkeypatch)
    _install_fill(monkeypatch, _ok_fill)
    try:
        resp = _get(owner, doc_id)
        assert resp.status_code == 200
        assert resp.content == _PDF_BYTES
        assert resp.headers["content-type"].startswith("application/pdf")
        disposition = resp.headers.get("content-disposition", "")
        assert "attachment" in disposition
        assert "Field_notes_annotated.pdf" in disposition
        assert _FILL_SECRET not in resp.text
        assert "error" not in resp.headers.get("content-type", "")
        assert _ok_fill.seen["source"] == "/tmp/owned.pdf"
        assert _ok_fill.seen["values"] == {"city": "Springfield"}
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


def test_export_stamp_failure_still_returns_filled_pdf(monkeypatch, caplog):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    content = _pdf_content(annotation=True)
    doc_id = _add_document(owner, content)
    _install_resolver(monkeypatch)
    _install_fill(monkeypatch, _ok_fill)
    _install_stamp(monkeypatch, _raising_stamp)
    caplog.set_level(logging.ERROR, logger="routes.document_routes")
    try:
        resp = _get(owner, doc_id)
        assert resp.status_code == 200
        assert resp.content == _PDF_BYTES
        assert _STAMP_SECRET not in resp.text
        assert "PDF fill failed:" not in resp.text
        assert _STAMP_SECRET in caplog.text
        assert "stamp_annotations failed for doc" in caplog.text
        assert _row(doc_id)["current_content"] == content
        assert _row(doc_id)["versions"] == []
    finally:
        _delete([doc_id])


def test_export_close_failure_on_success_still_returns_pdf(monkeypatch, caplog):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    content = _pdf_content()
    doc_id = _add_document(owner, content)
    _install_resolver(monkeypatch)
    _install_fill(monkeypatch, _ok_fill)
    _install_failing_session(monkeypatch, rollback_exc=RuntimeError(_ROLLBACK_SECRET))
    caplog.set_level(logging.ERROR, logger="routes.document_routes")
    try:
        resp = _get(owner, doc_id)
        assert resp.status_code == 200
        assert resp.content == _PDF_BYTES
        assert _ROLLBACK_SECRET not in resp.text
        assert "PDF export session close failed" in caplog.text
        assert _row(doc_id)["current_content"] == content
    finally:
        _delete([doc_id])


def test_export_client_does_not_read_error_body():
    source = (_REPO / "static/js/document.js").read_text(encoding="utf-8")
    start = source.index("async function _downloadFilledPdf()")
    end = source.index("async function _saveActiveDocBeforeExport()", start)
    block = source[start:end]
    assert "/export-pdf" in block
    assert "r.text()" not in block
    assert "e.message" not in block
    assert "detail" not in block
    assert "Export failed:" not in block
    assert "showError('Failed to export PDF')" in block
    assert "alert('Failed to export PDF')" in block

    overlay_start = source.index("const downloadBtn = overlay.querySelector('#pdf-export-download');")
    overlay_end = source.index("overlay.querySelector('#pdf-export-body').innerHTML", overlay_start)
    overlay = source[overlay_start:overlay_end]
    assert "/export-pdf" in overlay
    assert "method: 'POST'" in overlay
    assert "r.text()" not in overlay
    assert "e.message" not in overlay
    assert "detail" not in overlay
    assert "Error: " not in overlay
    assert "Failed to export PDF" in overlay


def test_export_route_does_not_interpolate_fill_exception():
    source = (_REPO / "routes/document_routes.py").read_text(encoding="utf-8")
    start = source.index("async def export_pdf(")
    end = source.index("async def prepare_signed_reply", start)
    body = source[start:end]
    assert "PDF fill failed:" not in body
    assert "HTTPException(500" not in body
    assert 'document_error("Failed to export PDF")' in body
    assert "PDF export rollback failed" in body
    assert "PDF export session close failed" in body
    assert "stamp_signatures failed for doc" in body
    assert "stamp_annotations failed for doc" in body
