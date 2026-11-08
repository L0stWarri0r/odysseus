"""Signature list must not leak exception text.

GET /api/signatures opened a session and queried it inside try/finally, with
no except. A query failure escaped, and db.close() was unguarded, so a close
failure could replace a successful list.

An unexpected failure now returns HTTP 502 with a fixed sentence. The
exception is logged and is not returned. A rollback failure, including one
raised again while closing the session, still returns that same 502. A
SessionLocal() failure has no session to roll back or close. An
HTTPException raised inside the handler is still passed through. A close
failure does not replace that response or a successful list.

The picker already returns an empty list when the response is not ok, and
it does not read the body. Save and delete are unchanged.
"""
import base64
import logging
import os
import uuid
from pathlib import Path

os.makedirs("data", exist_ok=True)

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import routes.signature_routes as signature_routes
from core.database import SessionLocal, Signature

_QUERY_SECRET = "QUERY_SECRET database lookup token=do-not-return"
_ROLLBACK_SECRET = "ROLLBACK_SECRET connection reset while rolling back signature"
_CLOSE_SECRET = "CLOSE_SECRET database session handle token=do-not-return"
_B64_SECRET = "NOT_B64_SECRET token=do-not-return"
_PNG = b"\x89PNG\r\n\x1a\nFAKE"
_B64 = base64.b64encode(_PNG).decode("ascii")
_REAL_SESSION = SessionLocal
_REPO = Path(__file__).resolve().parent.parent
_LIST_ERROR = "Failed to list signatures"
_SECRETS = (
    _QUERY_SECRET,
    _ROLLBACK_SECRET,
    _CLOSE_SECRET,
    _B64_SECRET,
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


class _HttpFailSession(_FailingSession):
    def query(self, *args, **kwargs):
        raise HTTPException(status_code=409, detail="Signature list refused")


def _client(username):
    app = FastAPI()

    @app.middleware("http")
    async def _stamp_user(request, call_next):
        if username is not None:
            request.state.current_user = username
        return await call_next(request)

    app.include_router(signature_routes.setup_signature_routes())
    return TestClient(app)


def _real_session():
    return _REAL_SESSION()


def _pin_session(monkeypatch):
    monkeypatch.setattr(signature_routes, "SessionLocal", _REAL_SESSION)


def _install_failing_session(
    monkeypatch,
    rollback_exc=None,
    close_exc=None,
    query_fails=False,
    http_fails=False,
):
    def _factory():
        inner = _REAL_SESSION()
        if query_fails:
            cls = _QueryFailSession
        elif http_fails:
            cls = _HttpFailSession
        else:
            cls = _FailingSession
        return cls(inner, rollback_exc=rollback_exc, close_exc=close_exc)

    monkeypatch.setattr(signature_routes, "SessionLocal", _factory)


def _add_signature(owner, name="Kept", data_png=None):
    db = _real_session()
    try:
        sig_id = str(uuid.uuid4())
        db.add(Signature(
            id=sig_id,
            owner=owner,
            name=name,
            data_png=data_png or _B64,
            width=12,
            height=8,
        ))
        db.commit()
        return sig_id
    finally:
        db.close()


def _delete_rows(sig_ids):
    db = _real_session()
    try:
        if sig_ids:
            db.query(Signature).filter(Signature.id.in_(list(sig_ids))).delete(
                synchronize_session=False
            )
        db.commit()
    finally:
        db.close()


def _get(username):
    return _client(username).get("/api/signatures")


def _assert_no_secrets(resp):
    for secret in _SECRETS:
        assert secret not in resp.text


def _assert_fixed(resp):
    assert resp.status_code == 502
    assert resp.json() == {"error": _LIST_ERROR}
    assert "detail" not in resp.json()
    assert _LIST_ERROR + ":" not in resp.text
    _assert_no_secrets(resp)


def test_list_query_failure_hides_exception(monkeypatch, caplog):
    owner = f"health-{uuid.uuid4().hex}"
    sig_id = _add_signature(owner, name="Kept")
    _install_failing_session(monkeypatch, query_fails=True)
    caplog.set_level(logging.ERROR, logger="routes.signature_routes")
    try:
        resp = _get(owner)
        _assert_fixed(resp)
        assert _QUERY_SECRET in caplog.text
        assert _LIST_ERROR in caplog.text
        assert "Signature list rollback failed" not in caplog.text
        assert "Signature list session close failed" not in caplog.text
    finally:
        _delete_rows([sig_id])


def test_list_session_factory_failure_hides_exception(monkeypatch, caplog):
    def _factory():
        raise RuntimeError(_QUERY_SECRET)

    monkeypatch.setattr(signature_routes, "SessionLocal", _factory)
    caplog.set_level(logging.ERROR, logger="routes.signature_routes")
    resp = _get(f"health-{uuid.uuid4().hex}")
    _assert_fixed(resp)
    assert _QUERY_SECRET in caplog.text
    assert "Signature list rollback failed" not in caplog.text
    assert "Signature list session close failed" not in caplog.text


def test_list_rollback_and_close_failure_stays_fixed_502(monkeypatch, caplog):
    owner = f"health-{uuid.uuid4().hex}"
    sig_id = _add_signature(owner, name="Kept")
    _install_failing_session(
        monkeypatch,
        rollback_exc=RuntimeError(_ROLLBACK_SECRET),
        close_exc=RuntimeError(_CLOSE_SECRET),
        query_fails=True,
    )
    caplog.set_level(logging.ERROR, logger="routes.signature_routes")
    try:
        resp = _get(owner)
        _assert_fixed(resp)
        assert _QUERY_SECRET in caplog.text
        assert _ROLLBACK_SECRET in caplog.text
        assert _CLOSE_SECRET in caplog.text
        assert "Signature list rollback failed" in caplog.text
        assert "Signature list session close failed" in caplog.text
    finally:
        _delete_rows([sig_id])


def test_list_http_exception_is_reraised_when_close_fails(monkeypatch, caplog):
    _install_failing_session(
        monkeypatch,
        close_exc=RuntimeError(_CLOSE_SECRET),
        http_fails=True,
    )
    caplog.set_level(logging.ERROR, logger="routes.signature_routes")
    resp = _get(f"health-{uuid.uuid4().hex}")
    assert resp.status_code == 409
    assert resp.json()["detail"] == "Signature list refused"
    assert "error" not in resp.json()
    _assert_no_secrets(resp)
    assert "Signature list session close failed" in caplog.text
    assert _CLOSE_SECRET in caplog.text
    assert _LIST_ERROR not in caplog.text


def test_list_success_returns_only_the_caller(monkeypatch):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    stranger = f"health-{uuid.uuid4().hex}"
    own = _add_signature(owner, name="Initials")
    other = _add_signature(stranger, name="Other")
    legacy = _add_signature(None, name="Legacy")
    try:
        resp = _get(owner)
        assert resp.status_code == 200
        body = resp.json()
        ids = [item["id"] for item in body["signatures"]]
        assert own in ids
        assert other not in ids
        assert legacy not in ids
        listed = next(item for item in body["signatures"] if item["id"] == own)
        assert listed["name"] == "Initials"
        assert listed["data_url"] == f"data:image/png;base64,{_B64}"
        assert listed["width"] == 12
        assert listed["height"] == 8
        assert listed["created_at"].endswith("Z")
        assert "error" not in body
        _assert_no_secrets(resp)
    finally:
        _delete_rows([own, other, legacy])


def test_list_without_user_stays_unfiltered(monkeypatch):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    own = _add_signature(owner, name="Initials")
    legacy = _add_signature(None, name="Legacy")
    try:
        resp = _get(None)
        assert resp.status_code == 200
        ids = [item["id"] for item in resp.json()["signatures"]]
        assert own in ids
        assert legacy in ids
        _assert_no_secrets(resp)
    finally:
        _delete_rows([own, legacy])


def test_list_success_close_failure_still_returns_signatures(monkeypatch, caplog):
    owner = f"health-{uuid.uuid4().hex}"
    sig_id = _add_signature(owner, name="Initials")
    _install_failing_session(monkeypatch, close_exc=RuntimeError(_CLOSE_SECRET))
    caplog.set_level(logging.ERROR, logger="routes.signature_routes")
    try:
        resp = _get(owner)
        assert resp.status_code == 200
        ids = [item["id"] for item in resp.json()["signatures"]]
        assert sig_id in ids
        assert resp.json()["signatures"]
        listed = next(item for item in resp.json()["signatures"] if item["id"] == sig_id)
        assert listed["name"] == "Initials"
        assert listed["data_url"] == f"data:image/png;base64,{_B64}"
        _assert_no_secrets(resp)
        assert _CLOSE_SECRET in caplog.text
        assert "Signature list session close failed" in caplog.text
        assert _LIST_ERROR not in caplog.text
    finally:
        _delete_rows([sig_id])


def test_signature_list_keeps_the_fixed_sentence_and_leaves_save_delete():
    source = (_REPO / "routes/signature_routes.py").read_text(encoding="utf-8")
    assert "def signature_error(message: str, status_code: int = 502, **extra):" in source
    list_start = source.index("async def list_signatures(")
    create_start = source.index("async def create_signature(")
    delete_start = source.index("async def delete_signature(")
    end = source.index("\n    return router", delete_start)
    listing = source[list_start:create_start]
    create = source[create_start:delete_start]
    delete = source[delete_start:end]

    assert "except HTTPException:" in listing
    assert 'signature_error("Failed to list signatures")' in listing
    assert 'logger.exception("Failed to list signatures")' in listing
    assert "Signature list rollback failed" in listing
    assert "Signature list session close failed" in listing
    assert "if user is not None:" in listing
    assert "Signature.owner == user" in listing
    assert "{e}" not in listing
    assert "str(e)" not in listing
    assert 'raise HTTPException(500, f"Failed to save signature: {e}")' in create
    assert "signature_error" not in create
    assert 'raise HTTPException(500, f"Failed to delete signature: {e}")' in delete
    assert "signature_error" not in delete
    assert "HTTPException(404, \"Signature not found\")" in delete
    assert "HTTPException(403, \"Not your signature\")" in delete

    client = (_REPO / "static/js/signature.js").read_text(encoding="utf-8")
    list_fn_start = client.index("async function _listSignatures(")
    save_fn_start = client.index("async function _saveSignature(")
    delete_fn = client.index("async function _deleteSignature(")
    listing_js = client[list_fn_start:save_fn_start]
    assert "if (!r.ok) return [];" in listing_js
    assert "await r.json()" in listing_js
    assert ".text()" not in listing_js
    assert "alert(" not in listing_js
    save_fn = client[save_fn_start:delete_fn]
    assert "const t = await r.text();" in save_fn
    click_start = client.index("saveBtn.onclick = async () => {")
    pick_start = client.index("export function pick(")
    click = client[click_start:pick_start]
    assert "alert('Failed to save signature: ' + e.message)" in click
    smooth_start = client.index("const SMOOTH_KEY", delete_fn)
    delete_body = client[delete_fn:smooth_start]
    assert "await fetch(" in delete_body
    assert ".text()" not in delete_body
    assert ".ok" not in delete_body

    doc_source = (_REPO / "routes/document_routes.py").read_text(encoding="utf-8")
    fill_start = doc_source.index("async def ai_fill_annotations(")
    render_start = doc_source.index("async def render_pdf(", fill_start)
    fill = doc_source[fill_start:render_start]
    assert "document_error" not in fill
    assert "signature_error" not in fill
    assert "No vision model available: {e}" in fill
    assert "import fitz" in fill
