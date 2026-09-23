"""Health assessment 2026-09-23: calendar delete must not look like success."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.responses import JSONResponse

from routes.calendar_routes import calendar_error, setup_calendar_routes

_REPO = Path(__file__).resolve().parents[1]
_CALENDAR_ROUTES = (_REPO / "routes" / "calendar_routes.py").read_text(encoding="utf-8")
_CALENDAR_JS = (_REPO / "static" / "js" / "calendar.js").read_text(encoding="utf-8")


def _delete_source() -> str:
    start = _CALENDAR_ROUTES.index("async def delete_calendar")
    end = _CALENDAR_ROUTES.index("_ICS_MAX_BYTES", start)
    return _CALENDAR_ROUTES[start:end]


def _route_endpoint(path: str, method: str):
    method = method.upper()
    router = setup_calendar_routes()
    for route in router.routes:
        if route.path == path and method in getattr(route, "methods", set()):
            return route.endpoint
    raise AssertionError(f"route not found: {method} {path}")


def _payload(res):
    if isinstance(res, JSONResponse):
        return res.status_code, json.loads(res.body)
    return 200, res


class _Req:
    def __init__(self, *, user="alice"):
        self.state = SimpleNamespace(current_user=user)


class _Query:
    def __init__(self, row):
        self._row = row
        self.deleted = False

    def filter(self, *args, **kwargs):
        return self

    def first(self):
        return self._row

    def delete(self):
        self.deleted = True
        return 1


class _Session:
    def __init__(self, row, *, commit_error=None):
        self.row = row
        self.commit_error = commit_error
        self.deleted = None
        self.rolled_back = False
        self.closed = False
        self.event_query = _Query(None)

    def query(self, model):
        name = getattr(model, "__name__", "")
        if name == "CalendarEvent":
            return self.event_query
        return _Query(self.row)

    def delete(self, obj):
        self.deleted = obj

    def commit(self):
        if self.commit_error:
            raise self.commit_error

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


def test_calendar_error_helper_returns_json_error_with_non_200_status():
    assert "def calendar_error(message: str, status_code: int = 502)" in _CALENDAR_ROUTES
    resp = calendar_error("Failed to delete calendar", status_code=500)
    assert resp.status_code == 500
    assert json.loads(resp.body) == {"ok": False, "error": "Failed to delete calendar"}


def test_delete_calendar_uses_calendar_error_instead_of_http_200():
    src = _delete_source()
    assert 'return calendar_error("Failed to delete calendar", status_code=500)' in src
    assert 'return {"error": str(e)}' not in src
    assert "logger.error" in src


def test_delete_calendar_ui_does_not_treat_http_errors_as_success():
    start = _CALENDAR_JS.index("delBtn.addEventListener('click'")
    end = _CALENDAR_JS.index("// ICS import", start)
    src = _CALENDAR_JS[start:end]
    assert "if (!res.ok || !data.ok)" in src
    assert "credentials: 'same-origin'" in src
    assert "row.remove();" in src
    assert "await fetch(`${API_BASE}/api/calendar/calendars/${id}`, { method: 'DELETE' });\n      row.remove();" not in src


@pytest.mark.asyncio
async def test_delete_calendar_exception_is_http_500_without_exception_text(monkeypatch):
    import routes.calendar_routes as calendar_routes

    session = _Session(
        SimpleNamespace(id="cal-1", owner="alice", name="Work"),
        commit_error=RuntimeError("secret-sql /data/app.db leaked"),
    )
    monkeypatch.setattr(calendar_routes, "SessionLocal", lambda: session)
    delete_calendar = _route_endpoint("/api/calendar/calendars/{cal_id}", "DELETE")
    status, body = _payload(await delete_calendar(_Req(), "cal-1"))
    assert status == 500
    assert body == {"ok": False, "error": "Failed to delete calendar"}
    assert "secret-sql" not in json.dumps(body)
    assert "/data/app.db" not in json.dumps(body)
    assert session.rolled_back is True
    assert session.closed is True


@pytest.mark.asyncio
async def test_delete_calendar_success_still_returns_200(monkeypatch):
    import routes.calendar_routes as calendar_routes

    cal = SimpleNamespace(id="cal-1", owner="alice", name="Work")
    session = _Session(cal)
    monkeypatch.setattr(calendar_routes, "SessionLocal", lambda: session)
    delete_calendar = _route_endpoint("/api/calendar/calendars/{cal_id}", "DELETE")
    status, body = _payload(await delete_calendar(_Req(), "cal-1"))
    assert status == 200
    assert body == {"ok": True}
    assert session.deleted is cal
    assert session.event_query.deleted is True
    assert session.closed is True


@pytest.mark.asyncio
async def test_delete_missing_calendar_stays_404(monkeypatch):
    import routes.calendar_routes as calendar_routes

    session = _Session(None)
    monkeypatch.setattr(calendar_routes, "SessionLocal", lambda: session)
    delete_calendar = _route_endpoint("/api/calendar/calendars/{cal_id}", "DELETE")
    with pytest.raises(HTTPException) as exc:
        await delete_calendar(_Req(), "missing")
    assert exc.value.status_code == 404
    assert session.closed is True
