"""Regression tests for the 2026-08-11 application health assessment.

Closes remaining deferred gaps after the cacd re-land:
  * sender_signatures keyed by (owner, from_address)
  * CalDAV connect-time DNS pin for sync + writeback
  * SessionManager rename/archive/create/delete lock coverage
  * Chat UI auto-sends Hermes private_mode for local endpoints
  * is_local_endpoint recognizes common localhost aliases
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from src.hermes_control.routing import is_local_endpoint


def test_sender_signatures_are_owner_keyed():
    helpers = Path("routes/email_helpers.py").read_text(encoding="utf-8")
    assert "sender_signatures__new" in helpers
    assert "PRIMARY KEY (owner, from_address)" in helpers

    actions = Path("src/builtin_actions.py").read_text(encoding="utf-8")
    assert "WHERE owner = ?" in actions[
        actions.index("FROM sender_signatures") :
        actions.index("FROM sender_signatures") + 160
    ]
    assert "(owner, from_address, signature_text" in actions

    routes = Path("routes/email_routes.py").read_text(encoding="utf-8")
    sig_block = routes[
        routes.index("FROM sender_signatures") - 40 :
        routes.index("FROM sender_signatures") + 160
    ]
    assert "AND owner = ?" in sig_block

    tasks = Path("routes/task_routes.py").read_text(encoding="utf-8")
    assert "sender_signatures" in tasks
    assert '"sender_signatures"' in tasks or "sender_signatures" in tasks
    assert "DELETE FROM {table} WHERE owner = ? OR owner = ''" in tasks


def test_caldav_connect_pins_dns_for_sync_and_writeback(monkeypatch):
    from src import caldav_sync

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))],
    )
    ip = caldav_sync._pick_caldav_connect_ip("calendar.example.com")
    assert ip == "93.184.216.34"

    src = Path("src/caldav_sync.py").read_text(encoding="utf-8")
    assert "_PinnedCalDAVDNS" in src
    assert "_make_caldav_client" in src
    assert "with dns_pin:" in src

    wb = Path("src/caldav_writeback.py").read_text(encoding="utf-8")
    assert "_make_caldav_client" in wb
    assert "with dns_pin:" in wb


def test_caldav_pick_connect_ip_rejects_private(monkeypatch):
    from src import caldav_sync

    monkeypatch.delenv("ODYSSEUS_ALLOW_PRIVATE_CALDAV", raising=False)
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.9", 0))],
    )
    with pytest.raises(ValueError, match="Private CalDAV"):
        caldav_sync._pick_caldav_connect_ip("cal.internal.example")


def test_session_manager_mutation_paths_use_lock():
    src = Path("core/session_manager.py").read_text(encoding="utf-8")
    for method in (
        "def update_session_name",
        "def archive_session",
        "def mark_important",
        "def create_session",
        "def delete_session",
        "def get_sessions_for_user",
    ):
        assert method in src
    # Rename/archive must update RAM under the lock after DB commit.
    assert src.count("with self._lock:") >= 6


def test_chat_js_sends_private_mode_for_local_endpoints():
    chat = Path("static/js/chat.js").read_text(encoding="utf-8")
    assert "private_mode" in chat
    assert "getCurrentEndpointUrl" in chat
    assert "fd.append('private_mode', 'true')" in chat


@pytest.mark.parametrize(
    "url, expected",
    [
        ("http://localhost:8080/v1", True),
        ("http://localhost.:8080/v1", True),
        ("http://127.0.0.1:5000/v1", True),
        ("http://127.1:5000/v1", True),
        ("http://127.2.3.4:5000/v1", True),
        ("http://[::1]:5000/v1", True),
        ("http://[::ffff:127.0.0.1]:5000/v1", True),
        ("http://foo.localhost/v1", True),
        ("https://api.openai.com/v1", False),
        ("", False),
        (None, False),
    ],
)
def test_is_local_endpoint_aliases(url, expected):
    assert is_local_endpoint(url) is expected
