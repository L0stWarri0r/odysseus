"""POST /api/research/result-peek/{session_id} must not return exception text.

An unexpected failure while reading a persisted result is HTTP 502
{"error": "Failed to read research result"}. The exception is logged and
is not returned. An HTTPException raised by json.loads is still passed
through. Anonymous callers stay HTTP 401. An invalid session id stays
HTTP 400 and does not call get_result. A disk-only corrupt file stays
HTTP 404 because ownership swallows the parse error. The 502 requires
the task to be in memory, get_result to be None, the file to exist, and
json.loads to fail. A missing file after that ownership check stays
HTTP 404 before the read. Archive, detail, and report still leak.
Clients and middleware were not changed.
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

_PEEK_SECRET = "PEEK_SECRET research result peek token=do-not-return"
_FIXED_ERROR = "Failed to read research result"
_LOG = "Research result peek failed"
_REFUSED = "Peek refused"
_SESSION = "peek-session"
_REPO = Path(__file__).resolve().parent.parent


def _handler(tasks=None, result=None):
    handler = MagicMock()
    handler._active_tasks = {} if tasks is None else tasks
    handler.get_result.return_value = result
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


def _post(username, handler, session_id=_SESSION):
    return _client(username, handler).post(f"/api/research/result-peek/{session_id}")


def _write(tmp_path, session_id, payload):
    data_dir = tmp_path / "data" / "deep_research"
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / f"{session_id}.json"
    if isinstance(payload, str):
        path.write_text(payload, encoding="utf-8")
    else:
        path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _owned(tmp_path, session_id=_SESSION, owner="alice", category=None):
    payload = {
        "owner": owner,
        "query": "harbor lights",
        "result": "A short report",
        "sources": [{"title": "One", "url": "https://example.test/one"}],
        "raw_findings": [{"title": "Finding", "summary": "notes"}],
    }
    if category is not None:
        payload["category"] = category
    return _write(tmp_path, session_id, payload)


def _body(resp):
    return _json_loads(resp.content)


def _assert_no_secret(resp):
    assert _PEEK_SECRET not in resp.text
    assert _LOG not in resp.text
    assert _REFUSED not in resp.text or resp.status_code == 409


def _assert_fixed(resp):
    assert resp.status_code == 502
    assert _body(resp) == {"error": _FIXED_ERROR}
    assert "detail" not in _body(resp)
    assert "Failed to read research result:" not in resp.text
    assert "No research found" not in resp.text
    assert "No research result available" not in resp.text
    _assert_no_secret(resp)


def test_anonymous_stays_401_and_does_not_call_get_result(caplog, monkeypatch, tmp_path):
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
    handler.get_result.assert_not_called()
    handler.get_sources.assert_not_called()
    handler.get_report_html.assert_not_called()
    assert path.read_bytes() == before
    assert _LOG not in caplog.text
    _assert_no_secret(resp)


def test_auth_disabled_passes_empty_owner_into_the_disk_read(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AUTH_ENABLED", "false")
    blank = _write(
        tmp_path,
        "blank-owner",
        {
            "owner": "",
            "result": "blank report",
            "sources": [{"title": "Blank"}],
            "raw_findings": [],
        },
    )
    blank_before = blank.read_bytes()
    alice = _write(tmp_path, "alice-owner", {"owner": "alice", "result": _PEEK_SECRET})
    alice_before = alice.read_bytes()
    handler = _handler()

    updated = _post(None, handler, "blank-owner")
    assert updated.status_code == 200
    assert _body(updated) == {
        "result": "blank report",
        "sources": [{"title": "Blank"}],
        "raw_findings": [],
        "category": "",
    }
    assert "error" not in _body(updated)
    assert blank.read_bytes() == blank_before
    handler.get_sources.assert_not_called()
    handler.get_report_html.assert_not_called()

    denied = _post(None, handler, "alice-owner")
    assert denied.status_code == 404
    assert denied.status_code != 502
    assert _body(denied)["detail"] == "No research found for this session"
    assert "error" not in _body(denied)
    assert _FIXED_ERROR not in denied.text
    assert alice.read_bytes() == alice_before
    handler.get_sources.assert_not_called()
    handler.get_report_html.assert_not_called()
    assert _PEEK_SECRET not in denied.text


def test_invalid_session_id_stays_400_and_does_not_call_get_result(caplog, monkeypatch, tmp_path):
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
    handler.get_result.assert_not_called()
    handler.get_report_html.assert_not_called()
    assert _LOG not in caplog.text


def test_missing_file_after_ownership_stays_404_before_the_read(caplog, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AUTH_ENABLED", "true")
    caplog.set_level(logging.INFO, logger="routes.research_routes")

    def boom(*_args, **_kwargs):
        raise AssertionError("json.loads called")

    monkeypatch.setattr(research_routes.json, "loads", boom)
    handler = _handler(tasks={_SESSION: {"owner": "alice"}})
    resp = _post("alice", handler)

    assert resp.status_code == 404
    assert resp.status_code != 502
    assert _body(resp)["detail"] == "No research result available"
    assert "error" not in _body(resp)
    handler.get_result.assert_called_once_with(_SESSION)
    handler.get_sources.assert_not_called()
    handler.get_report_html.assert_not_called()
    assert _LOG not in caplog.text
    _assert_no_secret(resp)


def test_disk_only_corrupt_json_stays_404_and_is_not_the_502(caplog, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AUTH_ENABLED", "true")
    caplog.set_level(logging.ERROR, logger="routes.research_routes")
    payload = "{" + _PEEK_SECRET
    path = _write(tmp_path, _SESSION, payload)
    before = path.read_bytes()
    handler = _handler()
    resp = _post("alice", handler)

    assert resp.status_code == 404
    assert resp.status_code != 502
    assert _body(resp)["detail"] == "No research found for this session"
    assert "error" not in _body(resp)
    assert _FIXED_ERROR not in resp.text
    assert path.read_bytes() == before
    handler.get_result.assert_not_called()
    handler.get_sources.assert_not_called()
    handler.get_report_html.assert_not_called()
    assert _LOG not in caplog.text
    assert _PEEK_SECRET not in caplog.text
    _assert_no_secret(resp)


def test_in_memory_corrupt_json_is_502_and_file_bytes_stay_corrupt(caplog, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AUTH_ENABLED", "true")
    caplog.set_level(logging.ERROR, logger="routes.research_routes")
    payload = "{" + _PEEK_SECRET
    path = _write(tmp_path, _SESSION, payload)
    before = path.read_bytes()
    handler = _handler(tasks={_SESSION: {"owner": "alice"}})
    resp = _post("alice", handler)

    _assert_fixed(resp)
    assert path.read_bytes() == before
    handler.get_sources.assert_not_called()
    handler.get_report_html.assert_not_called()
    assert f"{_LOG} for {_SESSION}" in caplog.text
    assert _PEEK_SECRET not in caplog.text


def test_cross_owner_disk_stays_404_and_file_is_not_written(caplog, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AUTH_ENABLED", "true")
    caplog.set_level(logging.ERROR, logger="routes.research_routes")
    path = _write(tmp_path, _SESSION, {"owner": "bob", "result": _PEEK_SECRET})
    before = path.read_bytes()
    handler = _handler()
    resp = _post("alice", handler)

    assert resp.status_code == 404
    assert resp.status_code != 502
    assert _body(resp)["detail"] == "No research found for this session"
    assert "error" not in _body(resp)
    assert _FIXED_ERROR not in resp.text
    assert path.read_bytes() == before
    handler.get_result.assert_not_called()
    handler.get_report_html.assert_not_called()
    assert _LOG not in caplog.text
    _assert_no_secret(resp)


def test_null_owner_disk_stays_404_and_file_is_not_written(caplog, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AUTH_ENABLED", "true")
    caplog.set_level(logging.ERROR, logger="routes.research_routes")
    path = _write(tmp_path, _SESSION, {"result": _PEEK_SECRET})
    before = path.read_bytes()
    handler = _handler()
    resp = _post("alice", handler)

    assert resp.status_code == 404
    assert resp.status_code != 502
    assert _body(resp)["detail"] == "No research found for this session"
    assert "error" not in _body(resp)
    assert _FIXED_ERROR not in resp.text
    assert path.read_bytes() == before
    handler.get_result.assert_not_called()
    handler.get_report_html.assert_not_called()
    assert _LOG not in caplog.text
    _assert_no_secret(resp)


def test_in_memory_cross_owner_stays_404_before_get_result(caplog, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AUTH_ENABLED", "true")
    caplog.set_level(logging.ERROR, logger="routes.research_routes")

    def boom(*_args, **_kwargs):
        raise AssertionError("json.loads called")

    monkeypatch.setattr(research_routes.json, "loads", boom)
    handler = _handler(tasks={_SESSION: {"owner": "bob"}})
    resp = _post("alice", handler)

    assert resp.status_code == 404
    assert resp.status_code != 502
    assert _body(resp)["detail"] == "No research found for this session"
    assert "error" not in _body(resp)
    handler.get_result.assert_not_called()
    handler.get_sources.assert_not_called()
    handler.get_report_html.assert_not_called()
    assert _LOG not in caplog.text
    _assert_no_secret(resp)


def test_runtime_error_from_loads_hides_exception_text(caplog, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AUTH_ENABLED", "true")
    caplog.set_level(logging.ERROR, logger="routes.research_routes")
    path = _owned(tmp_path)
    before = path.read_bytes()

    def boom(*_args, **_kwargs):
        raise RuntimeError(_PEEK_SECRET)

    monkeypatch.setattr(research_routes.json, "loads", boom)
    handler = _handler(tasks={_SESSION: {"owner": "alice"}})
    resp = _post("alice", handler)

    _assert_fixed(resp)
    assert path.read_bytes() == before
    handler.get_sources.assert_not_called()
    handler.get_report_html.assert_not_called()
    assert _PEEK_SECRET in caplog.text
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
    handler = _handler(tasks={_SESSION: {"owner": "alice"}})
    resp = _post("alice", handler)

    assert resp.status_code == 409
    assert _body(resp)["detail"] == _REFUSED
    assert "error" not in _body(resp)
    assert _FIXED_ERROR not in resp.text
    assert path.read_bytes() == before
    assert _LOG not in caplog.text
    assert _FIXED_ERROR not in caplog.text
    handler.get_sources.assert_not_called()
    handler.get_report_html.assert_not_called()
    assert _PEEK_SECRET not in resp.text


def test_in_memory_result_skips_disk_and_does_not_call_loads(caplog, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AUTH_ENABLED", "true")
    caplog.set_level(logging.INFO, logger="routes.research_routes")
    path = _owned(tmp_path)
    before = path.read_bytes()
    calls = {"n": 0}

    def boom(*_args, **_kwargs):
        calls["n"] += 1
        raise AssertionError("json.loads called")

    monkeypatch.setattr(research_routes.json, "loads", boom)
    handler = _handler(tasks={_SESSION: {"owner": "alice"}}, result="A short report")
    handler.get_sources.return_value = [{"title": "Live"}]
    handler.get_raw_findings.return_value = [{"title": "Live finding"}]
    resp = _post("alice", handler)

    assert resp.status_code == 200
    assert _body(resp) == {
        "result": "A short report",
        "sources": [{"title": "Live"}],
        "raw_findings": [{"title": "Live finding"}],
        "category": "",
    }
    assert "application/json" in resp.headers["content-type"]
    assert "error" not in _body(resp)
    assert calls["n"] == 0
    assert path.read_bytes() == before
    handler.get_sources.assert_called_once_with(_SESSION)
    handler.get_raw_findings.assert_called_once_with(_SESSION)
    handler.get_report_html.assert_not_called()
    assert _LOG not in caplog.text
    _assert_no_secret(resp)


def test_disk_success_uses_file_and_blank_category(caplog, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AUTH_ENABLED", "true")
    caplog.set_level(logging.INFO, logger="routes.research_routes")
    path = _owned(tmp_path)
    before = path.read_bytes()
    handler = _handler()
    resp = _post("alice", handler)

    assert resp.status_code == 200
    assert _body(resp) == {
        "result": "A short report",
        "sources": [{"title": "One", "url": "https://example.test/one"}],
        "raw_findings": [{"title": "Finding", "summary": "notes"}],
        "category": "",
    }
    assert "application/json" in resp.headers["content-type"]
    assert "error" not in _body(resp)
    assert path.read_bytes() == before
    handler.get_sources.assert_not_called()
    handler.get_raw_findings.assert_not_called()
    handler.get_report_html.assert_not_called()
    assert _LOG not in caplog.text
    _assert_no_secret(resp)


def test_disk_success_returns_category(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AUTH_ENABLED", "true")
    path = _owned(tmp_path, category="field-notes")
    before = path.read_bytes()
    handler = _handler()
    resp = _post("alice", handler)

    assert resp.status_code == 200
    body = _body(resp)
    assert body["category"] == "field-notes"
    assert body["result"] == "A short report"
    assert "error" not in body
    assert path.read_bytes() == before
    handler.get_sources.assert_not_called()
    handler.get_report_html.assert_not_called()


def test_in_memory_owner_returns_disk_payload_when_disk_owner_differs(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AUTH_ENABLED", "true")
    path = _write(
        tmp_path,
        _SESSION,
        {
            "owner": "bob",
            "result": "bob on disk",
            "sources": [{"title": "Bob"}],
            "raw_findings": [],
            "category": "other",
        },
    )
    before = path.read_bytes()
    handler = _handler(tasks={_SESSION: {"owner": "alice"}})
    resp = _post("alice", handler)

    assert resp.status_code == 200
    assert _body(resp) == {
        "result": "bob on disk",
        "sources": [{"title": "Bob"}],
        "raw_findings": [],
        "category": "other",
    }
    assert path.read_bytes() == before
    handler.get_sources.assert_not_called()
    handler.get_report_html.assert_not_called()


def test_research_result_peek_keeps_the_fixed_sentence_and_leaves_neighbors():
    research = (_REPO / "routes/research_routes.py").read_text(encoding="utf-8")
    assert "def research_error(message: str, status_code: int = 502, **extra):" in research
    assert research.count("def research_error(") == 1
    assert "from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse" in research

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
    assert 'logger.error(f"Visual report generation error: {e}", exc_info=True)' in report
    assert "research_error" not in report
    assert "except HTTPException:" not in report
    assert report.count("except Exception as e:") == 1

    assert 'raise HTTPException(500, f"Failed to read research: {e}")' in detail
    assert "except Exception as e:" in detail
    assert "research_error" not in detail
    assert "except HTTPException:" not in detail

    assert 'raise HTTPException(500, f"Failed to update research: {e}")' in archive
    assert "except HTTPException:" in archive
    assert archive.count("except HTTPException:") == 1
    assert archive.count("except Exception as e:") == 1
    assert "research_error" not in archive

    assert 'research_error("Failed to read research result")' in peek
    assert 'logger.exception("Research result peek failed for %s", session_id)' in peek
    assert "_require_user(request)" in peek
    assert "_validate_session_id(session_id)" in peek
    assert "if not _owns_in_memory(session_id, user):" in peek
    assert 'raise HTTPException(404, "No research found for this session")' in peek
    assert "result = research_handler.get_result(session_id)" in peek
    assert "if result is None:" in peek
    assert 'p = Path("data/deep_research") / f"{session_id}.json"' in peek
    assert "if not p.exists():" in peek
    assert 'raise HTTPException(404, "No research result available")' in peek
    assert 'd = json.loads(p.read_text(encoding="utf-8"))' in peek
    assert 'd.get("result", "")' in peek
    assert 'd.get("sources", [])' in peek
    assert 'd.get("raw_findings", [])' in peek
    assert 'd.get("category") or ""' in peek
    assert "sources = research_handler.get_sources(session_id) or []" in peek
    assert "raw_findings = research_handler.get_raw_findings(session_id) or []" in peek
    assert 'return {"result": result, "sources": sources, "raw_findings": raw_findings, "category": ""}' in peek
    assert "_assert_owns_research" not in peek
    assert 'd.get("owner")' not in peek
    assert 'd["owner"]' not in peek
    assert "get_report_html" not in peek
    assert "write_text" not in peek
    assert "SessionLocal" not in peek
    assert "db.rollback()" not in peek
    assert "request.json()" not in peek
    assert "str(e)" not in peek
    assert "{e}" not in peek
    assert "HTTPException(500" not in peek
    assert "except KeyError:" not in peek
    assert "except AttributeError:" not in peek
    assert "except Exception as e:" not in peek
    assert peek.count("except HTTPException:") == 1
    assert peek.count("except Exception:") == 1
    require_at = peek.index("_require_user(request)")
    validate_at = peek.index("_validate_session_id(session_id)")
    owns_at = peek.index("if not _owns_in_memory(session_id, user):")
    missing_owner_at = peek.index('raise HTTPException(404, "No research found for this session")')
    result_at = peek.index("result = research_handler.get_result(session_id)")
    none_at = peek.index("if result is None:")
    path_at = peek.index('p = Path("data/deep_research") / f"{session_id}.json"')
    exists_at = peek.index("if not p.exists():")
    missing_file_at = peek.index('raise HTTPException(404, "No research result available")')
    try_at = peek.index("try:")
    loads_at = peek.index('d = json.loads(p.read_text(encoding="utf-8"))')
    refused_at = peek.index("except HTTPException:")
    generic_at = peek.index("except Exception:")
    log_at = peek.index('logger.exception("Research result peek failed for %s", session_id)')
    err_at = peek.index('research_error("Failed to read research result")')
    disk_at = peek.index('d.get("result", "")')
    sources_at = peek.index("sources = research_handler.get_sources(session_id) or []")
    findings_at = peek.index("raw_findings = research_handler.get_raw_findings(session_id) or []")
    mem_at = peek.index(
        'return {"result": result, "sources": sources, "raw_findings": raw_findings, "category": ""}'
    )
    assert require_at < validate_at < owns_at < missing_owner_at < result_at < none_at
    assert none_at < path_at < exists_at < missing_file_at < try_at < loads_at
    assert loads_at < refused_at < generic_at < log_at < err_at < disk_at
    assert disk_at < sources_at < findings_at < mem_at
    assert research.count('research_error("Failed to read research result")') == 1
    assert 'research_error("Failed to update research")' not in research
    assert 'research_error("Failed to read research")' not in research
    assert 'research_error("Failed to generate report")' not in research
    assert 'raise HTTPException(500, f"Failed to update research: {e}")' in research
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

    jobs = (_REPO / "static/js/research/jobs.js").read_text(encoding="utf-8")
    panel = (_REPO / "static/js/research/panel.js").read_text(encoding="utf-8")
    peek_fetch = "`${_apiBase}/api/research/result-peek/${job.id}`"
    assert peek_fetch in jobs
    assert peek_fetch in panel
    assert "if (!res.ok) return;" in jobs
    assert "if (!res.ok) return;" in panel
    assert "research_error" not in jobs
    assert "research_error" not in panel
    assert "Failed to read research result" not in jobs
    assert "Failed to read research result" not in panel
