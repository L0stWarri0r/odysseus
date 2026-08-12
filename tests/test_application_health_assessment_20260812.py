"""Regression tests for 2026-08-12 health assessment (NEW gaps vs 0b04).

Covers issues still present on lost/personal-core @ 6dc5b4c that prior
health PRs did not close:
  - memory extract/import session IDOR
  - document null-owner session bypass
  - compare endpoint API-key owner scoping
  - MCP OAuth credential path confinement
  - CardDAV URL validation
  - chat tool_start toolLabel escaping
"""

from __future__ import annotations

import ast
from pathlib import Path

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


# --- smoke: helpers parse -------------------------------------------------

def test_memory_routes_ast_parses():
    ast.parse(_read("routes/memory_routes.py"))


def test_mcp_routes_ast_parses():
    ast.parse(_read("routes/mcp_routes.py"))


def test_contacts_routes_ast_parses():
    ast.parse(_read("routes/contacts_routes.py"))
