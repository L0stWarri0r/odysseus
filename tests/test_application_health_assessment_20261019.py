"""Creating a document must not return database exception text.

POST /api/document used to raise HTTP 500 with
"Failed to create document: {exception}". A failed commit can include SQL
and bound values in that message. An unexpected failure now returns HTTP
502 with a fixed sentence. The exception is logged and is not returned. A
rollback failure, including one raised again while closing the session,
still returns that same 502. An HTTPException raised inside the handler is
not rewritten. A missing session stays HTTP 404. Another user's session
stays HTTP 403. Success stays the _doc_to_dict payload with no error key.

The editor create path checks res.ok before reading the body, so a failure
is not added as a document tab. The toast stays the fixed sentence.
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

_COMMIT_SECRET = "COMMIT_SECRET sql=INSERT INTO documents content=secret-notes token=create-doc"
_ROLLBACK_SECRET = "ROLLBACK_SECRET connection reset while rolling back document create"
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
            db.query(DocumentVersion).filter(
                DocumentVersion.document_id.in_(doc_ids)
            ).delete(synchronize_session=False)
            db.query(Document).filter(Document.id.in_(doc_ids)).delete(synchronize_session=False)
        if session_ids:
            db.query(DbSession).filter(DbSession.id.in_(session_ids)).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


def _count_for_session(session_id):
    db = _real_session()
    try:
        return db.query(Document).filter(Document.session_id == session_id).count()
    finally:
        db.close()


def _count_titled(title):
    db = _real_session()
    try:
        return db.query(Document).filter(Document.title == title).count()
    finally:
        db.close()


def _body(session_id, title, content="hello notes"):
    payload = {"title": title, "content": content, "language": "markdown"}
    if session_id is not None:
        payload["session_id"] = session_id
    return payload


def test_create_document_commit_failure_hides_exception(monkeypatch, caplog):
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner)
    title = f"health-create-{uuid.uuid4().hex}"
    _install_failing_session(monkeypatch, commit_exc=RuntimeError(_COMMIT_SECRET))
    caplog.set_level(logging.ERROR, logger="routes.document_routes")
    try:
        resp = _client(owner).post(
            "/api/document",
            json=_body(session_id, title),
        )
        assert resp.status_code == 502
        assert resp.json() == {"error": "Failed to create document"}
        assert "detail" not in resp.json()
        assert _COMMIT_SECRET not in resp.text
        assert "Failed to create document:" not in resp.text
        assert _COMMIT_SECRET in caplog.text
        assert _count_for_session(session_id) == 0
        assert _count_titled(title) == 0
    finally:
        _delete([], [session_id])


def test_create_document_rollback_failure_stays_fixed_502(monkeypatch, caplog):
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner)
    title = f"health-create-{uuid.uuid4().hex}"
    _install_failing_session(
        monkeypatch,
        commit_exc=RuntimeError(_COMMIT_SECRET),
        rollback_exc=RuntimeError(_ROLLBACK_SECRET),
    )
    caplog.set_level(logging.ERROR, logger="routes.document_routes")
    try:
        resp = _client(owner).post(
            "/api/document",
            json=_body(session_id, title),
        )
        assert resp.status_code == 502
        assert resp.json() == {"error": "Failed to create document"}
        assert "detail" not in resp.json()
        assert _COMMIT_SECRET not in resp.text
        assert _ROLLBACK_SECRET not in resp.text
        assert _COMMIT_SECRET in caplog.text
        assert _ROLLBACK_SECRET in caplog.text
        assert "Document create rollback failed" in caplog.text
        assert _count_for_session(session_id) == 0
        assert _count_titled(title) == 0
    finally:
        _delete([], [session_id])


def test_create_document_http_exception_is_not_rewritten(monkeypatch):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    title = f"health-create-{uuid.uuid4().hex}"

    def _reject(*args, **kwargs):
        raise HTTPException(400, "Document PDF marker references an upload you do not own")

    monkeypatch.setattr(document_routes, "_assert_pdf_marker_upload_owned", _reject)
    resp = _client(owner).post("/api/document", json=_body(None, title, "pdf marker"))
    assert resp.status_code == 400
    assert resp.json() == {"detail": "Document PDF marker references an upload you do not own"}
    assert resp.json().get("error") != "Failed to create document"
    assert _count_titled(title) == 0


def test_create_document_missing_session_stays_404(monkeypatch):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    missing = uuid.uuid4().hex
    title = f"health-create-{uuid.uuid4().hex}"
    resp = _client(owner).post("/api/document", json=_body(missing, title))
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Session not found"
    assert "Failed to create document" not in resp.text
    assert _COMMIT_SECRET not in resp.text
    assert _count_for_session(missing) == 0
    assert _count_titled(title) == 0


def test_create_document_other_users_session_stays_403(monkeypatch):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    other = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(other)
    title = f"health-create-{uuid.uuid4().hex}"
    try:
        resp = _client(owner).post("/api/document", json=_body(session_id, title))
        assert resp.status_code == 403
        assert resp.json()["detail"] == "Cannot create document in another user's session"
        assert "Failed to create document" not in resp.text
        assert _count_for_session(session_id) == 0
        assert _count_titled(title) == 0
    finally:
        _delete([], [session_id])


def test_create_document_success_keeps_doc_shape(monkeypatch):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner)
    title = f"health-create-{uuid.uuid4().hex}"
    content = "hello notes"
    created = []
    try:
        owned = _client(owner).post(
            "/api/document",
            json=_body(session_id, title, content),
        )
        assert owned.status_code == 200
        body = owned.json()
        assert "error" not in body
        assert "detail" not in body
        assert _COMMIT_SECRET not in owned.text
        created.append(body["id"])
        db = _real_session()
        try:
            doc = db.query(Document).filter(Document.id == body["id"]).one()
            assert doc.owner == owner
            assert doc.session_id == session_id
            assert doc.title == title
            assert doc.language == "markdown"
            assert doc.current_content == content
            assert doc.version_count == 1
            assert body == _doc_to_dict(doc)
            versions = db.query(DocumentVersion).filter(
                DocumentVersion.document_id == doc.id
            ).all()
            assert len(versions) == 1
            assert versions[0].content == content
            assert versions[0].version_number == 1
        finally:
            db.close()

        library_title = f"health-create-{uuid.uuid4().hex}"
        library = _client(owner).post(
            "/api/document",
            json=_body(None, library_title, content),
        )
        assert library.status_code == 200
        library_body = library.json()
        assert "error" not in library_body
        created.append(library_body["id"])
        db = _real_session()
        try:
            doc = db.query(Document).filter(Document.id == library_body["id"]).one()
            assert doc.owner == owner
            assert doc.session_id is None
            assert doc.title == library_title
            assert library_body == _doc_to_dict(doc)
        finally:
            db.close()
    finally:
        _delete(created, [session_id])


def test_create_document_anonymous_inherits_session_owner(monkeypatch):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner)
    title = f"health-create-{uuid.uuid4().hex}"
    created = []
    monkeypatch.setattr("src.auth_helpers.require_privilege", lambda request, key: "")
    try:
        resp = _client(None).post("/api/document", json=_body(session_id, title))
        assert resp.status_code == 200
        body = resp.json()
        assert "error" not in body
        created.append(body["id"])
        db = _real_session()
        try:
            doc = db.query(Document).filter(Document.id == body["id"]).one()
            assert doc.owner == owner
            assert body == _doc_to_dict(doc)
        finally:
            db.close()
    finally:
        _delete(created, [session_id])


def test_create_document_client_checks_ok_before_json():
    source = (_REPO / "static/js/document.js").read_text(encoding="utf-8")
    start = source.index("export async function createDocument(sessionId)")
    end = source.index("/** Load an existing document into a tab */", start)
    block = source[start:end]
    guard = "if (!res.ok) throw new Error('Failed to create document');"
    assert guard in block
    assert block.index(guard) < block.index("const doc = await res.json();")
    failure = block[block.index(guard):block.index("const doc = await res.json();")]
    assert ".json()" not in failure
    assert "detail" not in failure
    assert "error" not in failure
    assert "showError('Failed to create document')" in block
    assert "e.message" not in block
    assert "detail" not in block


def test_auto_create_from_input_checks_ok_before_json():
    source = (_REPO / "static/js/document.js").read_text(encoding="utf-8")
    start = source.index("async function _autoCreateFromInput(content)")
    end = source.index("function saveCurrentToMap()", start)
    block = source[start:end]
    guard = "if (!res.ok) throw new Error('Failed to create document');"
    assert guard in block
    assert block.index(guard) < block.index("const doc = await res.json();")
    failure = block[block.index(guard):block.index("const doc = await res.json();")]
    assert ".json()" not in failure
    assert "detail" not in failure
    assert "showError" not in block
