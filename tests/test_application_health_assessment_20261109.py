"""GET /api/upload/{file_id}/vision must not return exception text.

Unexpected analysis failures are HTTP 502 {"error": "Vision analysis failed"}.
Invalid ids, missing files, path escapes, auth denials, and non-images keep
their existing status codes. A cache read failure falls through to analysis.
A cache write failure still returns the text. PUT is unchanged.
"""
import asyncio
import builtins
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

_REPO = Path(__file__).resolve().parent.parent
_ANALYSIS_SECRET = "VISION_ANALYSIS_SECRET"
_READ_SECRET = "VISION_CACHE_READ_SECRET"
_WRITE_SECRET = "VISION_CACHE_WRITE_SECRET"
_FIXED_ERROR = "Vision analysis failed"


class _AuthManager:
    is_configured = True

    def __init__(self, admins=()):
        self._admins = set(admins)

    def is_admin(self, user):
        return user in self._admins


class _Request:
    def __init__(self, user=None, auth_manager=None):
        self.state = SimpleNamespace(current_user=user)
        self.app = SimpleNamespace(state=SimpleNamespace(auth_manager=auth_manager))
        self.client = SimpleNamespace(host="127.0.0.1")


def _endpoints(upload_handler, monkeypatch):
    import fastapi.dependencies.utils as dependency_utils
    from routes.upload_routes import router, setup_upload_routes

    monkeypatch.setattr(dependency_utils, "ensure_multipart_is_installed", lambda: None)
    before = len(router.routes)
    setup_upload_routes(upload_handler)
    routes = router.routes[before:]
    return {route.endpoint.__name__: route.endpoint for route in routes}


def _store(tmp_path, monkeypatch, *, file_id, body=b"\x89PNG\r\n\x1a\nfake", owner=None, escape=False):
    from src.upload_handler import UploadHandler
    from src import constants

    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir(parents=True)
    base = tmp_path / "outside-base" if escape else tmp_path
    base.mkdir(parents=True, exist_ok=True)
    path = upload_dir / file_id
    path.write_bytes(body)
    index = {}
    if owner is not None:
        index["row"] = {
            "id": file_id,
            "path": str(path),
            "mime": "image/png",
            "name": file_id,
            "owner": owner,
        }
    (upload_dir / "uploads.json").write_text(json.dumps(index), encoding="utf-8")
    monkeypatch.setattr(constants, "UPLOAD_DIR", str(upload_dir))
    handler = UploadHandler(str(base), str(upload_dir))
    return handler, upload_dir


def _png_id(prefix: str) -> str:
    return (prefix * 32)[:32] + ".png"


def _get(endpoint, request, file_id, force=0):
    return asyncio.run(endpoint(request, file_id, force))


def _body(response):
    return json.loads(response.body.decode())


def _assert_fixed(response):
    assert response.status_code == 502
    body = _body(response)
    assert body == {"error": _FIXED_ERROR}
    assert "detail" not in body
    raw = response.body.decode()
    assert _ANALYSIS_SECRET not in raw
    assert _READ_SECRET not in raw
    assert _WRITE_SECRET not in raw


def _guard_open(monkeypatch, cache_path, *, fail_read=False, fail_write=False, secret="blocked"):
    original_open = builtins.open
    cache = str(cache_path)

    def guarded_open(path, mode="r", *args, **kwargs):
        same = str(path) == cache
        writing = any(flag in mode for flag in ("w", "a", "+"))
        if same and fail_write and writing:
            raise OSError(secret)
        if same and fail_read and not writing:
            raise OSError(secret)
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)


def test_invalid_file_id_stays_400(tmp_path, monkeypatch):
    handler, _upload_dir = _store(tmp_path, monkeypatch, file_id=_png_id("a"))
    get_vision_text = _endpoints(handler, monkeypatch)["get_vision_text"]

    with pytest.raises(HTTPException) as exc:
        _get(get_vision_text, _Request(), "not-an-id")

    assert exc.value.status_code == 400
    assert exc.value.detail == "Invalid file ID"


def test_missing_file_stays_404(tmp_path, monkeypatch):
    handler, _upload_dir = _store(tmp_path, monkeypatch, file_id=_png_id("a"))
    get_vision_text = _endpoints(handler, monkeypatch)["get_vision_text"]

    with pytest.raises(HTTPException) as exc:
        _get(get_vision_text, _Request(), _png_id("b"))

    assert exc.value.status_code == 404
    assert exc.value.detail == "File not found"


def test_path_escape_stays_403(tmp_path, monkeypatch):
    file_id = _png_id("c")
    handler, _upload_dir = _store(tmp_path, monkeypatch, file_id=file_id, escape=True)
    get_vision_text = _endpoints(handler, monkeypatch)["get_vision_text"]

    with pytest.raises(HTTPException) as exc:
        _get(get_vision_text, _Request(), file_id)

    assert exc.value.status_code == 403
    assert exc.value.detail == "Access denied"


def test_auth_denial_stays_403_and_404(tmp_path, monkeypatch):
    file_id = _png_id("d")
    handler, upload_dir = _store(tmp_path, monkeypatch, file_id=file_id, owner="bob")
    get_vision_text = _endpoints(handler, monkeypatch)["get_vision_text"]
    cache_path = upload_dir / ".vision" / f"{file_id}.txt"
    cache_path.parent.mkdir()
    cache_path.write_text("bob private cached text", encoding="utf-8")
    _guard_open(monkeypatch, cache_path, fail_read=True, secret=_READ_SECRET)
    auth = _AuthManager()

    with pytest.raises(HTTPException) as anon:
        _get(get_vision_text, _Request(auth_manager=auth), file_id)
    assert anon.value.status_code == 403
    assert anon.value.detail == "Access denied"

    with pytest.raises(HTTPException) as other:
        _get(get_vision_text, _Request(user="alice", auth_manager=auth), file_id)
    assert other.value.status_code == 404
    assert other.value.detail == "File not found"


def test_non_image_stays_400(tmp_path, monkeypatch):
    file_id = ("e" * 32) + ".txt"
    handler, _upload_dir = _store(tmp_path, monkeypatch, file_id=file_id, body=b"notes")
    get_vision_text = _endpoints(handler, monkeypatch)["get_vision_text"]

    with pytest.raises(HTTPException) as exc:
        _get(get_vision_text, _Request(), file_id)

    assert exc.value.status_code == 400
    assert exc.value.detail == "Not an image"


def test_cached_text_is_returned_without_analysis(tmp_path, monkeypatch):
    file_id = _png_id("f")
    handler, upload_dir = _store(tmp_path, monkeypatch, file_id=file_id)
    get_vision_text = _endpoints(handler, monkeypatch)["get_vision_text"]
    cache_path = upload_dir / ".vision" / f"{file_id}.txt"
    cache_path.parent.mkdir()
    cache_path.write_text("cached door", encoding="utf-8")

    def fail_analysis(_path):
        raise AssertionError("cache hit should not analyze")

    monkeypatch.setattr("src.document_processor.analyze_image_with_vl", fail_analysis)
    body = _get(get_vision_text, _Request(), file_id)
    assert body == {"text": "cached door", "cached": True}


def test_cache_read_failure_falls_through(tmp_path, monkeypatch, caplog):
    file_id = _png_id("1")
    handler, upload_dir = _store(tmp_path, monkeypatch, file_id=file_id)
    get_vision_text = _endpoints(handler, monkeypatch)["get_vision_text"]
    cache_path = upload_dir / ".vision" / f"{file_id}.txt"
    cache_path.parent.mkdir()
    cache_path.write_text("stale", encoding="utf-8")
    _guard_open(monkeypatch, cache_path, fail_read=True, secret=_READ_SECRET)

    def analyze(_path):
        return "fresh door"

    monkeypatch.setattr("src.document_processor.analyze_image_with_vl", analyze)
    caplog.set_level(logging.WARNING, logger="routes.upload_routes")
    body = _get(get_vision_text, _Request(), file_id)

    assert body == {"text": "fresh door", "cached": False}
    assert _READ_SECRET in caplog.text
    assert "Vision cache read failed" in caplog.text
    assert cache_path.read_text(encoding="utf-8") == "fresh door"


def test_analysis_failure_hides_exception(tmp_path, monkeypatch, caplog):
    file_id = _png_id("2")
    handler, upload_dir = _store(tmp_path, monkeypatch, file_id=file_id)
    get_vision_text = _endpoints(handler, monkeypatch)["get_vision_text"]

    def analyze(_path):
        raise RuntimeError(_ANALYSIS_SECRET)

    monkeypatch.setattr("src.document_processor.analyze_image_with_vl", analyze)
    caplog.set_level(logging.ERROR, logger="routes.upload_routes")
    response = _get(get_vision_text, _Request(), file_id)

    _assert_fixed(response)
    assert _ANALYSIS_SECRET in caplog.text
    assert "Vision analysis failed for" in caplog.text
    assert not (upload_dir / ".vision" / f"{file_id}.txt").exists()


def test_analysis_http_exception_is_reraised(tmp_path, monkeypatch, caplog):
    file_id = _png_id("3")
    handler, _upload_dir = _store(tmp_path, monkeypatch, file_id=file_id)
    get_vision_text = _endpoints(handler, monkeypatch)["get_vision_text"]

    def analyze(_path):
        raise HTTPException(409, "Vision analysis refused")

    monkeypatch.setattr("src.document_processor.analyze_image_with_vl", analyze)
    caplog.set_level(logging.ERROR, logger="routes.upload_routes")
    with pytest.raises(HTTPException) as exc:
        _get(get_vision_text, _Request(), file_id)

    assert exc.value.status_code == 409
    assert exc.value.detail == "Vision analysis refused"
    assert _FIXED_ERROR not in caplog.text
    assert _ANALYSIS_SECRET not in caplog.text


def test_cache_write_failure_still_returns_text(tmp_path, monkeypatch, caplog):
    file_id = _png_id("4")
    handler, upload_dir = _store(tmp_path, monkeypatch, file_id=file_id)
    get_vision_text = _endpoints(handler, monkeypatch)["get_vision_text"]
    cache_path = upload_dir / ".vision" / f"{file_id}.txt"
    _guard_open(monkeypatch, cache_path, fail_write=True, secret=_WRITE_SECRET)

    def analyze(_path):
        return "described door"

    monkeypatch.setattr("src.document_processor.analyze_image_with_vl", analyze)
    caplog.set_level(logging.WARNING, logger="routes.upload_routes")
    body = _get(get_vision_text, _Request(), file_id)

    assert body == {"text": "described door", "cached": False}
    assert _WRITE_SECRET not in json.dumps(body)
    assert _WRITE_SECRET in caplog.text
    assert "Vision cache write failed" in caplog.text
    assert not cache_path.exists()


def test_vision_route_keeps_the_fixed_sentence_and_leaves_put():
    source = (_REPO / "routes/upload_routes.py").read_text(encoding="utf-8")
    assert "def vision_error(message: str, status_code: int = 502, **extra):" in source
    get_start = source.index("    async def get_vision_text(")
    put_start = source.index("    async def put_vision_text(")
    cleanup_start = source.index("    async def periodic_rate_limit_cleanup(")
    getter = source[get_start:put_start]
    put = source[put_start:cleanup_start]
    analysis_start = getter.index("from src.document_processor import analyze_image_with_vl")
    write_start = getter.index('with open(cache_path, "w", encoding="utf-8")', analysis_start)
    analysis = getter[analysis_start:write_start]

    assert "except HTTPException:" in analysis
    assert 'vision_error("Vision analysis failed")' in analysis
    assert 'logger.exception("Vision analysis failed for %s", file_id)' in analysis
    assert "{e}" not in analysis
    assert "str(e)" not in analysis
    assert "HTTPException(500" not in analysis
    assert 'HTTPException(400, "Invalid file ID")' in getter
    assert 'HTTPException(404, "File not found")' in getter
    assert 'HTTPException(403, "Access denied")' in getter
    assert 'HTTPException(400, "Not an image")' in getter
    assert 'logger.warning(f"Vision cache read failed for {file_id}: {e}")' in getter
    assert 'logger.warning(f"Vision cache write failed for {file_id}: {e}")' in getter

    assert "vision_error" not in put
    assert 'HTTPException(400, "Invalid file ID")' in put
    assert 'HTTPException(404, "File not found")' in put
    assert 'HTTPException(403, "Access denied")' in put
    assert 'HTTPException(400, "text must be a string")' in put
    assert "{e}" not in put

    client = (_REPO / "static/js/chatRenderer.js").read_text(encoding="utf-8")
    editor_start = client.index("function _openVisionEditor(")
    editor_end = client.index("const TOOL_CALL_RE", editor_start)
    editor = client[editor_start:editor_end]
    load_start = editor.index("fetch(`/api/upload/${att.id}/vision`, { credentials: 'same-origin' })")
    load = editor[load_start:]
    catch_start = load.index(".catch(() => {")
    catch = load[catch_start:]
    assert "r.ok ? r.json() : Promise.reject(r)" in load
    assert "Could not load OCR text — type your correction and save." in catch
    assert ".text(" not in catch
    assert ".json(" not in catch
    assert "e.message" not in editor
    assert "Failed to save OCR text" in editor

    skills = (_REPO / "routes/skills_routes.py").read_text(encoding="utf-8")
    skill_start = skills.index("    async def get_builtin_skill(")
    skill_end = skills.index("    async def set_builtin_override(")
    skill = skills[skill_start:skill_end]
    assert "raise HTTPException(500, str(e))" in skill
    assert "vision_error" not in skill

    docs = (_REPO / "routes/document_routes.py").read_text(encoding="utf-8")
    fill_start = docs.index("    async def ai_fill_annotations(")
    render_start = docs.index("    async def render_pdf(", fill_start)
    fill = docs[fill_start:render_start]
    assert "import fitz" in fill
    assert "No vision model available: {e}" in fill
    assert "document_error" not in fill
    assert "vision_error" not in fill
