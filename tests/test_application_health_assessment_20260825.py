"""Health assessment 2026-08-25: Hermes defaults, local-lane detection, inventory gates."""

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.hermes_control.continuity import (
    _INVENTORY_FILE_CAP,
    _relative,
    build_continuity_inventory,
    default_hermes_home,
)
from src.hermes_control.maintenance import default_odysseus_repo
from src.hermes_control.models import HermesDecision, HermesRequestContext
from src.hermes_control.policy import evaluate
from src.hermes_control.routing import is_local_endpoint


ROOT = Path(__file__).resolve().parents[1]


def test_default_hermes_home_uses_unix_path_when_nothing_exists(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setattr("src.hermes_control.continuity.os.name", "posix")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    home = default_hermes_home()

    assert home == tmp_path / ".hermes"
    assert "AppData" not in str(home)


def test_default_hermes_home_prefers_existing_unix_dir(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    existing = tmp_path / ".hermes"
    existing.mkdir()

    assert default_hermes_home() == existing


def test_default_odysseus_repo_is_the_running_checkout(monkeypatch):
    monkeypatch.delenv("ODYSSEUS_REPO", raising=False)
    repo = default_odysseus_repo()
    assert repo == ROOT
    assert (repo / "app.py").is_file()
    assert (repo / "static" / "sw.js").is_file()


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:1234/v1",
        "http://127.1:1234/v1",
        "http://localhost:11434/v1",
        "http://localhost.:11434/v1",
        "http://[::1]:1234/v1",
        "http://[::ffff:127.0.0.1]:1234/v1",
        "http://0.0.0.0:8000/v1",
        "http://host.docker.internal:11434/v1",
    ],
)
def test_is_local_endpoint_accepts_loopback_aliases(url):
    assert is_local_endpoint(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "https://api.openai.com/v1",
        "http://192.168.1.10:11434/v1",
        "http://10.0.0.5:8000/v1",
        "http://ollama.example.com/v1",
        "",
        None,
    ],
)
def test_is_local_endpoint_rejects_non_loopback(url):
    assert is_local_endpoint(url) is False


def test_private_mode_on_docker_internal_host_is_opaque():
    result = evaluate(
        HermesRequestContext(
            message="OPENAI_API_KEY=sk-testsecret12345",
            session_id="s-docker",
            endpoint_url="http://host.docker.internal:11434/v1",
            model="local-model",
            private_mode=True,
            use_web=True,
        )
    )
    assert result.content_visible_to_hermes is False
    assert result.findings == []
    assert result.decision == HermesDecision.ALLOW_WITH_ADJUSTMENTS
    assert result.adjusted_context["use_web"] is False


def test_relative_hides_paths_outside_base(tmp_path):
    base = tmp_path / "hermes"
    base.mkdir()
    outside = tmp_path / "secret-name.txt"
    outside.write_text("nope", encoding="utf-8")
    link = base / "escaped.md"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks not supported")
    assert _relative(link, base) == "[outside-base]"
    assert str(outside) not in _relative(link, base)


def test_inventory_caps_profile_markdown_and_skips_symlink_escape(tmp_path):
    home = tmp_path / "hermes"
    profile = home / "user-profile"
    profile.mkdir(parents=True)
    for i in range(_INVENTORY_FILE_CAP + 25):
        (profile / f"note-{i:03d}.md").write_text("x", encoding="utf-8")
    outside = tmp_path / "outside.md"
    outside.write_text("escaped-content", encoding="utf-8")
    try:
        (profile / "escape-dir").symlink_to(tmp_path, target_is_directory=True)
    except OSError:
        pass

    inventory = build_continuity_inventory(home)

    assert inventory["profile_markdown_count"] == _INVENTORY_FILE_CAP
    assert "escaped-content" not in str(inventory)
    leaked = [
        item["relative_path"]
        for item in inventory["profile_markdown_files"]
        if item["relative_path"] == "[outside-base]" or os.path.isabs(item["relative_path"])
    ]
    assert not [p for p in leaked if os.path.isabs(p)]


def test_service_worker_activate_only_deletes_odysseus_caches():
    sw = (ROOT / "static" / "sw.js").read_text(encoding="utf-8")
    assert "k.startsWith('odysseus-') && k !== CACHE_NAME" in sw
    assert "keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k))" not in sw


def test_chat_and_compare_send_private_mode_with_incognito():
    chat_js = (ROOT / "static" / "js" / "chat.js").read_text(encoding="utf-8")
    compare_js = (ROOT / "static" / "js" / "compare" / "stream.js").read_text(encoding="utf-8")
    assert "fd.append('private_mode', 'true')" in chat_js
    assert "fd.append('private_mode', 'true')" in compare_js
    assert "fd.append('incognito', 'true')" in chat_js
    assert "fd.append('incognito', 'true')" in compare_js
