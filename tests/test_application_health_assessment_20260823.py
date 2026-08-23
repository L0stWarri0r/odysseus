"""Health assessment 2026-08-23: Hermes auth, local-lane aliases, PWA cache
scope, inventory bounds, incognito→private_mode, and CardDAV href origin pin.
"""
from pathlib import Path

import pytest

from routes.contacts_routes import _abs_url
from src.hermes_control.continuity import (
    _INVENTORY_MAX_FILES,
    _OUTSIDE_BASE,
    _relative,
    build_continuity_inventory,
)
from src.hermes_control.models import HermesDecision, HermesRequestContext
from src.hermes_control.policy import evaluate
from src.hermes_control.routing import is_local_endpoint


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:1234/v1",
        "http://localhost:1234/v1",
        "http://localhost.:1234/v1",
        "http://127.1:1234/v1",
        "http://[::1]:1234/v1",
        "http://[::ffff:127.0.0.1]:1234/v1",
        "http://host.docker.internal:1234/v1",
        "http://0.0.0.0:1234/v1",
    ],
)
def test_is_local_endpoint_covers_loopback_aliases(url):
    assert is_local_endpoint(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "https://api.openai.com/v1",
        "http://192.168.1.10:11434/v1",
        "http://example.com:1234/v1",
        "",
        None,
    ],
)
def test_is_local_endpoint_does_not_treat_cloud_or_lan_as_loopback(url):
    assert is_local_endpoint(url) is False


def test_loopback_alias_still_disables_web_for_local_lane():
    result = evaluate(
        HermesRequestContext(
            message="Search the web",
            session_id="s-alias",
            endpoint_url="http://127.1:1234/v1",
            model="local-model",
            use_web=True,
        )
    )
    assert result.decision == HermesDecision.ALLOW_WITH_ADJUSTMENTS
    assert result.adjusted_context["use_web"] is False


def test_inventory_relative_path_does_not_leak_escaped_absolute(tmp_path):
    base = tmp_path / "hermes"
    base.mkdir()
    outside = tmp_path / "secret-elsewhere.txt"
    outside.write_text("nope", encoding="utf-8")
    leaked = _relative(outside, base)
    assert leaked == _OUTSIDE_BASE
    assert str(outside) not in leaked
    assert "secret-elsewhere" not in leaked


def test_inventory_rglob_truncates_huge_profile_trees(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    profile_dir = home / "user-profile"
    profile_dir.mkdir(parents=True)
    for i in range(_INVENTORY_MAX_FILES + 5):
        (profile_dir / f"note-{i}.md").write_text("x", encoding="utf-8")

    inventory = build_continuity_inventory(home)

    assert inventory["profile_markdown_truncated"] is True
    assert inventory["profile_markdown_count"] == _INVENTORY_MAX_FILES
    assert len(inventory["profile_markdown_files"]) == _INVENTORY_MAX_FILES


def test_carddav_abs_url_pins_href_to_configured_origin():
    base = "https://dav.example.com/user/contacts/"
    assert (
        _abs_url("/user/contacts/abc.vcf", base)
        == "https://dav.example.com/user/contacts/abc.vcf"
    )
    assert (
        _abs_url("https://dav.example.com/user/contacts/abc.vcf", base)
        == "https://dav.example.com/user/contacts/abc.vcf"
    )
    assert _abs_url("https://evil.example/steal.vcf", base) is None
    assert _abs_url("//evil.example/steal.vcf", base) is None
    assert _abs_url("https://user:pass@dav.example.com/user/contacts/x.vcf", base) is None
    assert _abs_url("https://169.254.169.254/latest/meta-data", base) is None
    assert _abs_url("file:///etc/passwd", base) is None


def test_contacts_config_validates_carddav_url():
    text = Path("routes/contacts_routes.py").read_text(encoding="utf-8")
    assert "validate_caldav_url(str(raw))" in text
    assert "follow_redirects=False, trust_env=False" in text
    assert "follow_redirects=False" in text


def test_sw_activate_only_deletes_odysseus_caches():
    text = (ROOT / "static" / "sw.js").read_text(encoding="utf-8")
    assert "k.startsWith('odysseus-') && k !== CACHE_NAME" in text
    assert "keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k))" not in text


def test_chat_and_compare_send_private_mode_with_incognito():
    chat = (ROOT / "static" / "js" / "chat.js").read_text(encoding="utf-8")
    compare = (ROOT / "static" / "js" / "compare" / "stream.js").read_text(encoding="utf-8")
    assert "fd.append('private_mode', 'true')" in chat
    assert "fd.append('incognito', 'true')" in chat
    assert "fd.append('private_mode', 'true')" in compare


def test_hermes_routes_gate_inventory_and_preflight():
    text = Path("routes/hermes_routes.py").read_text(encoding="utf-8")
    assert text.count("_require_admin(request)") >= 3
    assert "async def hermes_preflight(request: Request" in text
    assert "async def hermes_continuity_inventory(request: Request)" in text
