"""Health assessment 2026-09-09: owner-scope email writing style and tasks.

One user's Extract Style / AI Reply / scheduled summarize-draft-calendar
pass used a single global settings.json key and iterated every enabled
mailbox. Concurrent one-shot tasks also flipped those global auto_* flags
on disk, racing each other and applying Alice's run to Bob's IMAP account.
"""
from pathlib import Path

import pytest


def _route_endpoint(router, path: str, method: str):
    method = method.upper()
    for route in router.routes:
        if route.path == path and method in getattr(route, "methods", set()):
            return route.endpoint
    raise AssertionError(f"route not found: {method} {path}")


def test_email_account_visible_to_owner_isolates_tenants():
    from routes.email_helpers import _email_account_visible_to_owner

    assert _email_account_visible_to_owner("", "bob", "b@x", "b@x") is True
    assert _email_account_visible_to_owner("alice", "alice", "a@x", "a@x") is True
    assert _email_account_visible_to_owner("alice", "bob", "b@x", "b@x") is False
    assert _email_account_visible_to_owner("alice", "", "alice", "alice@x") is True
    assert _email_account_visible_to_owner("alice", "", "carol", "carol@x") is False


def test_writing_style_is_owner_scoped(tmp_path, monkeypatch):
    import routes.email_helpers as helpers
    import routes.prefs_routes as prefs_routes
    import src.settings as settings_mod

    prefs_file = tmp_path / "user_prefs.json"
    settings_file = tmp_path / "settings.json"
    settings_file.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(prefs_routes, "PREFS_FILE", str(prefs_file))
    monkeypatch.setattr(helpers, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", str(settings_file))
    settings_mod._settings_cache = None

    helpers._save_writing_style_for_owner("", "GLOBAL DEFAULT")
    settings_mod._settings_cache = None
    assert helpers._writing_style_for_owner("") == "GLOBAL DEFAULT"
    assert helpers._writing_style_for_owner("alice") == "GLOBAL DEFAULT"

    helpers._save_writing_style_for_owner("alice", "Hi from Alice")
    helpers._save_writing_style_for_owner("bob", "Hi from Bob")
    settings_mod._settings_cache = None

    assert helpers._writing_style_for_owner("alice") == "Hi from Alice"
    assert helpers._writing_style_for_owner("bob") == "Hi from Bob"
    assert helpers._writing_style_for_owner("") == "GLOBAL DEFAULT"
    assert helpers._load_settings().get("email_writing_style") == "GLOBAL DEFAULT"


@pytest.mark.asyncio
async def test_style_routes_do_not_clobber_other_users(tmp_path, monkeypatch):
    import routes.email_helpers as helpers
    import routes.email_routes as email_routes
    import routes.prefs_routes as prefs_routes
    import src.settings as settings_mod

    prefs_file = tmp_path / "user_prefs.json"
    settings_file = tmp_path / "settings.json"
    settings_file.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(prefs_routes, "PREFS_FILE", str(prefs_file))
    monkeypatch.setattr(helpers, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", str(settings_file))
    settings_mod._settings_cache = None

    router = email_routes.setup_email_routes()
    get_style = _route_endpoint(router, "/api/email/style", "GET")
    put_style = _route_endpoint(router, "/api/email/style", "PUT")

    await put_style({"style": "Alice voice"}, owner="alice")
    await put_style({"style": "Bob voice"}, owner="bob")

    alice = await get_style(owner="alice")
    bob = await get_style(owner="bob")
    assert alice["style"] == "Alice voice"
    assert bob["style"] == "Bob voice"


@pytest.mark.asyncio
async def test_run_auto_summarize_once_does_not_mutate_settings(monkeypatch):
    import routes.email_pollers as email_pollers

    save_calls = []
    monkeypatch.setattr(
        "routes.email_helpers._save_settings",
        lambda s: save_calls.append(dict(s)),
    )
    captured = {}

    async def fake_pass(**kwargs):
        captured.update(kwargs)
        return "ok"

    monkeypatch.setattr(email_pollers, "_auto_summarize_pass", fake_pass)

    result = await email_pollers._run_auto_summarize_once(
        do_summary=True, do_reply=False, owner="alice",
    )

    assert result == "ok"
    assert save_calls == []
    assert captured["owner"] == "alice"
    assert captured["flag_overrides"]["email_auto_summarize"] is True
    assert captured["flag_overrides"]["email_auto_reply"] is False
    assert captured["flag_overrides"]["email_auto_calendar"] is False


@pytest.mark.asyncio
async def test_auto_summarize_pass_only_touches_owner_accounts(monkeypatch):
    import routes.email_pollers as email_pollers

    def accounts_for(owner=""):
        if owner == "alice":
            return [("alice-1", "Alice")]
        if owner == "carol":
            return []
        return [("alice-1", "Alice"), ("bob-1", "Bob")]

    monkeypatch.setattr(email_pollers, "_enabled_email_accounts", accounts_for)
    seen = []

    async def fake_single(**kwargs):
        seen.append(kwargs.get("account_id"))
        return "ok"

    monkeypatch.setattr(email_pollers, "_auto_summarize_pass_single", fake_single)

    result = await email_pollers._auto_summarize_pass(owner="alice")
    assert seen == ["alice-1"]
    assert "bob" not in result.lower()

    seen.clear()
    empty = await email_pollers._auto_summarize_pass(owner="carol")
    assert seen == []
    assert empty == "No email accounts"


@pytest.mark.asyncio
async def test_flag_overrides_bypass_disabled_global_settings(monkeypatch):
    import routes.email_pollers as email_pollers

    captured = {}

    class _Conn:
        def logout(self):
            captured["logout"] = True

        def select(self, *args, **kwargs):
            raise RuntimeError("stop after connect")

    def fake_connect(*args, **kwargs):
        captured["connected"] = True
        return _Conn()

    monkeypatch.setattr(email_pollers, "_load_settings", lambda: {
        "email_auto_summarize": False,
        "email_auto_reply": False,
        "email_auto_tag": False,
        "email_auto_spam": False,
        "email_auto_calendar": False,
    })
    monkeypatch.setattr(email_pollers, "_imap_connect", fake_connect)
    monkeypatch.setattr(email_pollers, "_owner_for_email_account", lambda account_id: "alice")

    skipped = await email_pollers._auto_summarize_pass_single(account_id="acct-1")
    assert skipped == "Nothing to do"
    assert "connected" not in captured

    ran = await email_pollers._auto_summarize_pass_single(
        account_id="acct-1",
        flag_overrides={"email_auto_summarize": True},
    )
    assert captured.get("connected") is True
    assert ran.startswith("Error:")


@pytest.mark.asyncio
async def test_summarize_and_draft_actions_pass_owner(monkeypatch):
    import src.builtin_actions as builtin_actions

    captured = []

    async def fake_once(**kwargs):
        captured.append(kwargs)
        return "Processed 3 emails"

    monkeypatch.setattr(
        "routes.email_pollers._run_auto_summarize_once",
        fake_once,
    )

    summary, ok = await builtin_actions.action_summarize_emails("alice")
    assert ok is True
    assert summary == "Processed 3 emails"
    assert captured[0]["owner"] == "alice"
    assert captured[0]["do_summary"] is True
    assert captured[0]["do_reply"] is False

    drafts, ok = await builtin_actions.action_draft_email_replies("bob")
    assert ok is True
    assert captured[1]["owner"] == "bob"
    assert captured[1]["do_reply"] is True
    assert captured[1]["do_summary"] is False
