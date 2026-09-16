"""Health assessment 2026-09-16: preset save/delete/expand failures must not look like success."""

import json
from pathlib import Path

import pytest
from fastapi.responses import JSONResponse

from routes.preset_routes import UserTemplateRequest, preset_error, setup_preset_routes
from src.request_models import PresetUpdateRequest

_REPO = Path(__file__).resolve().parents[1]
_PRESET_ROUTES = (_REPO / "routes" / "preset_routes.py").read_text(encoding="utf-8")
_PRESETS_JS = (_REPO / "static" / "js" / "presets.js").read_text(encoding="utf-8")
_GROUP_JS = (_REPO / "static" / "js" / "group.js").read_text(encoding="utf-8")


def _route_endpoint(router, path: str, method: str):
    method = method.upper()
    for route in router.routes:
        if route.path == path and method in getattr(route, "methods", set()):
            return route.endpoint
    raise AssertionError(f"route not found: {method} {path}")


def _payload(res):
    if isinstance(res, JSONResponse):
        return res.status_code, json.loads(res.body)
    return 200, res


class _Req:
    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        return self._payload


class _FakePresets:
    def __init__(self):
        self.fail = False
        self.templates = []
        self.groups = []
        self.presets = {"custom": {"name": "Custom", "enabled": False}}
        self.saved_custom = None
        self.saved_template = None
        self.deleted_id = None

    def update_custom(self, *args, **kwargs):
        self.saved_custom = (args, kwargs)
        return not self.fail

    def get_user_templates(self):
        return list(self.templates)

    def save_user_template(self, template):
        self.saved_template = template
        if self.fail:
            return False
        self.templates = [t for t in self.templates if t.get("id") != template.get("id")]
        self.templates.append(template)
        return True

    def delete_user_template(self, template_id):
        self.deleted_id = template_id
        if self.fail:
            return False
        self.templates = [t for t in self.templates if t.get("id") != template_id]
        return True

    def get_group_presets(self):
        return list(self.groups)

    def save_group_presets(self, groups):
        if self.fail:
            return False
        self.groups = list(groups)
        return True


@pytest.fixture
def preset_harness():
    manager = _FakePresets()
    router = setup_preset_routes(manager)
    return {
        "manager": manager,
        "custom": _route_endpoint(router, "/api/presets/custom", "POST"),
        "save_template": _route_endpoint(router, "/api/presets/templates", "POST"),
        "delete_template": _route_endpoint(router, "/api/presets/templates/{template_id}", "DELETE"),
        "expand": _route_endpoint(router, "/api/presets/expand", "POST"),
        "save_groups": _route_endpoint(router, "/api/presets/groups", "POST"),
    }


def test_preset_error_helper_returns_json_error_with_non_200_status():
    assert "def preset_error(message: str, status_code: int = 502)" in _PRESET_ROUTES
    resp = preset_error("Failed to save preset")
    assert resp.status_code == 502
    body = json.loads(resp.body)
    assert body == {"success": False, "message": "Failed to save preset"}


def test_preset_routes_use_preset_error_instead_of_http_200():
    assert 'return preset_error("Failed to save preset")' in _PRESET_ROUTES
    assert 'return preset_error("Failed to save template")' in _PRESET_ROUTES
    assert 'return preset_error("Template not found", 404)' in _PRESET_ROUTES
    assert 'return preset_error("Failed to delete template")' in _PRESET_ROUTES
    assert 'return preset_error("Nothing to expand", 400)' in _PRESET_ROUTES
    assert 'return preset_error("Failed to expand prompt")' in _PRESET_ROUTES
    assert 'return preset_error("Invalid group presets", 400)' in _PRESET_ROUTES
    assert 'return preset_error("Failed to save group presets")' in _PRESET_ROUTES
    assert 'return {"success": False, "message": "Failed to save preset"}' not in _PRESET_ROUTES
    assert 'return {"success": False, "message": str(e)}' not in _PRESET_ROUTES


def test_presets_ui_does_not_treat_failures_as_success():
    assert "if (!response.ok || result.success === false)" in _PRESETS_JS
    assert "if (!res.ok || data.success === false)" in _PRESETS_JS
    assert "if (!res.ok || data.success === false || !data.prompt)" in _PRESETS_JS
    assert "result.message || result.error || result.detail" in _PRESETS_JS
    assert "if (!r.ok) console.error('Failed to save group presets'" in _GROUP_JS
    assert "if (!saveRes.ok) console.error('Failed to save group presets'" in _GROUP_JS


def test_save_as_template_http_errors_are_not_restart_server():
    # HTTP 4xx/5xx used to throw and relabel the button "Restart server".
    assert "if (!res.ok) throw new Error(`Server returned ${res.status}`)" not in _PRESETS_JS
    assert "btn.textContent = 'Restart server'" in _PRESETS_JS


@pytest.mark.asyncio
async def test_custom_preset_write_failure_is_http_502(preset_harness):
    preset_harness["manager"].fail = True
    status, body = _payload(await preset_harness["custom"](
        PresetUpdateRequest(name="Ada", temperature=0.5, max_tokens=0, system_prompt="hi"),
        _admin="admin",
    ))
    assert status == 502
    assert body == {"success": False, "message": "Failed to save preset"}


@pytest.mark.asyncio
async def test_custom_preset_success_still_returns_200(preset_harness):
    status, body = _payload(await preset_harness["custom"](
        PresetUpdateRequest(name="Ada", temperature=0.5, max_tokens=0, system_prompt="hi"),
        _admin="admin",
    ))
    assert status == 200
    assert body == {"success": True, "message": "Custom preset updated"}
    assert preset_harness["manager"].saved_custom is not None


@pytest.mark.asyncio
async def test_save_template_write_failure_is_http_502(preset_harness):
    preset_harness["manager"].fail = True
    status, body = _payload(await preset_harness["save_template"](
        UserTemplateRequest(name="Ada", system_prompt="hi"),
        _admin="admin",
    ))
    assert status == 502
    assert body == {"success": False, "message": "Failed to save template"}


@pytest.mark.asyncio
async def test_save_template_success_still_returns_200(preset_harness):
    status, body = _payload(await preset_harness["save_template"](
        UserTemplateRequest(id="user-abc", name="Ada", system_prompt="hi"),
        _admin="admin",
    ))
    assert status == 200
    assert body["success"] is True
    assert body["template"]["id"] == "user-abc"
    assert body["template"]["name"] == "Ada"


@pytest.mark.asyncio
async def test_delete_template_missing_is_http_404(preset_harness):
    status, body = _payload(await preset_harness["delete_template"]("missing", _admin="admin"))
    assert status == 404
    assert body == {"success": False, "message": "Template not found"}
    assert preset_harness["manager"].deleted_id is None


@pytest.mark.asyncio
async def test_delete_template_write_failure_is_http_502(preset_harness):
    preset_harness["manager"].templates = [{"id": "user-abc", "name": "Ada"}]
    preset_harness["manager"].fail = True
    status, body = _payload(await preset_harness["delete_template"]("user-abc", _admin="admin"))
    assert status == 502
    assert body == {"success": False, "message": "Failed to delete template"}
    assert preset_harness["manager"].deleted_id == "user-abc"


@pytest.mark.asyncio
async def test_delete_template_success_still_returns_200(preset_harness):
    preset_harness["manager"].templates = [{"id": "user-abc", "name": "Ada"}]
    status, body = _payload(await preset_harness["delete_template"]("user-abc", _admin="admin"))
    assert status == 200
    assert body == {"success": True}
    assert preset_harness["manager"].templates == []


@pytest.mark.asyncio
async def test_expand_empty_input_is_http_400(preset_harness):
    status, body = _payload(await preset_harness["expand"](_Req({"name": " ", "prompt": ""})))
    assert status == 400
    assert body == {"success": False, "message": "Nothing to expand"}


@pytest.mark.asyncio
async def test_expand_llm_failure_is_http_502_without_exception_text(preset_harness, monkeypatch):
    import src.ai_interaction as ai_interaction
    import src.llm_core as llm_core

    monkeypatch.setattr(ai_interaction, "_resolve_model", lambda spec: ("http://llm", "m", {}))

    async def _boom(*a, **k):
        raise RuntimeError("secret-api-key-xyz leaked")

    monkeypatch.setattr(llm_core, "llm_call_async", _boom)
    status, body = _payload(await preset_harness["expand"](_Req({"name": "Ada", "prompt": "witty"})))
    assert status == 502
    assert body == {"success": False, "message": "Failed to expand prompt"}
    assert "secret-api-key-xyz" not in json.dumps(body)


@pytest.mark.asyncio
async def test_expand_success_still_returns_200(preset_harness, monkeypatch):
    import src.ai_interaction as ai_interaction
    import src.llm_core as llm_core

    monkeypatch.setattr(ai_interaction, "_resolve_model", lambda spec: ("http://llm", "m", {}))

    async def _ok(*a, **k):
        return "  You are Ada, a precise advisor.  "

    monkeypatch.setattr(llm_core, "llm_call_async", _ok)
    status, body = _payload(await preset_harness["expand"](_Req({"name": "Ada"})))
    assert status == 200
    assert body == {"success": True, "prompt": "You are Ada, a precise advisor."}


@pytest.mark.asyncio
async def test_save_groups_invalid_payload_is_http_400(preset_harness):
    status, body = _payload(await preset_harness["save_groups"](_Req({"groups": "nope"}), _admin="admin"))
    assert status == 400
    assert body == {"success": False, "message": "Invalid group presets"}


@pytest.mark.asyncio
async def test_save_groups_write_failure_is_http_502(preset_harness):
    preset_harness["manager"].fail = True
    status, body = _payload(await preset_harness["save_groups"](
        _Req({"groups": [{"name": "Pair"}]}),
        _admin="admin",
    ))
    assert status == 502
    assert body == {"success": False, "message": "Failed to save group presets"}


@pytest.mark.asyncio
async def test_save_groups_success_still_returns_200(preset_harness):
    groups = [{"name": "Pair", "participants": []}]
    status, body = _payload(await preset_harness["save_groups"](_Req({"groups": groups}), _admin="admin"))
    assert status == 200
    assert body == {"ok": True}
    assert preset_harness["manager"].groups == groups
