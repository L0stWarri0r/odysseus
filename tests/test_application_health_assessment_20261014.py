"""Updating a gallery image must not return database exception text.

PATCH /api/gallery/{image_id} used to raise HTTP 500 with str(e). SQLAlchemy
errors can include the statement and the tag or album values being written.
Unexpected failures, including a rollback failure after the original error,
now return HTTP 502 with a fixed sentence. The exception is logged and is not
returned. Success stays HTTP 200 with the image dict and no error key. A
missing image, another owner's image, and an anonymous caller stay HTTP 404.
"""
import logging
import os
import uuid

os.makedirs("data", exist_ok=True)

from fastapi import FastAPI
from fastapi.testclient import TestClient

import routes.gallery_routes as gallery_routes
from core.database import GalleryAlbum, GalleryImage

_COMMIT_SECRET = "COMMIT_SECRET unique constraint gallery_images.tags=/data/app.db"
_ROLLBACK_SECRET = "ROLLBACK_SECRET could not roll back /data/app.db"
# Saved before any test patches SessionLocal. Reads and cleanup must use this.
_REAL_SESSION = gallery_routes.SessionLocal


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


def _add(owner, tags, ai_tags="", favorite=False, album_id=None):
    db = _real_session()
    try:
        image_id = uuid.uuid4().hex
        db.add(GalleryImage(
            id=image_id,
            filename=f"health-{image_id}.png",
            prompt="",
            owner=owner,
            tags=tags,
            ai_tags=ai_tags,
            favorite=favorite,
            album_id=album_id,
            is_active=True,
        ))
        db.commit()
        return image_id
    finally:
        db.close()


def _add_album(owner, name):
    db = _real_session()
    try:
        album_id = uuid.uuid4().hex
        db.add(GalleryAlbum(id=album_id, name=name, owner=owner))
        db.commit()
        return album_id
    finally:
        db.close()


def _read(image_id):
    db = _real_session()
    try:
        img = db.query(GalleryImage).filter(GalleryImage.id == image_id).one()
        return img.tags, img.ai_tags, img.favorite, img.album_id
    finally:
        db.close()


def _delete(ids, album_ids=()):
    db = _real_session()
    try:
        if ids:
            db.query(GalleryImage).filter(GalleryImage.id.in_(ids)).delete(synchronize_session=False)
        if album_ids:
            db.query(GalleryAlbum).filter(GalleryAlbum.id.in_(album_ids)).delete(synchronize_session=False)
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


def test_patch_commit_failure_hides_exception(monkeypatch, caplog):
    owner = f"health-{uuid.uuid4().hex}"
    private = "leaked-user-tag-value"
    image_id = _add(owner, "keep-me", ai_tags="secret-ai-tag", favorite=False)
    caplog.set_level(logging.ERROR, logger="routes.gallery_routes")
    _install_commit_failure(monkeypatch)
    try:
        resp = _client(owner).patch(
            f"/api/gallery/{image_id}",
            json={"tags": f"secret-ai-tag, {private}", "favorite": True, "album_id": "foreign-album"},
        )
        assert resp.status_code == 502
        assert resp.json() == {"error": "Gallery update failed"}
        assert "detail" not in resp.json()
        assert _COMMIT_SECRET not in resp.text
        assert _ROLLBACK_SECRET not in resp.text
        assert private not in resp.text
        assert "secret-ai-tag" not in resp.text
        assert image_id not in resp.text
        assert _COMMIT_SECRET in caplog.text
        assert _ROLLBACK_SECRET in caplog.text
        assert _read(image_id) == ("keep-me", "secret-ai-tag", False, None)
    finally:
        _delete([image_id])


def test_patch_success_updates_fields_without_error_key():
    owner = f"health-{uuid.uuid4().hex}"
    album_id = _add_album(owner, "Trip")
    image_id = _add(owner, "old", ai_tags="dog", favorite=False)
    try:
        resp = _client(owner).patch(
            f"/api/gallery/{image_id}",
            json={"tags": "dog, beach, beach, ", "favorite": True, "album_id": album_id},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "error" not in body
        assert body["id"] == image_id
        assert body["tags"] == "beach"
        assert body["ai_tags"] == "dog"
        assert body["favorite"] is True
        assert body["album_id"] == album_id
        assert _read(image_id) == ("beach", "dog", True, album_id)

        cleared = _client(owner).patch(
            f"/api/gallery/{image_id}",
            json={"album_id": ""},
        )
        assert cleared.status_code == 200
        assert "error" not in cleared.json()
        assert cleared.json()["tags"] == "beach"
        assert cleared.json()["favorite"] is True
        assert cleared.json()["album_id"] is None
        assert _read(image_id) == ("beach", "dog", True, None)
    finally:
        _delete([image_id], [album_id])


def test_patch_other_owner_and_missing_stay_404():
    owner = f"health-{uuid.uuid4().hex}"
    other = f"health-{uuid.uuid4().hex}"
    image_id = _add(other, "theirs", favorite=False)
    try:
        resp = _client(owner).patch(
            f"/api/gallery/{image_id}",
            json={"tags": "stolen", "favorite": True},
        )
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Image not found"
        assert "error" not in resp.json()
        assert _read(image_id) == ("theirs", "", False, None)

        missing = _client(owner).patch(
            f"/api/gallery/{uuid.uuid4().hex}",
            json={"favorite": True},
        )
        assert missing.status_code == 404
        assert missing.json()["detail"] == "Image not found"
        assert "Gallery update failed" not in missing.text
    finally:
        _delete([image_id])


def test_patch_anonymous_stays_404():
    owner = f"health-{uuid.uuid4().hex}"
    image_id = _add(owner, "keep-me", favorite=False)
    try:
        resp = _client(None).patch(
            f"/api/gallery/{image_id}",
            json={"tags": "anon", "favorite": True},
        )
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Image not found"
        assert "error" not in resp.json()
        assert _read(image_id) == ("keep-me", "", False, None)
    finally:
        _delete([image_id])
