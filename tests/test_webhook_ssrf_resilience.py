import sys
import json
from datetime import datetime

# conftest.py stubs src.database with a fake module; webhook_manager imports
# from it, so drop the stub here to load the real module under test.
if "src.database" in sys.modules:
    del sys.modules["src.database"]

import pytest
from src.webhook_manager import validate_webhook_url, _pick_public_connect_ip


def test_webhook_url_ssrf_mitigation():
    # SSRF bypasses that must be rejected, including IPv6 unspecified and
    # IPv4-mapped IPv6 (loopback + cloud metadata).
    private_urls = [
        "http://[::]/",
        "http://[::ffff:127.0.0.1]/",
        "http://[::ffff:169.254.169.254]/",
        "http://127.0.0.1/",
        "http://0.0.0.0/",
    ]
    for url in private_urls:
        with pytest.raises(ValueError) as exc:
            validate_webhook_url(url)
        assert "private/internal addresses" in str(exc.value)

    # A clearly public IP literal must still be accepted.
    public_url = "http://93.184.216.34/"
    assert validate_webhook_url(public_url) == public_url


def test_pick_public_connect_ip_rejects_private_records(monkeypatch):
    import ipaddress
    import src.webhook_manager as wm

    monkeypatch.setattr(
        wm,
        "_resolve_hostname_ips",
        lambda host: [ipaddress.ip_address("10.0.0.8")],
    )
    with pytest.raises(ValueError, match="private/internal"):
        _pick_public_connect_ip("evil.example")


def test_pick_public_connect_ip_accepts_public_literal():
    assert _pick_public_connect_ip("93.184.216.34") == "93.184.216.34"


@pytest.mark.asyncio
async def test_webhook_delivery_uses_naive_utc_timestamps(monkeypatch):
    import src.webhook_manager as wm

    class _Query:
        def __init__(self, updates):
            self.updates = updates

        def filter(self, *_args, **_kwargs):
            return self

        def update(self, values):
            self.updates.append(values)

    class _Db:
        def __init__(self):
            self.updates = []
            self.committed = False
            self.closed = False

        def query(self, _model):
            return _Query(self.updates)

        def commit(self):
            self.committed = True

        def rollback(self):
            pass

        def close(self):
            self.closed = True

    captured = {}

    async def fake_post(url, body, headers):
        captured["url"] = url
        captured["body"] = body
        captured["headers"] = headers
        return 204

    db = _Db()
    monkeypatch.setattr(wm, "SessionLocal", lambda: db)
    monkeypatch.setattr(wm, "_post_to_resolved_public_url", fake_post)

    manager = wm.WebhookManager()
    await manager._client.aclose()

    await manager._deliver("hook-1", "http://93.184.216.34/", None, "webhook.test", {"ok": True})

    body = json.loads(captured["body"])
    payload_timestamp = datetime.fromisoformat(body["timestamp"])
    assert payload_timestamp.tzinfo is None
    assert captured["headers"]["X-Odysseus-Event"] == "webhook.test"
    assert db.updates[0]["last_triggered_at"].tzinfo is None
    assert db.updates[0]["last_status_code"] == 204
    assert db.committed is True
    assert db.closed is True
