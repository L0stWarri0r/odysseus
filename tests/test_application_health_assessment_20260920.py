"""Health assessment 2026-09-20: calendar quick-parse must not look like success."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.responses import JSONResponse

from routes.calendar_routes import calendar_error, setup_calendar_routes

_REPO = Path(__file__).resolve().parents[1]
_CALENDAR_ROUTES = (_REPO / "routes" / "calendar_routes.py").read_text(encoding="utf-8")
_CALENDAR_JS = (_REPO / "static" / "js" / "calendar.js").read_text(encoding="utf-8")


def _quick_parse_source() -> str:
    start = _CALENDAR_ROUTES.index("async def quick_parse")
    end = _CALENDAR_ROUTES.index("\n    return router", start)
    return _CALENDAR_ROUTES[start:end]


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
    def __init__(self, payload, *, json_error=None, user="alice"):
        self._payload = payload
        self._json_error = json_error
        self.state = SimpleNamespace(current_user=user)

    async def json(self):
        if self._json_error:
            raise self._json_error
        return self._payload


@pytest.fixture
def quick_parse():
    router = setup_calendar_routes()
    return _route_endpoint(router, "/api/calendar/quick-parse", "POST")


def test_calendar_error_helper_returns_json_error_with_non_200_status():
    assert "def calendar_error(message: str, status_code: int = 502)" in _CALENDAR_ROUTES
    resp = calendar_error("LLM call failed")
    assert resp.status_code == 502
    body = json.loads(resp.body)
    assert body == {"ok": False, "error": "LLM call failed"}


def test_quick_parse_uses_calendar_error_instead_of_http_200():
    src = _quick_parse_source()
    assert 'return calendar_error("text is required", 400)' in src
    assert 'return calendar_error("No LLM endpoint configured")' in src
    assert 'return calendar_error("LLM call failed")' in src
    assert 'return calendar_error("Could not extract JSON")' in src
    assert 'return calendar_error("Invalid JSON")' in src
    assert 'return calendar_error("Model did not produce a start time")' in src
    assert 'return {"ok": False, "error": "No LLM endpoint configured"}' not in src
    assert 'f"LLM call failed: {e}"' not in src
    assert '"raw": cleaned[:400]' not in src
    assert 'raise HTTPException(400, "text is required")' not in src


def test_calendar_ui_does_not_treat_quick_parse_failures_as_success():
    assert "if (!res.ok || !data.ok)" in _CALENDAR_JS
    assert "data.error || data.detail || `HTTP ${res.status}`" in _CALENDAR_JS
    assert "const data = await res.json();\n        if (!data.ok)" not in _CALENDAR_JS


@pytest.mark.asyncio
async def test_parse_empty_text_is_http_400(quick_parse):
    status, body = _payload(await quick_parse(_Req({"text": "   "})))
    assert status == 400
    assert body == {"ok": False, "error": "text is required"}


@pytest.mark.asyncio
async def test_parse_missing_text_is_http_400(quick_parse):
    status, body = _payload(await quick_parse(_Req({})))
    assert status == 400
    assert body == {"ok": False, "error": "text is required"}


@pytest.mark.asyncio
async def test_parse_non_object_body_is_http_400(quick_parse):
    status, body = _payload(await quick_parse(_Req(["lunch friday"])))
    assert status == 400
    assert body == {"ok": False, "error": "text is required"}


@pytest.mark.asyncio
async def test_parse_non_string_text_is_http_400(quick_parse):
    status, body = _payload(await quick_parse(_Req({"text": 12})))
    assert status == 400
    assert body == {"ok": False, "error": "text is required"}


@pytest.mark.asyncio
async def test_parse_invalid_json_body_is_http_400(quick_parse):
    status, body = _payload(await quick_parse(_Req(None, json_error=ValueError("not json"))))
    assert status == 400
    assert body == {"ok": False, "error": "text is required"}


@pytest.mark.asyncio
async def test_parse_missing_endpoint_is_http_502(quick_parse, monkeypatch):
    import src.endpoint_resolver as endpoint_resolver

    monkeypatch.setattr(endpoint_resolver, "resolve_endpoint", lambda spec: (None, None, {}))
    status, body = _payload(await quick_parse(_Req({"text": "lunch with sara friday 1pm"})))
    assert status == 502
    assert body == {"ok": False, "error": "No LLM endpoint configured"}


@pytest.mark.asyncio
async def test_parse_llm_failure_is_http_502_without_exception_text(quick_parse, monkeypatch):
    import src.endpoint_resolver as endpoint_resolver
    import src.llm_core as llm_core

    monkeypatch.setattr(endpoint_resolver, "resolve_endpoint", lambda spec: ("http://llm", "m", {}))

    async def _boom(*a, **k):
        raise RuntimeError("secret-api-key-xyz leaked")

    monkeypatch.setattr(llm_core, "llm_call_async", _boom)
    status, body = _payload(await quick_parse(_Req({"text": "lunch with sara friday 1pm"})))
    assert status == 502
    assert body == {"ok": False, "error": "LLM call failed"}
    assert "secret-api-key-xyz" not in json.dumps(body)
    assert "raw" not in body


@pytest.mark.asyncio
async def test_parse_non_json_llm_output_is_http_502_without_raw(quick_parse, monkeypatch):
    import src.endpoint_resolver as endpoint_resolver
    import src.llm_core as llm_core

    monkeypatch.setattr(endpoint_resolver, "resolve_endpoint", lambda spec: ("http://llm", "m", {}))

    async def _not_json(*a, **k):
        return "not a calendar event: secret-raw-dump"

    monkeypatch.setattr(llm_core, "llm_call_async", _not_json)
    status, body = _payload(await quick_parse(_Req({"text": "lunch with sara friday 1pm"})))
    assert status == 502
    assert body == {"ok": False, "error": "Could not extract JSON"}
    assert "secret-raw-dump" not in json.dumps(body)
    assert "raw" not in body


@pytest.mark.asyncio
async def test_parse_invalid_llm_json_is_http_502_without_exception_text(quick_parse, monkeypatch):
    import src.endpoint_resolver as endpoint_resolver
    import src.llm_core as llm_core

    monkeypatch.setattr(endpoint_resolver, "resolve_endpoint", lambda spec: ("http://llm", "m", {}))

    async def _bad_json(*a, **k):
        return '{summary: "Lunch", secret: "secret-json-error"}'

    monkeypatch.setattr(llm_core, "llm_call_async", _bad_json)
    status, body = _payload(await quick_parse(_Req({"text": "lunch with sara friday 1pm"})))
    assert status == 502
    assert body == {"ok": False, "error": "Invalid JSON"}
    assert "secret-json-error" not in json.dumps(body)
    assert "raw" not in body


@pytest.mark.asyncio
async def test_parse_missing_start_time_is_http_502_without_raw(quick_parse, monkeypatch):
    import src.endpoint_resolver as endpoint_resolver
    import src.llm_core as llm_core

    monkeypatch.setattr(endpoint_resolver, "resolve_endpoint", lambda spec: ("http://llm", "m", {}))

    async def _no_start(*a, **k):
        return json.dumps({"summary": "Lunch downtown secret-start-leak", "all_day": False})

    monkeypatch.setattr(llm_core, "llm_call_async", _no_start)
    status, body = _payload(await quick_parse(_Req({"text": "lunch with sara friday 1pm"})))
    assert status == 502
    assert body == {"ok": False, "error": "Model did not produce a start time"}
    assert "secret-start-leak" not in json.dumps(body)
    assert "raw" not in body


@pytest.mark.asyncio
async def test_parse_success_still_returns_200(quick_parse, monkeypatch):
    import src.endpoint_resolver as endpoint_resolver
    import src.llm_core as llm_core

    monkeypatch.setattr(endpoint_resolver, "resolve_endpoint", lambda spec: ("http://llm", "m", {}))

    async def _ok(*a, **k):
        return json.dumps({
            "summary": "Lunch with Sara",
            "dtstart": "2026-09-25T13:00:00",
            "dtend": "2026-09-25T14:00:00",
            "all_day": False,
            "location": "downtown",
            "description": "",
            "confidence": 0.9,
        })

    monkeypatch.setattr(llm_core, "llm_call_async", _ok)
    status, body = _payload(await quick_parse(_Req({
        "text": "lunch with sara friday 1pm downtown",
        "tz": "America/New_York",
    })))
    assert status == 200
    assert body["ok"] is True
    assert body["event"]["summary"] == "Lunch with Sara"
    assert body["event"]["dtstart"] == "2026-09-25T13:00:00"
    assert body["event"]["dtend"] == "2026-09-25T14:00:00"
    assert body["event"]["all_day"] is False
    assert body["event"]["location"] == "downtown"
    assert body["confidence"] == 0.9


@pytest.mark.asyncio
async def test_parse_fills_default_end_and_ignores_bad_confidence(quick_parse, monkeypatch):
    import src.endpoint_resolver as endpoint_resolver
    import src.llm_core as llm_core

    monkeypatch.setattr(endpoint_resolver, "resolve_endpoint", lambda spec: ("http://llm", "m", {}))

    async def _ok(*a, **k):
        return json.dumps({
            "summary": "Standup",
            "dtstart": "2026-09-21T09:00:00",
            "all_day": False,
            "location": ["room 4"],
            "description": None,
            "confidence": "high",
        })

    monkeypatch.setattr(llm_core, "llm_call_async", _ok)
    status, body = _payload(await quick_parse(_Req({"text": "standup tomorrow 9am"})))
    assert status == 200
    assert body["ok"] is True
    assert body["event"]["dtend"] == "2026-09-21T10:00:00"
    assert body["event"]["location"] == ""
    assert body["event"]["description"] == ""
    assert body["confidence"] == 0.7
