"""Regression tests for the 2026-08-10 follow-up health assessment.

Covers remaining gaps after the morning cacd pass:
  * email_calendar_extractions / email_urgency_alerts / email_boundaries
    keyed by (message_id, owner)
  * webhook delivery pins TCP peer to resolved public IP
  * CalDAV hostname DNS private-IP rejection
  * SQLite WAL pragma enabled on connect
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest


def test_remaining_email_cache_tables_are_owner_keyed():
    helpers = Path("routes/email_helpers.py").read_text(encoding="utf-8")
    for table in (
        "email_calendar_extractions",
        "email_urgency_alerts",
        "email_boundaries",
    ):
        assert f"{table}__new" in helpers, table
        assert "PRIMARY KEY (message_id, owner)" in helpers

    pollers = Path("routes/email_pollers.py").read_text(encoding="utf-8")
    assert "FROM email_calendar_extractions WHERE owner = ?" in pollers
    assert "FROM email_urgency_alerts WHERE owner = ?" in pollers
    assert "INSERT OR REPLACE INTO email_calendar_extractions" in pollers
    cal_insert = pollers[
        pollers.index("INSERT OR REPLACE INTO email_calendar_extractions") :
        pollers.index("INSERT OR REPLACE INTO email_calendar_extractions") + 220
    ]
    assert "owner" in cal_insert

    routes = Path("routes/email_routes.py").read_text(encoding="utf-8")
    assert "FROM email_boundaries" in routes
    assert "AND owner = ?" in routes[routes.index("FROM email_boundaries") - 80 :
                                     routes.index("FROM email_boundaries") + 160]

    actions = Path("src/builtin_actions.py").read_text(encoding="utf-8")
    assert "FROM email_boundaries WHERE owner = ?" in actions
    assert "(message_id, owner, uid, folder, sig_start" in actions


def test_webhook_delivery_pins_resolved_public_ip():
    src = Path("src/webhook_manager.py").read_text(encoding="utf-8")
    assert "async def _post_to_resolved_public_url" in src
    assert "_pick_public_connect_ip" in src
    assert "server_hostname=hostname if ssl_ctx else None" in src
    assert "await _post_to_resolved_public_url(url, body, headers)" in src
    # Must not fall back to bare httpx re-resolve on the delivery path.
    deliver = src[src.index("async def _deliver") : src.index("async def close")]
    assert "self._client.post" not in deliver


def test_caldav_validates_hostname_dns(monkeypatch):
    from src import caldav_sync

    monkeypatch.delenv("ODYSSEUS_ALLOW_PRIVATE_CALDAV", raising=False)
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 0))
        ],
    )
    with pytest.raises(ValueError, match="not allowed"):
        caldav_sync.validate_caldav_url("https://meta.example/dav")


def test_sqlite_wal_pragma_enabled():
    src = Path("core/database.py").read_text(encoding="utf-8")
    assert "PRAGMA journal_mode=WAL" in src
    assert "PRAGMA synchronous=NORMAL" in src


@pytest.mark.asyncio
async def test_webhook_post_helper_uses_pinned_ip(monkeypatch):
    import src.webhook_manager as wm

    monkeypatch.setattr(wm, "_pick_public_connect_ip", lambda host: "203.0.113.10")

    opened = {}

    class _Writer:
        def __init__(self):
            self.buf = b""

        def write(self, data):
            self.buf += data

        async def drain(self):
            return None

        def close(self):
            return None

        async def wait_closed(self):
            return None

    class _Reader:
        def __init__(self):
            self._lines = [b"HTTP/1.1 204 No Content\r\n", b"\r\n"]

        async def readline(self):
            return self._lines.pop(0) if self._lines else b""

    async def fake_open(host, port, ssl=None, server_hostname=None):
        opened["host"] = host
        opened["port"] = port
        opened["server_hostname"] = server_hostname
        return _Reader(), _Writer()

    monkeypatch.setattr(wm.asyncio, "open_connection", fake_open)
    status = await wm._post_to_resolved_public_url(
        "https://hooks.example/hook",
        '{"ok":true}',
        {"Content-Type": "application/json", "X-Odysseus-Event": "webhook.test"},
    )
    assert status == 204
    assert opened["host"] == "203.0.113.10"
    assert opened["port"] == 443
    assert opened["server_hostname"] == "hooks.example"
