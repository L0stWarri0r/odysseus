"""POST /api/research/{session_id}/archive must not return exception text.

An unexpected failure is HTTP 502 {"error": "Failed to update research"}.
The exception is logged and is not returned. An HTTPException raised by
json.loads is still passed through. Anonymous callers stay HTTP 401.
An invalid session id stays HTTP 400 and does not build a research path.
A missing file stays HTTP 404 before the read. Corrupt JSON is the 502,
and the file bytes stay corrupt. A cross-owner file and a JSON with no
owner stay HTTP 404, and the file is not written. The owner check stays
inside the try. Success stays {"ok": true, "id": session_id, "archived": bool}.
Report and detail still leak. Clients and middleware were not changed.
"""
import json
import logging
from json import loads as _json_loads
from pathlib import Path
from unittest.mock import MagicMock

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import routes.research_routes as research_routes
from routes.research_routes import setup_research_routes

_ARCHIVE_SECRET = "ARCHIVE_SECRET research archive token=do-not-return"
_FIXED_ERROR = "Failed to update research"
_LOG = "Research archive update failed"
_REFUSED = "Update refused"
_SESSION = "archive-session"
_REPO = Path(__file__).resolve().parent.parent


def _handler():
    handler = MagicMock()
    handler._active_tasks = {}
    return handler


def _client(username, handler):
    app = FastAPI()

    @app.middleware("http")
    async def _stamp_user(request, call_next):
        if username is not None:
            request.state.current_user = username
        return await call_next(request)

    app.include_router(setup_research_routes(handler))
    return TestClient(app)


def _post(username, handler, session_id=_SESSION, archived=None):
    params = None if archived is None else {"archived": archived}
    return _client(username, handler).post(
        f"/api/research/{session_id}/archive",
        params=params,
    )


def _write(tmp_path, session_id, payload):
    data_dir = tmp_path / "data" / "deep_research"
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / f"{session_id}.json"
    if isinstance(payload, str):
        path.write_text(payload, encoding="utf-8")
    else:
        path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _owned(tmp_path, session_id=_SESSION, owner="alice", archived=False):
    return _write(
        tmp_path,
        session_id,
        {
            "owner": owner,
            "query": "harbor lights",
            "result": "A short report",
            "archived": archived,
            "sources": [{"title": "One", "url": "https://example.test/one"}],
        },
    )


def _body(resp):
    return _json_loads(resp.content)


def _assert_no_secret(resp):
    assert _ARCHIVE_SECRET not in resp.text
    assert _LOG not in resp.text
    assert _REFUSED not in resp.text or resp.status_code == 409


def _assert_fixed(resp):
    assert resp.status_code == 502
    assert _body(resp) == {"error": _FIXED_ERROR}
    assert "detail" not in _body(resp)
    assert "Failed to update research:" not in resp.text
    assert "Research not found" not in resp.text
    _assert_no_secret(resp)


def test_anonymous_stays_401_and_does_not_read_or_write(caplog, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AUTH_ENABLED", "true")
    caplog.set_level(logging.INFO, logger="routes.research_routes")
    path = _owned(tmp_path)
    before = path.read_bytes()

    def boom(*_args, **_kwargs):
        raise AssertionError("research file path built")

    monkeypatch.setattr(research_routes, "Path", boom)
    handler = _handler()
    resp = _post(None, handler)

    assert resp.status_code == 401
    assert _body(resp)["detail"] == "Not authenticated"
    assert "error" not in _body(resp)
    handler.get_report_html.assert_not_called()
    assert path.read_bytes() == before
    assert _LOG not in caplog.text
    _assert_no_secret(resp)


def test_auth_disabled_passes_empty_owner_into_the_owner_comparison(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AUTH_ENABLED", "false")
    blank = _write(tmp_path, "blank-owner", {"owner": "", "query": "blank", "archived": False})
    alice = _write(tmp_path, "alice-owner", {"owner": "alice", "result": _ARCHIVE_SECRET, "archived": False})
    alice_before = alice.read_bytes()
    handler = _handler()

    updated = _post(None, handler, "blank-owner")
    assert updated.status_code == 200
    assert _body(updated) == {"ok": True, "id": "blank-owner", "archived": True}
    assert "error" not in _body(updated)
    assert _json_loads(blank.read_text(encoding="utf-8"))["archived"] is True
    handler.get_report_html.assert_not_called()

    denied = _post(None, handler, "alice-owner")
    assert denied.status_code == 404
    assert denied.status_code != 502
    assert _body(denied)["detail"] == "Research not found"
    assert "error" not in _body(denied)
    assert _FIXED_ERROR not in denied.text
    assert alice.read_bytes() == alice_before
    handler.get_report_html.assert_not_called()
    assert _ARCHIVE_SECRET not in denied.text


def test_invalid_session_id_stays_400_and_does_not_build_a_path(caplog, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AUTH_ENABLED", "true")
    caplog.set_level(logging.INFO, logger="routes.research_routes")

    def boom(*_args, **_kwargs):
        raise AssertionError("research file path built")

    monkeypatch.setattr(research_routes, "Path", boom)
    handler = _handler()
    resp = _post("alice", handler, "bad_id")

    assert resp.status_code == 400
    assert _body(resp)["detail"] == "Invalid session ID format"
    assert "error" not in _body(resp)
    handler.get_report_html.assert_not_called()
    assert _LOG not in caplog.text


def test_missing_file_stays_404_before_the_read(caplog, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AUTH_ENABLED", "true")
    caplog.set_level(logging.INFO, logger="routes.research_routes")

    def boom(*_args, **_kwargs):
        raise AssertionError("json.loads called")

    monkeypatch.setattr(research_routes.json, "loads", boom)
    handler = _handler()
    resp = _post("alice", handler)

    assert resp.status_code == 404
    assert resp.status_code != 502
    assert _body(resp)["detail"] == "Research not found"
    assert "error" not in _body(resp)
    handler.get_report_html.assert_not_called()
    assert _LOG not in caplog.text
    _assert_no_secret(resp)


def test_corrupt_json_is_502_and_file_bytes_stay_corrupt(caplog, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AUTH_ENABLED", "true")
    caplog.set_level(logging.ERROR, logger="routes.research_routes")
    payload = "{" + _ARCHIVE_SECRET
    path = _write(tmp_path, _SESSION, payload)
    before = path.read_bytes()
    handler = _handler()
    resp = _post("alice", handler)

    _assert_fixed(resp)
    assert path.read_bytes() == before
    handler.get_report_html.assert_not_called()
    assert f"{_LOG} for {_SESSION}" in caplog.text
    assert _ARCHIVE_SECRET not in caplog.text


def test_cross_owner_stays_404_and_file_is_not_written(caplog, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AUTH_ENABLED", "true")
    caplog.set_level(logging.ERROR, logger="routes.research_routes")
    path = _write(tmp_path, _SESSION, {"owner": "bob", "result": _ARCHIVE_SECRET, "archived": False})
    before = path.read_bytes()
    handler = _handler()
    resp = _post("alice", handler)

    assert resp.status_code == 404
    assert resp.status_code != 502
    assert _body(resp)["detail"] == "Research not found"
    assert "error" not in _body(resp)
    assert _FIXED_ERROR not in resp.text
    assert path.read_bytes() == before
    handler.get_report_html.assert_not_called()
    assert _LOG not in caplog.text
    _assert_no_secret(resp)


def test_null_owner_stays_404_and_file_is_not_written(caplog, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AUTH_ENABLED", "true")
    caplog.set_level(logging.ERROR, logger="routes.research_routes")
    path = _write(tmp_path, _SESSION, {"result": _ARCHIVE_SECRET, "archived": False})
    before = path.read_bytes()
    handler = _handler()
    resp = _post("alice", handler)

    assert resp.status_code == 404
    assert resp.status_code != 502
    assert _body(resp)["detail"] == "Research not found"
    assert "error" not in _body(resp)
    assert _FIXED_ERROR not in resp.text
    assert path.read_bytes() == before
    handler.get_report_html.assert_not_called()
    assert _LOG not in caplog.text
    _assert_no_secret(resp)


def test_runtime_error_from_loads_hides_exception_text_and_does_not_write(caplog, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AUTH_ENABLED", "true")
    caplog.set_level(logging.ERROR, logger="routes.research_routes")
    path = _owned(tmp_path)
    before = path.read_bytes()

    def boom(*_args, **_kwargs):
        raise RuntimeError(_ARCHIVE_SECRET)

    monkeypatch.setattr(research_routes.json, "loads", boom)
    handler = _handler()
    resp = _post("alice", handler)

    _assert_fixed(resp)
    assert path.read_bytes() == before
    handler.get_report_html.assert_not_called()
    assert _ARCHIVE_SECRET in caplog.text
    assert f"{_LOG} for {_SESSION}" in caplog.text


def test_runtime_error_from_dumps_hides_exception_text_and_does_not_write(caplog, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AUTH_ENABLED", "true")
    caplog.set_level(logging.ERROR, logger="routes.research_routes")
    path = _owned(tmp_path)
    before = path.read_bytes()
    real_dumps = research_routes.json.dumps

    def boom(obj, *args, **kwargs):
        # JSONResponse serializes the 502 with the same json.dumps. Only the
        # research document should fail; the error payload still encodes.
        if isinstance(obj, dict) and obj.get("error") == _FIXED_ERROR:
            return real_dumps(obj, *args, **kwargs)
        raise RuntimeError(_ARCHIVE_SECRET)

    monkeypatch.setattr(research_routes.json, "dumps", boom)
    handler = _handler()
    resp = _post("alice", handler)

    _assert_fixed(resp)
    assert path.read_bytes() == before
    handler.get_report_html.assert_not_called()
    assert _ARCHIVE_SECRET in caplog.text
    assert f"{_LOG} for {_SESSION}" in caplog.text


def test_http_exception_from_loads_is_reraised_and_file_is_unchanged(caplog, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AUTH_ENABLED", "true")
    caplog.set_level(logging.ERROR, logger="routes.research_routes")
    path = _owned(tmp_path)
    before = path.read_bytes()

    def boom(*_args, **_kwargs):
        raise HTTPException(status_code=409, detail=_REFUSED)

    monkeypatch.setattr(research_routes.json, "loads", boom)
    handler = _handler()
    resp = _post("alice", handler)

    assert resp.status_code == 409
    assert _body(resp)["detail"] == _REFUSED
    assert "error" not in _body(resp)
    assert _FIXED_ERROR not in resp.text
    assert path.read_bytes() == before
    assert _LOG not in caplog.text
    assert _FIXED_ERROR not in caplog.text
    handler.get_report_html.assert_not_called()
    assert _ARCHIVE_SECRET not in resp.text


def test_success_default_query_archives(caplog, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AUTH_ENABLED", "true")
    caplog.set_level(logging.INFO, logger="routes.research_routes")
    path = _owned(tmp_path, archived=False)
    handler = _handler()
    resp = _post("alice", handler)

    assert resp.status_code == 200
    assert _body(resp) == {"ok": True, "id": _SESSION, "archived": True}
    assert "application/json" in resp.headers["content-type"]
    assert "error" not in _body(resp)
    stored = _json_loads(path.read_text(encoding="utf-8"))
    assert stored["archived"] is True
    assert stored["query"] == "harbor lights"
    handler.get_report_html.assert_not_called()
    assert _LOG not in caplog.text
    _assert_no_secret(resp)


def test_archived_false_restores(caplog, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AUTH_ENABLED", "true")
    caplog.set_level(logging.INFO, logger="routes.research_routes")
    path = _owned(tmp_path, archived=True)
    handler = _handler()
    resp = _post("alice", handler, archived="false")

    assert resp.status_code == 200
    assert _body(resp) == {"ok": True, "id": _SESSION, "archived": False}
    assert "error" not in _body(resp)
    stored = _json_loads(path.read_text(encoding="utf-8"))
    assert stored["archived"] is False
    handler.get_report_html.assert_not_called()
    assert _LOG not in caplog.text
    _assert_no_secret(resp)


def test_research_archive_keeps_the_fixed_sentence_and_leaves_neighbors():
    research = (_REPO / "routes/research_routes.py").read_text(encoding="utf-8")
    assert "def research_error(message: str, status_code: int = 502, **extra):" in research
    assert research.count("def research_error(") == 1
    assert "from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse" in research

    report_start = research.index("    async def research_report(")
    hide_start = research.index("    class HideImageRequest")
    detail_start = research.index("    async def research_detail(")
    archive_start = research.index("    async def research_archive(")
    delete_start = research.index("    async def research_delete(")
    report = research[report_start:hide_start]
    detail = research[detail_start:archive_start]
    archive = research[archive_start:delete_start]

    assert 'raise HTTPException(500, f"Report generation failed: {e}")' in report
    assert 'logger.error(f"Visual report generation error: {e}", exc_info=True)' in report
    assert "research_error" not in report
    assert "except HTTPException:" not in report
    assert report.count("except Exception as e:") == 1

    assert 'raise HTTPException(500, f"Failed to read research: {e}")' in detail
    assert "except Exception as e:" in detail
    assert "research_error" not in detail
    assert "except HTTPException:" not in detail

    assert 'research_error("Failed to update research")' in archive
    assert 'logger.exception("Research archive update failed for %s", session_id)' in archive
    assert "_require_user(request)" in archive
    assert "_validate_session_id(session_id)" in archive
    assert 'path = Path("data/deep_research") / f"{session_id}.json"' in archive
    assert "if not path.exists():" in archive
    assert 'data = json.loads(path.read_text(encoding="utf-8"))' in archive
    assert 'if data.get("owner") != user:' in archive
    assert 'raise HTTPException(404, "Research not found")' in archive
    assert 'data["archived"] = bool(archived)' in archive
    assert 'path.write_text(json.dumps(data), encoding="utf-8")' in archive
    assert 'return {"ok": True, "id": session_id, "archived": bool(archived)}' in archive
    assert "_assert_owns_research" not in archive
    assert "get_report_html" not in archive
    assert "SessionLocal" not in archive
    assert "db.rollback()" not in archive
    assert "request.json()" not in archive
    assert "str(e)" not in archive
    assert "{e}" not in archive
    assert "HTTPException(500" not in archive
    assert "except KeyError:" not in archive
    assert "except Exception as e:" not in archive
    assert archive.count("except HTTPException:") == 1
    assert archive.count("except Exception:") == 1
    require_at = archive.index("_require_user(request)")
    validate_at = archive.index("_validate_session_id(session_id)")
    path_at = archive.index('path = Path("data/deep_research") / f"{session_id}.json"')
    exists_at = archive.index("if not path.exists():")
    missing_at = archive.index('raise HTTPException(404, "Research not found")')
    try_at = archive.index("try:")
    loads_at = archive.index('data = json.loads(path.read_text(encoding="utf-8"))')
    owner_at = archive.index('if data.get("owner") != user:')
    owner_404 = archive.index('raise HTTPException(404, "Research not found")', missing_at + 1)
    archived_at = archive.index('data["archived"] = bool(archived)')
    write_at = archive.index('path.write_text(json.dumps(data), encoding="utf-8")')
    refused_at = archive.index("except HTTPException:")
    generic_at = archive.index("except Exception:")
    log_at = archive.index('logger.exception("Research archive update failed for %s", session_id)')
    err_at = archive.index('research_error("Failed to update research")')
    ok_at = archive.index('return {"ok": True, "id": session_id, "archived": bool(archived)}')
    assert require_at < validate_at < path_at < exists_at < missing_at < try_at < loads_at
    assert loads_at < owner_at < owner_404 < archived_at < write_at
    assert write_at < refused_at < generic_at < log_at < err_at < ok_at
    assert research.count('research_error("Failed to update research")') == 1
    assert 'raise HTTPException(500, f"Failed to update research: {e}")' not in research
    assert 'raise HTTPException(500, f"Failed to read research: {e}")' in research
    assert 'raise HTTPException(500, f"Report generation failed: {e}")' in research

    history = (_REPO / "routes/history_routes.py").read_text(encoding="utf-8")
    assert "def research_error(" not in history
    assert "def history_error(" not in history
    compact_start = history.index("    async def compact_session(")
    compact = history[compact_start:]
    assert 'logger.error(f"Manual compact error {session_id}: {e}")' in compact
    assert "raise HTTPException(500, str(e))" in compact

    skills = (_REPO / "routes/skills_routes.py").read_text(encoding="utf-8")
    assert 'return {"builtin": [], "count": 0, "error": str(e)}' in skills
    assert "def skill_error(" not in skills

    upload = (_REPO / "routes/upload_routes.py").read_text(encoding="utf-8")
    assert 'raise HTTPException(500, f"Vision analysis failed: {e}")' in upload
    assert "def vision_error(" not in upload

    drafts = (_REPO / "routes/editor_draft_routes.py").read_text(encoding="utf-8")
    assert "raise HTTPException(500, str(e))" in drafts
    assert "def draft_error(" not in drafts

    middleware = (_REPO / "core/middleware.py").read_text(encoding="utf-8")
    assert 'is_report = path.startswith("/api/research/report/")' in middleware
    assert "research_error" not in middleware

    library = (_REPO / "static/js/documentLibrary.js").read_text(encoding="utf-8")
    assert "await fetch(`${API_BASE}/api/research/${item.id}/archive?archived=${toArchived}`, { method: 'POST', credentials: 'same-origin' });" in library
    assert "try { await fetch('/api/research/' + rid + '/archive?archived=' + toArchived, { method: 'POST', credentials: 'same-origin' }); } catch {}" in library
    assert "if (type === 'research') return fetch('/api/research/' + id + '/archive?archived=false', { method: 'POST', credentials: 'same-origin' });" in library
    assert "research_error" not in library
    assert "Failed to update research" not in library
