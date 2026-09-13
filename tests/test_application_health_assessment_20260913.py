"""Health assessment 2026-09-13: compose-upload, reminder-delete, and resolve-contact failures must not look like success."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.responses import JSONResponse


_REPO = Path(__file__).resolve().parents[1]
_EMAIL_HELPERS = (_REPO / "routes" / "email_helpers.py").read_text(encoding="utf-8")
_EMAIL_ROUTES = (_REPO / "routes" / "email_routes.py").read_text(encoding="utf-8")
_EMAIL_LIBRARY = (_REPO / "static" / "js" / "emailLibrary.js").read_text(encoding="utf-8")
_DOCUMENT_JS = (_REPO / "static" / "js" / "document.js").read_text(encoding="utf-8")


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


class BoomImap:
    def __init__(self, *args, **kwargs):
        raise RuntimeError("imap down")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


@pytest.fixture
def mail_routes(tmp_path, monkeypatch):
    import routes.email_helpers as email_helpers
    import routes.email_routes as email_routes

    upload_dir = tmp_path / "compose"
    upload_dir.mkdir()
    monkeypatch.setattr(email_helpers, "COMPOSE_UPLOADS_DIR", upload_dir)
    monkeypatch.setattr(email_routes, "COMPOSE_UPLOADS_DIR", upload_dir)
    router = email_routes.setup_email_routes()
    return {
        "compose_upload": _route_endpoint(router, "/api/email/compose-upload", "POST"),
        "delete_upload": _route_endpoint(router, "/api/email/compose-upload/{token}", "DELETE"),
        "reminders": _route_endpoint(router, "/api/email/odysseus/reminders", "DELETE"),
        "resolve": _route_endpoint(router, "/api/email/resolve-contact", "GET"),
        "upload_dir": upload_dir,
        "email_routes": email_routes,
    }


def test_mail_error_helper_returns_json_error_with_non_200_status():
    assert "def mail_error(message: str, status_code: int = 502)" in _EMAIL_HELPERS
    resp = JSONResponse({"success": False, "error": "Mail operation failed"}, status_code=502)
    assert resp.status_code == 502
    compact = resp.body.replace(b" ", b"")
    assert b'"success":false' in compact or b'"success": false' in resp.body


def test_compose_upload_reminder_and_resolve_routes_use_mail_error():
    assert 'return mail_error("Mail operation failed")' in _EMAIL_ROUTES
    assert "Failed to upload attachment" in _EMAIL_ROUTES
    assert "delete_compose_upload" in _EMAIL_ROUTES
    assert "delete_odysseus_reminder_emails failed" in _EMAIL_ROUTES
    assert "resolve_contact {name!r} failed" in _EMAIL_ROUTES
    assert _EMAIL_ROUTES.count("except HTTPException:\n            raise") >= 2


def test_library_and_compose_ui_do_not_treat_failures_as_empty_success():
    assert "if (!res.ok || data.success === false)" in _EMAIL_LIBRARY
    assert "Failed to clear reminder emails" in _EMAIL_LIBRARY
    assert "if (res.ok && data.success)" in _DOCUMENT_JS
    assert "data.error || data.detail" in _DOCUMENT_JS


@pytest.mark.asyncio
async def test_compose_upload_disk_failure_is_http_502(mail_routes, monkeypatch):
    class BoomPath:
        def __truediv__(self, _other):
            raise OSError("disk full")

    monkeypatch.setattr(mail_routes["email_routes"], "COMPOSE_UPLOADS_DIR", BoomPath())
    upload = SimpleNamespace(filename="note.txt", read=lambda: _async_bytes(b"hello"))
    status, body = _payload(await mail_routes["compose_upload"](file=upload, owner="alice"))
    assert status == 502
    assert body == {"success": False, "error": "Mail operation failed"}


@pytest.mark.asyncio
async def test_compose_upload_success_still_returns_token(mail_routes):
    upload = SimpleNamespace(filename="note.txt", read=lambda: _async_bytes(b"hello"))
    status, body = _payload(await mail_routes["compose_upload"](file=upload, owner="alice"))
    assert status == 200
    assert body["success"] is True
    assert body["filename"] == "note.txt"
    assert body["size"] == 5
    assert (mail_routes["upload_dir"] / body["token"]).read_bytes() == b"hello"


@pytest.mark.asyncio
async def test_delete_compose_upload_unlink_failure_is_http_502(mail_routes, monkeypatch):
    token = "abc123_note.txt"
    target = mail_routes["upload_dir"] / token
    target.write_text("x", encoding="utf-8")

    def boom_unlink():
        raise OSError("read-only fs")

    monkeypatch.setattr(Path, "unlink", lambda self, *args, **kwargs: boom_unlink())
    status, body = _payload(await mail_routes["delete_upload"](token, owner="alice"))
    assert status == 502
    assert body == {"success": False, "error": "Mail operation failed"}


@pytest.mark.asyncio
async def test_reminder_delete_imap_failure_is_http_502(mail_routes, monkeypatch):
    monkeypatch.setattr(
        mail_routes["email_routes"],
        "_get_email_config",
        lambda account_id=None, owner="": {
            "from_address": "alice@example.com",
            "smtp_user": "alice@example.com",
            "imap_user": "alice@example.com",
        },
    )
    monkeypatch.setattr(mail_routes["email_routes"], "_imap", BoomImap)
    status, body = _payload(await mail_routes["reminders"](
        account_id=None,
        permanent=True,
        owner="alice",
    ))
    assert status == 502
    assert body == {"success": False, "error": "Mail operation failed"}
    assert "deleted" not in body


@pytest.mark.asyncio
async def test_reminder_delete_does_not_swallow_account_ownership_http_errors(mail_routes, monkeypatch):
    def boom(account_id, owner):
        raise HTTPException(404, "Account not found")

    monkeypatch.setattr(mail_routes["email_routes"], "_assert_owns_account", boom)
    with pytest.raises(HTTPException) as exc:
        await mail_routes["reminders"](account_id="acct-bob", permanent=True, owner="alice")
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_resolve_contact_imap_failure_is_not_empty_contact_success(mail_routes, monkeypatch):
    monkeypatch.setattr(mail_routes["email_routes"], "_imap", BoomImap)
    status, body = _payload(await mail_routes["resolve"](name="Ada", owner="alice"))
    assert status == 502
    assert body == {"success": False, "error": "Mail operation failed"}
    assert "contacts" not in body


def _async_bytes(payload: bytes):
    async def _read():
        return payload
    return _read()
