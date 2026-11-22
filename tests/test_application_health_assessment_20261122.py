"""GET /api/research/report/{session_id} must not return exception text.

An unexpected failure is HTTP 502 {"error": "Failed to generate report"}.
The exception is logged and is not returned. An HTTPException raised by
get_report_html is still passed through. Anonymous callers stay HTTP 401.
An invalid session id stays HTTP 400 and does not read the research file.
A missing file, unreadable JSON, and an owner mismatch stay HTTP 404
"Research not found" and do not call get_report_html. None stays HTTP 404
"No visual report available for this session" after the try. Success stays
HTML. Detail and archive still leak. Clients and middleware were not changed.
"""
import json
import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import routes.research_routes as research_routes
from routes.research_routes import setup_research_routes

_REPORT_SECRET = "REPORT_SECRET report generation token=do-not-return"
_FIXED_ERROR = "Failed to generate report"
_LOG = "Visual report generation failed"
_REFUSED = "Report refused"
_SESSION = "report-session"
_HTML = "<p>Visual report ready</p>"
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
    return _client(username, handler).get(f"/api/research/report/{session_id}")


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
    return _write(tmp_path, session_id, {"owner": owner, "result": "owned report"})


def _assert_no_secret(resp):
    assert _REPORT_SECRET not in resp.text
    assert _LOG not in resp.text
    assert _REFUSED not in resp.text or resp.status_code == 409


def _assert_fixed(resp):
    assert resp.status_code == 502
    assert resp.json() == {"error": _FIXED_ERROR}
    assert "detail" not in resp.json()
    assert "Report generation failed" not in resp.text
    assert "Research not found" not in resp.text
    _assert_no_secret(resp)


def test_anonymous_stays_401_and_does_not_generate(caplog, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AUTH_ENABLED", "true")
    caplog.set_level(logging.INFO, logger="routes.research_routes")
    _owned(tmp_path)
    handler = _handler()
    resp = _get(None, handler)

    assert resp.status_code == 401
    assert resp.json()["detail"] == "Not authenticated"
    assert "error" not in resp.json()
    handler.get_report_html.assert_not_called()
    assert _LOG not in caplog.text
    assert "Visual report requested" not in caplog.text
    _assert_no_secret(resp)


def test_auth_disabled_passes_empty_owner_into_the_ownership_check(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AUTH_ENABLED", "false")
    _write(tmp_path, "blank-owner", {"owner": "", "result": "blank"})
    _write(tmp_path, "alice-owner", {"owner": "alice", "result": _REPORT_SECRET})
    handler = _handler()
    handler.get_report_html.return_value = _HTML

    blank = _get(None, handler, "blank-owner")
    assert blank.status_code == 200
    assert blank.text == _HTML
    handler.get_report_html.assert_called_once_with("blank-owner")

    handler.get_report_html.reset_mock()
    denied = _get(None, handler, "alice-owner")
    assert denied.status_code == 404
    assert denied.json()["detail"] == "Research not found"
    assert "error" not in denied.json()
    handler.get_report_html.assert_not_called()
    assert _REPORT_SECRET not in denied.text


def test_invalid_session_id_stays_400_and_does_not_read_the_file(caplog, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AUTH_ENABLED", "true")
    caplog.set_level(logging.INFO, logger="routes.research_routes")

    def boom(*_args, **_kwargs):
        raise AssertionError("research file path built")

    monkeypatch.setattr(research_routes, "Path", boom)
    handler = _handler()
    resp = _get("alice", handler, "bad_id")

    assert resp.status_code == 400
    assert resp.json()["detail"] == "Invalid session ID format"
    assert "error" not in resp.json()
    handler.get_report_html.assert_not_called()
    assert _LOG not in caplog.text
    assert "Visual report requested" not in caplog.text


def test_missing_file_stays_404_and_does_not_generate(caplog, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AUTH_ENABLED", "true")
    caplog.set_level(logging.INFO, logger="routes.research_routes")
    handler = _handler()
    resp = _get("alice", handler)

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Research not found"
    assert "error" not in resp.json()
    handler.get_report_html.assert_not_called()
    assert _LOG not in caplog.text
    assert "Visual report requested" not in caplog.text
    assert "No report data found" not in caplog.text
    _assert_no_secret(resp)


def test_corrupt_json_stays_404_and_does_not_generate(caplog, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AUTH_ENABLED", "true")
    caplog.set_level(logging.INFO, logger="routes.research_routes")
    _write(tmp_path, _SESSION, "{" + _REPORT_SECRET)
    handler = _handler()
    resp = _get("alice", handler)

    assert resp.status_code == 404
    assert resp.status_code != 502
    assert resp.json()["detail"] == "Research not found"
    assert "error" not in resp.json()
    assert _FIXED_ERROR not in resp.text
    handler.get_report_html.assert_not_called()
    assert _LOG not in caplog.text
    assert _REPORT_SECRET not in caplog.text
    _assert_no_secret(resp)


def test_owner_mismatch_stays_404_and_does_not_generate(caplog, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AUTH_ENABLED", "true")
    caplog.set_level(logging.INFO, logger="routes.research_routes")
    _write(tmp_path, _SESSION, {"owner": "bob", "result": _REPORT_SECRET})
    handler = _handler()
    resp = _get("alice", handler)

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Research not found"
    assert "error" not in resp.json()
    handler.get_report_html.assert_not_called()
    assert _LOG not in caplog.text
    assert "Visual report requested" not in caplog.text
    _assert_no_secret(resp)


def test_runtime_error_hides_exception_text(caplog, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AUTH_ENABLED", "true")
    caplog.set_level(logging.ERROR, logger="routes.research_routes")
    _owned(tmp_path)
    handler = _handler()
    handler.get_report_html.side_effect = RuntimeError(_REPORT_SECRET)
    resp = _get("alice", handler)

    _assert_fixed(resp)
    handler.get_report_html.assert_called_once_with(_SESSION)
    assert _REPORT_SECRET in caplog.text
    assert f"{_LOG} for {_SESSION}" in caplog.text


def test_http_exception_from_report_is_reraised(caplog, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AUTH_ENABLED", "true")
    caplog.set_level(logging.ERROR, logger="routes.research_routes")
    _owned(tmp_path)
    handler = _handler()
    handler.get_report_html.side_effect = HTTPException(status_code=409, detail=_REFUSED)
    resp = _get("alice", handler)

    assert resp.status_code == 409
    assert resp.json()["detail"] == _REFUSED
    assert "error" not in resp.json()
    assert _FIXED_ERROR not in resp.text
    assert _LOG not in caplog.text
    assert _FIXED_ERROR not in caplog.text
    handler.get_report_html.assert_called_once_with(_SESSION)
    assert _REPORT_SECRET not in resp.text


def test_none_stays_404_after_the_try(caplog, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AUTH_ENABLED", "true")
    caplog.set_level(logging.INFO, logger="routes.research_routes")
    _owned(tmp_path)
    handler = _handler()
    handler.get_report_html.return_value = None
    resp = _get("alice", handler)

    assert resp.status_code == 404
    assert resp.status_code != 502
    assert resp.json()["detail"] == "No visual report available for this session"
    assert "error" not in resp.json()
    handler.get_report_html.assert_called_once_with(_SESSION)
    assert f"No report data found for session {_SESSION}" in caplog.text
    assert _LOG not in caplog.text
    assert f"Visual report requested for session {_SESSION}" in caplog.text
    _assert_no_secret(resp)


def test_success_returns_html(caplog, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AUTH_ENABLED", "true")
    caplog.set_level(logging.INFO, logger="routes.research_routes")
    _owned(tmp_path)
    handler = _handler()
    handler.get_report_html.return_value = _HTML
    resp = _get("alice", handler)

    assert resp.status_code == 200
    assert resp.text == _HTML
    assert "text/html" in resp.headers["content-type"]
    assert "application/json" not in resp.headers["content-type"]
    assert _FIXED_ERROR not in resp.text
    handler.get_report_html.assert_called_once_with(_SESSION)
    assert f"Visual report requested for session {_SESSION}" in caplog.text
    assert _LOG not in caplog.text
    _assert_no_secret(resp)


def test_research_report_keeps_the_fixed_sentence_and_leaves_neighbors():
    research = (_REPO / "routes/research_routes.py").read_text(encoding="utf-8")
    assert "def research_error(message: str, status_code: int = 502, **extra):" in research
    assert research.count("def research_error(") == 1
    assert "from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse" in research
    assert 'HTTPException(500, f"Report generation failed: {e}")' not in research
    assert 'logger.error(f"Visual report generation error: {e}", exc_info=True)' not in research

    report_start = research.index("    async def research_report(")
    hide_start = research.index("    class HideImageRequest")
    detail_start = research.index("    async def research_detail(")
    archive_start = research.index("    async def research_archive(")
    delete_start = research.index("    async def research_delete(")
    report = research[report_start:hide_start]
    detail = research[detail_start:archive_start]
    archive = research[archive_start:delete_start]

    assert 'research_error("Failed to generate report")' in report
    assert 'logger.exception("Visual report generation failed for %s", session_id)' in report
    assert "_require_user(request)" in report
    assert "_validate_session_id(session_id)" in report
    assert "_assert_owns_research(session_id, user)" in report
    assert "research_handler.get_report_html(session_id)" in report
    assert 'HTTPException(404, "No visual report available for this session")' in report
    assert "return HTMLResponse(content=html_content)" in report
    assert 'logger.warning(f"No report data found for session {session_id}")' in report
    assert 'logger.info(f"Visual report requested for session {session_id}")' in report
    assert "SessionLocal" not in report
    assert "db.rollback()" not in report
    assert "request.json()" not in report
    assert "str(e)" not in report
    assert "{e}" not in report
    assert "HTTPException(500" not in report
    assert "except KeyError:" not in report
    assert report.count("except HTTPException:") == 1
    assert report.count("except Exception:") == 1
    assert report.count("except Exception as e:") == 0
    require_at = report.index("_require_user(request)")
    validate_at = report.index("_validate_session_id(session_id)")
    owns_at = report.index("_assert_owns_research(session_id, user)")
    info_at = report.index('logger.info(f"Visual report requested for session {session_id}")')
    try_at = report.index("try:")
    html_at = report.index("research_handler.get_report_html(session_id)")
    refused_at = report.index("except HTTPException:")
    generic_at = report.index("except Exception:")
    log_at = report.index('logger.exception("Visual report generation failed for %s", session_id)')
    err_at = report.index('research_error("Failed to generate report")')
    none_at = report.index("if html_content is None:")
    missing_at = report.index('HTTPException(404, "No visual report available for this session")')
    ok_at = report.index("return HTMLResponse(content=html_content)")
    assert require_at < validate_at < owns_at < info_at < try_at < html_at
    assert html_at < refused_at < generic_at < log_at < err_at < none_at < missing_at < ok_at
    assert research.count('research_error("Failed to generate report")') == 1
    assert "research_error" not in detail
    assert "research_error" not in archive
    assert 'raise HTTPException(500, f"Failed to read research: {e}")' in detail
    assert "except HTTPException:" not in detail
    assert 'raise HTTPException(500, f"Failed to update research: {e}")' in archive
    assert archive.count("except HTTPException:") == 1

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

    renderer = (_REPO / "static/js/chatRenderer.js").read_text(encoding="utf-8")
    button_at = renderer.index("function _appendReportButton(")
    button_end = renderer.index("window.toggleSources = function(id)", button_at)
    button = renderer[button_at:button_end]
    open_at = button.index("var reportUrl = apiBase + '/api/research/report/' + sessionId;")
    open_end = button.index("wrap.appendChild(btn);", open_at)
    opened = button[open_at:open_end]
    assert "window.open(reportUrl, '_blank');" in opened
    assert "res.json()" not in opened
    assert "research_error" not in button
    assert "Failed to generate report" not in renderer

    library = (_REPO / "static/js/documentLibrary.js").read_text(encoding="utf-8")
    assert library.count("'/api/research/report/'") == 3
    assert "research_error" not in library
    assert "Failed to generate report" not in library

    tasks = (_REPO / "static/js/tasks.js").read_text(encoding="utf-8")
    assert "window.open(`${API_BASE}/api/research/report/${encodeURIComponent(entry.researchId)}`, '_blank');" in tasks
    assert "research_error" not in tasks

    sessions = (_REPO / "static/js/sessions.js").read_text(encoding="utf-8")
    assert sessions.count("window.open(`${API_BASE}/api/research/report/${item.id}`, '_blank')") == 2
    assert "research_error" not in sessions

    panel = (_REPO / "static/js/research/panel.js").read_text(encoding="utf-8")
    assert panel.count("window.open(`${_apiBase}/api/research/report/${job.id}`, '_blank');") == 2
    assert "research_error" not in panel
