"""GET /api/research/detail/{session_id} must not return exception text.

An unexpected failure is HTTP 502 {"error": "Failed to read research"}.
The exception is logged and is not returned. An HTTPException raised by
json.loads is still passed through. Anonymous callers stay HTTP 401.
An invalid session id stays HTTP 400 and does not build a research path.
A missing file stays HTTP 404 before the read. Corrupt JSON is the 502,
not a 404. A cross-owner file and a JSON with no owner stay HTTP 404
after a successful parse. Success stays the parsed dict. Report and
archive still leak. Clients and middleware were not changed.
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

_DETAIL_SECRET = "DETAIL_SECRET research detail token=do-not-return"
_FIXED_ERROR = "Failed to read research"
_LOG = "Research detail read failed"
_REFUSED = "Read refused"
_SESSION = "detail-session"
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


def _get(username, handler, session_id=_SESSION):
    return _client(username, handler).get(f"/api/research/detail/{session_id}")


def _write(tmp_path, session_id, payload):
    data_dir = tmp_path / "data" / "deep_research"
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / f"{session_id}.json"
    if isinstance(payload, str):
        path.write_text(payload, encoding="utf-8")
    else:
        path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _owned(tmp_path, session_id=_SESSION, owner="alice"):
    return _write(
        tmp_path,
        session_id,
        {
            "owner": owner,
            "query": "harbor lights",
            "result": "A short report",
            "sources": [{"title": "One", "url": "https://example.test/one"}],
            "stats": {"Rounds": "2"},
        },
    )


def _body(resp):
    return _json_loads(resp.content)


def _assert_no_secret(resp):
    assert _DETAIL_SECRET not in resp.text
    assert _LOG not in resp.text
    assert _REFUSED not in resp.text or resp.status_code == 409


def _assert_fixed(resp):
    assert resp.status_code == 502
    assert _body(resp) == {"error": _FIXED_ERROR}
    assert "detail" not in _body(resp)
    assert "Failed to read research:" not in resp.text
    assert "Research not found" not in resp.text
    _assert_no_secret(resp)


def test_anonymous_stays_401_and_does_not_read(caplog, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AUTH_ENABLED", "true")
    caplog.set_level(logging.INFO, logger="routes.research_routes")
    _owned(tmp_path)

    def boom(*_args, **_kwargs):
        raise AssertionError("research file path built")

    monkeypatch.setattr(research_routes, "Path", boom)
    handler = _handler()
    resp = _get(None, handler)

    assert resp.status_code == 401
    assert _body(resp)["detail"] == "Not authenticated"
    assert "error" not in _body(resp)
    handler.get_report_html.assert_not_called()
    assert _LOG not in caplog.text
    _assert_no_secret(resp)


def test_auth_disabled_passes_empty_owner_into_the_owner_comparison(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AUTH_ENABLED", "false")
    blank_payload = {"owner": "", "query": "blank", "result": "empty owner"}
    _write(tmp_path, "blank-owner", blank_payload)
    _write(tmp_path, "alice-owner", {"owner": "alice", "result": _DETAIL_SECRET})
    handler = _handler()

    blank = _get(None, handler, "blank-owner")
    assert blank.status_code == 200
    assert _body(blank) == blank_payload
    assert "error" not in _body(blank)
    handler.get_report_html.assert_not_called()

    denied = _get(None, handler, "alice-owner")
    assert denied.status_code == 404
    assert denied.status_code != 502
    assert _body(denied)["detail"] == "Research not found"
    assert "error" not in _body(denied)
    assert _FIXED_ERROR not in denied.text
    handler.get_report_html.assert_not_called()
    assert _DETAIL_SECRET not in denied.text


def test_invalid_session_id_stays_400_and_does_not_build_a_path(caplog, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AUTH_ENABLED", "true")
    caplog.set_level(logging.INFO, logger="routes.research_routes")

    def boom(*_args, **_kwargs):
        raise AssertionError("research file path built")

    monkeypatch.setattr(research_routes, "Path", boom)
    handler = _handler()
    resp = _get("alice", handler, "bad_id")

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
    resp = _get("alice", handler)

    assert resp.status_code == 404
    assert resp.status_code != 502
    assert _body(resp)["detail"] == "Research not found"
    assert "error" not in _body(resp)
    handler.get_report_html.assert_not_called()
    assert _LOG not in caplog.text
    _assert_no_secret(resp)


def test_corrupt_json_is_502_not_404(caplog, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AUTH_ENABLED", "true")
    caplog.set_level(logging.ERROR, logger="routes.research_routes")
    _write(tmp_path, _SESSION, "{" + _DETAIL_SECRET)
    handler = _handler()
    resp = _get("alice", handler)

    _assert_fixed(resp)
    handler.get_report_html.assert_not_called()
    assert f"{_LOG} for {_SESSION}" in caplog.text
    assert _DETAIL_SECRET not in caplog.text


def test_cross_owner_stays_404_after_a_successful_parse(caplog, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AUTH_ENABLED", "true")
    caplog.set_level(logging.ERROR, logger="routes.research_routes")
    _write(tmp_path, _SESSION, {"owner": "bob", "result": _DETAIL_SECRET})
    handler = _handler()
    resp = _get("alice", handler)

    assert resp.status_code == 404
    assert resp.status_code != 502
    assert _body(resp)["detail"] == "Research not found"
    assert "error" not in _body(resp)
    assert _FIXED_ERROR not in resp.text
    handler.get_report_html.assert_not_called()
    assert _LOG not in caplog.text
    _assert_no_secret(resp)


def test_null_owner_stays_404_after_a_successful_parse(caplog, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AUTH_ENABLED", "true")
    caplog.set_level(logging.ERROR, logger="routes.research_routes")
    _write(tmp_path, _SESSION, {"result": _DETAIL_SECRET})
    handler = _handler()
    resp = _get("alice", handler)

    assert resp.status_code == 404
    assert resp.status_code != 502
    assert _body(resp)["detail"] == "Research not found"
    assert "error" not in _body(resp)
    assert _FIXED_ERROR not in resp.text
    handler.get_report_html.assert_not_called()
    assert _LOG not in caplog.text
    _assert_no_secret(resp)


def test_runtime_error_hides_exception_text(caplog, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AUTH_ENABLED", "true")
    caplog.set_level(logging.ERROR, logger="routes.research_routes")
    _owned(tmp_path)

    def boom(*_args, **_kwargs):
        raise RuntimeError(_DETAIL_SECRET)

    monkeypatch.setattr(research_routes.json, "loads", boom)
    handler = _handler()
    resp = _get("alice", handler)

    _assert_fixed(resp)
    handler.get_report_html.assert_not_called()
    assert _DETAIL_SECRET in caplog.text
    assert f"{_LOG} for {_SESSION}" in caplog.text


def test_http_exception_from_loads_is_reraised(caplog, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AUTH_ENABLED", "true")
    caplog.set_level(logging.ERROR, logger="routes.research_routes")
    _owned(tmp_path)

    def boom(*_args, **_kwargs):
        raise HTTPException(status_code=409, detail=_REFUSED)

    monkeypatch.setattr(research_routes.json, "loads", boom)
    handler = _handler()
    resp = _get("alice", handler)

    assert resp.status_code == 409
    assert _body(resp)["detail"] == _REFUSED
    assert "error" not in _body(resp)
    assert _FIXED_ERROR not in resp.text
    assert _LOG not in caplog.text
    assert _FIXED_ERROR not in caplog.text
    handler.get_report_html.assert_not_called()
    assert _DETAIL_SECRET not in resp.text


def test_success_returns_the_parsed_dict(caplog, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AUTH_ENABLED", "true")
    caplog.set_level(logging.INFO, logger="routes.research_routes")
    path = _owned(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    handler = _handler()
    resp = _get("alice", handler)

    assert resp.status_code == 200
    assert _body(resp) == payload
    assert "application/json" in resp.headers["content-type"]
    assert "text/html" not in resp.headers["content-type"]
    assert "error" not in _body(resp)
    handler.get_report_html.assert_not_called()
    assert _LOG not in caplog.text
    _assert_no_secret(resp)


def test_research_detail_keeps_the_fixed_sentence_and_leaves_neighbors():
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

    assert 'research_error("Failed to read research")' in detail
    assert 'logger.exception("Research detail read failed for %s", session_id)' in detail
    assert "_require_user(request)" in detail
    assert "_validate_session_id(session_id)" in detail
    assert 'path = Path("data/deep_research") / f"{session_id}.json"' in detail
    assert "if not path.exists():" in detail
    assert 'data = json.loads(path.read_text(encoding="utf-8"))' in detail
    assert 'if data.get("owner") != user:' in detail
    assert 'raise HTTPException(404, "Research not found")' in detail
    assert "return data" in detail
    assert "_assert_owns_research" not in detail
    assert "get_report_html" not in detail
    assert "SessionLocal" not in detail
    assert "db.rollback()" not in detail
    assert "request.json()" not in detail
    assert "str(e)" not in detail
    assert "{e}" not in detail
    assert "HTTPException(500" not in detail
    assert "except KeyError:" not in detail
    assert "except Exception as e:" not in detail
    assert detail.count("except HTTPException:") == 1
    assert detail.count("except Exception:") == 1
    require_at = detail.index("_require_user(request)")
    validate_at = detail.index("_validate_session_id(session_id)")
    path_at = detail.index('path = Path("data/deep_research") / f"{session_id}.json"')
    exists_at = detail.index("if not path.exists():")
    missing_at = detail.index('raise HTTPException(404, "Research not found")')
    try_at = detail.index("try:")
    loads_at = detail.index('data = json.loads(path.read_text(encoding="utf-8"))')
    refused_at = detail.index("except HTTPException:")
    generic_at = detail.index("except Exception:")
    log_at = detail.index('logger.exception("Research detail read failed for %s", session_id)')
    err_at = detail.index('research_error("Failed to read research")')
    owner_at = detail.index('if data.get("owner") != user:')
    ok_at = detail.index("return data")
    assert require_at < validate_at < path_at < exists_at < missing_at < try_at < loads_at
    assert loads_at < refused_at < generic_at < log_at < err_at < owner_at < ok_at
    assert research.count('research_error("Failed to read research")') == 1
    assert 'raise HTTPException(500, f"Failed to read research: {e}")' not in research

    assert "research_error" not in archive
    assert 'raise HTTPException(500, f"Failed to update research: {e}")' in archive
    assert archive.count("except HTTPException:") == 1
    assert "except Exception as e:" in archive

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
    assert "const res = await fetch(`${API_BASE}/api/research/detail/${item.id}`, { credentials: 'same-origin' });" in library
    assert "if (res.ok) detail = await res.json();" in library
    assert "const res = await fetch('/api/research/detail/' + r.id, { credentials: 'same-origin' });" in library
    assert "if (!res.ok) return null;" in library
    assert "research_error" not in library
    assert "Failed to read research" not in library
