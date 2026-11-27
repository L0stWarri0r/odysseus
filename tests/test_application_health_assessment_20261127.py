"""POST /api/memory/import must not return LLM exception text.

A failed extraction is HTTP 502 {"error": "Failed to extract memories"}.
The exception is logged and is not returned. An HTTPException from the
model call is still passed through. A non-JSON model reply stays a 200
line split. The JSON fast path, empty text, missing model, missing
session, and unsupported extension stay as they were. Gallery replace
and the research routes still leak. Clients were not changed.
"""
import logging
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

_REPO = Path(__file__).resolve().parent.parent
(_REPO / "data").mkdir(exist_ok=True)
os.environ["DATABASE_URL"] = "sqlite:////tmp/odysseus-health-20261127.db"
os.environ["AUTH_ENABLED"] = "true"

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import routes.memory_routes as memory_routes
from routes.memory_routes import setup_memory_routes

_EXTRACT_SECRET = "EXTRACT_SECRET memory import token=do-not-return"
_FIXED_ERROR = "Failed to extract memories"
_LOG = "Memory import extraction failed"
_REFUSED = "Extract refused"
_TEXT = b"Alice keeps a spare key under the mat\n"
_ENDPOINT = ("http://llm.test/v1", "test-model", {})
_NO_MODEL = ("", "", {})


def _client(username, memory_manager, session_manager):
    app = FastAPI()

    @app.middleware("http")
    async def _stamp_user(request, call_next):
        if username is not None:
            request.state.current_user = username
        return await call_next(request)

    app.include_router(setup_memory_routes(memory_manager, session_manager))
    return TestClient(app)


def _post(
    username,
    monkeypatch,
    *,
    filename="notes.txt",
    content=_TEXT,
    session=None,
    allow=True,
    endpoint=_ENDPOINT,
    llm=None,
    session_manager=None,
):
    if allow:
        monkeypatch.setattr(
            "src.auth_helpers.require_privilege",
            lambda request, key: "alice",
        )
    memory_manager = MagicMock()
    if session_manager is None:
        session_manager = MagicMock()
    resolve = MagicMock(return_value=endpoint)
    monkeypatch.setattr(memory_routes, "resolve_endpoint", resolve)
    if llm is None:
        llm = AsyncMock()
    monkeypatch.setattr(memory_routes, "llm_call_async", llm)
    client = _client(username, memory_manager, session_manager)
    kwargs = {"files": {"file": (filename, content, "text/plain")}}
    if session is not None:
        kwargs["data"] = {"session": session}
    resp = client.post("/api/memory/import", **kwargs)
    return resp, llm, resolve, session_manager


def _assert_no_secret(resp):
    assert _EXTRACT_SECRET not in resp.text
    assert _LOG not in resp.text
    assert "LLM extraction failed" not in resp.text
    assert _REFUSED not in resp.text or resp.status_code == 409


def _assert_fixed(resp):
    assert resp.status_code == 502
    assert resp.json() == {"error": _FIXED_ERROR}
    assert "detail" not in resp.json()
    assert "application/json" in resp.headers["content-type"]
    _assert_no_secret(resp)


def test_anonymous_is_401_and_does_not_call_the_model(caplog, monkeypatch):
    caplog.set_level(logging.ERROR, logger="routes.memory_routes")
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("LOCALHOST_BYPASS", "false")

    resp, llm, resolve, session_manager = _post(None, monkeypatch, allow=False)

    assert resp.status_code == 401
    assert resp.json()["detail"] == "Not authenticated"
    assert "error" not in resp.json()
    llm.assert_not_called()
    resolve.assert_not_called()
    session_manager.get_session.assert_not_called()
    assert _LOG not in caplog.text
    _assert_no_secret(resp)


def test_missing_model_is_400_and_does_not_call_the_model(caplog, monkeypatch):
    caplog.set_level(logging.ERROR, logger="routes.memory_routes")

    resp, llm, resolve, _session_manager = _post(
        "alice", monkeypatch, endpoint=_NO_MODEL
    )

    assert resp.status_code == 400
    assert resp.json()["detail"] == "No LLM model configured. Set a default model in Settings."
    assert "error" not in resp.json()
    llm.assert_not_called()
    resolve.assert_called_once()
    assert resolve.call_args.args[0] == "utility"
    assert resolve.call_args.kwargs["owner"] == "alice"
    assert _LOG not in caplog.text
    _assert_no_secret(resp)


def test_missing_session_is_404_and_does_not_resolve(caplog, monkeypatch):
    caplog.set_level(logging.ERROR, logger="routes.memory_routes")
    session_manager = MagicMock()
    session_manager.get_session.side_effect = KeyError("missing-session")

    resp, llm, resolve, _session_manager = _post(
        "alice",
        monkeypatch,
        session="missing-session",
        session_manager=session_manager,
    )

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Session not found — needed for LLM config"
    assert "error" not in resp.json()
    llm.assert_not_called()
    resolve.assert_not_called()
    session_manager.get_session.assert_called_once_with("missing-session")
    assert _LOG not in caplog.text
    _assert_no_secret(resp)


def test_unsupported_extension_is_400_and_does_not_call_the_model(caplog, monkeypatch):
    caplog.set_level(logging.ERROR, logger="routes.memory_routes")

    resp, llm, _resolve, _session_manager = _post(
        "alice", monkeypatch, filename="payload.exe", content=b"not-a-real-binary"
    )

    assert resp.status_code == 400
    assert resp.json()["detail"] == "Unsupported file type: .exe"
    assert "error" not in resp.json()
    llm.assert_not_called()
    assert _LOG not in caplog.text
    _assert_no_secret(resp)


def test_empty_text_is_200_and_does_not_call_the_model(caplog, monkeypatch):
    caplog.set_level(logging.ERROR, logger="routes.memory_routes")

    resp, llm, _resolve, _session_manager = _post(
        "alice", monkeypatch, filename="blank.txt", content=b"   \n"
    )

    assert resp.status_code == 200
    assert resp.json() == {"suggestions": [], "message": "No readable content found"}
    assert "error" not in resp.json()
    llm.assert_not_called()
    assert _LOG not in caplog.text
    assert _FIXED_ERROR not in resp.text
    _assert_no_secret(resp)


def test_json_fast_path_strips_the_prefix_and_skips_the_model(caplog, monkeypatch):
    caplog.set_level(logging.ERROR, logger="routes.memory_routes")

    resp, llm, _resolve, _session_manager = _post(
        "alice",
        monkeypatch,
        filename="notes.json",
        content=b'[{"text":"1. Likes tea","category":"preference"}]',
    )

    assert resp.status_code == 200
    assert resp.json() == {
        "suggestions": [{"text": "Likes tea", "category": "preference"}],
        "filename": "notes.json",
    }
    assert "error" not in resp.json()
    llm.assert_not_called()
    assert _LOG not in caplog.text
    assert _FIXED_ERROR not in resp.text
    _assert_no_secret(resp)


def test_non_json_model_reply_stays_line_split_facts(caplog, monkeypatch):
    caplog.set_level(logging.ERROR, logger="routes.memory_routes")
    llm = AsyncMock(return_value="not json\nAlice prefers tea")

    resp, llm, _resolve, _session_manager = _post("alice", monkeypatch, llm=llm)

    assert resp.status_code == 200
    assert resp.json() == {
        "suggestions": [
            {"text": "not json", "category": "fact"},
            {"text": "Alice prefers tea", "category": "fact"},
        ],
        "filename": "notes.txt",
    }
    assert "error" not in resp.json()
    llm.assert_awaited_once()
    assert _FIXED_ERROR not in resp.text
    assert _LOG not in caplog.text
    _assert_no_secret(resp)


def test_success_strips_the_list_prefix(caplog, monkeypatch):
    caplog.set_level(logging.ERROR, logger="routes.memory_routes")
    llm = AsyncMock(return_value='[{"text":"2) Lives in Oslo","category":"identity"}]')

    resp, llm, resolve, _session_manager = _post("alice", monkeypatch, llm=llm)

    assert resp.status_code == 200
    assert resp.json() == {
        "suggestions": [{"text": "Lives in Oslo", "category": "identity"}],
        "filename": "notes.txt",
    }
    assert "error" not in resp.json()
    assert "application/json" in resp.headers["content-type"]
    llm.assert_awaited_once()
    resolve.assert_called_once()
    assert resolve.call_args.args[0] == "utility"
    assert resolve.call_args.kwargs["owner"] == "alice"
    assert _LOG not in caplog.text
    assert "2)" not in resp.json()["suggestions"][0]["text"]
    _assert_no_secret(resp)


def test_llm_runtime_error_is_502_and_hides_the_secret(caplog, monkeypatch):
    caplog.set_level(logging.ERROR, logger="routes.memory_routes")
    llm = AsyncMock(side_effect=RuntimeError(_EXTRACT_SECRET))

    resp, llm, _resolve, _session_manager = _post(
        "alice", monkeypatch, filename="notes.txt", content=_TEXT, llm=llm
    )

    _assert_fixed(resp)
    llm.assert_awaited_once()
    assert f"{_LOG} for notes.txt" in caplog.text
    assert _EXTRACT_SECRET in caplog.text
    assert _FIXED_ERROR not in caplog.text
    assert "LLM extraction failed" not in caplog.text


def test_llm_http_exception_is_reraised(caplog, monkeypatch):
    caplog.set_level(logging.ERROR, logger="routes.memory_routes")
    llm = AsyncMock(side_effect=HTTPException(status_code=409, detail=_REFUSED))

    resp, llm, _resolve, _session_manager = _post("alice", monkeypatch, llm=llm)

    assert resp.status_code == 409
    assert resp.json()["detail"] == _REFUSED
    assert "error" not in resp.json()
    assert _FIXED_ERROR not in resp.text
    llm.assert_awaited_once()
    assert _LOG not in caplog.text
    assert _FIXED_ERROR not in caplog.text
    assert _EXTRACT_SECRET not in caplog.text
    assert _EXTRACT_SECRET not in resp.text


def test_memory_import_keeps_the_fixed_sentence_and_leaves_neighbors():
    source = (_REPO / "routes" / "memory_routes.py").read_text(encoding="utf-8")
    assert "def memory_error(message: str, status_code: int = 502, **extra):" in source
    assert source.count("def memory_error(") == 1
    assert source.count('memory_error("Failed to extract memories")') == 1
    assert source.index("def memory_error(") < source.index("def setup_memory_routes(")
    assert "from fastapi.responses import JSONResponse" in source
    assert "LLM extraction failed" not in source
    assert "Memory import extraction failed: {e}" not in source
    assert "def gallery_error(" not in source
    assert "def research_error(" not in source

    import_start = source.index("    async def import_memories_from_file(")
    pin_start = source.index("    def pin_memory(")
    get_start = source.index("    def get_memory_item(")
    importer = source[import_start:pin_start]
    pin = source[pin_start:get_start]

    fallback = (
        "        except json.JSONDecodeError:\n"
        '            # Fallback: split by lines, stripping any "1.", "2)" markdown-list\n'
        "            # numbering the model added so saved memories don't keep the prefix.\n"
        "            lines = [_strip_list_prefix(l.strip()) for l in raw.splitlines() if l.strip() and len(l.strip()) > 5]\n"
        '            return {"suggestions": [{"text": l, "category": "fact"} for l in lines[:20]], "filename": filename}\n'
    )
    assert fallback in importer
    assert importer.count("except json.JSONDecodeError:") == 2
    assert 'require_privilege(request, "can_manage_memory")' in importer
    assert "sess = session_manager.get_session(session)" in importer
    assert (
        '            except KeyError:\n'
        '                 raise HTTPException(404, "Session not found — needed for LLM config")\n'
    ) in importer
    assert 'resolve_endpoint("utility", owner=_owner(request))' in importer
    assert 'raise HTTPException(400, "No LLM model configured. Set a default model in Settings.")' in importer
    assert 'raise HTTPException(400, f"Unsupported file type: {ext}")' in importer
    assert "from src.document_processor import _process_pdf" in importer
    assert 'return {"suggestions": [], "message": "No readable content found"}' in importer
    assert 'return {"suggestions": direct, "filename": filename}' in importer
    assert "raw = await llm_call_async(" in importer
    assert "except Exception as e:" not in importer
    assert importer.count("except HTTPException:") == 1
    assert importer.count("except Exception:") == 1
    assert "str(e)" not in importer
    assert "{e}" not in importer
    assert "HTTPException(502" not in importer
    assert 'logger.exception("Memory import extraction failed for %s", filename)' in importer
    assert 'return memory_error("Failed to extract memories")' in importer
    privilege_at = importer.index('require_privilege(request, "can_manage_memory")')
    session_at = importer.index("sess = session_manager.get_session(session)")
    missing_at = importer.index('raise HTTPException(404, "Session not found — needed for LLM config")')
    resolve_at = importer.index('resolve_endpoint("utility", owner=_owner(request))')
    model_at = importer.index("No LLM model configured")
    ext_at = importer.index("Unsupported file type")
    pdf_at = importer.index("_process_pdf")
    empty_at = importer.index("No readable content found")
    fast_at = importer.index('return {"suggestions": direct, "filename": filename}')
    llm_at = importer.index("raw = await llm_call_async(")
    fallback_at = importer.index(fallback)
    refused_at = importer.index("        except HTTPException:\n            raise\n")
    generic_at = importer.index("        except Exception:\n")
    log_at = importer.index('logger.exception("Memory import extraction failed for %s", filename)')
    err_at = importer.index('return memory_error("Failed to extract memories")')
    assert privilege_at < session_at < missing_at < resolve_at < model_at < ext_at
    assert ext_at < pdf_at < empty_at < fast_at < llm_at < fallback_at
    assert fallback_at < refused_at < generic_at < log_at < err_at

    assert "def pin_memory(" in pin
    assert "memory_error" not in pin
    assert 'return {"ok": True, "pinned": pinned}' in pin

    gallery = (_REPO / "routes" / "gallery_routes.py").read_text(encoding="utf-8")
    assert "def gallery_error(" not in gallery
    assert "def memory_error(" not in gallery
    replace_start = gallery.index("    async def gallery_replace(")
    rename_start = gallery.index("    async def gallery_rename(")
    replace = gallery[replace_start:rename_start]
    assert 'raise HTTPException(500, f"DB commit failed: {e}")' in replace
    assert "except Exception as e:" in replace
    assert "gallery_error" not in replace
    assert "memory_error" not in replace
    assert "except HTTPException:" not in replace

    research = (_REPO / "routes" / "research_routes.py").read_text(encoding="utf-8")
    assert "def research_error(" not in research
    assert "def memory_error(" not in research
    assert "def gallery_error(" not in research
    report = research[research.index("    async def research_report("):research.index("    class HideImageRequest")]
    detail = research[research.index("    async def research_detail("):research.index("    async def research_archive(")]
    archive = research[research.index("    async def research_archive("):research.index("    async def research_delete(")]
    peek = research[research.index("    async def research_result_peek("):research.index("    async def research_spinoff(")]
    assert 'raise HTTPException(500, f"Report generation failed: {e}")' in report
    assert "research_error" not in report
    assert "memory_error" not in report
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
    assert "memory_error" not in peek
    assert "gallery_error" not in peek
    assert "except HTTPException:" not in peek

    history = (_REPO / "routes" / "history_routes.py").read_text(encoding="utf-8")
    assert "def history_error(" not in history
    assert "def memory_error(" not in history
    compact = history[history.index("    async def compact_session("):]
    assert 'logger.error(f"Manual compact error {session_id}: {e}")' in compact
    assert "raise HTTPException(500, str(e))" in compact

    skills = (_REPO / "routes" / "skills_routes.py").read_text(encoding="utf-8")
    assert 'return {"builtin": [], "count": 0, "error": str(e)}' in skills
    upload = (_REPO / "routes" / "upload_routes.py").read_text(encoding="utf-8")
    assert 'raise HTTPException(500, f"Vision analysis failed: {e}")' in upload
    drafts = (_REPO / "routes" / "editor_draft_routes.py").read_text(encoding="utf-8")
    assert "raise HTTPException(500, str(e))" in drafts

    sessions = (_REPO / "routes" / "session_routes.py").read_text(encoding="utf-8")
    assert "def session_error(" not in sessions
    assert "def memory_error(" not in sessions
    assert 'raise HTTPException(502, f"Auto-sort failed: {str(e)}")' in sessions
    assert 'logger.error(f"Auto-sort LLM call failed: {e}")' in sessions

    client = (_REPO / "static" / "js" / "memory.js").read_text(encoding="utf-8")
    assert "`${window.location.origin}/api/memory/import`" in client
    assert "err.detail || 'Import failed'" in client
    assert "err.error" not in client
    assert "Failed to extract memories" not in client
    assert "memory_error" not in client
