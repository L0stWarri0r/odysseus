"""AI fill annotations must not leak exception text.

POST /api/document/{doc_id}/ai-fill-annotations closed the metadata session
without a guard, then imported PyMuPDF and opened the PDF outside that try.
A vision-model failure returned HTTP 503 with the exception text appended.
A database error, a JSON body error, a missing PyMuPDF install, a PDF open
error, or a page-render error escaped as an unhandled failure. A close
failure could also replace a successful annotation list or an HTTP error.

An unexpected failure now returns HTTP 502 with a fixed sentence. The
exception is logged and is not returned. A rollback failure, including one
raised again while closing the session, still returns that same 502. Closing
the PDF document is guarded the same way. An HTTPException raised inside the
handler is still passed through: a missing document and another user's
document stay HTTP 404, a document with no PDF marker stays HTTP 400, a
missing source file stays HTTP 404, an anonymous caller stays HTTP 403, and
an empty instruction stays HTTP 400. A vision-model failure stays HTTP 503
with the fixed sentence and no exception text. A per-page vision failure
still skips that page.

The AI fill button shows that fixed client sentence and does not read the
fill response body. The later document PUT in the same function is unchanged.
"""
import builtins
import json
import logging
import os
import sys
import uuid
from pathlib import Path

os.makedirs("data", exist_ok=True)

from fastapi import FastAPI
from fastapi.testclient import TestClient

import routes.document_routes as document_routes
from core.database import Document, DocumentVersion, SessionLocal

_QUERY_SECRET = "QUERY_SECRET database lookup token=do-not-return"
_ROLLBACK_SECRET = "ROLLBACK_SECRET connection reset while rolling back ai fill"
_CLOSE_SECRET = "CLOSE_SECRET database session handle token=do-not-return"
_VL_SECRET = "VL_SECRET vision endpoint token=do-not-return"
_FITZ_SECRET = "FITZ_SECRET pymupdf import path token=do-not-return"
_OPEN_SECRET = "OPEN_SECRET pymupdf path=/tmp/secret-fill.pdf token=do-not-return"
_RENDER_SECRET = "RENDER_SECRET pixmap buffer token=do-not-return"
_PDF_CLOSE_SECRET = "PDF_CLOSE_SECRET pymupdf document handle token=do-not-return"
_LLM_SECRET = "LLM_SECRET vision call token=do-not-return"
_RAW_SECRET = "RAW_SECRET model prose token=do-not-return"
_JSON_SECRET = "JSON_SECRET not-a-document token=do-not-return"
_UPLOAD_ID = "abcdef0123456789abcdef0123456789.pdf"
_ORIGINAL = "old body that must stay while annotations are filled"
_PNG = b"\x89PNG\r\n\x1a\nFAKEPAGE"
_REAL_SESSION = SessionLocal
_REPO = Path(__file__).resolve().parent.parent
_FIXED_ERROR = "Failed to fill annotations"
_VISION_ERROR = "No vision model available"
_SECRETS = (
    _QUERY_SECRET,
    _ROLLBACK_SECRET,
    _CLOSE_SECRET,
    _VL_SECRET,
    _FITZ_SECRET,
    _OPEN_SECRET,
    _RENDER_SECRET,
    _PDF_CLOSE_SECRET,
    _LLM_SECRET,
    _RAW_SECRET,
    _JSON_SECRET,
)


class _FailingSession:
    """Session whose rollback or close can raise. Close still closes the inner session."""

    def __init__(self, inner, rollback_exc=None, close_exc=None):
        self._inner = inner
        self._rollback_exc = rollback_exc
        self._close_exc = close_exc

    def rollback(self):
        if self._rollback_exc is not None:
            raise self._rollback_exc
        return self._inner.rollback()

    def close(self):
        try:
            if self._close_exc is not None:
                raise self._close_exc
            self.rollback()
        finally:
            self._inner.close()

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _QueryFailSession(_FailingSession):
    def query(self, *args, **kwargs):
        raise RuntimeError(_QUERY_SECRET)


class _FakePix:
    def tobytes(self, fmt):
        assert fmt == "png"
        return _PNG


class _FakePage:
    def __init__(self, pixmap_exc=None):
        self._pixmap_exc = pixmap_exc

    def get_pixmap(self, matrix, alpha=False):
        if self._pixmap_exc is not None:
            raise self._pixmap_exc
        return _FakePix()


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
        self.path = None

        class Matrix:
            def __init__(self, a, b):
                self.a = a
                self.b = b

        self.Matrix = Matrix

    def open(self, path):
        self.opened = True
        self.path = path
        if self._open_exc is not None:
            raise self._open_exc
        return self._doc


def _pdf_content(body=_ORIGINAL):
    return (
        f'<!-- pdf_source upload_id="{_UPLOAD_ID}" -->\n'
        "\n"
        "# Field notes\n"
        "\n"
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


def _install_failing_session(monkeypatch, rollback_exc=None, close_exc=None, query_fails=False):
    def _factory():
        inner = _REAL_SESSION()
        cls = _QueryFailSession if query_fails else _FailingSession
        return cls(inner, rollback_exc=rollback_exc, close_exc=close_exc)

    monkeypatch.setattr(document_routes, "SessionLocal", _factory)


def _install_resolver(monkeypatch, path="/tmp/owned.pdf"):
    monkeypatch.setattr(
        document_routes,
        "_resolve_user_upload_path",
        lambda *args, **kwargs: path,
    )


def _install_fitz(monkeypatch, fitz):
    monkeypatch.setitem(sys.modules, "fitz", fitz)


def _install_missing_fitz(monkeypatch):
    monkeypatch.delitem(sys.modules, "fitz", raising=False)
    real_import = builtins.__import__

    def _import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "fitz":
            raise ImportError(_FITZ_SECRET)
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", _import)


def _install_vl(monkeypatch, resolver=None):
    monkeypatch.setattr(
        "src.document_processor._load_vl_settings",
        lambda: {"vision_model": "vision-test"},
    )

    def _default(configured):
        assert configured == "vision-test"
        return ("http://127.0.0.1:9/v1", "vision-test", {})

    monkeypatch.setattr(
        "src.document_processor._resolve_vl_model",
        _default if resolver is None else resolver,
    )


def _install_llm(monkeypatch, llm):
    monkeypatch.setattr("src.llm_core.llm_call_async", llm)


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


def _post(username, doc_id, instruction="Fill the applicant name"):
    return _client(username).post(
        f"/api/document/{doc_id}/ai-fill-annotations",
        json={"instruction": instruction},
    )


def _assert_no_secrets(resp):
    for secret in _SECRETS:
        assert secret not in resp.text
    assert _ORIGINAL not in resp.text


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


def _two_pages(pixmap_exc=None, close_exc=None):
    return _FakeDoc(
        [_FakePage(pixmap_exc=pixmap_exc), _FakePage(pixmap_exc=pixmap_exc)],
        close_exc=close_exc,
    )


def test_ai_fill_query_failure_hides_exception(monkeypatch, caplog):
    owner = f"health-{uuid.uuid4().hex}"
    content = _pdf_content()
    doc_id = _add_document(owner, content)
    _install_failing_session(monkeypatch, query_fails=True)
    _install_vl(monkeypatch, resolver=lambda configured: (_ for _ in ()).throw(AssertionError("vl")))
    caplog.set_level(logging.ERROR, logger="routes.document_routes")
    try:
        resp = _post(owner, doc_id)
        _assert_fixed_502(resp)
        assert _QUERY_SECRET in caplog.text
        assert _FIXED_ERROR in caplog.text
        assert "AI fill rollback failed" not in caplog.text
        _assert_unchanged(doc_id, content, owner)
    finally:
        _delete([doc_id])


def test_ai_fill_session_factory_failure_hides_exception(monkeypatch, caplog):
    owner = f"health-{uuid.uuid4().hex}"
    content = _pdf_content()
    doc_id = _add_document(owner, content)

    def _factory():
        raise RuntimeError(_QUERY_SECRET)

    monkeypatch.setattr(document_routes, "SessionLocal", _factory)
    caplog.set_level(logging.ERROR, logger="routes.document_routes")
    try:
        resp = _post(owner, doc_id)
        _assert_fixed_502(resp)
        assert _QUERY_SECRET in caplog.text
        assert "AI fill rollback failed" not in caplog.text
        assert "AI fill session close failed" not in caplog.text
        _assert_unchanged(doc_id, content, owner)
    finally:
        _delete([doc_id])


def test_ai_fill_rollback_and_close_failure_stays_fixed_502(monkeypatch, caplog):
    owner = f"health-{uuid.uuid4().hex}"
    content = _pdf_content()
    doc_id = _add_document(owner, content)
    _install_failing_session(
        monkeypatch,
        rollback_exc=RuntimeError(_ROLLBACK_SECRET),
        close_exc=RuntimeError(_CLOSE_SECRET),
        query_fails=True,
    )
    caplog.set_level(logging.ERROR, logger="routes.document_routes")
    try:
        resp = _post(owner, doc_id)
        _assert_fixed_502(resp)
        assert _QUERY_SECRET in caplog.text
        assert _ROLLBACK_SECRET in caplog.text
        assert _CLOSE_SECRET in caplog.text
        assert "AI fill rollback failed" in caplog.text
        assert "AI fill session close failed" in caplog.text
        _assert_unchanged(doc_id, content, owner)
    finally:
        _delete([doc_id])


def test_ai_fill_missing_document_stays_404(monkeypatch):
    _pin_session(monkeypatch)
    _install_missing_fitz(monkeypatch)
    resp = _post(f"health-{uuid.uuid4().hex}", str(uuid.uuid4()))
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Document not found"
    assert "error" not in resp.json()
    _assert_no_secrets(resp)


def test_ai_fill_missing_document_close_failure_stays_404(monkeypatch, caplog):
    _install_failing_session(monkeypatch, close_exc=RuntimeError(_CLOSE_SECRET))
    _install_missing_fitz(monkeypatch)
    caplog.set_level(logging.ERROR, logger="routes.document_routes")
    resp = _post(f"health-{uuid.uuid4().hex}", str(uuid.uuid4()))
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Document not found"
    assert "error" not in resp.json()
    _assert_no_secrets(resp)
    assert "AI fill session close failed" in caplog.text
    assert _CLOSE_SECRET in caplog.text


def test_ai_fill_other_owner_stays_404(monkeypatch):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    stranger = f"health-{uuid.uuid4().hex}"
    content = _pdf_content()
    doc_id = _add_document(owner, content)
    _install_resolver(monkeypatch, path=None)
    _install_missing_fitz(monkeypatch)
    try:
        resp = _post(stranger, doc_id)
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Document not found"
        _assert_no_secrets(resp)
        _assert_unchanged(doc_id, content, owner)
    finally:
        _delete([doc_id])


def test_ai_fill_anonymous_stays_403(monkeypatch):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    content = _pdf_content()
    doc_id = _add_document(owner, content)
    _install_resolver(monkeypatch, path=None)
    _install_missing_fitz(monkeypatch)
    try:
        resp = _post(None, doc_id)
        assert resp.status_code == 403
        assert resp.json()["detail"] == "Authentication required"
        assert "error" not in resp.json()
        _assert_no_secrets(resp)
        _assert_unchanged(doc_id, content, owner)
    finally:
        _delete([doc_id])


def test_ai_fill_empty_instruction_stays_400(monkeypatch):
    opened = []

    def _factory():
        opened.append("session")
        raise AssertionError("session should not open")

    monkeypatch.setattr(document_routes, "SessionLocal", _factory)
    _install_missing_fitz(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    for instruction in ("", "   "):
        resp = _client(owner).post(
            f"/api/document/{uuid.uuid4()}/ai-fill-annotations",
            json={"instruction": instruction},
        )
        assert resp.status_code == 400
        assert resp.json()["detail"] == "instruction is required"
        assert "error" not in resp.json()
        _assert_no_secrets(resp)
    plain = _client(owner).post(
        f"/api/document/{uuid.uuid4()}/ai-fill-annotations",
        content=b'{"instruction":"Fill the applicant name"}',
        headers={"content-type": "text/plain"},
    )
    assert plain.status_code == 400
    assert plain.json()["detail"] == "instruction is required"
    assert opened == []


def test_ai_fill_bad_json_hides_exception(monkeypatch):
    opened = []

    def _factory():
        opened.append("session")
        raise AssertionError("session should not open")

    monkeypatch.setattr(document_routes, "SessionLocal", _factory)
    owner = f"health-{uuid.uuid4().hex}"
    resp = _client(owner).post(
        f"/api/document/{uuid.uuid4()}/ai-fill-annotations",
        content=b'{"instruction":"' + _JSON_SECRET.encode() + b'"',
        headers={"content-type": "application/json"},
    )
    _assert_fixed_502(resp)
    listed = _client(owner).post(
        f"/api/document/{uuid.uuid4()}/ai-fill-annotations",
        content=b"[1]",
        headers={"content-type": "application/json"},
    )
    _assert_fixed_502(listed)
    assert "attribute" not in listed.text.lower()
    assert opened == []


def test_ai_fill_without_pdf_marker_stays_400(monkeypatch):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    content = "just notes without a pdf"
    doc_id = _add_document(owner, content)
    _install_missing_fitz(monkeypatch)

    def _resolver(*args, **kwargs):
        raise AssertionError("upload lookup should not run")

    monkeypatch.setattr(document_routes, "_resolve_user_upload_path", _resolver)
    try:
        resp = _post(owner, doc_id)
        assert resp.status_code == 400
        assert resp.json()["detail"] == "Document is not linked to a source PDF"
        assert "error" not in resp.json()
        _assert_no_secrets(resp)
        _assert_unchanged(doc_id, content, owner)
    finally:
        _delete([doc_id])


def test_ai_fill_missing_source_stays_404(monkeypatch):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    content = _pdf_content()
    doc_id = _add_document(owner, content)
    _install_resolver(monkeypatch, path=None)
    _install_missing_fitz(monkeypatch)

    def _resolver(configured):
        raise AssertionError("vision model should not resolve")

    _install_vl(monkeypatch, resolver=_resolver)
    try:
        resp = _post(owner, doc_id)
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Source PDF not found"
        assert "in uploads" not in resp.text
        assert "error" not in resp.json()
        _assert_no_secrets(resp)
        _assert_unchanged(doc_id, content, owner)
    finally:
        _delete([doc_id])


def test_ai_fill_vision_failure_stays_fixed_503(monkeypatch, caplog):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    content = _pdf_content()
    doc_id = _add_document(owner, content)
    _install_resolver(monkeypatch)
    _install_missing_fitz(monkeypatch)

    def _resolver(configured):
        raise RuntimeError(_VL_SECRET)

    _install_vl(monkeypatch, resolver=_resolver)
    caplog.set_level(logging.ERROR, logger="routes.document_routes")
    try:
        resp = _post(owner, doc_id)
        assert resp.status_code == 503
        assert resp.json()["detail"] == _VISION_ERROR
        assert "error" not in resp.json()
        assert _VISION_ERROR + ":" not in resp.text
        _assert_no_secrets(resp)
        assert _VL_SECRET in caplog.text
        _assert_unchanged(doc_id, content, owner)
    finally:
        _delete([doc_id])


def test_ai_fill_missing_pymupdf_hides_exception(monkeypatch, caplog):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    content = _pdf_content()
    doc_id = _add_document(owner, content)
    _install_resolver(monkeypatch)
    _install_vl(monkeypatch)
    _install_missing_fitz(monkeypatch)
    caplog.set_level(logging.ERROR, logger="routes.document_routes")
    try:
        resp = _post(owner, doc_id)
        _assert_fixed_502(resp)
        assert _FITZ_SECRET in caplog.text
        assert "503" not in resp.text
        _assert_unchanged(doc_id, content, owner)
    finally:
        _delete([doc_id])


def test_ai_fill_open_failure_hides_exception(monkeypatch, caplog):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    content = _pdf_content()
    doc_id = _add_document(owner, content)
    _install_resolver(monkeypatch, path="/tmp/owned.pdf")
    _install_vl(monkeypatch)
    fitz = _FakeFitz(open_exc=RuntimeError(_OPEN_SECRET))
    _install_fitz(monkeypatch, fitz)
    called = []

    async def _llm(*args, **kwargs):
        called.append(args)
        raise AssertionError("vision model should not be called")

    _install_llm(monkeypatch, _llm)
    caplog.set_level(logging.ERROR, logger="routes.document_routes")
    try:
        resp = _post(owner, doc_id)
        _assert_fixed_502(resp)
        assert _OPEN_SECRET in caplog.text
        assert fitz.opened is True
        assert fitz.path == "/tmp/owned.pdf"
        assert called == []
        _assert_unchanged(doc_id, content, owner)
    finally:
        _delete([doc_id])


def test_ai_fill_render_and_document_close_failure_stays_fixed_502(monkeypatch, caplog):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    content = _pdf_content()
    doc_id = _add_document(owner, content)
    _install_resolver(monkeypatch)
    _install_vl(monkeypatch)
    doc = _two_pages(
        pixmap_exc=RuntimeError(_RENDER_SECRET),
        close_exc=RuntimeError(_PDF_CLOSE_SECRET),
    )
    fitz = _FakeFitz(doc=doc)
    _install_fitz(monkeypatch, fitz)
    called = []

    async def _llm(*args, **kwargs):
        called.append(args)
        raise AssertionError("vision model should not be called")

    _install_llm(monkeypatch, _llm)
    caplog.set_level(logging.ERROR, logger="routes.document_routes")
    try:
        resp = _post(owner, doc_id)
        _assert_fixed_502(resp)
        assert _RENDER_SECRET in caplog.text
        assert _PDF_CLOSE_SECRET in caplog.text
        assert "AI fill document close failed" in caplog.text
        assert doc.closed is True
        assert called == []
        _assert_unchanged(doc_id, content, owner)
    finally:
        _delete([doc_id])


def test_ai_fill_page_vision_failure_continues(monkeypatch, caplog):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    content = _pdf_content()
    doc_id = _add_document(owner, content)
    _install_resolver(monkeypatch)
    _install_vl(monkeypatch)
    doc = _two_pages()
    _install_fitz(monkeypatch, _FakeFitz(doc=doc))

    async def _llm(url, model_id, messages, **kwargs):
        text = messages[1]["content"][0]["text"]
        if "page 1 of" in text:
            raise RuntimeError(_LLM_SECRET)
        return json.dumps([
            {"x": 4, "y": 5, "w": 12, "h": 3, "value": "kept"},
        ])

    _install_llm(monkeypatch, _llm)
    caplog.set_level(logging.ERROR, logger="routes.document_routes")
    try:
        resp = _post(owner, doc_id)
        assert resp.status_code == 200
        assert resp.json() == {
            "annotations": [
                {"page": 2, "x": 4.0, "y": 5.0, "w": 12.0, "h": 3.0, "value": "kept"},
            ]
        }
        _assert_no_secrets(resp)
        assert _LLM_SECRET in caplog.text
        assert "VL call failed on page 1" in caplog.text
        assert doc.closed is True
        _assert_unchanged(doc_id, content, owner)
    finally:
        _delete([doc_id])


def test_ai_fill_success_returns_clamped_annotations_and_leaves_documents(monkeypatch, caplog):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    other = f"health-{uuid.uuid4().hex}"
    content = _pdf_content()
    other_content = _pdf_content("other owner body that must stay")
    doc_id = _add_document(owner, content)
    other_id = _add_document(other, other_content)
    _install_resolver(monkeypatch, path="/tmp/owned.pdf")
    _install_vl(monkeypatch)
    doc = _two_pages()
    fitz = _FakeFitz(doc=doc)
    _install_fitz(monkeypatch, fitz)
    proposal = [
        {"x": 10.5, "y": 20.25, "w": 22, "h": 3.5, "value": "123 Main St"},
        {"x": 120, "y": -5, "w": 40, "h": 10, "value": "Jane Doe"},
        {"x": 1, "y": 1, "w": 0.4, "h": 5, "value": "too small"},
        {"x": 1, "y": 1, "w": 10, "h": 0.2, "value": "too short"},
        {"x": 1, "y": 1, "w": 10, "h": 5, "value": "   "},
        {"x": "nope", "y": 1, "w": 10, "h": 5, "value": "bad"},
        "not-a-dict",
    ]

    async def _llm(url, model_id, messages, **kwargs):
        assert url == "http://127.0.0.1:9/v1"
        assert model_id == "vision-test"
        text = messages[1]["content"][0]["text"]
        assert "Fill the applicant name" in text
        image = messages[1]["content"][1]["image_url"]["url"]
        assert image.startswith("data:image/png;base64,")
        if "page 1 of 2" in text:
            return "```json\n" + json.dumps(proposal) + "\n```"
        if "page 2 of 2" in text:
            return "not json " + _RAW_SECRET
        raise AssertionError(text)

    _install_llm(monkeypatch, _llm)
    caplog.set_level(logging.WARNING, logger="routes.document_routes")
    try:
        resp = _post(owner, doc_id)
        assert resp.status_code == 200
        assert resp.json() == {
            "annotations": [
                {"page": 1, "x": 10.5, "y": 20.25, "w": 22.0, "h": 3.5, "value": "123 Main St"},
                {"page": 1, "x": 99.0, "y": 0.0, "w": 1.0, "h": 10.0, "value": "Jane Doe"},
            ]
        }
        _assert_no_secrets(resp)
        assert _RAW_SECRET in caplog.text
        assert "returned non-JSON" in caplog.text
        assert fitz.opened is True
        assert doc.closed is True
        _assert_unchanged(doc_id, content, owner)
        _assert_unchanged(other_id, other_content, other)
    finally:
        _delete([doc_id, other_id])


def test_ai_fill_close_failure_on_success_still_returns_annotations(monkeypatch, caplog):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    content = _pdf_content()
    doc_id = _add_document(owner, content)
    _install_resolver(monkeypatch)
    _install_vl(monkeypatch)
    doc = _FakeDoc(
        [_FakePage()],
        close_exc=RuntimeError(_PDF_CLOSE_SECRET),
    )
    _install_fitz(monkeypatch, _FakeFitz(doc=doc))

    async def _llm(url, model_id, messages, **kwargs):
        return json.dumps([{"x": 2, "y": 3, "w": 8, "h": 2, "value": "Ada"}])

    _install_llm(monkeypatch, _llm)
    caplog.set_level(logging.ERROR, logger="routes.document_routes")
    try:
        resp = _post(owner, doc_id)
        assert resp.status_code == 200
        assert resp.json()["annotations"] == [
            {"page": 1, "x": 2.0, "y": 3.0, "w": 8.0, "h": 2.0, "value": "Ada"},
        ]
        _assert_no_secrets(resp)
        assert doc.closed is True
        assert _PDF_CLOSE_SECRET in caplog.text
        assert "AI fill document close failed" in caplog.text
        _assert_unchanged(doc_id, content, owner)
    finally:
        _delete([doc_id])


def test_ai_fill_session_close_failure_on_success_still_returns_annotations(monkeypatch, caplog):
    owner = f"health-{uuid.uuid4().hex}"
    content = _pdf_content()
    doc_id = _add_document(owner, content)
    _install_failing_session(monkeypatch, close_exc=RuntimeError(_CLOSE_SECRET))
    _install_resolver(monkeypatch)
    _install_vl(monkeypatch)
    doc = _FakeDoc([_FakePage()])
    _install_fitz(monkeypatch, _FakeFitz(doc=doc))

    async def _llm(url, model_id, messages, **kwargs):
        return json.dumps([{"x": 2, "y": 3, "w": 8, "h": 2, "value": "Ada"}])

    _install_llm(monkeypatch, _llm)
    caplog.set_level(logging.ERROR, logger="routes.document_routes")
    try:
        resp = _post(owner, doc_id)
        assert resp.status_code == 200
        assert resp.json()["annotations"][0]["value"] == "Ada"
        _assert_no_secrets(resp)
        assert _CLOSE_SECRET in caplog.text
        assert "AI fill session close failed" in caplog.text
        assert doc.closed is True
        _assert_unchanged(doc_id, content, owner)
    finally:
        _delete([doc_id])


def test_ai_fill_client_does_not_append_response_text():
    source = (_REPO / "static/js/document.js").read_text(encoding="utf-8")
    start = source.index("async function _aiFillAnnotations(")
    end = source.index("function _schedulePdfPaneSave(", start)
    block = source[start:end]
    fill_at = block.index("ai-fill-annotations")
    put_at = block.index("method: 'PUT'", fill_at)
    fill_part = block[fill_at:put_at]
    assert "res.text()" not in fill_part
    assert "statusText" not in fill_part
    assert "throw new Error('AI fill failed')" in fill_part
    assert "r2.text()" in block
    assert "method: 'PUT'" in block
    assert "e.message" not in block
    assert "_setPdfSaveStatus('error', 'AI fill failed')" in block
    assert "_setPdfSaveStatus('error', `AI fill failed:" not in block
    catch = block[block.index("} catch (e) {"):]
    assert "${" not in catch
    assert "e.message" not in catch


def test_ai_fill_route_does_not_interpolate_exception_or_touch_other_handlers():
    source = (_REPO / "routes/document_routes.py").read_text(encoding="utf-8")
    preview_start = source.index("async def export_pdf_preview(")
    pages_start = source.index("async def render_pages(", preview_start)
    png_start = source.index("async def render_page_png(", pages_start)
    fill_start = source.index("async def ai_fill_annotations(", png_start)
    render_start = source.index("async def render_pdf(", fill_start)
    preview = source[preview_start:pages_start]
    pages = source[pages_start:png_start]
    png = source[png_start:fill_start]
    fill = source[fill_start:render_start]
    for body in (preview, pages, png):
        assert "document_error" not in body
        assert "except HTTPException:" not in body
    assert 'document_error("Failed to fill annotations")' in fill
    assert "except HTTPException:" in fill
    assert 'HTTPException(503, "No vision model available")' in fill
    assert "No vision model available:" not in fill
    assert "Failed to fill annotations:" not in fill
    assert "str(e)" not in fill
    assert "import fitz" in fill
    assert "_load_pdf_viewer_fitz" not in fill
    assert "AI fill rollback failed" in fill
    assert "AI fill session close failed" in fill
    assert "AI fill document close failed" in fill
    assert 'HTTPException(503, str(exc))' in source
    assert "logger.error(f\"VL call failed on page {page_index + 1}: {e}\")" in fill
