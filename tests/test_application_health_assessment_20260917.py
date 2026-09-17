"""Health assessment 2026-09-17: task-parse failures must not look like success."""

import json
from pathlib import Path

import pytest
from fastapi.responses import JSONResponse

from routes.task_routes import setup_task_routes, task_error

_REPO = Path(__file__).resolve().parents[1]
_TASK_ROUTES = (_REPO / "routes" / "task_routes.py").read_text(encoding="utf-8")
_TASKS_JS = (_REPO / "static" / "js" / "tasks.js").read_text(encoding="utf-8")


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
    def __init__(self, payload, *, json_error=None):
        self._payload = payload
        self._json_error = json_error

    async def json(self):
        if self._json_error:
            raise self._json_error
        return self._payload


@pytest.fixture
def parse_task():
    router = setup_task_routes(task_scheduler=None)
    return _route_endpoint(router, "/api/tasks/parse", "POST")


def test_task_error_helper_returns_json_error_with_non_200_status():
    assert "def task_error(message: str, status_code: int = 502)" in _TASK_ROUTES
    resp = task_error("Failed to parse task")
    assert resp.status_code == 502
    body = json.loads(resp.body)
    assert body == {"success": False, "message": "Failed to parse task"}


def test_task_parse_uses_task_error_instead_of_http_200():
    assert 'return task_error("Nothing to parse", 400)' in _TASK_ROUTES
    assert 'return task_error("No model endpoint configured")' in _TASK_ROUTES
    assert 'return task_error("Could not extract a task instruction")' in _TASK_ROUTES
    assert 'return task_error("Failed to parse task")' in _TASK_ROUTES
    assert 'return {"success": False, "message": "Nothing to parse"}' not in _TASK_ROUTES
    assert 'return {"success": False, "message": "No model endpoint configured"}' not in _TASK_ROUTES
    assert 'return {"success": False, "message": "Could not extract a task instruction"}' not in _TASK_ROUTES
    assert 'return {"success": False, "message": str(e)}' not in _TASK_ROUTES


def test_tasks_ui_does_not_treat_parse_failures_as_success():
    assert "if (!res.ok || data.success === false || !data.draft)" in _TASKS_JS
    assert "data.message || data.error || 'Could not draft task'" in _TASKS_JS
    assert "const data = await res.json();\n    if (!data.success || !data.draft)" not in _TASKS_JS


@pytest.mark.asyncio
async def test_parse_empty_description_is_http_400(parse_task):
    status, body = _payload(await parse_task(_Req({"description": "   "})))
    assert status == 400
    assert body == {"success": False, "message": "Nothing to parse"}


@pytest.mark.asyncio
async def test_parse_missing_description_is_http_400(parse_task):
    status, body = _payload(await parse_task(_Req({})))
    assert status == 400
    assert body == {"success": False, "message": "Nothing to parse"}


@pytest.mark.asyncio
async def test_parse_non_object_body_is_http_400(parse_task):
    status, body = _payload(await parse_task(_Req(["every morning"])))
    assert status == 400
    assert body == {"success": False, "message": "Nothing to parse"}


@pytest.mark.asyncio
async def test_parse_non_string_description_is_http_400(parse_task):
    status, body = _payload(await parse_task(_Req({"description": 12})))
    assert status == 400
    assert body == {"success": False, "message": "Nothing to parse"}


@pytest.mark.asyncio
async def test_parse_invalid_json_is_http_400(parse_task):
    status, body = _payload(await parse_task(_Req(None, json_error=ValueError("not json"))))
    assert status == 400
    assert body == {"success": False, "message": "Nothing to parse"}


@pytest.mark.asyncio
async def test_parse_missing_endpoint_is_http_502(parse_task, monkeypatch):
    import src.endpoint_resolver as endpoint_resolver

    monkeypatch.setattr(endpoint_resolver, "resolve_endpoint", lambda spec: (None, None, {}))
    status, body = _payload(await parse_task(_Req({"description": "every morning summarize inbox"})))
    assert status == 502
    assert body == {"success": False, "message": "No model endpoint configured"}


@pytest.mark.asyncio
async def test_parse_llm_failure_is_http_502_without_exception_text(parse_task, monkeypatch):
    import src.endpoint_resolver as endpoint_resolver
    import src.llm_core as llm_core

    monkeypatch.setattr(endpoint_resolver, "resolve_endpoint", lambda spec: ("http://llm", "m", {}))

    async def _boom(*a, **k):
        raise RuntimeError("secret-api-key-xyz leaked")

    monkeypatch.setattr(llm_core, "llm_call_async", _boom)
    status, body = _payload(await parse_task(_Req({"description": "every morning summarize inbox"})))
    assert status == 502
    assert body == {"success": False, "message": "Failed to parse task"}
    assert "secret-api-key-xyz" not in json.dumps(body)


@pytest.mark.asyncio
async def test_parse_invalid_llm_json_is_http_502_without_exception_text(parse_task, monkeypatch):
    import src.endpoint_resolver as endpoint_resolver
    import src.llm_core as llm_core

    monkeypatch.setattr(endpoint_resolver, "resolve_endpoint", lambda spec: ("http://llm", "m", {}))

    async def _not_json(*a, **k):
        return "not a task draft: secret-json-error"

    monkeypatch.setattr(llm_core, "llm_call_async", _not_json)
    status, body = _payload(await parse_task(_Req({"description": "every morning summarize inbox"})))
    assert status == 502
    assert body == {"success": False, "message": "Failed to parse task"}
    assert "secret-json-error" not in json.dumps(body)


@pytest.mark.asyncio
async def test_parse_missing_prompt_is_http_502(parse_task, monkeypatch):
    import src.endpoint_resolver as endpoint_resolver
    import src.llm_core as llm_core

    monkeypatch.setattr(endpoint_resolver, "resolve_endpoint", lambda spec: ("http://llm", "m", {}))

    async def _no_prompt(*a, **k):
        return json.dumps({"name": "Inbox digest", "schedule": "daily"})

    monkeypatch.setattr(llm_core, "llm_call_async", _no_prompt)
    status, body = _payload(await parse_task(_Req({"description": "every morning summarize inbox"})))
    assert status == 502
    assert body == {"success": False, "message": "Could not extract a task instruction"}


@pytest.mark.asyncio
async def test_parse_success_still_returns_200(parse_task, monkeypatch):
    import src.endpoint_resolver as endpoint_resolver
    import src.llm_core as llm_core

    monkeypatch.setattr(endpoint_resolver, "resolve_endpoint", lambda spec: ("http://llm", "m", {}))

    async def _ok(*a, **k):
        return json.dumps({
            "task_type": "research",
            "name": "AI news digest",
            "prompt": "Research the top AI news and summarize it.",
            "schedule": "daily",
            "scheduled_time": "07:00",
            "output_target": "email",
        })

    monkeypatch.setattr(llm_core, "llm_call_async", _ok)
    status, body = _payload(await parse_task(_Req({"description": "every morning at 7 research AI news and email me"})))
    assert status == 200
    assert body["success"] is True
    assert body["draft"]["task_type"] == "research"
    assert body["draft"]["name"] == "AI news digest"
    assert body["draft"]["prompt"] == "Research the top AI news and summarize it."
    assert body["draft"]["schedule"] == "daily"
    assert body["draft"]["scheduled_time"] == "07:00"
    assert body["draft"]["output_target"] == "email"
    assert body["draft"]["trigger_type"] == "schedule"
