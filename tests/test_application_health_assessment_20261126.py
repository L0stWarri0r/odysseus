"""POST /api/gallery/{image_id}/replace must not return exception text.

A failed database commit is HTTP 502 {"error": "Failed to replace image"}.
The exception is logged and is not returned. An HTTPException raised by
commit is still passed through, after rollback. The new file bytes stay
on disk. A missing row stays HTTP 404, including for an anonymous
caller. An existing row with no user, or with a different owner, stays
HTTP 403. No upload stays HTTP 400. Research peek, archive, detail, and
report still leak. Clients were not changed.
"""
import logging
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

_REPO = Path(__file__).resolve().parent.parent
(_REPO / "data").mkdir(exist_ok=True)
os.environ.setdefault("DATABASE_URL", "sqlite:////tmp/odysseus-health-20261126.db")

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import routes.gallery_routes as gallery_routes
from routes.gallery_routes import setup_gallery_routes

_REPLACE_SECRET = "REPLACE_SECRET gallery replace token=do-not-return"
_FIXED_ERROR = "Failed to replace image"
_LOG = "Gallery replace failed"
_REFUSED = "Replace refused"
_IMAGE = "img-replace"
_FILENAME = "photo.png"
_BYTES = b"not-a-real-image"


def _row(owner="alice"):
    return SimpleNamespace(owner=owner, filename=_FILENAME, width=10, height=20)


def _db(row):
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = row
    return db


def _pin(monkeypatch, db):
    monkeypatch.setattr(gallery_routes, "SessionLocal", lambda: db)


def _client(username):
    app = FastAPI()

    @app.middleware("http")
    async def _stamp_user(request, call_next):
        if username is not None:
            request.state.current_user = username
        return await call_next(request)

    app.include_router(setup_gallery_routes())
    return TestClient(app)


def _post(username, *, files=None):
    kwargs = {}
    if files is not None:
        kwargs["files"] = files
    return _client(username).post(f"/api/gallery/{_IMAGE}/replace", **kwargs)


def _image_files():
    return {"image": ("edited.png", _BYTES, "application/octet-stream")}


def _written(tmp_path):
    return tmp_path / "data" / "generated_images" / _FILENAME


def _forbid_form(monkeypatch):
    async def boom(self):
        raise AssertionError("request.form called")

    monkeypatch.setattr("fastapi.Request.form", boom)


def _assert_no_secret(resp):
    assert _REPLACE_SECRET not in resp.text
    assert _LOG not in resp.text
    assert _REFUSED not in resp.text or resp.status_code == 409


def _assert_fixed(resp):
    assert resp.status_code == 502
    assert resp.json() == {"error": _FIXED_ERROR}
    assert "detail" not in resp.json()
    assert "Failed to replace image:" not in resp.text
    assert "DB commit failed" not in resp.text
    assert "Image not found" not in resp.text
    assert "No image provided" not in resp.text
    _assert_no_secret(resp)


def test_missing_image_is_404_for_anonymous_and_does_not_write(caplog, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    caplog.set_level(logging.ERROR, logger="routes.gallery_routes")
    _forbid_form(monkeypatch)
    db = _db(None)
    _pin(monkeypatch, db)

    resp = _post(None)

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Image not found"
    assert "error" not in resp.json()
    assert resp.status_code != 401
    db.commit.assert_not_called()
    db.rollback.assert_not_called()
    db.close.assert_called_once()
    assert not _written(tmp_path).exists()
    assert _LOG not in caplog.text
    _assert_no_secret(resp)


def test_missing_image_is_404_for_the_owner_and_does_not_write(caplog, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    caplog.set_level(logging.ERROR, logger="routes.gallery_routes")
    _forbid_form(monkeypatch)
    db = _db(None)
    _pin(monkeypatch, db)

    resp = _post("alice")

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Image not found"
    assert "error" not in resp.json()
    db.commit.assert_not_called()
    db.rollback.assert_not_called()
    db.close.assert_called_once()
    assert not _written(tmp_path).exists()
    assert _LOG not in caplog.text


def test_anonymous_existing_row_is_403_not_401(caplog, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    caplog.set_level(logging.ERROR, logger="routes.gallery_routes")
    _forbid_form(monkeypatch)
    db = _db(_row("alice"))
    _pin(monkeypatch, db)

    resp = _post(None)

    assert resp.status_code == 403
    assert resp.json()["detail"] == "Not your image"
    assert "error" not in resp.json()
    assert resp.status_code != 401
    assert resp.status_code != 404
    db.commit.assert_not_called()
    db.rollback.assert_not_called()
    db.close.assert_called_once()
    assert not _written(tmp_path).exists()
    assert _LOG not in caplog.text
    _assert_no_secret(resp)


def test_different_owner_is_403_and_does_not_write(caplog, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    caplog.set_level(logging.ERROR, logger="routes.gallery_routes")
    _forbid_form(monkeypatch)
    db = _db(_row("bob"))
    _pin(monkeypatch, db)

    resp = _post("alice")

    assert resp.status_code == 403
    assert resp.json()["detail"] == "Not your image"
    assert "error" not in resp.json()
    db.commit.assert_not_called()
    db.rollback.assert_not_called()
    db.close.assert_called_once()
    assert not _written(tmp_path).exists()
    assert _LOG not in caplog.text
    _assert_no_secret(resp)


def test_null_owner_row_is_403_and_does_not_write(caplog, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    caplog.set_level(logging.ERROR, logger="routes.gallery_routes")
    _forbid_form(monkeypatch)
    db = _db(_row(None))
    _pin(monkeypatch, db)

    resp = _post("alice")

    assert resp.status_code == 403
    assert resp.json()["detail"] == "Not your image"
    assert "error" not in resp.json()
    db.commit.assert_not_called()
    db.close.assert_called_once()
    assert not _written(tmp_path).exists()
    assert _LOG not in caplog.text


def test_no_image_is_400_and_does_not_write(caplog, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    caplog.set_level(logging.ERROR, logger="routes.gallery_routes")
    db = _db(_row())
    _pin(monkeypatch, db)

    resp = _post("alice", files={"note": ("x.txt", b"hi", "text/plain")})

    assert resp.status_code == 400
    assert resp.json()["detail"] == "No image provided"
    assert "error" not in resp.json()
    db.commit.assert_not_called()
    db.rollback.assert_not_called()
    db.close.assert_called_once()
    assert not _written(tmp_path).exists()
    assert not (tmp_path / "data" / "generated_images").exists()
    assert _LOG not in caplog.text
    _assert_no_secret(resp)


def test_success_keeps_existing_dimensions_and_writes_photo(caplog, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    caplog.set_level(logging.ERROR, logger="routes.gallery_routes")
    db = _db(_row())
    _pin(monkeypatch, db)

    resp = _post("alice", files=_image_files())

    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "width": 10, "height": 20}
    assert "error" not in resp.json()
    assert "application/json" in resp.headers["content-type"]
    db.commit.assert_called_once()
    db.rollback.assert_not_called()
    db.close.assert_called_once()
    assert _written(tmp_path).read_bytes() == _BYTES
    assert not (tmp_path / "data" / "generated_images" / "edited.png").exists()
    assert _LOG not in caplog.text
    _assert_no_secret(resp)


def test_commit_runtime_error_is_502_and_keeps_the_new_bytes(caplog, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    caplog.set_level(logging.ERROR, logger="routes.gallery_routes")
    db = _db(_row())
    db.commit.side_effect = RuntimeError(_REPLACE_SECRET)
    _pin(monkeypatch, db)

    resp = _post("alice", files=_image_files())

    _assert_fixed(resp)
    db.commit.assert_called_once()
    db.rollback.assert_called_once()
    db.close.assert_called_once()
    assert _written(tmp_path).read_bytes() == _BYTES
    assert f"{_LOG} for {_IMAGE}" in caplog.text
    assert _REPLACE_SECRET in caplog.text
    assert _FIXED_ERROR not in caplog.text


def test_commit_http_exception_is_reraised_and_keeps_the_new_bytes(caplog, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    caplog.set_level(logging.ERROR, logger="routes.gallery_routes")
    db = _db(_row())
    db.commit.side_effect = HTTPException(status_code=409, detail=_REFUSED)
    _pin(monkeypatch, db)

    resp = _post("alice", files=_image_files())

    assert resp.status_code == 409
    assert resp.json()["detail"] == _REFUSED
    assert "error" not in resp.json()
    assert _FIXED_ERROR not in resp.text
    db.commit.assert_called_once()
    db.rollback.assert_called_once()
    db.close.assert_called_once()
    assert _written(tmp_path).read_bytes() == _BYTES
    assert _LOG not in caplog.text
    assert _FIXED_ERROR not in caplog.text
    assert _REPLACE_SECRET not in resp.text


def test_gallery_replace_keeps_the_fixed_sentence_and_leaves_neighbors():
    gallery = (_REPO / "routes" / "gallery_routes.py").read_text(encoding="utf-8")
    assert "def gallery_error(message: str, status_code: int = 502, **extra):" in gallery
    assert gallery.count("def gallery_error(") == 1
    assert gallery.count('gallery_error("Failed to replace image")') == 1
    assert "from fastapi.responses import JSONResponse" in gallery
    assert "DB commit failed" not in gallery
    assert "def research_error(" not in gallery

    replace_start = gallery.index("    async def gallery_replace(")
    rename_start = gallery.index("    async def gallery_rename(")
    rotate_start = gallery.index("    async def gallery_rotate(")
    replace = gallery[replace_start:rename_start]
    rename = gallery[rename_start:rotate_start]

    assert 'logger.exception("Gallery replace failed for %s", image_id)' in replace
    assert 'gallery_error("Failed to replace image")' in replace
    assert "user = get_current_user(request)" in replace
    assert "db = SessionLocal()" in replace
    assert 'raise HTTPException(404, "Image not found")' in replace
    assert 'raise HTTPException(403, "Not your image")' in replace
    assert "form = await request.form()" in replace
    assert 'raise HTTPException(400, "No image provided")' in replace
    assert 'img_path = img_dir / _sanitize_gallery_filename(img.filename)' in replace
    assert "img_path.write_bytes(content)" in replace
    assert "from PIL import Image" in replace
    assert "except Exception:\n                pass" in replace
    assert 'return {"ok": True, "width": img.width, "height": img.height}' in replace
    assert "db.close()" in replace
    assert "_require_user" not in replace
    assert "AUTH_ENABLED" not in replace
    assert "str(e)" not in replace
    assert "{e}" not in replace
    assert "HTTPException(500" not in replace
    assert "except Exception as e:" not in replace
    assert replace.count("except Exception:") == 2
    assert replace.count("except HTTPException:") == 1
    assert replace.count("db.rollback()") == 2
    user_at = replace.index("user = get_current_user(request)")
    session_at = replace.index("db = SessionLocal()")
    missing_at = replace.index('raise HTTPException(404, "Image not found")')
    owner_at = replace.index('raise HTTPException(403, "Not your image")')
    form_at = replace.index("form = await request.form()")
    empty_at = replace.index('raise HTTPException(400, "No image provided")')
    write_at = replace.index("img_path.write_bytes(content)")
    pil_at = replace.index("except Exception:\n                pass")
    commit_at = replace.index("db.commit()")
    refused_at = replace.index("except HTTPException:")
    rollback_at = replace.index("db.rollback()")
    generic_at = replace.index("except Exception:", pil_at + 1)
    rollback2_at = replace.index("db.rollback()", rollback_at + 1)
    log_at = replace.index('logger.exception("Gallery replace failed for %s", image_id)')
    err_at = replace.index('gallery_error("Failed to replace image")')
    ok_at = replace.index('return {"ok": True, "width": img.width, "height": img.height}')
    assert user_at < session_at < missing_at < owner_at < form_at < empty_at < write_at
    assert write_at < pil_at < commit_at < refused_at < rollback_at < generic_at
    assert generic_at < rollback2_at < log_at < err_at < ok_at

    assert "db.commit()" in rename
    assert "gallery_error" not in rename
    assert "except Exception" not in rename
    assert "try:\n            db.commit()" not in rename
    assert 'return {"ok": True, "name": new_name}' in rename

    research = (_REPO / "routes" / "research_routes.py").read_text(encoding="utf-8")
    assert "def research_error(" not in research
    assert "def gallery_error(" not in research
    report_start = research.index("    async def research_report(")
    hide_start = research.index("    class HideImageRequest")
    detail_start = research.index("    async def research_detail(")
    archive_start = research.index("    async def research_archive(")
    delete_start = research.index("    async def research_delete(")
    peek_start = research.index("    async def research_result_peek(")
    spinoff_start = research.index("    async def research_spinoff(")
    report = research[report_start:hide_start]
    detail = research[detail_start:archive_start]
    archive = research[archive_start:delete_start]
    peek = research[peek_start:spinoff_start]
    assert 'raise HTTPException(500, f"Report generation failed: {e}")' in report
    assert "research_error" not in report
    assert "gallery_error" not in report
    assert "except HTTPException:" not in report
    assert 'raise HTTPException(500, f"Failed to read research: {e}")' in detail
    assert "research_error" not in detail
    assert "except HTTPException:" not in detail
    assert 'raise HTTPException(500, f"Failed to update research: {e}")' in archive
    assert archive.count("except HTTPException:") == 1
    assert archive.count("except Exception as e:") == 1
    assert "research_error" not in archive
    assert "if p.exists():" in peek
    assert 'd = json.loads(p.read_text(encoding="utf-8"))' in peek
    assert "research_error" not in peek
    assert "gallery_error" not in peek
    assert "except HTTPException:" not in peek

    history = (_REPO / "routes" / "history_routes.py").read_text(encoding="utf-8")
    assert "def history_error(" not in history
    assert "def research_error(" not in history
    compact = history[history.index("    async def compact_session("):]
    assert 'logger.error(f"Manual compact error {session_id}: {e}")' in compact
    assert "raise HTTPException(500, str(e))" in compact

    skills = (_REPO / "routes" / "skills_routes.py").read_text(encoding="utf-8")
    assert 'return {"builtin": [], "count": 0, "error": str(e)}' in skills
    upload = (_REPO / "routes" / "upload_routes.py").read_text(encoding="utf-8")
    assert 'raise HTTPException(500, f"Vision analysis failed: {e}")' in upload
    drafts = (_REPO / "routes" / "editor_draft_routes.py").read_text(encoding="utf-8")
    assert "raise HTTPException(500, str(e))" in drafts

    editor = (_REPO / "static" / "js" / "galleryEditor.js").read_text(encoding="utf-8")
    assert "`${API_BASE}/api/gallery/${state.imageId}/replace`" in editor
    assert "j.detail || j.error" in editor
    assert "Failed to replace image" not in editor
    assert "gallery_error" not in editor
