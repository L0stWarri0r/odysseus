"""Regression tests for 2026-08-12 health assessment (NEW gaps vs 0b04).

Covers issues still present on lost/personal-core @ 6dc5b4c that prior
health PRs did not close:
  - memory extract/import session IDOR
  - document null-owner session bypass
  - compare endpoint API-key owner scoping
  - MCP OAuth credential path confinement
  - CardDAV URL validation
  - chat tool_start toolLabel escaping
  - send_to_session null-owner IDOR + KeyError
  - chat screenshot / compare toolLabel / group roleLabel XSS
  - backup import owner spoof
  - email null-owner account gates
  - builtin SSH StrictHostKeyChecking + option-like host reject
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

REPO = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


# --- memory session ownership ---------------------------------------------

def test_memory_extract_asserts_session_ownership():
    src = _read("routes/memory_routes.py")
    assert "_assert_session_owned" in src
    assert "_verify_session_owner" in src
    # extract must gate before loading history
    extract_idx = src.index("async def extract_memory")
    body = src[extract_idx: extract_idx + 400]
    assert "_assert_session_owned(request, session)" in body
    assert body.index("_assert_session_owned") < body.index("get_session(session)")


def test_memory_import_asserts_session_ownership():
    src = _read("routes/memory_routes.py")
    import_idx = src.index("async def import_memories_from_file")
    body = src[import_idx: import_idx + 900]
    assert "_assert_session_owned(request, session)" in body


# --- document null-owner session gate -------------------------------------

def test_document_routes_reject_null_owner_sessions():
    src = _read("routes/document_routes.py")
    # Classic bypass was `session.owner and session.owner != user`
    assert "session.owner and session.owner != user" not in src
    assert "sess.owner and sess.owner != user" not in src
    assert "if user and session.owner != user" in src
    assert "if user and sess.owner != user" in src


# --- compare owner-scoped API key copy ------------------------------------

def test_compare_copies_api_key_with_owner_filter():
    src = _read("routes/compare_routes.py")
    assert "owner_filter" in src
    assert "owner_filter(q, ModelEndpoint, user)" in src


# --- MCP OAuth path confinement -------------------------------------------

def test_mcp_oauth_write_confined_to_data_dir():
    src = _read("routes/mcp_routes.py")
    assert "data" in src and "mcp_oauth" in src
    assert "os.path.basename" in src
    assert "OAuth credential directory is not allowed" in src
    # Must not write directly to expanduser(oauth_dir) anymore
    assert "os.makedirs(oauth_dir, exist_ok=True)" not in src


# --- CardDAV URL validation -----------------------------------------------

def test_carddav_config_validates_url():
    src = _read("routes/contacts_routes.py")
    assert "validate_caldav_url" in src
    cfg_idx = src.index("async def update_config")
    body = src[cfg_idx: cfg_idx + 700]
    assert "validate_caldav_url" in body
    assert "carddav_url" in body


# --- chat toolLabel XSS ---------------------------------------------------

def test_chat_tool_start_escapes_tool_label():
    src = _read("static/js/chat.js")
    assert "const toolLabel = esc(_toolLabels[json.tool.toLowerCase()] || json.tool);" in src


def test_chat_screenshot_src_is_escaped():
    src = _read("static/js/chat.js")
    assert 'src="${esc(json.screenshot)}"' in src
    assert 'src="${json.screenshot}"' not in src


def test_compare_tool_start_escapes_tool_label():
    src = _read("static/js/compare/stream.js")
    assert "const toolLabel = escapeHtml(_toolLabels[toolName.toLowerCase()] || toolName);" in src


def test_group_bubble_escapes_role_label():
    src = _read("static/js/group.js")
    idx = src.index("function _createGroupBubble")
    body = src[idx: idx + 500]
    assert "uiModule.esc(" in body


# --- send_to_session null-owner -------------------------------------------

def test_send_to_session_owner_check_is_fail_closed():
    src = _read("src/ai_interaction.py")
    idx = src.index("async def do_send_to_session")
    body = src[idx: idx + 1200]
    assert "except KeyError" in body
    # Soft pattern must be gone
    assert "getattr(sess, \"owner\", None) and sess.owner != owner" not in body
    assert "getattr(sess, \"owner\", None) != owner" in body


def test_do_send_to_session_rejects_null_owner(monkeypatch):
    import src.ai_interaction as ai

    sess = SimpleNamespace(
        owner=None,
        endpoint_url="http://x",
        model="m",
        headers={},
        get_context_messages=lambda: [],
    )
    sm = MagicMock()
    sm.get_session.return_value = sess
    monkeypatch.setattr(ai, "_session_manager", sm)

    out = asyncio.run(ai.do_send_to_session("sid1\nhello", owner="alice"))
    assert "error" in out
    assert "not found" in out["error"].lower()


def test_do_send_to_session_handles_missing_session(monkeypatch):
    import src.ai_interaction as ai

    sm = MagicMock()
    sm.get_session.side_effect = KeyError("missing")
    monkeypatch.setattr(ai, "_session_manager", sm)

    out = asyncio.run(ai.do_send_to_session("sid1\nhello", owner="alice"))
    assert "error" in out


# --- backup import owner spoof --------------------------------------------

def test_backup_import_always_stamps_owner():
    src = _read("routes/backup_routes.py")
    assert 'if user and not mem.get("owner"):' not in src
    assert 'if user and not skill.get("owner"):' not in src
    assert 'mem["owner"] = user' in src
    assert 'skill["owner"] = user' in src


# --- email null-owner gates -----------------------------------------------

def test_email_assert_owns_account_fail_closed():
    src = _read("routes/email_helpers.py")
    assert "if row.owner and row.owner != owner:" not in src
    assert "if row.owner != owner:" in src
    assert "if row is not None and owner and row.owner and row.owner != owner:" not in src
    assert "if row is not None and owner and row.owner != owner:" in src


# --- builtin SSH hardening ------------------------------------------------

def test_builtin_ssh_uses_accept_new_and_rejects_option_hosts():
    src = _read("src/builtin_actions.py")
    assert "StrictHostKeyChecking=accept-new" in src
    assert "def _ssh_remote_argv" in src
    from src.builtin_actions import _ssh_remote_argv
    argv = _ssh_remote_argv("user@host.example", "uptime")
    assert "StrictHostKeyChecking=accept-new" in argv
    with pytest.raises(ValueError):
        _ssh_remote_argv("-oProxyCommand=evil", "id")


# --- smoke: helpers parse -------------------------------------------------

def test_memory_routes_ast_parses():
    ast.parse(_read("routes/memory_routes.py"))


def test_mcp_routes_ast_parses():
    ast.parse(_read("routes/mcp_routes.py"))


def test_contacts_routes_ast_parses():
    ast.parse(_read("routes/contacts_routes.py"))


def test_ai_interaction_ast_parses():
    ast.parse(_read("src/ai_interaction.py"))


def test_builtin_actions_ast_parses():
    ast.parse(_read("src/builtin_actions.py"))
