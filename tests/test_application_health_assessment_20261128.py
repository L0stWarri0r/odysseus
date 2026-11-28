"""POST /api/sessions/auto-sort must not return LLM exception text.

A failed model call is HTTP 502 {"error": "Failed to sort sessions"}.
The exception is logged and is not returned. An HTTPException from the
model call is still passed through. Invalid JSON, a missing endpoint,
skip_llm, and a single unfiled session stay as they were. Memory import,
gallery replace, and the research routes still leak. Clients were not
changed.
"""
import logging
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

_REPO = Path(__file__).resolve().parent.parent
(_REPO / "data").mkdir(exist_ok=True)
os.environ["DATABASE_URL"] = "sqlite:////tmp/odysseus-health-20261128.db"
os.environ["AUTH_ENABLED"] = "true"

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import routes.session_routes as session_routes
from routes.session_routes import setup_session_routes

_SORT_SECRET = "SORT_SECRET session auto-sort token=do-not-return"
_FIXED_ERROR = "Failed to sort sessions"
_LOG = "Auto-sort LLM call failed"
_REFUSED = "Sort refused"
_INVALID_JSON = (
    "AI returned invalid JSON for auto-sort — the model may not follow JSON "
    "instructions; try a different utility model in Settings."
)
_NO_ENDPOINT = "No available model endpoint for auto-sort"
_ENDPOINT = ("http://llm.test/v1", "test-model", {})
_NONE = (None, None, None)

_session_manager = MagicMock()
setup_session_routes(_session_manager, {})


def _chat(sid, name, updated_at):
    return SimpleNamespace(id=sid, name=name, archived=False, updated_at=updated_at)


_PLAN = _chat("aaaaaaaa-1111", "Project plan", "2026-01-02")
_GROCERY = _chat("bbbbbbbb-2222", "Grocery list", "2026-01-01")
_TWO = {_PLAN.id: _PLAN, _GROCERY.id: _GROCERY}
_ONE = {_GROCERY.id: _GROCERY}


def _client(username):
    app = FastAPI()

    @app.middleware("http")
    async def _stamp_user(request, call_next):
        if username is not None:
            request.state.current_user = username
        return await call_next(request)

    app.include_router(session_routes.router)
    return TestClient(app)


def _stub_db(monkeypatch):
    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = []
    db.query.return_value.group_by.return_value.all.return_value = []
    db.query.return_value.filter.return_value.group_by.return_value.all.return_value = []
    factory = MagicMock(return_value=db)
    monkeypatch.setattr(session_routes, "SessionLocal", factory)
    return factory, db


def _post(
    monkeypatch,
    *,
    sessions=None,
    skip_llm=False,
    llm=None,
    endpoint=_ENDPOINT,
    pick_endpoint=_ENDPOINT,
):
    if sessions is None:
        sessions = _TWO
    _session_manager.get_sessions_for_user.reset_mock()
    _session_manager.delete_session.reset_mock()
    _session_manager.get_sessions_for_user.side_effect = None
    _session_manager.get_sessions_for_user.return_value = sessions
    factory, db = _stub_db(monkeypatch)
    if llm is None:
        llm = MagicMock()
    monkeypatch.setattr("src.llm_core.llm_call", llm)
    resolve = MagicMock(return_value=endpoint)
    monkeypatch.setattr("src.task_endpoint.resolve_task_endpoint", resolve)
    pick = MagicMock(return_value=pick_endpoint)
    monkeypatch.setattr(session_routes, "_pick_endpoint_for_sort", pick)
    url = "/api/sessions/auto-sort"
    if skip_llm:
        url += "?skip_llm=true"
    resp = _client("alice").post(url)
    return resp, llm, resolve, pick, factory, db


def _assert_no_secret(resp):
    assert _SORT_SECRET not in resp.text
    assert _LOG not in resp.text
    assert "Auto-sort failed:" not in resp.text
    assert _REFUSED not in resp.text or resp.status_code == 409


def _assert_fixed(resp):
    assert resp.status_code == 502
    assert resp.json() == {"error": _FIXED_ERROR}
    assert "detail" not in resp.json()
    assert "application/json" in resp.headers["content-type"]
    _assert_no_secret(resp)


def test_llm_runtime_error_is_502_and_hides_the_secret(caplog, monkeypatch):
    caplog.set_level(logging.ERROR, logger="routes.session_routes")
    llm = MagicMock(side_effect=RuntimeError(_SORT_SECRET))

    resp, llm, resolve, pick, factory, db = _post(monkeypatch, llm=llm)

    _assert_fixed(resp)
    llm.assert_called_once()
    resolve.assert_called_once()
    assert resolve.call_args.kwargs["owner"] == "alice"
    pick.assert_not_called()
    factory.assert_called_once()
    db.close.assert_called_once()
    db.commit.assert_not_called()
    _session_manager.get_sessions_for_user.assert_called_once_with("alice")
    _session_manager.delete_session.assert_not_called()
    assert _LOG in caplog.text
    assert _SORT_SECRET in caplog.text
    assert _FIXED_ERROR not in caplog.text
    assert "Auto-sort failed:" not in caplog.text


def test_llm_http_exception_is_reraised(caplog, monkeypatch):
    caplog.set_level(logging.ERROR, logger="routes.session_routes")
    llm = MagicMock(side_effect=HTTPException(status_code=409, detail=_REFUSED))

    resp, llm, resolve, pick, factory, db = _post(monkeypatch, llm=llm)

    assert resp.status_code == 409
    assert resp.json()["detail"] == _REFUSED
    assert "error" not in resp.json()
    assert _FIXED_ERROR not in resp.text
    llm.assert_called_once()
    resolve.assert_called_once()
    pick.assert_not_called()
    factory.assert_called_once()
    db.close.assert_called_once()
    db.commit.assert_not_called()
    assert _LOG not in caplog.text
    assert _FIXED_ERROR not in caplog.text
    assert _SORT_SECRET not in caplog.text
    assert _SORT_SECRET not in resp.text


def test_invalid_json_stays_the_existing_detail(caplog, monkeypatch):
    caplog.set_level(logging.ERROR, logger="routes.session_routes")
    llm = MagicMock(return_value="not json")

    resp, llm, _resolve, pick, factory, db = _post(monkeypatch, llm=llm)

    assert resp.status_code == 502
    assert resp.json()["detail"] == _INVALID_JSON
    assert "error" not in resp.json()
    assert _FIXED_ERROR not in resp.text
    assert "Auto-sort failed:" not in resp.text
    llm.assert_called_once()
    pick.assert_not_called()
    factory.assert_called_once()
    db.close.assert_called_once()
    db.commit.assert_not_called()
    assert "Auto-sort: could not parse JSON" in caplog.text
    assert _LOG not in caplog.text
    assert _FIXED_ERROR not in caplog.text
    assert _SORT_SECRET not in resp.text


def test_missing_endpoint_is_503_and_does_not_call_the_model(caplog, monkeypatch):
    caplog.set_level(logging.ERROR, logger="routes.session_routes")

    resp, llm, resolve, pick, factory, db = _post(
        monkeypatch, endpoint=_NONE, pick_endpoint=_NONE
    )

    assert resp.status_code == 503
    assert resp.json()["detail"] == _NO_ENDPOINT
    assert "error" not in resp.json()
    llm.assert_not_called()
    resolve.assert_called_once()
    assert resolve.call_args.kwargs["owner"] == "alice"
    pick.assert_called_once()
    assert pick.call_args.kwargs["owner"] == "alice"
    factory.assert_called_once()
    db.close.assert_called_once()
    db.commit.assert_not_called()
    assert _LOG not in caplog.text
    assert _FIXED_ERROR not in resp.text
    _assert_no_secret(resp)


def test_skip_llm_short_circuits_before_the_model(caplog, monkeypatch):
    caplog.set_level(logging.ERROR, logger="routes.session_routes")

    resp, llm, resolve, pick, factory, db = _post(monkeypatch, skip_llm=True)

    assert resp.status_code == 200
    body = resp.json()
    assert body["skipped_llm"] is True
    assert body["updated"] == 0
    assert body["status"] == "ok"
    assert "error" not in body
    llm.assert_not_called()
    resolve.assert_not_called()
    pick.assert_not_called()
    factory.assert_called_once()
    db.close.assert_called_once()
    db.commit.assert_not_called()
    assert _LOG not in caplog.text
    assert _FIXED_ERROR not in resp.text
    _assert_no_secret(resp)


def test_one_unfiled_session_stays_skipped(caplog, monkeypatch):
    caplog.set_level(logging.ERROR, logger="routes.session_routes")

    resp, llm, resolve, pick, factory, db = _post(monkeypatch, sessions=_ONE)

    assert resp.status_code == 200
    assert resp.json() == {"status": "skipped", "reason": "No unfiled sessions to sort"}
    assert "error" not in resp.json()
    llm.assert_not_called()
    resolve.assert_not_called()
    pick.assert_not_called()
    factory.assert_called_once()
    db.close.assert_called_once()
    db.commit.assert_not_called()
    _session_manager.get_sessions_for_user.assert_called_once_with("alice")
    assert _LOG not in caplog.text
    assert _FIXED_ERROR not in resp.text
    _assert_no_secret(resp)


def test_auto_sort_keeps_the_fixed_sentence_and_leaves_neighbors():
    source = (_REPO / "routes" / "session_routes.py").read_text(encoding="utf-8")
    assert "def session_error(message: str, status_code: int = 502, **extra):" in source
    assert source.count("def session_error(") == 1
    assert source.count('session_error("Failed to sort sessions")') == 1
    assert source.index("def _sanitize_export_filename(") < source.index("def session_error(")
    assert source.index("def session_error(") < source.index("def _verify_session_owner(")
    assert source.index("def session_error(") < source.index("def setup_session_routes(")
    assert "from fastapi.responses import JSONResponse" in source
    assert "Auto-sort failed:" not in source
    assert 'logger.error(f"Auto-sort LLM call failed: {e}")' not in source
    assert source.count('logger.exception("Auto-sort LLM call failed")') == 1
    assert "def memory_error(" not in source
    assert "def gallery_error(" not in source
    assert "def research_error(" not in source

    handler_start = source.index("    def auto_sort_sessions(")
    handler_end = source.index("    async def get_context_info(")
    handler = source[handler_start:handler_end]
    assert "effective_user(request)" in handler
    assert "require_user" not in handler
    assert '"skipped_llm": True' in handler
    assert 'return {"status": "skipped", "reason": "No unfiled sessions to sort"}' in handler
    assert 'raise HTTPException(503, "No available model endpoint for auto-sort")' in handler
    assert (
        'raise HTTPException(502, "AI returned invalid JSON for auto-sort — '
        'the model may not follow JSON instructions; try a different utility model in Settings.")'
    ) in handler
    assert handler.count("except HTTPException:") == 1
    assert "        except HTTPException:\n            raise\n" in handler
    assert handler.count("except Exception as e:") == 1
    assert handler.count("        except Exception:\n") == 1
    assert 'logger.exception("Auto-sort LLM call failed")' in handler
    assert 'return session_error("Failed to sort sessions")' in handler
    assert "str(e)" not in handler.split('return session_error("Failed to sort sessions")')[0].split("except HTTPException:")[-1]
    assert 'logger.error(f"Auto-sort DB update failed: {e}")' in handler
    assert 'raise HTTPException(500, "Failed to apply folder assignments")' in handler
    assert "session_error" not in handler.split("Failed to apply folder assignments")[1]
    refused_at = handler.index("        except HTTPException:\n            raise\n")
    generic_at = handler.index("        except Exception:\n")
    log_at = handler.index('logger.exception("Auto-sort LLM call failed")')
    err_at = handler.index('return session_error("Failed to sort sessions")')
    db_at = handler.index('logger.error(f"Auto-sort DB update failed: {e}")')
    assert refused_at < generic_at < log_at < err_at < db_at

    memory = (_REPO / "routes" / "memory_routes.py").read_text(encoding="utf-8")
    assert "def memory_error(" not in memory
    assert "def session_error(" not in memory
    import_start = memory.index("    async def import_memories_from_file(")
    pin_start = memory.index("    def pin_memory(")
    importer = memory[import_start:pin_start]
    assert 'raise HTTPException(502, f"LLM extraction failed: {str(e)}")' in importer
    assert "except Exception as e:" in importer
    assert "memory_error" not in importer
    assert "session_error" not in importer
    assert "except HTTPException:" not in importer

    gallery = (_REPO / "routes" / "gallery_routes.py").read_text(encoding="utf-8")
    assert "def gallery_error(" not in gallery
    assert "def session_error(" not in gallery
    replace_start = gallery.index("    async def gallery_replace(")
    rename_start = gallery.index("    async def gallery_rename(")
    replace = gallery[replace_start:rename_start]
    assert 'raise HTTPException(500, f"DB commit failed: {e}")' in replace
    assert "except Exception as e:" in replace
    assert "gallery_error" not in replace
    assert "session_error" not in replace
    assert "except HTTPException:" not in replace

    research = (_REPO / "routes" / "research_routes.py").read_text(encoding="utf-8")
    assert "def research_error(" not in research
    assert "def session_error(" not in research
    report = research[research.index("    async def research_report("):research.index("    class HideImageRequest")]
    detail = research[research.index("    async def research_detail("):research.index("    async def research_archive(")]
    archive = research[research.index("    async def research_archive("):research.index("    async def research_delete(")]
    peek = research[research.index("    async def research_result_peek("):research.index("    async def research_spinoff(")]
    assert 'raise HTTPException(500, f"Report generation failed: {e}")' in report
    assert "research_error" not in report
    assert "session_error" not in report
    assert "except HTTPException:" not in report
    assert 'raise HTTPException(500, f"Failed to read research: {e}")' in detail
    assert "research_error" not in detail
    assert "except HTTPException:" not in detail
    assert 'raise HTTPException(500, f"Failed to update research: {e}")' in archive
    assert "research_error" not in archive
    assert "if p.exists():" in peek
    assert 'd = json.loads(p.read_text(encoding="utf-8"))' in peek
    assert "research_error" not in peek
    assert "session_error" not in peek
    assert "except HTTPException:" not in peek

    history = (_REPO / "routes" / "history_routes.py").read_text(encoding="utf-8")
    assert "def history_error(" not in history
    assert "def session_error(" not in history
    compact = history[history.index("    async def compact_session("):]
    assert 'logger.error(f"Manual compact error {session_id}: {e}")' in compact
    assert "raise HTTPException(500, str(e))" in compact

    skills = (_REPO / "routes" / "skills_routes.py").read_text(encoding="utf-8")
    assert 'return {"builtin": [], "count": 0, "error": str(e)}' in skills
    upload = (_REPO / "routes" / "upload_routes.py").read_text(encoding="utf-8")
    assert 'raise HTTPException(500, f"Vision analysis failed: {e}")' in upload
    drafts = (_REPO / "routes" / "editor_draft_routes.py").read_text(encoding="utf-8")
    assert "raise HTTPException(500, str(e))" in drafts

    personal = (_REPO / "routes" / "personal_routes.py").read_text(encoding="utf-8")
    assert "def personal_error(" not in personal
    assert "def session_error(" not in personal
    assert 'raise HTTPException(500, f"Failed to add directory: {str(e)}")' in personal
    assert 'raise HTTPException(500, f"Failed to remove directory: {str(e)}")' in personal
    embedding = (_REPO / "routes" / "embedding_routes.py").read_text(encoding="utf-8")
    assert 'raise HTTPException(500, f"Download failed: {str(e)}")' in embedding
    assert "def session_error(" not in embedding

    app_js = (_REPO / "static" / "app.js").read_text(encoding="utf-8")
    assert "data.detail || 'Auto-sort failed'" in app_js
    assert "Failed to sort sessions" not in app_js
    assert "session_error" not in app_js
    library = (_REPO / "static" / "js" / "documentLibrary.js").read_text(encoding="utf-8")
    assert "data.detail || 'Tidy failed'" in library
    assert "Failed to sort sessions" not in library
    assert "data.error" not in library
    slash = (_REPO / "static" / "js" / "slashCommands.js").read_text(encoding="utf-8")
    assert "slashReply('Auto-sort failed')" in slash
    assert "Failed to sort sessions" not in slash
    assert "session_error" not in slash
