"""Loading the gallery library must not return database exception text.

GET /api/gallery/library used to raise HTTP 500 with str(e). A failed query
or rollback can include the statement and a photo prompt. Unexpected
failures, including a rollback failure after the original error, now return
HTTP 502 with a fixed sentence. The exception is logged and is not returned.
Success stays HTTP 200 with the existing library payload and no error key.
An HTTPException raised inside the handler is not rewritten. Invalid limit
stays HTTP 422.

The gallery grid and the editor's gallery picker check the status before
treating the body as a library. They do not display the response body.
"""
import logging
import os
import uuid
from pathlib import Path

os.makedirs("data", exist_ok=True)

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import routes.gallery_routes as gallery_routes
from core.database import GalleryImage

_QUERY_SECRET = "QUERY_SECRET unique constraint gallery_images.prompt=/data/app.db"
_ROLLBACK_SECRET = "ROLLBACK_SECRET could not roll back /data/app.db"
_PRIVATE = "leaked-library-prompt"
# Saved before any test patches SessionLocal. Reads and cleanup must use this.
_REAL_SESSION = gallery_routes.SessionLocal
_REPO = Path(__file__).resolve().parent.parent


def _client(username):
    app = FastAPI()

    @app.middleware("http")
    async def _stamp_user(request, call_next):
        if username is not None:
            request.state.current_user = username
        return await call_next(request)

    app.include_router(gallery_routes.setup_gallery_routes())
    return TestClient(app)


def _real_session():
    return _REAL_SESSION()


def _add(owner, prompt, model="health-model", tags="health-tag"):
    db = _real_session()
    try:
        image_id = uuid.uuid4().hex
        filename = f"health-{image_id}.png"
        db.add(GalleryImage(
            id=image_id,
            filename=filename,
            prompt=prompt,
            model=model,
            owner=owner,
            tags=tags,
            ai_tags="",
            favorite=False,
            is_active=True,
        ))
        db.commit()
        return image_id, filename
    finally:
        db.close()


def _delete(ids):
    db = _real_session()
    try:
        if ids:
            db.query(GalleryImage).filter(GalleryImage.id.in_(ids)).delete(synchronize_session=False)
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

    monkeypatch.setattr(gallery_routes, "SessionLocal", _factory)


def test_library_query_failure_hides_exception(monkeypatch, caplog):
    owner = f"health-{uuid.uuid4().hex}"
    image_id, _filename = _add(owner, prompt=_PRIVATE)
    caplog.set_level(logging.ERROR, logger="routes.gallery_routes")
    _install_query_failure(monkeypatch)
    try:
        resp = _client(owner).get("/api/gallery/library")
        assert resp.status_code == 502
        assert resp.json() == {"error": "Failed to load gallery library"}
        assert "detail" not in resp.json()
        assert _QUERY_SECRET not in resp.text
        assert _ROLLBACK_SECRET not in resp.text
        assert _PRIVATE not in resp.text
        assert image_id not in resp.text
        assert _QUERY_SECRET in caplog.text
        assert _ROLLBACK_SECRET in caplog.text
    finally:
        _delete([image_id])


def test_library_success_keeps_shape_and_owner_scope():
    owner = f"health-{uuid.uuid4().hex}"
    other = f"health-{uuid.uuid4().hex}"
    own_prompt = f"own-{uuid.uuid4().hex}"
    other_prompt = f"other-{uuid.uuid4().hex}"
    image_id, filename = _add(owner, prompt=own_prompt, model="owner-model", tags="owner-tag")
    other_id, _other_filename = _add(other, prompt=other_prompt, model="other-model", tags="other-tag")
    try:
        resp = _client(owner).get("/api/gallery/library", params={"search": own_prompt})
        assert resp.status_code == 200
        body = resp.json()
        assert set(body) >= {"items", "total", "total_tagged", "tags", "models"}
        assert "error" not in body
        assert body["total"] == 1
        assert [item["id"] for item in body["items"]] == [image_id]
        assert body["items"][0]["filename"] == filename
        assert body["items"][0]["prompt"] == own_prompt
        assert "owner-tag" in body["tags"]
        assert "owner-model" in body["models"]
        assert other_prompt not in resp.text
        assert other_id not in resp.text

        hidden = _client(other).get("/api/gallery/library", params={"search": own_prompt})
        assert hidden.status_code == 200
        assert hidden.json()["items"] == []
        assert hidden.json()["total"] == 0
        assert "error" not in hidden.json()
        assert own_prompt not in hidden.text
    finally:
        _delete([image_id, other_id])


def test_library_http_exception_is_not_rewritten(monkeypatch):
    owner = f"health-{uuid.uuid4().hex}"
    image_id, _filename = _add(owner, prompt=_PRIVATE)

    def _raise(*_args, **_kwargs):
        raise HTTPException(404, "Image not found")

    monkeypatch.setattr(gallery_routes, "_image_to_dict", _raise)
    try:
        resp = _client(owner).get("/api/gallery/library", params={"search": _PRIVATE})
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Image not found"
        assert "error" not in resp.json()
        assert "Failed to load gallery library" not in resp.text
        assert _PRIVATE not in resp.text
    finally:
        _delete([image_id])


def test_library_invalid_limit_stays_422():
    resp = _client(f"health-{uuid.uuid4().hex}").get("/api/gallery/library", params={"limit": 0})
    assert resp.status_code == 422
    assert "Failed to load gallery library" not in resp.text
    assert "detail" in resp.json()


def test_library_callers_check_status_before_reading_items():
    gallery = (_REPO / "static" / "js" / "gallery.js").read_text(encoding="utf-8")
    start = gallery.index("async function _fetchLibrary")
    end = gallery.index("async function _fetchAlbums")
    fetch_body = gallery[start:end]
    ok_at = fetch_body.index("if (!res.ok)")
    json_at = fetch_body.index("res.json()")
    assert ok_at < json_at
    assert "Could not load photos." in fetch_body
    assert "data.error" not in fetch_body
    assert "res.text(" not in fetch_body
    assert "Failed to fetch gallery library" not in fetch_body

    picker = (_REPO / "static" / "js" / "editor" / "wire-import.js").read_text(encoding="utf-8")
    picker_start = picker.index("/api/gallery/library?limit=50")
    picker_body = picker[picker_start:picker_start + 700]
    assert picker_body.index("if (!res.ok)") < picker_body.index("res.json()")
    assert "Could not load gallery" in picker_body
    assert "No images in gallery" in picker[picker_start:]
    # The empty-library toast stays behind the status check.
    assert picker_body.index("if (!res.ok)") < picker_body.index("No images in gallery")
    assert "data.error" not in picker_body
    assert "res.text(" not in picker_body


def test_library_route_no_longer_returns_exception_text():
    source = (_REPO / "routes" / "gallery_routes.py").read_text(encoding="utf-8")
    assert 'f"Failed to fetch gallery library: {e}"' not in source
    start = source.index("async def gallery_library")
    end = source.index("async def list_albums")
    handler = source[start:end]
    assert 'gallery_error("Failed to load gallery library")' in handler
    assert "except HTTPException:" in handler
    assert "logger.exception(\"Gallery library failed\")" in handler
