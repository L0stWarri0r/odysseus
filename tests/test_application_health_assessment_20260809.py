"""Application health assessment regressions (2026-08-09).

Covers new findings on lost/personal-core tip after re-landing PR #13:
  * AI tool model resolution must not use other tenants' ModelEndpoint keys
  * Research start/spinoff must owner-scope endpoint lookup
  * Incognito chat must not DB-persist messages via Session.add_message
  * list_served_models must be admin-gated (exposes /proc cmdline)
  * Gallery image endpoint lookup must be owner-scoped
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from core.models import ChatMessage, Session
from routes.gallery_helpers import _find_owned_image_endpoint
from src.tool_security import NON_ADMIN_BLOCKED_TOOLS, is_public_blocked_tool


def test_list_served_models_blocked_for_non_admins():
    assert "list_served_models" in NON_ADMIN_BLOCKED_TOOLS
    assert is_public_blocked_tool("list_served_models") is True


def test_incognito_add_message_skips_persist():
    sess = Session(
        id="incog-1",
        name="Incognito",
        endpoint_url="http://localhost:8000/v1/chat/completions",
        model="test",
    )
    fake_mgr = MagicMock()
    with patch("core.models._session_manager", fake_mgr):
        sess.add_message(ChatMessage("user", "secret"), persist=False)
        sess.add_message(ChatMessage("assistant", "reply"), persist=True)

    assert len(sess.history) == 2
    fake_mgr._persist_message.assert_called_once()
    assert fake_mgr._persist_message.call_args[0][0] == "incog-1"
    assert fake_mgr._persist_message.call_args[0][1].role == "assistant"


def test_chat_helpers_incognito_does_not_persist():
    from routes.chat_helpers import add_user_message, save_assistant_response

    sess = Session(
        id="incog-2",
        name="Incognito",
        endpoint_url="http://localhost:8000/v1/chat/completions",
        model="test",
    )
    pre = SimpleNamespace(
        user_content="hello private",
        attachment_meta=None,
        text_for_context="hello private",
    )
    chat_handler = MagicMock()
    session_manager = MagicMock()
    fake_mgr = MagicMock()

    with patch("core.models._session_manager", fake_mgr):
        add_user_message(sess, chat_handler, pre, incognito=True)
        save_assistant_response(
            sess,
            session_manager,
            "incog-2",
            "assistant says hi",
            last_metrics={},
            incognito=True,
        )

    assert len(sess.history) == 2
    fake_mgr._persist_message.assert_not_called()
    session_manager.save_sessions.assert_not_called()
    chat_handler.update_session_name_if_needed.assert_not_called()


def test_resolve_model_scopes_endpoints_by_owner():
    from src.ai_interaction import _resolve_model

    alice_ep = SimpleNamespace(
        name="Alice Local",
        base_url="http://127.0.0.1:9001/v1",
        api_key="alice-secret",
        owner="alice",
        is_enabled=True,
    )
    bob_ep = SimpleNamespace(
        name="Bob Cloud",
        base_url="https://api.openai.com/v1",
        api_key="bob-secret",
        owner="bob",
        is_enabled=True,
    )

    class _Q:
        def __init__(self, rows):
            self.rows = list(rows)

        def filter(self, *args, **kwargs):
            # owner_filter adds an OR expression; keep rows whose owner matches
            # or is None. For name.ilike we keep all in this stub.
            return self

        def all(self):
            return self.rows

    class _DB:
        def query(self, model):
            return _Q([alice_ep, bob_ep])

        def close(self):
            return None

    def _owner_filter(query, model_cls, user, *, include_shared=True):
        kept = [
            r
            for r in query.rows
            if r.owner == user or (include_shared and r.owner is None)
        ]
        query.rows = kept
        return query

    with patch("src.database.SessionLocal", return_value=_DB()), patch(
        "src.auth_helpers.owner_filter", side_effect=_owner_filter
    ), patch(
        "src.ai_interaction.build_headers",
        side_effect=lambda key, base: {"Authorization": f"Bearer {key}"},
    ), patch(
        "src.llm_core._detect_provider", return_value="openai"
    ), patch(
        "httpx.get"
    ) as http_get:
        http_get.return_value = MagicMock(
            raise_for_status=MagicMock(),
            json=MagicMock(return_value={"data": [{"id": "gpt-test"}]}),
        )
        url, model, headers = _resolve_model("gpt-test", owner="alice")

    assert model == "gpt-test"
    assert "9001" in url
    assert headers["Authorization"] == "Bearer alice-secret"
    # Bob's endpoint must not be probed when owner=alice.
    assert http_get.call_count == 1


def test_research_owned_endpoint_helper_filters_foreign_rows():
    from routes.research_routes import _owned_enabled_endpoint

    class _EP:
        id = "id"
        owner = "owner"
        is_enabled = True

        def __init__(self, id, owner):
            self.id = id
            self.owner = owner
            self.is_enabled = True

    alice = _EP("ep-a", "alice")
    bob = _EP("ep-b", "bob")

    class _Q:
        def __init__(self, rows):
            self.rows = list(rows)

        def filter(self, *args, **kwargs):
            return self

        def first(self):
            return self.rows[0] if self.rows else None

    class _DB:
        def __init__(self, rows):
            self.rows = rows

        def query(self, model):
            return _Q(self.rows)

    def _of(query, model_cls, user, *, include_shared=True):
        query.rows = [r for r in query.rows if r.owner == user]
        return query

    with patch("routes.research_routes.owner_filter", side_effect=_of), patch(
        "src.database.ModelEndpoint", _EP
    ):
        assert _owned_enabled_endpoint(_DB([alice, bob]), None, "alice") is alice
        # After owner filter, bob-only list yields nothing for alice.
        assert _owned_enabled_endpoint(_DB([bob]), "ep-b", "alice") is None


def test_find_owned_image_endpoint_scopes_by_owner():
    alice = SimpleNamespace(
        id="img-a",
        owner="alice",
        is_enabled=True,
        model_type="image",
        base_url="http://127.0.0.1:7860/v1",
        api_key="alice-img",
    )
    bob = SimpleNamespace(
        id="img-b",
        owner="bob",
        is_enabled=True,
        model_type="image",
        base_url="http://127.0.0.1:7861/v1",
        api_key="bob-img",
    )

    class _Q:
        def __init__(self, rows):
            self.rows = list(rows)

        def filter(self, *args, **kwargs):
            # Keep image-type rows when model_type filter is applied; our stub
            # already only holds image endpoints.
            return self

        def first(self):
            return self.rows[0] if self.rows else None

        def all(self):
            return list(self.rows)

    class _DB:
        def query(self, model):
            return _Q([alice, bob])

    def _of(query, model_cls, user, *, include_shared=True):
        query.rows = [r for r in query.rows if r.owner == user]
        return query

    with patch("src.auth_helpers.owner_filter", side_effect=_of):
        ep = _find_owned_image_endpoint(_DB(), "alice")
        assert ep is alice
        assert ep.api_key == "alice-img"

        ep_url = _find_owned_image_endpoint(
            _DB(), "alice", endpoint_url="http://127.0.0.1:7861/v1"
        )
        assert ep_url is None  # bob's URL must not resolve for alice
