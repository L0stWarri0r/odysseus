"""Application health assessment regressions (2026-08-08).

Covers post-PR#12 reliability findings that were still open on
lost/personal-core tip 6dc5b4c:
  * SearchService.fetch_content awaited a sync dict-returning helper
  * search/content disk caches used unlocked non-atomic writes
  * webhook fire() spawned unbounded create_task fan-out
  * maintenance status defaulted to ~/odysseus and blocked the event loop
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.search.cache import (
    atomic_write_json,
    generate_cache_key,
    read_json_cache,
)
from services.search.service import SearchService
from src.hermes_control.maintenance import default_odysseus_repo

# conftest.py stubs src.database; drop it so webhook_manager can import the real module.
if "src.database" in sys.modules:
    del sys.modules["src.database"]
if "src.webhook_manager" in sys.modules:
    del sys.modules["src.webhook_manager"]

from src import webhook_manager as wm  # noqa: E402


@pytest.mark.asyncio
async def test_search_service_fetch_content_offloads_sync_helper():
    svc = SearchService(fetch_content=True)
    # Constructor flag must not shadow the async method.
    assert callable(svc.fetch_content)
    assert svc.fetch_content_default is True
    fake = {
        "success": True,
        "content": "  hello from page  ",
        "url": "https://example.com",
    }
    with patch(
        "services.search.service.fetch_webpage_content",
        return_value=fake,
    ) as mocked, patch(
        "services.search.service.asyncio.to_thread",
        new_callable=AsyncMock,
        side_effect=lambda fn, *a, **k: fn(*a, **k),
    ) as to_thread:
        text = await svc.fetch_content("https://example.com/page")

    assert text == "hello from page"
    mocked.assert_called_once_with("https://example.com/page")
    to_thread.assert_awaited()


@pytest.mark.asyncio
async def test_search_service_fetch_content_returns_none_on_failure():
    svc = SearchService()
    with patch(
        "services.search.service.asyncio.to_thread",
        new_callable=AsyncMock,
        return_value={"success": False, "content": "", "error": "boom"},
    ):
        assert await svc.fetch_content("https://example.com/missing") is None


def test_search_cache_atomic_write_and_read(tmp_path, monkeypatch):
    cache_dir = tmp_path / "search"
    cache_dir.mkdir()
    monkeypatch.setattr("services.search.cache.SEARCH_CACHE_DIR", cache_dir)

    cache_file = cache_dir / f"{generate_cache_key('q')}.cache"
    payload = {"timestamp": "2026-08-08T00:00:00", "data": [{"url": "https://x"}]}
    atomic_write_json(cache_file, payload)

    assert cache_file.exists()
    assert not list(cache_dir.glob("*.tmp"))
    assert read_json_cache(cache_file) == payload


def test_content_cache_read_deletes_corrupt_json(tmp_path):
    cache_file = tmp_path / "broken.cache"
    cache_file.write_text("{not-json", encoding="utf-8")
    assert read_json_cache(cache_file) is None
    assert not cache_file.exists()


def test_default_odysseus_repo_uses_checkout_root(monkeypatch):
    monkeypatch.delenv("ODYSSEUS_REPO", raising=False)
    repo = default_odysseus_repo()
    assert (repo / "app.py").exists()
    assert (repo / "src" / "hermes_control" / "maintenance.py").exists()
    assert repo != Path.home() / "odysseus" or (Path.home() / "odysseus" / "app.py").exists()


@pytest.mark.asyncio
async def test_webhook_fire_caps_in_flight_deliveries(monkeypatch):
    manager = wm.WebhookManager(max_in_flight=2)
    manager._client = MagicMock()
    manager._client.post = AsyncMock(return_value=MagicMock(status_code=200))

    class _Wh:
        def __init__(self, wid):
            self.id = wid
            self.url = "https://example.com/hook"
            self.secret = None
            self.events = "chat.completed"
            self.is_active = True

    hooks = [_Wh(f"w{i}") for i in range(5)]

    class _Q:
        def filter(self, *a, **k):
            return self

        def all(self):
            return hooks

        def update(self, *a, **k):
            return 1

    class _DB:
        def query(self, *a, **k):
            return _Q()

        def commit(self):
            return None

        def close(self):
            return None

    monkeypatch.setattr(wm, "SessionLocal", lambda: _DB())
    monkeypatch.setattr(wm, "validate_webhook_url", lambda url: url)

    started = asyncio.Event()
    release = asyncio.Event()
    active = {"n": 0, "peak": 0}

    async def slow_deliver(webhook_id, url, secret, event, payload):
        active["n"] += 1
        active["peak"] = max(active["peak"], active["n"])
        started.set()
        await release.wait()
        active["n"] -= 1

    monkeypatch.setattr(manager, "_deliver", slow_deliver)

    await manager.fire("chat.completed", {"ok": True})
    # Let scheduled tasks start and hit the cap
    await asyncio.sleep(0.05)
    assert manager._in_flight == 2
    assert active["peak"] <= 2

    release.set()
    await asyncio.sleep(0.05)
    assert manager._in_flight == 0


@pytest.mark.asyncio
async def test_maintenance_status_route_offloads_to_thread(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes.hermes_routes import setup_hermes_routes

    class FakeAuthManager:
        is_configured = True

        def is_admin(self, username):
            return username == "admin"

    called = {"to_thread": False}

    async def fake_to_thread(fn, *a, **k):
        called["to_thread"] = True
        return {
            "status": "ok",
            "content_returned": False,
            "repo": {"exists": True, "path": "/tmp"},
            "pwa": {},
            "automation": {},
        }

    monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)

    app = FastAPI()
    app.state.auth_manager = FakeAuthManager()

    @app.middleware("http")
    async def _stamp(request, call_next):
        request.state.current_user = "admin"
        return await call_next(request)

    app.include_router(setup_hermes_routes())
    client = TestClient(app)
    resp = client.get("/api/hermes/maintenance/status")
    assert resp.status_code == 200
    assert called["to_thread"] is True
    assert resp.json()["content_returned"] is False
