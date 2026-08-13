"""Regression tests for the 2026-08-13 application health assessment.

These pin NEW fail-closed ownership and XSS gaps that were still present
on lost/personal-core after prior health PRs #1–#17 (unmerged).
"""

from pathlib import Path
from types import SimpleNamespace

from src.auth_helpers import owns_record

_REPO = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# owns_record — shared fail-closed helper
# ---------------------------------------------------------------------------

def test_owns_record_authenticated_must_match_exactly():
    assert owns_record("alice", "alice") is True
    assert owns_record("bob", "alice") is False
    assert owns_record(None, "alice") is False
    assert owns_record("", "alice") is False


def test_owns_record_unauthenticated_only_unowned_rows():
    assert owns_record(None, None) is True
    assert owns_record("", None) is True
    assert owns_record("", "") is True
    assert owns_record("alice", None) is False
    assert owns_record("alice", "") is False


def test_owns_record_strips_whitespace_usernames():
    assert owns_record(" alice ", "alice") is True
    assert owns_record("  ", None) is True


# ---------------------------------------------------------------------------
# Editor drafts / signatures / notes helpers
# ---------------------------------------------------------------------------

def test_editor_draft_owns_fail_closed(monkeypatch):
    from tests.test_editor_draft_payload import _load_module

    mod = _load_module(monkeypatch)
    alice_draft = SimpleNamespace(owner="alice")
    bob_draft = SimpleNamespace(owner="bob")
    unowned = SimpleNamespace(owner=None)

    assert mod._owns(alice_draft, "alice") is True
    assert mod._owns(bob_draft, "alice") is False
    assert mod._owns(unowned, "alice") is False
    assert mod._owns(unowned, None) is True
    assert mod._owns(alice_draft, None) is False


def test_session_owned_fail_closed():
    from src.ai_interaction import _session_owned

    alice = SimpleNamespace(owner="alice")
    unowned = SimpleNamespace(owner=None)
    assert _session_owned(alice, "alice") is True
    assert _session_owned(alice, "bob") is False
    assert _session_owned(unowned, "alice") is False
    assert _session_owned(unowned, None) is True
    assert _session_owned(None, "alice") is False
    assert _session_owned(None, None) is False


# ---------------------------------------------------------------------------
# XSS: user-controlled labels must go through esc()/escapeHtml()
# ---------------------------------------------------------------------------

def test_chat_js_escapes_role_label_and_tool_sinks():
    src = (_REPO / "static" / "js" / "chat.js").read_text(encoding="utf-8")
    assert "${uiModule.esc(roleLabel)}" in src
    assert "${uiModule.esc(agentModelLabel)}" in src
    assert "${esc(toolLabel)}" in src
    assert "${esc(json.screenshot)}" in src
    assert "${roleLabel} <span class=\"role-timestamp\">" not in src
    assert "${toolLabel}</span><span class=\"agent-thread-wave\">" not in src
    assert 'src="${json.screenshot}"' not in src


def test_group_js_escapes_role_label():
    src = (_REPO / "static" / "js" / "group.js").read_text(encoding="utf-8")
    assert "${uiModule.esc(roleLabel)}" in src
    assert "${roleLabel} <span class=\"role-timestamp\">" not in src


def test_compare_stream_js_escapes_tool_label():
    src = (_REPO / "static" / "js" / "compare" / "stream.js").read_text(encoding="utf-8")
    assert "${escapeHtml(toolLabel)}" in src
    assert "${toolLabel}</span><span class=\"agent-thread-wave\">" not in src
