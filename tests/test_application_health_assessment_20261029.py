"""Signed-reply preparation must not leak exception text or temp files.

POST /api/document/{doc_id}/prepare-signed-reply called fill_fields with no
handler around it, and closed the database session without a guard. A PDF
library error or a session error escaped as an unhandled failure, and the
temporary PDF was left on disk. An unexpected failure now returns HTTP 502
with a fixed sentence. The exception is logged and is not returned. A
rollback failure, including one raised again while closing the session,
still returns that same 502. An HTTPException raised inside the handler is
not rewritten: a missing document and another user's document stay HTTP 404,
a document with no source email stays HTTP 400, a document with no PDF
marker stays HTTP 400, a missing source file stays HTTP 404, and an
anonymous caller stays HTTP 403.

A stamp failure is still logged and the filled PDF is still attached. The
signed-reply client reads result.error and does not read detail or the
exception text.
"""
import logging
import os
import sys
import types
import uuid
from pathlib import Path

os.makedirs("data", exist_ok=True)

from fastapi import FastAPI
from fastapi.testclient import TestClient

import routes.document_routes as document_routes
from core.database import Document, DocumentVersion, SessionLocal

_FILL_SECRET = "FILL_SECRET acroform widget path=/tmp/secret-form.pdf token=do-not-return"
_ROLLBACK_SECRET = "ROLLBACK_SECRET connection reset while rolling back signed reply"
_STAMP_SECRET = "STAMP_SECRET annotation png bytes=do-not-return"
_PARSE_SECRET = "PARSE_SECRET markdown field decoder token=do-not-return"
_SIDECAR_SECRET = "SIDECAR_SECRET field schema path=/tmp/secret-sidecar.json token=do-not-return"
_UPLOAD_ID = "abcdef0123456789abcdef0123456789.pdf"
_ORIGINAL = "old body that must stay while the signed reply is prepared"
_PDF_BYTES = b"%PDF-1.4\n%signed-reply\n"
# The route looks up SessionLocal on this module at call time. Another test
# replaces that name with a temp engine; reads and cleanup use the app engine.
_REAL_SESSION = SessionLocal
_REPO = Path(__file__).resolve().parent.parent
_FIXED_ERROR = "Failed to prepare signed reply"


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


class _ImapConn:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def select(self, folder, readonly=True):
        return "OK", None

    def fetch(self, uid, query):
        raw = (
            b"From: Ada Lovelace <ada@example.com>\r\n"
            b"Subject: Quarterly form\r\n"
            b"Message-ID: <src@example.com>\r\n"
            b"References: <older@example.com>\r\n"
            b"\r\n"
        )
        return "OK", [(b"1", raw)]


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


def _isolate_compose(monkeypatch, tmp_path):
    monkeypatch.setenv("ODYSSEUS_MAIL_ATTACHMENTS_DIR", str(tmp_path))


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


def _install_imap(monkeypatch):
    """Header fetch imports routes.email_routes inside the handler.

    A stand-in module keeps the test off the real IMAP connection.
    """
    module = types.ModuleType("routes.email_routes")
    module._imap = lambda *args, **kwargs: _ImapConn()
    module._decode_header = lambda value: value or ""
    monkeypatch.setitem(sys.modules, "routes.email_routes", module)


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


def _add_document(owner, content, title="Field notes", source_email=True):
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
            source_email_uid="42" if source_email else None,
            source_email_folder="INBOX" if source_email else None,
            source_email_account_id="acct-1" if source_email else None,
            source_email_message_id="<src@example.com>" if source_email else None,
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
    return _client(username).post(f"/api/document/{doc_id}/prepare-signed-reply")


def _compose_files(tmp_path):
    folder = Path(tmp_path) / "_compose"
    if not folder.exists():
        return []
    return sorted(path.name for path in folder.iterdir() if path.is_file())


def _assert_fixed_502(resp):
    assert resp.status_code == 502
    assert resp.json() == {"error": _FIXED_ERROR}
    assert "detail" not in resp.json()
    assert _FILL_SECRET not in resp.text
    assert _ROLLBACK_SECRET not in resp.text
    assert _STAMP_SECRET not in resp.text
    assert _PARSE_SECRET not in resp.text
    assert _SIDECAR_SECRET not in resp.text
    assert "PDF fill failed:" not in resp.text


def test_signed_reply_fill_failure_hides_exception(monkeypatch, tmp_path, caplog):
    _pin_session(monkeypatch)
    _isolate_compose(monkeypatch, tmp_path)
    owner = f"health-{uuid.uuid4().hex}"
    content = _pdf_content()
    doc_id = _add_document(owner, content)
    _install_resolver(monkeypatch)
    _install_fill(monkeypatch, _raising_fill)
    caplog.set_level(logging.ERROR, logger="routes.document_routes")
    try:
        resp = _post(owner, doc_id)
        _assert_fixed_502(resp)
        assert _FILL_SECRET in caplog.text
        assert _FIXED_ERROR in caplog.text
        assert not os.path.exists(_raising_fill.seen)
        assert _compose_files(tmp_path) == []
        row = _row(doc_id)
        assert row["current_content"] == content
        assert row["version_count"] == 1
        assert row["versions"] == []
    finally:
        _delete([doc_id])


def test_signed_reply_rollback_and_close_failure_stays_fixed_502(monkeypatch, tmp_path, caplog):
    _pin_session(monkeypatch)
    _isolate_compose(monkeypatch, tmp_path)
    owner = f"health-{uuid.uuid4().hex}"
    content = _pdf_content()
    doc_id = _add_document(owner, content)
    _install_resolver(monkeypatch)
    _install_fill(monkeypatch, _raising_fill)
    _install_failing_session(monkeypatch, rollback_exc=RuntimeError(_ROLLBACK_SECRET))
    caplog.set_level(logging.ERROR, logger="routes.document_routes")
    try:
        resp = _post(owner, doc_id)
        _assert_fixed_502(resp)
        assert _FILL_SECRET in caplog.text
        assert _ROLLBACK_SECRET in caplog.text
        assert "Signed reply rollback failed" in caplog.text
        assert "Signed reply session close failed" in caplog.text
        assert not os.path.exists(_raising_fill.seen)
        assert _compose_files(tmp_path) == []
        assert _row(doc_id)["current_content"] == content
        assert _row(doc_id)["versions"] == []
    finally:
        _delete([doc_id])


def test_signed_reply_parse_failure_hides_exception(monkeypatch, tmp_path, caplog):
    _pin_session(monkeypatch)
    _isolate_compose(monkeypatch, tmp_path)
    owner = f"health-{uuid.uuid4().hex}"
    content = _pdf_content()
    doc_id = _add_document(owner, content)
    _install_resolver(monkeypatch)
    _install_fill(monkeypatch, _forbidden_fill)
    monkeypatch.setattr("src.pdf_form_doc.parse_markdown_to_values", _raising_parse)
    caplog.set_level(logging.ERROR, logger="routes.document_routes")
    try:
        resp = _post(owner, doc_id)
        _assert_fixed_502(resp)
        assert _PARSE_SECRET in caplog.text
        assert _row(doc_id)["current_content"] == content
        assert _row(doc_id)["versions"] == []
    finally:
        _delete([doc_id])


def test_signed_reply_sidecar_failure_hides_exception(monkeypatch, tmp_path, caplog):
    _pin_session(monkeypatch)
    _isolate_compose(monkeypatch, tmp_path)
    owner = f"health-{uuid.uuid4().hex}"
    content = _pdf_content()
    doc_id = _add_document(owner, content)
    _install_resolver(monkeypatch)
    _install_fill(monkeypatch, _forbidden_fill)
    monkeypatch.setattr("src.pdf_form_doc.load_field_sidecar", _raising_sidecar)
    caplog.set_level(logging.ERROR, logger="routes.document_routes")
    try:
        resp = _post(owner, doc_id)
        _assert_fixed_502(resp)
        assert _SIDECAR_SECRET in caplog.text
        assert _row(doc_id)["current_content"] == content
        assert _row(doc_id)["versions"] == []
    finally:
        _delete([doc_id])


def test_signed_reply_missing_document_stays_404(monkeypatch, tmp_path):
    _pin_session(monkeypatch)
    _isolate_compose(monkeypatch, tmp_path)
    _install_fill(monkeypatch, _forbidden_fill)
    resp = _post(f"health-{uuid.uuid4().hex}", str(uuid.uuid4()))
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Document not found"
    assert "error" not in resp.json()


def test_signed_reply_other_owner_stays_404(monkeypatch, tmp_path):
    _pin_session(monkeypatch)
    _isolate_compose(monkeypatch, tmp_path)
    owner = f"health-{uuid.uuid4().hex}"
    stranger = f"health-{uuid.uuid4().hex}"
    content = _pdf_content()
    doc_id = _add_document(owner, content)
    _install_resolver(monkeypatch)
    _install_fill(monkeypatch, _forbidden_fill)
    try:
        resp = _post(stranger, doc_id)
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Document not found"
        assert _FILL_SECRET not in resp.text
        assert _row(doc_id)["current_content"] == content
        assert _row(doc_id)["owner"] == owner
        assert _row(doc_id)["versions"] == []
    finally:
        _delete([doc_id])


def test_signed_reply_anonymous_stays_403(monkeypatch, tmp_path):
    _pin_session(monkeypatch)
    _isolate_compose(monkeypatch, tmp_path)
    owner = f"health-{uuid.uuid4().hex}"
    content = _pdf_content()
    doc_id = _add_document(owner, content)
    _install_resolver(monkeypatch)
    _install_fill(monkeypatch, _forbidden_fill)
    try:
        resp = _post(None, doc_id)
        assert resp.status_code == 403
        assert resp.json()["detail"] == "Authentication required"
        assert "error" not in resp.json()
        assert _row(doc_id)["current_content"] == content
    finally:
        _delete([doc_id])


def test_signed_reply_without_source_email_stays_400(monkeypatch, tmp_path):
    _pin_session(monkeypatch)
    _isolate_compose(monkeypatch, tmp_path)
    owner = f"health-{uuid.uuid4().hex}"
    content = _pdf_content()
    doc_id = _add_document(owner, content, source_email=False)
    _install_resolver(monkeypatch)
    _install_fill(monkeypatch, _forbidden_fill)
    try:
        resp = _post(owner, doc_id)
        assert resp.status_code == 400
        assert resp.json()["detail"] == "Document has no source email — cannot reply"
        assert "error" not in resp.json()
        assert _row(doc_id)["current_content"] == content
        assert _row(doc_id)["versions"] == []
    finally:
        _delete([doc_id])


def test_signed_reply_without_pdf_marker_stays_400(monkeypatch, tmp_path):
    _pin_session(monkeypatch)
    _isolate_compose(monkeypatch, tmp_path)
    owner = f"health-{uuid.uuid4().hex}"
    content = "plain notes without a marker\n"
    doc_id = _add_document(owner, content)
    _install_fill(monkeypatch, _forbidden_fill)
    try:
        resp = _post(owner, doc_id)
        assert resp.status_code == 400
        assert resp.json()["detail"] == "Document is not linked to a source PDF"
        assert "error" not in resp.json()
        assert _row(doc_id)["current_content"] == content
        assert _row(doc_id)["versions"] == []
    finally:
        _delete([doc_id])


def test_signed_reply_missing_source_pdf_stays_404(monkeypatch, tmp_path):
    _pin_session(monkeypatch)
    _isolate_compose(monkeypatch, tmp_path)
    owner = f"health-{uuid.uuid4().hex}"
    content = _pdf_content()
    doc_id = _add_document(owner, content)
    _install_resolver(monkeypatch, path=None)
    _install_fill(monkeypatch, _forbidden_fill)
    try:
        resp = _post(owner, doc_id)
        assert resp.status_code == 404
        assert resp.json()["detail"] == f"Source PDF {_UPLOAD_ID} not found"
        assert "error" not in resp.json()
        assert _row(doc_id)["current_content"] == content
        assert _row(doc_id)["versions"] == []
    finally:
        _delete([doc_id])


def test_signed_reply_success_returns_attachment_and_leaves_documents(monkeypatch, tmp_path):
    _pin_session(monkeypatch)
    _isolate_compose(monkeypatch, tmp_path)
    _install_imap(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    other = f"health-{uuid.uuid4().hex}"
    content = _pdf_content()
    other_content = _pdf_content("other body")
    doc_id = _add_document(owner, content)
    other_id = _add_document(other, other_content)
    _install_resolver(monkeypatch)
    _install_fill(monkeypatch, _ok_fill)
    try:
        resp = _post(owner, doc_id)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is True
        assert body["attachment"]["filename"] == "Field_notes_signed.pdf"
        assert body["attachment"]["size"] == len(_PDF_BYTES)
        token = body["attachment"]["token"]
        assert token.endswith("_Field_notes_signed.pdf")
        saved = Path(tmp_path) / "_compose" / token
        assert saved.read_bytes() == _PDF_BYTES
        assert _FILL_SECRET not in resp.text
        assert _ok_fill.seen["source"] == "/tmp/owned.pdf"
        assert _ok_fill.seen["values"] == {"city": "Springfield"}
        reply = body["reply"]
        assert reply["to"] == "ada@example.com"
        assert reply["to_name"] == "Ada Lovelace"
        assert reply["subject"] == "Re: Quarterly form"
        assert reply["in_reply_to"] == "<src@example.com>"
        assert "<older@example.com>" in reply["references"]
        assert reply["account_id"] == "acct-1"
        assert reply["source_uid"] == "42"
        assert reply["source_folder"] == "INBOX"
        assert reply["source_message_id"] == "<src@example.com>"
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


def test_signed_reply_stamp_failure_still_returns_attachment(monkeypatch, tmp_path, caplog):
    _pin_session(monkeypatch)
    _isolate_compose(monkeypatch, tmp_path)
    _install_imap(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    content = _pdf_content(annotation=True)
    doc_id = _add_document(owner, content)
    _install_resolver(monkeypatch)
    _install_fill(monkeypatch, _ok_fill)
    _install_stamp(monkeypatch, _raising_stamp)
    caplog.set_level(logging.WARNING, logger="routes.document_routes")
    try:
        resp = _post(owner, doc_id)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is True
        token = body["attachment"]["token"]
        saved = Path(tmp_path) / "_compose" / token
        assert saved.read_bytes() == _PDF_BYTES
        assert _STAMP_SECRET not in resp.text
        assert "PDF fill failed:" not in resp.text
        assert _STAMP_SECRET in caplog.text
        assert "stamp_annotations failed for" in caplog.text
        assert _row(doc_id)["current_content"] == content
        assert _row(doc_id)["versions"] == []
    finally:
        _delete([doc_id])


def test_signed_reply_close_failure_on_success_still_returns_attachment(monkeypatch, tmp_path, caplog):
    _pin_session(monkeypatch)
    _isolate_compose(monkeypatch, tmp_path)
    _install_imap(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    content = _pdf_content()
    doc_id = _add_document(owner, content)
    _install_resolver(monkeypatch)
    _install_fill(monkeypatch, _ok_fill)
    _install_failing_session(monkeypatch, rollback_exc=RuntimeError(_ROLLBACK_SECRET))
    caplog.set_level(logging.ERROR, logger="routes.document_routes")
    try:
        resp = _post(owner, doc_id)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is True
        token = body["attachment"]["token"]
        saved = Path(tmp_path) / "_compose" / token
        assert saved.read_bytes() == _PDF_BYTES
        assert _ROLLBACK_SECRET not in resp.text
        assert "Signed reply session close failed" in caplog.text
        assert _row(doc_id)["current_content"] == content
    finally:
        _delete([doc_id])


def test_signed_reply_client_does_not_read_error_detail():
    source = (_REPO / "static/js/document.js").read_text(encoding="utf-8")
    start = source.index("async function _sendSignedReply(")
    end = source.index("const att = result.attachment;", start)
    block = source[start:end]
    assert "/prepare-signed-reply" in block
    assert "result.error" in block
    assert "result.detail" not in block
    assert "res.text()" not in block
    assert "e.message" not in block
    assert "Couldn't prepare signed reply: ${msg}" in block
    assert 'showError("Couldn\'t prepare signed reply")' in block


def test_signed_reply_route_does_not_interpolate_exception():
    source = (_REPO / "routes/document_routes.py").read_text(encoding="utf-8")
    start = source.index("async def prepare_signed_reply(")
    end = source.index("\n    return router", start)
    body = source[start:end]
    assert "HTTPException(500" not in body
    assert 'document_error("Failed to prepare signed reply")' in body
    assert "Signed reply rollback failed" in body
    assert "Signed reply session close failed" in body
    assert "Signed reply temp cleanup failed" in body
    assert "stamp_signatures failed for" in body
    assert "stamp_annotations failed for" in body
    assert "Failed to prepare signed reply:" not in body
