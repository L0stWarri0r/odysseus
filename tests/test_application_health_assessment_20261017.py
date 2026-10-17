"""Loading the document library must not return database exception text.

GET /api/documents/library used to raise HTTP 500 with str(e). A failed
query or rollback can include the statement and document text. Unexpected
failures, including a rollback failure after the original error, now return
HTTP 502 with a fixed sentence. The exception is logged and is not returned.
Success stays HTTP 200 with the existing library payload and no error key.
An HTTPException raised inside the handler is not rewritten. Invalid limit
stays HTTP 422.

The sessions document grid and the archive loader check the status before
treating the body as documents. They do not display the response body.
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

_QUERY_SECRET = "QUERY_SECRET unique constraint documents.current_content=/data/app.db"
_ROLLBACK_SECRET = "ROLLBACK_SECRET could not roll back /data/app.db"
_PRIVATE = "leaked-document-body"
# The route looks up SessionLocal on this module at call time. Another test
# replaces that name with a temp engine; reads and cleanup use the app engine.
_REAL_SESSION = SessionLocal
_REPO = Path(__file__).resolve().parent.parent


def _client(username):
    app = FastAPI()

    @app.middleware("http")
    async def _stamp_user(request, call_next):
        if username is not None:
            request.state.current_user = username
        return await call_next(request)

    app.include_router(document_routes.setup_document_routes(None))
    return TestClient(app)


def _real_session():
    return _REAL_SESSION()


def _add(owner, content, title="Health note", language="markdown", archived=False, session_name="Health chat"):
    db = _real_session()
    try:
        doc_id = uuid.uuid4().hex
        session_id = uuid.uuid4().hex
        db.add(DbSession(
            id=session_id,
            name=session_name,
            endpoint_url="http://127.0.0.1:9",
            model="health-model",
            owner=owner,
        ))
        db.add(Document(
            id=doc_id,
            session_id=session_id,
            title=title,
            language=language,
            current_content=content,
            version_count=2,
            is_active=True,
            archived=archived,
            owner=owner,
        ))
        db.commit()
        return doc_id, session_id
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


def _install_query_failure(monkeypatch):
    """SessionLocal whose query and rollback raise, after a real rollback."""
    real_factory = _REAL_SESSION

    def _factory():
        real = real_factory()

        class _Wrap:
            def query(self, *entities, **kwargs):
                raise RuntimeError(_QUERY_SECRET)

            def rollback(self):
                real.rollback()
                raise RuntimeError(_ROLLBACK_SECRET)

            def close(self):
                real.close()

            def __getattr__(self, name):
                return getattr(real, name)

        return _Wrap()

    monkeypatch.setattr(document_routes, "SessionLocal", _factory)


def test_library_query_failure_hides_exception(monkeypatch, caplog):
    monkeypatch.setattr(document_routes, "SessionLocal", _REAL_SESSION)
    owner = f"health-{uuid.uuid4().hex}"
    doc_id, session_id = _add(owner, content=_PRIVATE)
    caplog.set_level(logging.ERROR, logger="routes.document_routes")
    _install_query_failure(monkeypatch)
    try:
        resp = _client(owner).get("/api/documents/library")
        assert resp.status_code == 502
        assert resp.json() == {"error": "Failed to load document library"}
        assert "detail" not in resp.json()
        assert _QUERY_SECRET not in resp.text
        assert _ROLLBACK_SECRET not in resp.text
        assert _PRIVATE not in resp.text
        assert doc_id not in resp.text
        assert _QUERY_SECRET in caplog.text
        assert _ROLLBACK_SECRET in caplog.text
    finally:
        _delete([doc_id], [session_id])


def test_library_success_keeps_shape_and_owner_scope(monkeypatch):
    monkeypatch.setattr(document_routes, "SessionLocal", _REAL_SESSION)
    owner = f"health-{uuid.uuid4().hex}"
    other = f"health-{uuid.uuid4().hex}"
    tail = f"TAIL-{uuid.uuid4().hex}"
    own_body = ("own-body " * 80) + tail
    other_body = f"other-body-{uuid.uuid4().hex}"
    doc_id, session_id = _add(
        owner, content=own_body, title="Owner essay", language="markdown", session_name="Owner chat",
    )
    other_id, other_session = _add(other, content=other_body, title="Other essay", language="python")
    try:
        resp = _client(owner).get("/api/documents/library", params={"search": "Owner essay"})
        assert resp.status_code == 200
        body = resp.json()
        assert set(body) >= {"documents", "total", "languages", "session_count"}
        assert "error" not in body
        assert body["total"] == 1
        assert body["session_count"] == 1
        assert body["languages"].get("markdown") == 1
        assert [item["id"] for item in body["documents"]] == [doc_id]
        preview = body["documents"][0]["preview"]
        assert preview == own_body[:500]
        assert len(preview) == 500
        assert tail not in preview
        assert tail not in resp.text
        assert body["documents"][0]["session_name"] == "Owner chat"
        assert body["documents"][0]["title"] == "Owner essay"
        assert body["documents"][0]["version_count"] == 2
        assert other_body not in resp.text
        assert other_id not in resp.text

        hidden = _client(other).get("/api/documents/library", params={"search": "Owner essay"})
        assert hidden.status_code == 200
        assert hidden.json()["documents"] == []
        assert hidden.json()["total"] == 0
        assert "error" not in hidden.json()
        assert own_body[:40] not in hidden.text

        anonymous = _client(None).get("/api/documents/library", params={"search": "Owner essay"})
        assert anonymous.status_code == 200
        assert anonymous.json()["documents"] == []
        assert anonymous.json()["total"] == 0
        assert "error" not in anonymous.json()
        assert own_body[:40] not in anonymous.text
        assert other_body not in anonymous.text
    finally:
        _delete([doc_id, other_id], [session_id, other_session])


def test_library_archived_flag_splits_views(monkeypatch):
    monkeypatch.setattr(document_routes, "SessionLocal", _REAL_SESSION)
    owner = f"health-{uuid.uuid4().hex}"
    active_id, active_session = _add(owner, content="visible-note", title="Visible note", archived=False)
    archived_id, archived_session = _add(owner, content="archived-note", title="Archived note", archived=True)
    try:
        active = _client(owner).get("/api/documents/library", params={"search": "note"})
        assert active.status_code == 200
        assert [item["id"] for item in active.json()["documents"]] == [active_id]
        assert "error" not in active.json()
        assert archived_id not in active.text

        archived = _client(owner).get(
            "/api/documents/library", params={"search": "note", "archived": "true"},
        )
        assert archived.status_code == 200
        assert [item["id"] for item in archived.json()["documents"]] == [archived_id]
        assert "error" not in archived.json()
        assert active_id not in archived.text
    finally:
        _delete([active_id, archived_id], [active_session, archived_session])


def test_library_http_exception_is_not_rewritten(monkeypatch):
    monkeypatch.setattr(document_routes, "SessionLocal", _REAL_SESSION)
    owner = f"health-{uuid.uuid4().hex}"
    doc_id, session_id = _add(owner, content=_PRIVATE)

    def _raise(*_args, **_kwargs):
        raise HTTPException(404, "Document not found")

    monkeypatch.setattr(document_routes, "_aggregate_language_facets", _raise)
    try:
        resp = _client(owner).get("/api/documents/library", params={"search": "Health note"})
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Document not found"
        assert "error" not in resp.json()
        assert "Failed to load document library" not in resp.text
        assert _PRIVATE not in resp.text
    finally:
        _delete([doc_id], [session_id])


def test_library_invalid_limit_stays_422():
    resp = _client(f"health-{uuid.uuid4().hex}").get("/api/documents/library", params={"limit": 0})
    assert resp.status_code == 422
    assert "Failed to load document library" not in resp.text
    assert "detail" in resp.json()


def test_library_callers_check_status_before_reading_documents():
    sessions = (_REPO / "static" / "js" / "sessions.js").read_text(encoding="utf-8")
    start = sessions.index("async function _renderLibDocuments")
    end = sessions.index("async function _renderLibResearch")
    fetch_body = sessions[start:end]
    ok_at = fetch_body.index("if (!res.ok)")
    json_at = fetch_body.index("res.json()")
    assert ok_at < json_at
    assert ok_at < fetch_body.index("No documents found")
    assert "Failed to load documents" in fetch_body
    assert "data.error" not in fetch_body
    assert "data.detail" not in fetch_body
    assert "res.text(" not in fetch_body
    assert "e.message" not in fetch_body
    assert "Failed to fetch document library" not in fetch_body

    library = (_REPO / "static" / "js" / "documentLibrary.js").read_text(encoding="utf-8")
    archive_at = library.index("/api/documents/library?archived=true&limit=50")
    archive_body = library[archive_at:archive_at + 500]
    assert archive_body.index("if (!r.ok)") < archive_body.index("r.json()")
    assert "_loadFailed" in archive_body
    assert "r.text(" not in archive_body
    assert "Could not load archived documents." in library
    assert "d.detail" not in library[archive_at:archive_at + 1200]
    assert "d.error" not in library[archive_at:archive_at + 1200]

    fetch_start = library.index("async function libraryFetch")
    fetch_end = library.index("function libraryRenderStats")
    main_fetch = library[fetch_start:fetch_end]
    assert main_fetch.index("if (!res.ok)") < main_fetch.index("res.json()")
    assert "data.error" not in main_fetch
    assert "res.text(" not in main_fetch

    tour = (_REPO / "static" / "js" / "slashCommands.js").read_text(encoding="utf-8")
    tour_at = tour.index("/api/documents/library?limit=1&sort=recent")
    tour_body = tour[tour_at:tour_at + 400]
    assert tour_body.index("if (r.ok)") < tour_body.index("r.json()")
    assert "data.error" not in tour_body


def test_library_route_no_longer_returns_exception_text():
    source = (_REPO / "routes" / "document_routes.py").read_text(encoding="utf-8")
    assert 'f"Failed to fetch document library: {e}"' not in source
    start = source.index("async def documents_library")
    end = source.index("async def list_documents")
    handler = source[start:end]
    assert 'document_error("Failed to load document library")' in handler
    assert "except HTTPException:" in handler
    assert 'logger.exception("Document library failed")' in handler
    assert 'logger.exception("Document library rollback failed")' in handler
