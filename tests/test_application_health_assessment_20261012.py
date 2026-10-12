"""Gallery tag cleanup must not return database exception text.

POST /api/gallery/clear-user-tags and POST /api/gallery/dedupe-tags used to
raise HTTP 500 with str(e). SQLAlchemy errors can include the statement and
bound tag values. Unexpected failures now return HTTP 502 with a fixed
sentence. The exception is logged and is not returned. Success stays HTTP 200
with the existing ok payload and no error key.
"""
import logging
import os
import uuid

os.makedirs("data", exist_ok=True)

from fastapi import FastAPI
from fastapi.testclient import TestClient

import routes.gallery_routes as gallery_routes
from core.database import GalleryImage

_COMMIT_SECRET = "COMMIT_SECRET unique constraint gallery_images.tags=/data/app.db"
_ROLLBACK_SECRET = "ROLLBACK_SECRET could not roll back /data/app.db"
# Saved before any test patches SessionLocal. Reads and cleanup must use this.
_REAL_SESSION = gallery_routes.SessionLocal


def _client(username: str) -> TestClient:
    app = FastAPI()

    @app.middleware("http")
    async def _stamp_user(request, call_next):
        request.state.current_user = username
        return await call_next(request)

    app.include_router(gallery_routes.setup_gallery_routes())
    return TestClient(app)


def _real_session():
    return _REAL_SESSION()


def _add(owner: str, tags: str, ai_tags: str, active: bool = True) -> str:
    db = _real_session()
    try:
        image_id = uuid.uuid4().hex
        db.add(GalleryImage(
            id=image_id,
            filename=f"health-{image_id}.png",
            owner=owner,
            tags=tags,
            ai_tags=ai_tags,
            is_active=active,
        ))
        db.commit()
        return image_id
    finally:
        db.close()


def _read(image_id: str):
    db = _real_session()
    try:
        img = db.query(GalleryImage).filter(GalleryImage.id == image_id).one()
        return img.tags, img.ai_tags, img.is_active
    finally:
        db.close()


def _delete(ids):
    db = _real_session()
    try:
        db.query(GalleryImage).filter(GalleryImage.id.in_(ids)).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


def _install_commit_failure(monkeypatch):
    """SessionLocal whose commit and rollback raise, after a real rollback."""
    real_factory = _REAL_SESSION

    def _factory():
        real = real_factory()

        class _Wrap:
            def commit(self):
                raise RuntimeError(_COMMIT_SECRET)

            def rollback(self):
                real.rollback()
                raise RuntimeError(_ROLLBACK_SECRET)

            def close(self):
                real.close()

            def __getattr__(self, name):
                return getattr(real, name)

        return _Wrap()

    monkeypatch.setattr(gallery_routes, "SessionLocal", _factory)


def _assert_hidden(resp, message: str):
    assert resp.status_code == 502
    assert resp.json() == {"error": message}
    assert _COMMIT_SECRET not in resp.text
    assert _ROLLBACK_SECRET not in resp.text
    assert "detail" not in resp.json()


def test_clear_user_tags_commit_failure_hides_exception(monkeypatch, caplog):
    owner = f"health-{uuid.uuid4().hex}"
    other = f"health-{uuid.uuid4().hex}"
    private = "user-private-tag-value"
    ids = [
        _add(owner, private, "dog"),
        _add(other, "bob-only", "cat"),
    ]
    caplog.set_level(logging.ERROR, logger="routes.gallery_routes")
    _install_commit_failure(monkeypatch)
    try:
        resp = _client(owner).post("/api/gallery/clear-user-tags")
        _assert_hidden(resp, "Clear user tags failed")
        assert private not in resp.text
        assert _COMMIT_SECRET in caplog.text
        assert _ROLLBACK_SECRET in caplog.text
        assert _read(ids[0])[0] == private
        assert _read(ids[0])[1] == "dog"
        assert _read(ids[1])[0] == "bob-only"
    finally:
        _delete(ids)


def test_dedupe_tags_commit_failure_hides_exception(monkeypatch, caplog):
    owner = f"health-{uuid.uuid4().hex}"
    private = "Beach, secret-place"
    ids = [_add(owner, private, "secret-place")]
    caplog.set_level(logging.ERROR, logger="routes.gallery_routes")
    _install_commit_failure(monkeypatch)
    try:
        resp = _client(owner).post("/api/gallery/dedupe-tags")
        _assert_hidden(resp, "Dedupe tags failed")
        assert "secret-place" not in resp.text
        assert _COMMIT_SECRET in caplog.text
        assert _ROLLBACK_SECRET in caplog.text
        assert _read(ids[0])[0] == private
        assert _read(ids[0])[1] == "secret-place"
    finally:
        _delete(ids)


def test_clear_user_tags_success_is_owner_scoped():
    owner = f"health-{uuid.uuid4().hex}"
    other = f"health-{uuid.uuid4().hex}"
    ids = [
        _add(owner, "vacation, secret-tag", "dog"),
        _add(owner, "", "cat"),
        _add(owner, "inactive-tag", "bird", active=False),
        _add(other, "bob-tag", "sun"),
    ]
    try:
        resp = _client(owner).post("/api/gallery/clear-user-tags")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "cleared": 1}
        assert "error" not in resp.json()
        assert _read(ids[0]) == ("", "dog", True)
        assert _read(ids[1]) == ("", "cat", True)
        assert _read(ids[2])[0] == "inactive-tag"
        assert _read(ids[3]) == ("bob-tag", "sun", True)
    finally:
        _delete(ids)


def test_dedupe_tags_success_drops_ai_overlap_for_owner_only():
    owner = f"health-{uuid.uuid4().hex}"
    other = f"health-{uuid.uuid4().hex}"
    ids = [
        _add(owner, "Beach, sunset, beach, ocean", "sunset, ocean"),
        _add(owner, "cat", "dog"),
        _add(owner, "sunset, ocean", "sunset", active=False),
        _add(other, "sunset, keep", "sunset"),
    ]
    try:
        resp = _client(owner).post("/api/gallery/dedupe-tags")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "rows_touched": 1, "tags_removed": 3}
        assert "error" not in resp.json()
        assert _read(ids[0])[0] == "Beach"
        assert _read(ids[0])[1] == "sunset, ocean"
        assert _read(ids[1])[0] == "cat"
        assert _read(ids[2])[0] == "sunset, ocean"
        assert _read(ids[3])[0] == "sunset, keep"
    finally:
        _delete(ids)
