"""PDF page rendering must not leak exception text.

GET /api/document/{doc_id}/render-pages had no handler around PyMuPDF,
field loading, or markdown parsing, and closed the database session without
a guard. GET /api/document/{doc_id}/page/{page_no}.png closed that session
without a guard, then opened PyMuPDF outside the try. A sidecar error, a
markdown parse error, a render error, or a session error escaped as an
unhandled failure. A close failure could also replace a successful page
list, a PNG, or an HTTPException.

An unexpected failure now returns HTTP 502 with a fixed sentence. The
exception is logged and is not returned. A rollback failure, including one
raised again while closing the session, still returns that same 502. Closing
the PDF document is guarded the same way. An HTTPException raised inside
either handler is still passed through: a missing document and another
user's document stay HTTP 404, a document with no PDF marker stays HTTP 400,
a missing source file stays HTTP 404, an anonymous caller stays HTTP 403, a
page outside the document stays HTTP 404, and a missing PyMuPDF runtime stays
HTTP 503 with the existing fixed setup sentence.

The PDF pane shows that fixed sentence and does not append the response
body or the client exception text. The page image is still assigned as
img.src and still has no error text of its own.
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
from src.pdf_runtime import PDF_VIEWER_PYMUPDF_MISSING

_PARSE_SECRET = "PARSE_SECRET markdown field decoder token=do-not-return"
_SIDECAR_SECRET = "SIDECAR_SECRET field schema path=/tmp/secret-sidecar.json token=do-not-return"
_OPEN_SECRET = "OPEN_SECRET pymupdf path=/tmp/secret-render.pdf token=do-not-return"
_RENDER_SECRET = "RENDER_SECRET pixmap buffer token=do-not-return"
_ROLLBACK_SECRET = "ROLLBACK_SECRET connection reset while rolling back pdf view"
_CLOSE_SECRET = "CLOSE_SECRET pymupdf document handle token=do-not-return"
_LOADER_SECRET = "LOADER_SECRET unexpected pdf runtime token=do-not-return"
_QUERY_SECRET = "QUERY_SECRET database lookup token=do-not-return"
_UPLOAD_ID = "abcdef0123456789abcdef0123456789.pdf"
_ORIGINAL = "old body that must stay while the pdf view is loaded"
_PNG = b"\x89PNG\r\n\x1a\nFAKEPAGE"
# The route looks up SessionLocal on this module at call time. Another test
# replaces that name with a temp engine; reads and cleanup use the app engine.
_REAL_SESSION = SessionLocal
_REPO = Path(__file__).resolve().parent.parent
_FIXED_ERROR = "Failed to load PDF view"
_SECRETS = (
    _PARSE_SECRET,
    _SIDECAR_SECRET,
    _OPEN_SECRET,
    _RENDER_SECRET,
    _ROLLBACK_SECRET,
    _CLOSE_SECRET,
    _LOADER_SECRET,
    _QUERY_SECRET,
)


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


class _QueryFailSession(_FailingSession):
    def query(self, *args, **kwargs):
        raise RuntimeError(_QUERY_SECRET)


class _Rect:
    def __init__(self, width, height):
        self.width = width
        self.height = height


class _FakePage:
    def __init__(self, width, height, pixmap_exc=None):
        self.rect = _Rect(width, height)
        self._pixmap_exc = pixmap_exc

    def get_pixmap(self, matrix, alpha=False):
        if self._pixmap_exc is not None:
            raise self._pixmap_exc
        return _FakePix()


class _FakePix:
    def tobytes(self, fmt):
        assert fmt == "png"
        return _PNG


class _FakeDoc:
    def __init__(self, pages, close_exc=None):
        self.page_count = len(pages)
        self._pages = pages
        self._close_exc = close_exc
        self.closed = False

    def __getitem__(self, index):
        return self._pages[index]

    def close(self):
        self.closed = True
        if self._close_exc is not None:
            raise self._close_exc


class _FakeFitz:
    def __init__(self, doc=None, open_exc=None):
        self._doc = doc
        self._open_exc = open_exc
        self.opened = False

    class Matrix:
        def __init__(self, a, b):
            self.a = a
            self.b = b

    def open(self, path):
        self.opened = True
        if self._open_exc is not None:
            raise self._open_exc
        return self._doc


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


def _schema():
    return [
        {
            "name": "city",
            "label": "City",
            "type": "text",
            "options": [],
            "page": 1,
            "rect": [10, 20, 110, 40],
            "value": "fallback",
        },
        {
            "name": "notes",
            "label": "Notes",
            "type": "text",
            "options": [],
            "page": 1,
            "rect": [12, 48, 90, 60],
        },
        {
            "name": "unused",
            "label": "Unused",
            "type": "checkbox",
            "options": [],
            "page": 2,
            "rect": [1, 2, 8, 9],
            "value": False,
        },
    ]


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


def _install_failing_session(monkeypatch, rollback_exc=None, query_fails=False):
    def _factory():
        inner = _REAL_SESSION()
        exc = rollback_exc
        if query_fails:
            return _QueryFailSession(inner, rollback_exc=exc)
        return _FailingSession(inner, rollback_exc=exc)

    monkeypatch.setattr(document_routes, "SessionLocal", _factory)


def _install_resolver(monkeypatch, path="/tmp/owned.pdf"):
    monkeypatch.setattr(
        document_routes,
        "_resolve_user_upload_path",
        lambda *args, **kwargs: path,
    )


def _install_fitz(monkeypatch, fitz):
    monkeypatch.setattr(
        "src.pdf_runtime.load_pymupdf_for_pdf_viewer",
        lambda: fitz,
    )


def _forbid_loader():
    raise AssertionError("pdf runtime should not load")


def _raising_parse(content):
    raise RuntimeError(_PARSE_SECRET)


def _raising_sidecar(path):
    raise RuntimeError(_SIDECAR_SECRET)


def _forbidden_parse(content):
    raise AssertionError("parse_markdown_to_values should not run")


def _forbidden_sidecar(path):
    raise AssertionError("load_field_sidecar should not run")


def _two_pages(pixmap_exc=None, close_exc=None, item_exc=None):
    pages = [
        _FakePage(100, 50, pixmap_exc=pixmap_exc),
        _FakePage(100, 50, pixmap_exc=pixmap_exc),
    ]
    if item_exc is not None:
        class _BrokenDoc(_FakeDoc):
            def __getitem__(self, index):
                raise item_exc
        return _BrokenDoc(pages, close_exc=close_exc)
    return _FakeDoc(pages, close_exc=close_exc)


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


def _get_pages(username, doc_id):
    return _client(username).get(f"/api/document/{doc_id}/render-pages")


def _get_png(username, doc_id, page_no=1):
    return _client(username).get(f"/api/document/{doc_id}/page/{page_no}.png")


def _assert_no_secrets(resp):
    for secret in _SECRETS:
        assert secret not in resp.text


def _assert_fixed_502(resp):
    assert resp.status_code == 502
    assert resp.json() == {"error": _FIXED_ERROR}
    assert "detail" not in resp.json()
    _assert_no_secrets(resp)


def _assert_unchanged(doc_id, content, owner=None):
    row = _row(doc_id)
    assert row["current_content"] == content
    assert row["version_count"] == 1
    assert row["versions"] == []
    if owner is not None:
        assert row["owner"] == owner


def test_render_pages_parse_failure_hides_exception(monkeypatch, caplog):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    content = _pdf_content()
    doc_id = _add_document(owner, content)
    _install_resolver(monkeypatch)
    fitz = _FakeFitz(open_exc=AssertionError("fitz.open should not run"))
    _install_fitz(monkeypatch, fitz)
    monkeypatch.setattr("src.pdf_form_doc.parse_markdown_to_values", _raising_parse)
    monkeypatch.setattr(
        "src.pdf_form_doc.load_field_sidecar",
        lambda path: [{"name": "city", "label": "City", "type": "text", "page": 1, "rect": [0, 0, 1, 1]}],
    )
    caplog.set_level(logging.ERROR, logger="routes.document_routes")
    try:
        resp = _get_pages(owner, doc_id)
        _assert_fixed_502(resp)
        assert _PARSE_SECRET in caplog.text
        assert _FIXED_ERROR in caplog.text
        assert fitz.opened is False
        _assert_unchanged(doc_id, content, owner)
    finally:
        _delete([doc_id])


def test_render_pages_sidecar_failure_hides_exception(monkeypatch, caplog):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    content = _pdf_content()
    doc_id = _add_document(owner, content)
    _install_resolver(monkeypatch)
    fitz = _FakeFitz(open_exc=AssertionError("fitz.open should not run"))
    _install_fitz(monkeypatch, fitz)
    monkeypatch.setattr("src.pdf_form_doc.load_field_sidecar", _raising_sidecar)
    monkeypatch.setattr("src.pdf_form_doc.parse_markdown_to_values", _forbidden_parse)
    caplog.set_level(logging.ERROR, logger="routes.document_routes")
    try:
        resp = _get_pages(owner, doc_id)
        _assert_fixed_502(resp)
        assert _SIDECAR_SECRET in caplog.text
        assert fitz.opened is False
        _assert_unchanged(doc_id, content)
    finally:
        _delete([doc_id])


def test_render_pages_open_failure_hides_exception(monkeypatch, caplog):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    content = _pdf_content()
    doc_id = _add_document(owner, content)
    _install_resolver(monkeypatch)
    fitz = _FakeFitz(open_exc=RuntimeError(_OPEN_SECRET))
    _install_fitz(monkeypatch, fitz)
    monkeypatch.setattr("src.pdf_form_doc.load_field_sidecar", lambda path: [])
    caplog.set_level(logging.ERROR, logger="routes.document_routes")
    try:
        resp = _get_pages(owner, doc_id)
        _assert_fixed_502(resp)
        assert _OPEN_SECRET in caplog.text
        assert fitz.opened is True
        _assert_unchanged(doc_id, content)
    finally:
        _delete([doc_id])


def test_render_pages_render_and_document_close_failure_stays_fixed_502(monkeypatch, caplog):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    content = _pdf_content()
    doc_id = _add_document(owner, content)
    _install_resolver(monkeypatch)
    doc = _two_pages(item_exc=RuntimeError(_RENDER_SECRET), close_exc=RuntimeError(_CLOSE_SECRET))
    fitz = _FakeFitz(doc=doc)
    _install_fitz(monkeypatch, fitz)
    monkeypatch.setattr("src.pdf_form_doc.load_field_sidecar", lambda path: [])
    caplog.set_level(logging.ERROR, logger="routes.document_routes")
    try:
        resp = _get_pages(owner, doc_id)
        _assert_fixed_502(resp)
        assert _RENDER_SECRET in caplog.text
        assert _CLOSE_SECRET in caplog.text
        assert "PDF view document close failed" in caplog.text
        assert doc.closed is True
        _assert_unchanged(doc_id, content)
    finally:
        _delete([doc_id])


def test_render_pages_rollback_and_close_failure_stays_fixed_502(monkeypatch, caplog):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    content = _pdf_content()
    doc_id = _add_document(owner, content)
    _install_resolver(monkeypatch)
    _install_fitz(monkeypatch, _FakeFitz(open_exc=AssertionError("fitz.open should not run")))
    monkeypatch.setattr("src.pdf_form_doc.parse_markdown_to_values", _raising_parse)
    monkeypatch.setattr("src.pdf_form_doc.load_field_sidecar", lambda path: [])
    _install_failing_session(monkeypatch, rollback_exc=RuntimeError(_ROLLBACK_SECRET))
    caplog.set_level(logging.ERROR, logger="routes.document_routes")
    try:
        resp = _get_pages(owner, doc_id)
        _assert_fixed_502(resp)
        assert _PARSE_SECRET in caplog.text
        assert _ROLLBACK_SECRET in caplog.text
        assert "PDF view rollback failed" in caplog.text
        assert "PDF view session close failed" in caplog.text
        _assert_unchanged(doc_id, content)
    finally:
        _delete([doc_id])


def test_render_pages_missing_document_stays_404(monkeypatch):
    _pin_session(monkeypatch)
    monkeypatch.setattr("src.pdf_runtime.load_pymupdf_for_pdf_viewer", _forbid_loader)
    monkeypatch.setattr("src.pdf_form_doc.parse_markdown_to_values", _forbidden_parse)
    resp = _get_pages(f"health-{uuid.uuid4().hex}", str(uuid.uuid4()))
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Document not found"
    assert "error" not in resp.json()


def test_render_pages_missing_document_close_failure_stays_404(monkeypatch, caplog):
    _pin_session(monkeypatch)
    _install_failing_session(monkeypatch, rollback_exc=RuntimeError(_ROLLBACK_SECRET))
    monkeypatch.setattr("src.pdf_runtime.load_pymupdf_for_pdf_viewer", _forbid_loader)
    caplog.set_level(logging.ERROR, logger="routes.document_routes")
    resp = _get_pages(f"health-{uuid.uuid4().hex}", str(uuid.uuid4()))
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Document not found"
    assert "error" not in resp.json()
    _assert_no_secrets(resp)
    assert "PDF view session close failed" in caplog.text


def test_render_pages_other_owner_stays_404(monkeypatch):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    stranger = f"health-{uuid.uuid4().hex}"
    content = _pdf_content()
    doc_id = _add_document(owner, content)
    _install_resolver(monkeypatch)
    monkeypatch.setattr("src.pdf_runtime.load_pymupdf_for_pdf_viewer", _forbid_loader)
    monkeypatch.setattr("src.pdf_form_doc.parse_markdown_to_values", _forbidden_parse)
    try:
        resp = _get_pages(stranger, doc_id)
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Document not found"
        _assert_no_secrets(resp)
        _assert_unchanged(doc_id, content, owner)
    finally:
        _delete([doc_id])


def test_render_pages_anonymous_stays_403(monkeypatch):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    content = _pdf_content()
    doc_id = _add_document(owner, content)
    _install_resolver(monkeypatch)
    monkeypatch.setattr("src.pdf_runtime.load_pymupdf_for_pdf_viewer", _forbid_loader)
    try:
        resp = _get_pages(None, doc_id)
        assert resp.status_code == 403
        assert resp.json()["detail"] == "Authentication required"
        assert "error" not in resp.json()
        _assert_unchanged(doc_id, content)
    finally:
        _delete([doc_id])


def test_render_pages_without_pdf_marker_stays_400(monkeypatch):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    content = "plain notes without a marker\n"
    doc_id = _add_document(owner, content)
    monkeypatch.setattr("src.pdf_runtime.load_pymupdf_for_pdf_viewer", _forbid_loader)
    monkeypatch.setattr("src.pdf_form_doc.parse_markdown_to_values", _forbidden_parse)
    monkeypatch.setattr("src.pdf_form_doc.load_field_sidecar", _forbidden_sidecar)
    try:
        resp = _get_pages(owner, doc_id)
        assert resp.status_code == 400
        assert resp.json()["detail"] == "Document is not linked to a source PDF"
        assert "error" not in resp.json()
        _assert_unchanged(doc_id, content)
    finally:
        _delete([doc_id])


def test_render_pages_missing_source_pdf_stays_404(monkeypatch):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    content = _pdf_content()
    doc_id = _add_document(owner, content)
    _install_resolver(monkeypatch, path=None)
    monkeypatch.setattr("src.pdf_runtime.load_pymupdf_for_pdf_viewer", _forbid_loader)
    monkeypatch.setattr("src.pdf_form_doc.load_field_sidecar", _forbidden_sidecar)
    monkeypatch.setattr("src.pdf_form_doc.parse_markdown_to_values", _forbidden_parse)
    try:
        resp = _get_pages(owner, doc_id)
        assert resp.status_code == 404
        assert resp.json()["detail"] == f"Source PDF {_UPLOAD_ID} not found"
        assert "in uploads" not in resp.json()["detail"]
        assert "error" not in resp.json()
        _assert_unchanged(doc_id, content)
    finally:
        _delete([doc_id])


def test_render_pages_missing_pymupdf_stays_503(monkeypatch):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    content = _pdf_content()
    doc_id = _add_document(owner, content)
    _install_resolver(monkeypatch)

    def _missing():
        raise RuntimeError(PDF_VIEWER_PYMUPDF_MISSING)

    monkeypatch.setattr("src.pdf_runtime.load_pymupdf_for_pdf_viewer", _missing)
    monkeypatch.setattr("src.pdf_form_doc.load_field_sidecar", _forbidden_sidecar)
    monkeypatch.setattr("src.pdf_form_doc.parse_markdown_to_values", _forbidden_parse)
    try:
        resp = _get_pages(owner, doc_id)
        assert resp.status_code == 503
        assert resp.json()["detail"] == PDF_VIEWER_PYMUPDF_MISSING
        assert "error" not in resp.json()
        _assert_no_secrets(resp)
        _assert_unchanged(doc_id, content)
    finally:
        _delete([doc_id])


def test_render_pages_unexpected_loader_failure_hides_exception(monkeypatch, caplog):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    content = _pdf_content()
    doc_id = _add_document(owner, content)
    _install_resolver(monkeypatch)

    def _boom():
        raise ValueError(_LOADER_SECRET)

    monkeypatch.setattr("src.pdf_runtime.load_pymupdf_for_pdf_viewer", _boom)
    monkeypatch.setattr("src.pdf_form_doc.load_field_sidecar", _forbidden_sidecar)
    caplog.set_level(logging.ERROR, logger="routes.document_routes")
    try:
        resp = _get_pages(owner, doc_id)
        _assert_fixed_502(resp)
        assert _LOADER_SECRET in caplog.text
        _assert_unchanged(doc_id, content)
    finally:
        _delete([doc_id])


def test_render_pages_success_returns_geometry_and_leaves_documents(monkeypatch, tmp_path):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    other = f"health-{uuid.uuid4().hex}"
    content = _pdf_content()
    other_content = _pdf_content("other body")
    doc_id = _add_document(owner, content)
    other_id = _add_document(other, other_content)
    pdf_path = tmp_path / "owned.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%view\n")
    (tmp_path / "owned.pdf.fields.json").write_text(
        json.dumps(_schema()),
        encoding="utf-8",
    )
    doc = _two_pages()
    fitz = _FakeFitz(doc=doc)
    _install_resolver(monkeypatch, path=str(pdf_path))
    _install_fitz(monkeypatch, fitz)
    try:
        resp = _get_pages(owner, doc_id)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["doc_id"] == doc_id
        assert body["scale"] == 2.0
        assert body["pages"] == [
            {
                "page": 1,
                "width": 200,
                "height": 100,
                "fields": [
                    {
                        "name": "city",
                        "type": "text",
                        "label": "City",
                        "options": [],
                        "value": "Springfield",
                        "rect_px": [20, 40, 220, 80],
                    },
                    {
                        "name": "notes",
                        "type": "text",
                        "label": "Notes",
                        "options": [],
                        "value": "",
                        "rect_px": [24, 96, 180, 120],
                    },
                ],
            },
            {
                "page": 2,
                "width": 200,
                "height": 100,
                "fields": [
                    {
                        "name": "unused",
                        "type": "checkbox",
                        "label": "Unused",
                        "options": [],
                        "value": False,
                        "rect_px": [2, 4, 16, 18],
                    },
                ],
            },
        ]
        assert "ghost" not in resp.text
        _assert_no_secrets(resp)
        assert doc.closed is True
        _assert_unchanged(doc_id, content, owner)
        other_row = _row(other_id)
        assert other_row["current_content"] == other_content
        assert other_row["owner"] == other
        assert other_row["versions"] == []
    finally:
        _delete([doc_id, other_id])


def test_render_pages_close_failure_on_success_still_returns_geometry(monkeypatch, caplog):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    content = _pdf_content()
    doc_id = _add_document(owner, content)
    _install_resolver(monkeypatch)
    doc = _two_pages(close_exc=RuntimeError(_CLOSE_SECRET))
    _install_fitz(monkeypatch, _FakeFitz(doc=doc))
    monkeypatch.setattr("src.pdf_form_doc.load_field_sidecar", lambda path: [])
    _install_failing_session(monkeypatch, rollback_exc=RuntimeError(_ROLLBACK_SECRET))
    caplog.set_level(logging.ERROR, logger="routes.document_routes")
    try:
        resp = _get_pages(owner, doc_id)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["doc_id"] == doc_id
        assert body["scale"] == 2.0
        assert [page["page"] for page in body["pages"]] == [1, 2]
        assert body["pages"][0]["width"] == 200
        assert body["pages"][0]["fields"] == []
        _assert_no_secrets(resp)
        assert "PDF view document close failed" in caplog.text
        assert "PDF view session close failed" in caplog.text
        assert "PDF view rollback failed" not in caplog.text
        assert doc.closed is True
        _assert_unchanged(doc_id, content)
    finally:
        _delete([doc_id])


def test_render_page_query_failure_hides_exception(monkeypatch, caplog):
    _pin_session(monkeypatch)
    _install_failing_session(
        monkeypatch,
        rollback_exc=RuntimeError(_ROLLBACK_SECRET),
        query_fails=True,
    )
    monkeypatch.setattr("src.pdf_runtime.load_pymupdf_for_pdf_viewer", _forbid_loader)
    caplog.set_level(logging.ERROR, logger="routes.document_routes")
    resp = _get_png(f"health-{uuid.uuid4().hex}", str(uuid.uuid4()))
    _assert_fixed_502(resp)
    assert _QUERY_SECRET in caplog.text
    assert _ROLLBACK_SECRET in caplog.text
    assert "PDF page rollback failed" in caplog.text
    assert "PDF page session close failed" in caplog.text


def test_render_page_pixmap_failure_hides_exception(monkeypatch, caplog):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    content = _pdf_content()
    doc_id = _add_document(owner, content)
    _install_resolver(monkeypatch)
    doc = _two_pages(pixmap_exc=RuntimeError(_RENDER_SECRET))
    _install_fitz(monkeypatch, _FakeFitz(doc=doc))
    caplog.set_level(logging.ERROR, logger="routes.document_routes")
    try:
        resp = _get_png(owner, doc_id, page_no=1)
        _assert_fixed_502(resp)
        assert _RENDER_SECRET in caplog.text
        assert doc.closed is True
        _assert_unchanged(doc_id, content)
    finally:
        _delete([doc_id])


def test_render_page_out_of_range_stays_404(monkeypatch):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    content = _pdf_content()
    doc_id = _add_document(owner, content)
    _install_resolver(monkeypatch)
    doc = _two_pages()
    _install_fitz(monkeypatch, _FakeFitz(doc=doc))
    try:
        resp = _get_png(owner, doc_id, page_no=9)
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Page out of range"
        assert "error" not in resp.json()
        assert doc.closed is True
        _assert_unchanged(doc_id, content)
    finally:
        _delete([doc_id])


def test_render_page_out_of_range_close_failure_stays_404(monkeypatch, caplog):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    content = _pdf_content()
    doc_id = _add_document(owner, content)
    _install_resolver(monkeypatch)
    doc = _two_pages(close_exc=RuntimeError(_CLOSE_SECRET))
    _install_fitz(monkeypatch, _FakeFitz(doc=doc))
    caplog.set_level(logging.ERROR, logger="routes.document_routes")
    try:
        resp = _get_png(owner, doc_id, page_no=0)
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Page out of range"
        assert "error" not in resp.json()
        _assert_no_secrets(resp)
        assert "PDF page document close failed" in caplog.text
        assert doc.closed is True
    finally:
        _delete([doc_id])


def test_render_page_missing_source_stays_404(monkeypatch):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    content = _pdf_content()
    doc_id = _add_document(owner, content)
    _install_resolver(monkeypatch, path=None)
    monkeypatch.setattr("src.pdf_runtime.load_pymupdf_for_pdf_viewer", _forbid_loader)
    try:
        resp = _get_png(owner, doc_id)
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Source PDF not found"
        assert "error" not in resp.json()
        _assert_unchanged(doc_id, content)
    finally:
        _delete([doc_id])


def test_render_page_missing_document_stays_404(monkeypatch):
    _pin_session(monkeypatch)
    monkeypatch.setattr("src.pdf_runtime.load_pymupdf_for_pdf_viewer", _forbid_loader)
    resp = _get_png(f"health-{uuid.uuid4().hex}", str(uuid.uuid4()))
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Document not found"
    assert "error" not in resp.json()


def test_render_page_other_owner_stays_404(monkeypatch):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    stranger = f"health-{uuid.uuid4().hex}"
    content = _pdf_content()
    doc_id = _add_document(owner, content)
    monkeypatch.setattr("src.pdf_runtime.load_pymupdf_for_pdf_viewer", _forbid_loader)
    try:
        resp = _get_png(stranger, doc_id)
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Document not found"
        _assert_unchanged(doc_id, content, owner)
    finally:
        _delete([doc_id])


def test_render_page_anonymous_stays_403(monkeypatch):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    content = _pdf_content()
    doc_id = _add_document(owner, content)
    monkeypatch.setattr("src.pdf_runtime.load_pymupdf_for_pdf_viewer", _forbid_loader)
    try:
        resp = _get_png(None, doc_id)
        assert resp.status_code == 403
        assert resp.json()["detail"] == "Authentication required"
        assert "error" not in resp.json()
        _assert_unchanged(doc_id, content)
    finally:
        _delete([doc_id])


def test_render_page_without_pdf_marker_stays_400(monkeypatch):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    content = "plain notes without a marker\n"
    doc_id = _add_document(owner, content)
    monkeypatch.setattr("src.pdf_runtime.load_pymupdf_for_pdf_viewer", _forbid_loader)
    try:
        resp = _get_png(owner, doc_id)
        assert resp.status_code == 400
        assert resp.json()["detail"] == "Document is not linked to a source PDF"
        assert "error" not in resp.json()
        _assert_unchanged(doc_id, content)
    finally:
        _delete([doc_id])


def test_render_page_missing_pymupdf_stays_503(monkeypatch):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    content = _pdf_content()
    doc_id = _add_document(owner, content)
    _install_resolver(monkeypatch)

    def _missing():
        raise RuntimeError(PDF_VIEWER_PYMUPDF_MISSING)

    monkeypatch.setattr("src.pdf_runtime.load_pymupdf_for_pdf_viewer", _missing)
    try:
        resp = _get_png(owner, doc_id)
        assert resp.status_code == 503
        assert resp.json()["detail"] == PDF_VIEWER_PYMUPDF_MISSING
        assert "error" not in resp.json()
        _assert_unchanged(doc_id, content)
    finally:
        _delete([doc_id])


def test_render_page_success_returns_png_and_leaves_documents(monkeypatch):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    other = f"health-{uuid.uuid4().hex}"
    content = _pdf_content()
    other_content = _pdf_content("other body")
    doc_id = _add_document(owner, content)
    other_id = _add_document(other, other_content)
    _install_resolver(monkeypatch)
    doc = _two_pages()
    fitz = _FakeFitz(doc=doc)
    _install_fitz(monkeypatch, fitz)
    try:
        resp = _get_png(owner, doc_id, page_no=1)
        assert resp.status_code == 200, resp.text
        assert resp.content == _PNG
        assert resp.headers["content-type"].startswith("image/png")
        assert resp.headers["cache-control"] == "public, max-age=3600"
        _assert_no_secrets(resp)
        assert doc.closed is True
        assert fitz.opened is True
        _assert_unchanged(doc_id, content, owner)
        other_row = _row(other_id)
        assert other_row["current_content"] == other_content
        assert other_row["owner"] == other
    finally:
        _delete([doc_id, other_id])


def test_render_page_close_failure_on_success_still_returns_png(monkeypatch, caplog):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    content = _pdf_content()
    doc_id = _add_document(owner, content)
    _install_resolver(monkeypatch)
    doc = _two_pages(close_exc=RuntimeError(_CLOSE_SECRET))
    _install_fitz(monkeypatch, _FakeFitz(doc=doc))
    _install_failing_session(monkeypatch, rollback_exc=RuntimeError(_ROLLBACK_SECRET))
    caplog.set_level(logging.ERROR, logger="routes.document_routes")
    try:
        resp = _get_png(owner, doc_id, page_no=2)
        assert resp.status_code == 200, resp.text
        assert resp.content == _PNG
        _assert_no_secrets(resp)
        assert "PDF page document close failed" in caplog.text
        assert "PDF page session close failed" in caplog.text
        assert "PDF page rollback failed" not in caplog.text
        assert doc.closed is True
        _assert_unchanged(doc_id, content)
    finally:
        _delete([doc_id])


def test_pdf_pane_does_not_append_response_text():
    source = (_REPO / "static/js/document.js").read_text(encoding="utf-8")
    assert "function _pdfResponseErrorMessage" not in source
    start = source.index("async function _renderPdfPane(")
    end = source.index("if (docId !== activeDocId) return;", start)
    block = source[start:end]
    assert "res.text()" not in block
    assert "e.message" not in block
    assert "statusText" not in block
    assert "detail" not in block
    assert "_pdfResponseErrorMessage" not in block
    assert "Failed to load PDF view" in block
    assert "Failed to load PDF view:" not in block
    catch_at = block.index("} catch {")
    assert "${" not in block[catch_at:]
    img_at = source.index("/page/${page.page}.png", end)
    img_window = source[img_at:img_at + 500]
    assert "onerror" not in img_window
    assert "Failed to load" not in img_window


def test_render_routes_do_not_interpolate_exception():
    source = (_REPO / "routes/document_routes.py").read_text(encoding="utf-8")
    preview_start = source.index("async def export_pdf_preview(")
    pages_start = source.index("async def render_pages(", preview_start)
    png_start = source.index("async def render_page_png(", pages_start)
    fill_start = source.index("async def ai_fill_annotations(", png_start)
    preview = source[preview_start:pages_start]
    pages = source[pages_start:png_start]
    png = source[png_start:fill_start]
    assert "document_error" not in preview
    assert "except HTTPException:" not in preview
    for body in (pages, png):
        assert "HTTPException(500" not in body
        assert "str(e)" not in body
        assert 'document_error("Failed to load PDF view")' in body
        assert "except HTTPException:" in body
        assert "Failed to load PDF view:" not in body
    assert "PDF view rollback failed" in pages
    assert "PDF view session close failed" in pages
    assert "PDF view document close failed" in pages
    assert "PDF page rollback failed" in png
    assert "PDF page session close failed" in png
    assert "PDF page document close failed" in png
    assert 'HTTPException(503, str(exc))' in source
