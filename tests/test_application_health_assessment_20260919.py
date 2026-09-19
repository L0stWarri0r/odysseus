"""Health assessment 2026-09-19: CalDAV tests must not look like success."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi.responses import JSONResponse

from routes.calendar_routes import calendar_error, setup_calendar_routes

_REPO = Path(__file__).resolve().parents[1]
_CALENDAR_ROUTES = (_REPO / "routes" / "calendar_routes.py").read_text(encoding="utf-8")
_SETTINGS_JS = (_REPO / "static" / "js" / "settings.js").read_text(encoding="utf-8")
_ADMIN_JS = (_REPO / "static" / "js" / "admin.js").read_text(encoding="utf-8")


def _test_connection_source() -> str:
    start = _CALENDAR_ROUTES.index("async def test_connection")
    end = _CALENDAR_ROUTES.index("\n    @router.post(\"/sync\")", start)
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


def _user_request(body=None, user="alice"):
    async def _json():
        if body is None:
            raise ValueError("no body")
        return body

    return SimpleNamespace(
        state=SimpleNamespace(current_user=user),
        json=_json,
        cookies={},
        client=SimpleNamespace(host="127.0.0.1"),
        app=SimpleNamespace(state=SimpleNamespace(auth_manager=None)),
    )


class _FakeResp:
    def __init__(self, status_code=207, text="", headers=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}


class _FakeClient:
    def __init__(self, resp=None, error=None):
        self._resp = resp
        self._error = error
        self.requests = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        if self._error:
            raise self._error
        return self._resp


@pytest.fixture
def test_endpoint():
    router = setup_calendar_routes()
    return _route_endpoint(router, "/api/calendar/test", "POST")


def test_calendar_error_helper_returns_json_error_with_non_200_status():
    assert "def calendar_error(message: str, status_code: int = 502)" in _CALENDAR_ROUTES
    resp = calendar_error("CalDAV connection failed")
    assert resp.status_code == 502
    body = json.loads(resp.body)
    assert body == {"ok": False, "error": "CalDAV connection failed"}


def test_caldav_test_uses_calendar_error_instead_of_http_200():
    src = _test_connection_source()
    assert "return calendar_error(" in src
    assert 'return {"ok": False, "error":' not in src
    assert "str(e)" not in src
    assert 'f"Connection refused: {e}"' not in src
    assert "return calendar_error(str(e)" not in src
    assert "follow_redirects=False, trust_env=False" in src
    assert "Redirects are not followed for CalDAV safety" in src


def test_caldav_ui_does_not_treat_test_failures_as_success():
    assert "const ok = r.ok && d.ok;" in _SETTINGS_JS
    assert "return { ok, error: d.error };" in _SETTINGS_JS
    assert "return await r.json();" not in _SETTINGS_JS.split("const _runCalDavTest")[1].split("const _setCalDavMsg")[0]
    assert "const ok = res.ok && d.ok;" in _ADMIN_JS
    assert "status.textContent = d.ok ? `Connected (${d.calendars} calendars)`" not in _ADMIN_JS


@pytest.mark.asyncio
async def test_missing_credentials_is_http_400(test_endpoint, monkeypatch):
    import routes.prefs_routes as prefs_routes

    monkeypatch.setattr(prefs_routes, "_load_for_user", lambda _owner: {})
    status, body = _payload(await test_endpoint(_user_request({})))
    assert status == 400
    assert body == {"ok": False, "error": "Missing URL, username, or password"}


@pytest.mark.asyncio
async def test_invalid_url_is_http_400_without_validation_text(test_endpoint):
    status, body = _payload(
        await test_endpoint(
            _user_request(
                {
                    "url": "http://localhost:5232/dav",
                    "username": "alice",
                    "password": "secret-caldav-pass",
                }
            )
        )
    )
    assert status == 400
    assert body == {"ok": False, "error": "Invalid CalDAV URL"}
    dumped = json.dumps(body)
    assert "host is not allowed" not in dumped
    assert "localhost" not in dumped
    assert "secret-caldav-pass" not in dumped


@pytest.mark.asyncio
async def test_http_failure_is_http_502_without_response_body(test_endpoint, monkeypatch):
    fake = _FakeClient(
        resp=_FakeResp(status_code=500, text="secret-caldav-body xyz")
    )
    client_kwargs = {}

    def _client(**kwargs):
        client_kwargs.update(kwargs)
        return fake

    monkeypatch.setattr(httpx, "AsyncClient", _client)
    status, body = _payload(
        await test_endpoint(
            _user_request(
                {
                    "url": "https://calendar.example.com/dav",
                    "username": "alice",
                    "password": "pw",
                }
            )
        )
    )
    assert status == 502
    assert body == {"ok": False, "error": "HTTP 500"}
    assert "secret-caldav-body" not in json.dumps(body)
    assert client_kwargs.get("follow_redirects") is False
    assert client_kwargs.get("trust_env") is False
    assert fake.requests[0][0] == "PROPFIND"
    assert fake.requests[0][1] == "https://calendar.example.com/dav"


@pytest.mark.asyncio
async def test_auth_failure_is_http_502(test_endpoint, monkeypatch):
    fake = _FakeClient(resp=_FakeResp(status_code=401, text="www-authenticate-secret"))

    def _client(**kwargs):
        return fake

    monkeypatch.setattr(httpx, "AsyncClient", _client)
    status, body = _payload(
        await test_endpoint(
            _user_request(
                {
                    "url": "https://calendar.example.com/dav",
                    "username": "alice",
                    "password": "wrong",
                }
            )
        )
    )
    assert status == 502
    assert body == {"ok": False, "error": "Auth failed — check username/password"}
    assert "www-authenticate-secret" not in json.dumps(body)


@pytest.mark.asyncio
async def test_not_found_is_http_502(test_endpoint, monkeypatch):
    fake = _FakeClient(resp=_FakeResp(status_code=404, text="calendar-not-here"))

    def _client(**kwargs):
        return fake

    monkeypatch.setattr(httpx, "AsyncClient", _client)
    status, body = _payload(
        await test_endpoint(
            _user_request(
                {
                    "url": "https://calendar.example.com/missing",
                    "username": "alice",
                    "password": "pw",
                }
            )
        )
    )
    assert status == 502
    assert body == {"ok": False, "error": "Not found — check the URL path"}
    assert "calendar-not-here" not in json.dumps(body)


@pytest.mark.asyncio
async def test_redirect_is_http_502(test_endpoint, monkeypatch):
    fake = _FakeClient(
        resp=_FakeResp(status_code=302, headers={"location": "http://169.254.169.254/"})
    )

    def _client(**kwargs):
        return fake

    monkeypatch.setattr(httpx, "AsyncClient", _client)
    status, body = _payload(
        await test_endpoint(
            _user_request(
                {
                    "url": "https://calendar.example.com/dav",
                    "username": "alice",
                    "password": "pw",
                }
            )
        )
    )
    assert status == 502
    assert body["ok"] is False
    assert "Redirects are not followed" in body["error"]
    assert "169.254.169.254" not in json.dumps(body)


@pytest.mark.asyncio
async def test_connect_error_is_http_502_without_exception_text(test_endpoint, monkeypatch):
    fake = _FakeClient(error=httpx.ConnectError("proxy-secret-token leaked"))

    def _client(**kwargs):
        return fake

    monkeypatch.setattr(httpx, "AsyncClient", _client)
    status, body = _payload(
        await test_endpoint(
            _user_request(
                {
                    "url": "https://calendar.example.com/dav",
                    "username": "alice",
                    "password": "pw",
                }
            )
        )
    )
    assert status == 502
    assert body == {"ok": False, "error": "Connection refused"}
    assert "proxy-secret-token" not in json.dumps(body)


@pytest.mark.asyncio
async def test_timeout_is_http_502(test_endpoint, monkeypatch):
    fake = _FakeClient(error=httpx.TimeoutException("secret-timeout-detail"))

    def _client(**kwargs):
        return fake

    monkeypatch.setattr(httpx, "AsyncClient", _client)
    status, body = _payload(
        await test_endpoint(
            _user_request(
                {
                    "url": "https://calendar.example.com/dav",
                    "username": "alice",
                    "password": "pw",
                }
            )
        )
    )
    assert status == 502
    assert body == {"ok": False, "error": "Connection timed out"}
    assert "secret-timeout-detail" not in json.dumps(body)


@pytest.mark.asyncio
async def test_generic_exception_is_http_502_without_exception_text(test_endpoint, monkeypatch):
    fake = _FakeClient(error=RuntimeError("tls-cert-secret leaked"))

    def _client(**kwargs):
        return fake

    monkeypatch.setattr(httpx, "AsyncClient", _client)
    status, body = _payload(
        await test_endpoint(
            _user_request(
                {
                    "url": "https://calendar.example.com/dav",
                    "username": "alice",
                    "password": "pw",
                }
            )
        )
    )
    assert status == 502
    assert body == {"ok": False, "error": "CalDAV connection failed"}
    assert "tls-cert-secret" not in json.dumps(body)


@pytest.mark.asyncio
async def test_success_still_returns_200(test_endpoint, monkeypatch):
    fake = _FakeClient(resp=_FakeResp(status_code=207, text="ok"))

    def _client(**kwargs):
        return fake

    monkeypatch.setattr(httpx, "AsyncClient", _client)
    status, body = _payload(
        await test_endpoint(
            _user_request(
                {
                    "url": "https://calendar.example.com/dav/",
                    "username": "alice",
                    "password": "pw",
                }
            )
        )
    )
    assert status == 200
    assert body == {"ok": True}
    assert fake.requests[0][1] == "https://calendar.example.com/dav"


@pytest.mark.asyncio
async def test_saved_credentials_are_used_when_body_is_incomplete(test_endpoint, monkeypatch):
    import routes.prefs_routes as prefs_routes
    import src.secret_storage as secret_storage

    monkeypatch.setattr(
        prefs_routes,
        "_load_for_user",
        lambda owner: {
            "caldav": {
                "url": "https://calendar.example.com/dav",
                "username": owner,
                "password": "enc:stored",
            }
        },
    )
    monkeypatch.setattr(
        secret_storage,
        "decrypt",
        lambda value: "decrypted-password" if value == "enc:stored" else value,
    )
    fake = _FakeClient(resp=_FakeResp(status_code=200, text="ok"))

    def _client(**kwargs):
        return fake

    monkeypatch.setattr(httpx, "AsyncClient", _client)
    status, body = _payload(await test_endpoint(_user_request({})))
    assert status == 200
    assert body == {"ok": True}
    auth = fake.requests[0][2]["auth"]
    assert auth == ("alice", "decrypted-password")
