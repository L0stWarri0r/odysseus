"""Clearing AI tags must not return database exception text.

POST /api/gallery/clear-ai-tags used to raise HTTP 500 with str(e). SQLAlchemy
errors can include the statement and bound tag values. Unexpected failures,
including a rollback failure after the original error, now return HTTP 502
with a fixed sentence. The exception is logged and is not returned. Success
stays HTTP 200 with the existing ok payload and no error key. Owner scope,
inactive rows, and user tags are unchanged.
"""
import logging
import os
import uuid

os.makedirs("data", exist_ok=True)

from fastapi import FastAPI
from fastapi.testclient import TestClient

import routes.gallery_routes as gallery_routes
from core.database import GalleryImage

_COMMIT_SECRET = "COMMIT_SECRET unique constraint gallery_images.ai_tags=/data/app.db"
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


def _assert_hidden(resp):
    assert resp.status_code == 502
    assert resp.json() == {"error": "Clear AI tags failed"}
    assert _COMMIT_SECRET not in resp.text
    assert _ROLLBACK_SECRET not in resp.text
    assert "detail" not in resp.json()


def test_clear_ai_tags_commit_failure_hides_exception(monkeypatch, caplog):
    owner = f"health-{uuid.uuid4().hex}"
    other = f"health-{uuid.uuid4().hex}"
    private = "ai-private-tag-value"
    ids = [
        _add(owner, "vacation", private),
        _add(other, "bob-only", "cat"),
    ]
    caplog.set_level(logging.ERROR, logger="routes.gallery_routes")
    _install_commit_failure(monkeypatch)
    try:
        resp = _client(owner).post("/api/gallery/clear-ai-tags")
        _assert_hidden(resp)
        assert private not in resp.text
        assert _COMMIT_SECRET in caplog.text
        assert _ROLLBACK_SECRET in caplog.text
        assert _read(ids[0]) == ("vacation", private, True)
        assert _read(ids[1]) == ("bob-only", "cat", True)
    finally:
        _delete(ids)


def test_clear_one_ai_tags_commit_failure_hides_exception(monkeypatch, caplog):
    owner = f"health-{uuid.uuid4().hex}"
    private = "single-photo-secret-tag"
    image_id = _add(owner, "kept-user-tag", private)
    caplog.set_level(logging.ERROR, logger="routes.gallery_routes")
    _install_commit_failure(monkeypatch)
    try:
        resp = _client(owner).post(
            "/api/gallery/clear-ai-tags",
            params={"image_id": image_id},
        )
        _assert_hidden(resp)
        assert private not in resp.text
        assert image_id not in resp.text
        assert _COMMIT_SECRET in caplog.text
        assert _ROLLBACK_SECRET in caplog.text
        assert _read(image_id) == ("kept-user-tag", private, True)
    finally:
        _delete([image_id])


def test_clear_ai_tags_success_is_owner_scoped():
    owner = f"health-{uuid.uuid4().hex}"
    other = f"health-{uuid.uuid4().hex}"
    ids = [
        _add(owner, "vacation", "dog, woman"),
        _add(owner, "empty-ai", ""),
        _add(owner, "inactive-user", "bird", active=False),
        _add(other, "bob-tag", "sun"),
    ]
    try:
        resp = _client(owner).post("/api/gallery/clear-ai-tags")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "cleared": 1}
        assert "error" not in resp.json()
        assert _read(ids[0]) == ("vacation", "", True)
        assert _read(ids[1]) == ("empty-ai", "", True)
        assert _read(ids[2]) == ("inactive-user", "bird", False)
        assert _read(ids[3]) == ("bob-tag", "sun", True)
    finally:
        _delete(ids)


def test_clear_one_ai_tags_leaves_siblings_and_other_owners():
    owner = f"health-{uuid.uuid4().hex}"
    other = f"health-{uuid.uuid4().hex}"
    target = _add(owner, "keep-mine", "dog, woman")
    sibling = _add(owner, "keep-sibling", "cat")
    inactive = _add(owner, "inactive-user", "bird", active=False)
    foreign = _add(other, "bob-tag", "sun")
    ids = [target, sibling, inactive, foreign]
    try:
        resp = _client(owner).post(
            "/api/gallery/clear-ai-tags",
            params={"image_id": target},
        )
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "cleared": 1}
        assert "error" not in resp.json()
        assert _read(target) == ("keep-mine", "", True)
        assert _read(sibling) == ("keep-sibling", "cat", True)
        assert _read(inactive) == ("inactive-user", "bird", False)

        missed = _client(owner).post(
            "/api/gallery/clear-ai-tags",
            params={"image_id": foreign},
        )
        assert missed.status_code == 200
        assert missed.json() == {"ok": True, "cleared": 0}
        assert _read(foreign) == ("bob-tag", "sun", True)

        inactive_resp = _client(owner).post(
            "/api/gallery/clear-ai-tags",
            params={"image_id": inactive},
        )
        assert inactive_resp.status_code == 200
        assert inactive_resp.json() == {"ok": True, "cleared": 0}
        assert _read(inactive) == ("inactive-user", "bird", False)
    finally:
        _delete(ids)
