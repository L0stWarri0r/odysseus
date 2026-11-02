"""Signature save and delete must not leak exception text.

POST /api/signatures caught a database error, rolled back without a guard,
and returned HTTP 500 with the exception text. Closing the session was also
unguarded, so a close failure could replace the saved signature. DELETE
/api/signatures/{sig_id} re-raised HTTPException, then rolled back without a
guard and returned HTTP 500 with the exception text and no log. Its close
was unguarded too.

An unexpected failure now returns HTTP 502 with a fixed sentence. The
exception is logged and is not returned. A rollback failure, including one
raised again while closing the session, still returns that same 502. An
HTTPException raised inside the handler is still passed through: invalid
base64 stays HTTP 400, a missing signature stays HTTP 404, and another
user's signature stays HTTP 403. A close failure does not replace those
responses or a successful save or delete.

The draw modal shows that fixed client sentence and does not read the save
response body. The delete fetch is unchanged and still ignores the response.
GET /api/signatures is unchanged.
"""
import base64
import logging
import os
import uuid
from pathlib import Path

os.makedirs("data", exist_ok=True)

from fastapi import FastAPI
from fastapi.testclient import TestClient

import routes.signature_routes as signature_routes
from core.database import SessionLocal, Signature

_COMMIT_SECRET = "COMMIT_SECRET database write token=do-not-return"
_QUERY_SECRET = "QUERY_SECRET database lookup token=do-not-return"
_ROLLBACK_SECRET = "ROLLBACK_SECRET connection reset while rolling back signature"
_CLOSE_SECRET = "CLOSE_SECRET database session handle token=do-not-return"
_B64_SECRET = "NOT_B64_SECRET token=do-not-return"
_PNG = b"\x89PNG\r\n\x1a\nFAKE"
_B64 = base64.b64encode(_PNG).decode("ascii")
_REAL_SESSION = SessionLocal
_REPO = Path(__file__).resolve().parent.parent
_SAVE_ERROR = "Failed to save signature"
_DELETE_ERROR = "Failed to delete signature"
_SECRETS = (
    _COMMIT_SECRET,
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


class _CommitFailSession(_FailingSession):
    def commit(self):
        raise RuntimeError(_COMMIT_SECRET)


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
    commit_fails=False,
):
    def _factory():
        inner = _REAL_SESSION()
        if query_fails:
            cls = _QueryFailSession
        elif commit_fails:
            cls = _CommitFailSession
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


def _row(sig_id):
    db = _real_session()
    try:
        sig = db.query(Signature).filter(Signature.id == sig_id).first()
        if sig is None:
            return None
        return {
            "id": sig.id,
            "owner": sig.owner,
            "name": sig.name,
            "data_png": sig.data_png,
            "width": sig.width,
            "height": sig.height,
        }
    finally:
        db.close()


def _post(username, data, name="Signature", width=120, height=40):
    return _client(username).post(
        "/api/signatures",
        json={"data": data, "name": name, "width": width, "height": height},
    )


def _delete(username, sig_id):
    return _client(username).delete(f"/api/signatures/{sig_id}")


def _assert_no_secrets(resp):
    for secret in _SECRETS:
        assert secret not in resp.text


def _assert_fixed(resp, message):
    assert resp.status_code == 502
    assert resp.json() == {"error": message}
    assert "detail" not in resp.json()
    assert message + ":" not in resp.text
    _assert_no_secrets(resp)


def test_save_commit_failure_hides_exception(monkeypatch, caplog):
    owner = f"health-{uuid.uuid4().hex}"
    _install_failing_session(monkeypatch, commit_fails=True)
    caplog.set_level(logging.ERROR, logger="routes.signature_routes")
    resp = _post(owner, _B64, name="Initials")
    _assert_fixed(resp, _SAVE_ERROR)
    assert _COMMIT_SECRET in caplog.text
    assert _SAVE_ERROR in caplog.text
    assert "Signature save rollback failed" not in caplog.text
    assert "Signature save session close failed" not in caplog.text


def test_save_session_factory_failure_hides_exception(monkeypatch, caplog):
    def _factory():
        raise RuntimeError(_QUERY_SECRET)

    monkeypatch.setattr(signature_routes, "SessionLocal", _factory)
    caplog.set_level(logging.ERROR, logger="routes.signature_routes")
    resp = _post(f"health-{uuid.uuid4().hex}", _B64)
    _assert_fixed(resp, _SAVE_ERROR)
    assert _QUERY_SECRET in caplog.text
    assert "Signature save rollback failed" not in caplog.text
    assert "Signature save session close failed" not in caplog.text


def test_save_rollback_and_close_failure_stays_fixed_502(monkeypatch, caplog):
    _install_failing_session(
        monkeypatch,
        rollback_exc=RuntimeError(_ROLLBACK_SECRET),
        close_exc=RuntimeError(_CLOSE_SECRET),
        commit_fails=True,
    )
    caplog.set_level(logging.ERROR, logger="routes.signature_routes")
    resp = _post(f"health-{uuid.uuid4().hex}", _B64)
    _assert_fixed(resp, _SAVE_ERROR)
    assert _COMMIT_SECRET in caplog.text
    assert _ROLLBACK_SECRET in caplog.text
    assert _CLOSE_SECRET in caplog.text
    assert "Signature save rollback failed" in caplog.text
    assert "Signature save session close failed" in caplog.text


def test_save_invalid_base64_stays_400(monkeypatch):
    opened = []

    def _factory():
        opened.append("session")
        raise AssertionError("session should not open")

    monkeypatch.setattr(signature_routes, "SessionLocal", _factory)
    owner = f"health-{uuid.uuid4().hex}"
    payloads = (
        "",
        "   ",
        _B64_SECRET,
        "data:image/png;base64,",
        f"data:image/gif;base64,{_B64}",
    )
    for data in payloads:
        resp = _post(owner, data)
        assert resp.status_code == 400
        assert resp.json()["detail"] == "Signature data must be base64-encoded PNG bytes"
        assert "error" not in resp.json()
        _assert_no_secrets(resp)
    assert opened == []


def test_save_success_stamps_owner_and_lists_only_own(monkeypatch):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    stranger = f"health-{uuid.uuid4().hex}"
    created = []
    try:
        resp = _post(owner, f"data:image/png;base64,{_B64}", name="  ")
        assert resp.status_code == 200
        body = resp.json()
        assert body["name"] == "Signature"
        assert body["data_url"] == f"data:image/png;base64,{_B64}"
        assert body["width"] == 120
        assert body["height"] == 40
        assert body["created_at"].endswith("Z")
        created.append(body["id"])
        row = _row(body["id"])
        assert row["owner"] == owner
        assert row["name"] == "Signature"
        assert row["data_png"] == _B64
        assert _B64_SECRET not in resp.text

        jpeg = _post(owner, f"data:image/jpeg;base64,{_B64}", name="Initials", width=10, height=4)
        assert jpeg.status_code == 200
        created.append(jpeg.json()["id"])
        assert jpeg.json()["name"] == "Initials"
        assert jpeg.json()["data_url"] == f"data:image/png;base64,{_B64}"
        assert _row(jpeg.json()["id"])["data_png"] == _B64

        listed = _client(owner).get("/api/signatures")
        assert listed.status_code == 200
        ids = [item["id"] for item in listed.json()["signatures"]]
        assert created[0] in ids
        assert created[1] in ids
        hidden = _client(stranger).get("/api/signatures")
        hidden_ids = [item["id"] for item in hidden.json()["signatures"]]
        assert created[0] not in hidden_ids
        assert created[1] not in hidden_ids
    finally:
        _delete_rows(created)


def test_save_success_close_failure_still_returns_signature(monkeypatch, caplog):
    _install_failing_session(monkeypatch, close_exc=RuntimeError(_CLOSE_SECRET))
    owner = f"health-{uuid.uuid4().hex}"
    caplog.set_level(logging.ERROR, logger="routes.signature_routes")
    created = []
    try:
        resp = _post(owner, _B64, name="Initials")
        assert resp.status_code == 200
        body = resp.json()
        created.append(body["id"])
        assert body["name"] == "Initials"
        assert body["data_url"] == f"data:image/png;base64,{_B64}"
        assert "error" not in body
        _assert_no_secrets(resp)
        row = _row(body["id"])
        assert row["owner"] == owner
        assert row["data_png"] == _B64
        assert _CLOSE_SECRET in caplog.text
        assert "Signature save session close failed" in caplog.text
        assert _SAVE_ERROR not in caplog.text
    finally:
        _delete_rows(created)


def test_delete_missing_stays_404(monkeypatch):
    _pin_session(monkeypatch)
    resp = _delete(f"health-{uuid.uuid4().hex}", str(uuid.uuid4()))
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Signature not found"
    assert "error" not in resp.json()
    _assert_no_secrets(resp)


def test_delete_missing_close_failure_stays_404(monkeypatch, caplog):
    _install_failing_session(monkeypatch, close_exc=RuntimeError(_CLOSE_SECRET))
    caplog.set_level(logging.ERROR, logger="routes.signature_routes")
    resp = _delete(f"health-{uuid.uuid4().hex}", str(uuid.uuid4()))
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Signature not found"
    assert "error" not in resp.json()
    _assert_no_secrets(resp)
    assert "Signature delete session close failed" in caplog.text
    assert _CLOSE_SECRET in caplog.text
    assert _DELETE_ERROR not in caplog.text


def test_delete_other_owner_stays_403(monkeypatch):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    stranger = f"health-{uuid.uuid4().hex}"
    sig_id = _add_signature(owner, name="Kept")
    try:
        resp = _delete(stranger, sig_id)
        assert resp.status_code == 403
        assert resp.json()["detail"] == "Not your signature"
        assert "error" not in resp.json()
        _assert_no_secrets(resp)
        assert _row(sig_id)["owner"] == owner
        assert _row(sig_id)["name"] == "Kept"
    finally:
        _delete_rows([sig_id])


def test_delete_other_owner_close_failure_stays_403(monkeypatch, caplog):
    _install_failing_session(monkeypatch, close_exc=RuntimeError(_CLOSE_SECRET))
    owner = f"health-{uuid.uuid4().hex}"
    sig_id = _add_signature(owner)
    caplog.set_level(logging.ERROR, logger="routes.signature_routes")
    try:
        resp = _delete(f"health-{uuid.uuid4().hex}", sig_id)
        assert resp.status_code == 403
        assert resp.json()["detail"] == "Not your signature"
        _assert_no_secrets(resp)
        assert _row(sig_id)["owner"] == owner
        assert "Signature delete session close failed" in caplog.text
        assert _DELETE_ERROR not in caplog.text
    finally:
        _delete_rows([sig_id])


def test_delete_null_owner_stays_403_when_user_set(monkeypatch):
    _pin_session(monkeypatch)
    sig_id = _add_signature(None, name="Legacy")
    try:
        resp = _delete(f"health-{uuid.uuid4().hex}", sig_id)
        assert resp.status_code == 403
        assert resp.json()["detail"] == "Not your signature"
        assert _row(sig_id)["name"] == "Legacy"
        assert _row(sig_id)["owner"] is None
    finally:
        _delete_rows([sig_id])


def test_delete_commit_failure_hides_exception(monkeypatch, caplog):
    owner = f"health-{uuid.uuid4().hex}"
    sig_id = _add_signature(owner, name="Kept")
    _install_failing_session(monkeypatch, commit_fails=True)
    caplog.set_level(logging.ERROR, logger="routes.signature_routes")
    try:
        resp = _delete(owner, sig_id)
        _assert_fixed(resp, _DELETE_ERROR)
        assert _COMMIT_SECRET in caplog.text
        assert _DELETE_ERROR in caplog.text
        assert "Signature delete rollback failed" not in caplog.text
        assert _row(sig_id)["name"] == "Kept"
        assert _row(sig_id)["owner"] == owner
    finally:
        _delete_rows([sig_id])


def test_delete_query_failure_hides_exception(monkeypatch, caplog):
    owner = f"health-{uuid.uuid4().hex}"
    sig_id = _add_signature(owner, name="Kept")
    _install_failing_session(monkeypatch, query_fails=True)
    caplog.set_level(logging.ERROR, logger="routes.signature_routes")
    try:
        resp = _delete(owner, sig_id)
        _assert_fixed(resp, _DELETE_ERROR)
        assert _QUERY_SECRET in caplog.text
        assert "Signature delete rollback failed" not in caplog.text
        assert "Signature delete session close failed" not in caplog.text
        assert _row(sig_id)["name"] == "Kept"
    finally:
        _delete_rows([sig_id])


def test_delete_session_factory_failure_hides_exception(monkeypatch, caplog):
    owner = f"health-{uuid.uuid4().hex}"
    sig_id = _add_signature(owner, name="Kept")

    def _factory():
        raise RuntimeError(_QUERY_SECRET)

    monkeypatch.setattr(signature_routes, "SessionLocal", _factory)
    caplog.set_level(logging.ERROR, logger="routes.signature_routes")
    try:
        resp = _delete(owner, sig_id)
        _assert_fixed(resp, _DELETE_ERROR)
        assert _QUERY_SECRET in caplog.text
        assert "Signature delete rollback failed" not in caplog.text
        assert "Signature delete session close failed" not in caplog.text
        assert _row(sig_id)["name"] == "Kept"
    finally:
        _delete_rows([sig_id])


def test_delete_rollback_and_close_failure_stays_fixed_502(monkeypatch, caplog):
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
        resp = _delete(owner, sig_id)
        _assert_fixed(resp, _DELETE_ERROR)
        assert _QUERY_SECRET in caplog.text
        assert _ROLLBACK_SECRET in caplog.text
        assert _CLOSE_SECRET in caplog.text
        assert "Signature delete rollback failed" in caplog.text
        assert "Signature delete session close failed" in caplog.text
        assert _row(sig_id)["name"] == "Kept"
    finally:
        _delete_rows([sig_id])


def test_delete_success_removes_only_that_signature(monkeypatch):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    gone = _add_signature(owner, name="Gone")
    kept = _add_signature(owner, name="Kept")
    try:
        resp = _delete(owner, gone)
        assert resp.status_code == 200
        assert resp.json() == {"deleted": gone}
        assert "error" not in resp.json()
        _assert_no_secrets(resp)
        assert _row(gone) is None
        assert _row(kept)["name"] == "Kept"
    finally:
        _delete_rows([gone, kept])


def test_delete_success_close_failure_still_returns_deleted(monkeypatch, caplog):
    owner = f"health-{uuid.uuid4().hex}"
    sig_id = _add_signature(owner, name="Gone")
    _install_failing_session(monkeypatch, close_exc=RuntimeError(_CLOSE_SECRET))
    caplog.set_level(logging.ERROR, logger="routes.signature_routes")
    try:
        resp = _delete(owner, sig_id)
        assert resp.status_code == 200
        assert resp.json() == {"deleted": sig_id}
        _assert_no_secrets(resp)
        assert _row(sig_id) is None
        assert _CLOSE_SECRET in caplog.text
        assert "Signature delete session close failed" in caplog.text
        assert _DELETE_ERROR not in caplog.text
    finally:
        _delete_rows([sig_id])


def test_signature_routes_and_client_keep_the_fixed_sentences():
    source = (_REPO / "routes/signature_routes.py").read_text(encoding="utf-8")
    assert "def signature_error(message: str, status_code: int = 502, **extra):" in source
    list_start = source.index("async def list_signatures(")
    create_start = source.index("async def create_signature(")
    delete_start = source.index("async def delete_signature(")
    end = source.index("\n    return router", delete_start)
    listing = source[list_start:create_start]
    create = source[create_start:delete_start]
    delete = source[delete_start:end]

    assert "signature_error" not in listing
    assert "except" not in listing
    assert "db.close()" in listing
    assert "HTTPException(400, \"Signature data must be base64-encoded PNG bytes\")" in create
    assert "except HTTPException:" in create
    assert 'signature_error("Failed to save signature")' in create
    assert "Signature save rollback failed" in create
    assert "Signature save session close failed" in create
    assert "{e}" not in create
    assert "str(e)" not in create
    assert "HTTPException(404, \"Signature not found\")" in delete
    assert "HTTPException(403, \"Not your signature\")" in delete
    assert "except HTTPException:" in delete
    assert 'signature_error("Failed to delete signature")' in delete
    assert "Signature delete rollback failed" in delete
    assert "Signature delete session close failed" in delete
    assert "{e}" not in delete
    assert "str(e)" not in delete
    assert "logger.exception(\"Failed to delete signature\")" in delete

    client = (_REPO / "static/js/signature.js").read_text(encoding="utf-8")
    save_start = client.index("async function _saveSignature(")
    delete_fn = client.index("async function _deleteSignature(")
    save_fn = client[save_start:delete_fn]
    assert "r.text()" not in save_fn
    assert ".text()" not in save_fn
    assert "throw new Error('Failed to save signature')" in save_fn
    click_start = client.index("saveBtn.onclick = async () => {")
    pick_start = client.index("export function pick(")
    click = client[click_start:pick_start]
    assert "e.message" not in click
    assert "alert('Failed to save signature')" in click
    assert "console.error('Failed to save signature:', e)" in click
    smooth_start = client.index("const SMOOTH_KEY", delete_fn)
    delete_body = client[delete_fn:smooth_start]
    assert "await fetch(" in delete_body
    assert ".text()" not in delete_body
    assert ".ok" not in delete_body
    assert "btn.closest('.sig-tile')?.remove()" in client

    doc_source = (_REPO / "routes/document_routes.py").read_text(encoding="utf-8")
    fill_start = doc_source.index("async def ai_fill_annotations(")
    render_start = doc_source.index("async def render_pdf(", fill_start)
    fill = doc_source[fill_start:render_start]
    assert "document_error" not in fill
    assert "signature_error" not in fill
    assert 'No vision model available: {e}' in fill
    assert "import fitz" in fill
