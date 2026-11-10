"""GET /api/skills/builtin/{name} must not return exception text.

Unexpected import failures are HTTP 502 {"error": "Failed to load built-in skill"}.
Unknown names stay HTTP 404. PUT and DELETE are unchanged. The built-in list
still returns the import exception on HTTP 200.
"""
import asyncio
import builtins
import json
import logging
import os
import sys
import types
from pathlib import Path

import pytest
from fastapi import HTTPException

_REPO = Path(__file__).resolve().parent.parent
_IMPORT_SECRET = "BUILTIN_IMPORT_SECRET"
_FIXED_ERROR = "Failed to load built-in skill"
_SECTIONS = {
    "bash": "shipped bash instructions",
    ("web_search", "web_fetch"): "shipped web instructions",
}


def _endpoints():
    os.environ.setdefault("DATABASE_URL", "sqlite:///./data/app.db")
    (_REPO / "data").mkdir(exist_ok=True)
    from routes.skills_routes import setup_skills_routes

    router = setup_skills_routes(object())
    return {route.endpoint.__name__: route.endpoint for route in router.routes}


def _install_sections(monkeypatch, overrides=None):
    module = types.ModuleType("src.agent_loop")
    module.TOOL_SECTIONS = dict(_SECTIONS)
    module.get_builtin_overrides = lambda: dict(overrides or {})
    monkeypatch.setitem(sys.modules, "src.agent_loop", module)


def _raise_on_agent_loop(monkeypatch, exc):
    original = builtins.__import__

    def guarded(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "src.agent_loop":
            raise exc
        return original(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guarded)


def _get(endpoint, name):
    return asyncio.run(endpoint(name, None))


def _list(endpoint):
    return asyncio.run(endpoint(None))


def _body(response):
    return json.loads(response.body.decode())


def _assert_fixed(response):
    assert response.status_code == 502
    body = _body(response)
    assert body == {"error": _FIXED_ERROR}
    assert "detail" not in body
    assert _IMPORT_SECRET not in response.body.decode()


def test_known_builtin_returns_shipped_text(monkeypatch):
    _install_sections(monkeypatch)
    get_builtin_skill = _endpoints()["get_builtin_skill"]

    body = _get(get_builtin_skill, "bash")

    assert body == {
        "name": "bash",
        "text": "shipped bash instructions",
        "default": "shipped bash instructions",
        "is_overridden": False,
    }


def test_tuple_key_builtin_returns_shipped_text(monkeypatch):
    _install_sections(monkeypatch)
    get_builtin_skill = _endpoints()["get_builtin_skill"]

    body = _get(get_builtin_skill, "web_fetch")

    assert body == {
        "name": "web_fetch",
        "text": "shipped web instructions",
        "default": "shipped web instructions",
        "is_overridden": False,
    }


def test_override_replaces_text_and_keeps_default(monkeypatch):
    _install_sections(monkeypatch, {"bash": "custom bash instructions"})
    get_builtin_skill = _endpoints()["get_builtin_skill"]

    body = _get(get_builtin_skill, "bash")

    assert body == {
        "name": "bash",
        "text": "custom bash instructions",
        "default": "shipped bash instructions",
        "is_overridden": True,
    }


def test_unknown_builtin_stays_404(monkeypatch, caplog):
    _install_sections(monkeypatch)
    get_builtin_skill = _endpoints()["get_builtin_skill"]
    caplog.set_level(logging.ERROR, logger="routes.skills_routes")

    with pytest.raises(HTTPException) as exc:
        _get(get_builtin_skill, "no-such-tool")

    assert exc.value.status_code == 404
    assert exc.value.detail == "No built-in tool named 'no-such-tool'"
    assert "Built-in skill lookup failed" not in caplog.text


def test_import_failure_hides_exception(monkeypatch, caplog):
    get_builtin_skill = _endpoints()["get_builtin_skill"]
    _raise_on_agent_loop(monkeypatch, RuntimeError(_IMPORT_SECRET))
    caplog.set_level(logging.ERROR, logger="routes.skills_routes")

    response = _get(get_builtin_skill, "bash")

    _assert_fixed(response)
    assert _IMPORT_SECRET in caplog.text
    assert "Built-in skill lookup failed for bash" in caplog.text


def test_import_http_exception_is_reraised(monkeypatch, caplog):
    get_builtin_skill = _endpoints()["get_builtin_skill"]
    _raise_on_agent_loop(monkeypatch, HTTPException(409, "Built-in skill refused"))
    caplog.set_level(logging.ERROR, logger="routes.skills_routes")

    with pytest.raises(HTTPException) as exc:
        _get(get_builtin_skill, "bash")

    assert exc.value.status_code == 409
    assert exc.value.detail == "Built-in skill refused"
    assert _FIXED_ERROR not in caplog.text
    assert _IMPORT_SECRET not in caplog.text
    assert "Built-in skill lookup failed" not in caplog.text


def test_list_still_returns_import_error_text(monkeypatch):
    list_builtin_skills = _endpoints()["list_builtin_skills"]
    _raise_on_agent_loop(monkeypatch, RuntimeError(_IMPORT_SECRET))

    body = _list(list_builtin_skills)

    assert body == {"builtin": [], "count": 0, "error": _IMPORT_SECRET}
    assert not hasattr(body, "status_code")


def test_builtin_route_keeps_the_fixed_sentence_and_leaves_put_and_delete():
    source = (_REPO / "routes/skills_routes.py").read_text(encoding="utf-8")
    assert "def skill_error(message: str, status_code: int = 502, **extra):" in source
    list_start = source.index("    async def list_builtin_skills(")
    get_start = source.index("    async def get_builtin_skill(")
    put_start = source.index("    async def set_builtin_override(")
    delete_start = source.index("    async def reset_builtin_override(")
    add_start = source.index("    async def add_skill(")
    listing = source[list_start:get_start]
    getter = source[get_start:put_start]
    put = source[put_start:delete_start]
    delete = source[delete_start:add_start]
    import_end = getter.index("default = None")
    imported = getter[:import_end]

    assert "except HTTPException:" in imported
    assert 'skill_error("Failed to load built-in skill")' in imported
    assert 'logger.exception("Built-in skill lookup failed for %s", name)' in imported
    assert "{e}" not in imported
    assert "str(e)" not in imported
    assert "HTTPException(500" not in imported
    assert 'HTTPException(404, f"No built-in tool named {name!r}")' in getter
    assert "str(e)" not in getter
    assert "{e}" not in getter

    assert 'return {"builtin": [], "count": 0, "error": str(e)}' in listing
    assert "skill_error" not in listing

    assert "skill_error" not in put
    assert "from src.agent_loop import TOOL_SECTIONS" in put
    assert 'HTTPException(404, f"No built-in tool named {name!r}")' in put
    assert 'HTTPException(400, "text is required")' in put
    assert "{e}" not in put
    assert "str(e)" not in put

    assert "skill_error" not in delete
    assert 'return {"ok": True, "name": name, "is_overridden": False}' in delete
    assert "{e}" not in delete
    assert "str(e)" not in delete

    client = (_REPO / "static/js/skills.js").read_text(encoding="utf-8")
    load_start = client.index("export async function loadSkills(")
    load_end = client.index("function _focusSkillRow(", load_start)
    load = client[load_start:load_end]
    assert "/api/skills/builtin" not in load
    assert "Built-in capabilities are no longer surfaced" in load

    expand_start = client.index("async function _expandBuiltinCard(")
    expand_end = client.index("function _toggleBuiltinEdit(", expand_start)
    expand = client[expand_start:expand_end]
    assert "if (!res.ok) throw new Error(`HTTP ${res.status}`)" in expand
    catch = expand[expand.index("} catch (e) {"):]
    assert "Failed to load." in catch
    assert ".text(" not in catch
    assert ".json(" not in catch
    assert "e.message" not in catch

    save_start = client.index("async function _saveBuiltinEdit(")
    revert_start = client.index("async function _revertBuiltin(", save_start)
    filtered_start = client.index("function _getFilteredSkills(", revert_start)
    save = client[save_start:revert_start]
    revert = client[revert_start:filtered_start]
    assert "if (!res.ok) throw new Error(`HTTP ${res.status}`)" in save
    assert "uiModule.showError('Save failed: ' + e.message)" in save
    assert "res.text(" not in save
    assert "if (!res.ok) throw new Error(`HTTP ${res.status}`)" in revert
    assert "uiModule.showError('Revert failed: ' + e.message)" in revert
    assert "res.text(" not in revert

    upload = (_REPO / "routes/upload_routes.py").read_text(encoding="utf-8")
    assert 'raise HTTPException(500, f"Vision analysis failed: {e}")' in upload
    assert "def vision_error(" not in upload
    assert "skill_error" not in upload

    docs = (_REPO / "routes/document_routes.py").read_text(encoding="utf-8")
    fill_start = docs.index("    async def ai_fill_annotations(")
    render_start = docs.index("    async def render_pdf(", fill_start)
    fill = docs[fill_start:render_start]
    assert "import fitz" in fill
    assert "No vision model available: {e}" in fill
    assert "document_error" not in fill
    assert "skill_error" not in fill
