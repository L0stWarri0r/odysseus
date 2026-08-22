"""Health assessment 2026-08-22 — outbound URL pinning and SSH argv.

These gaps were still on lost/personal-core after PRs #16–#18:
  * CardDAV REPORT hrefs were used as-is, so PUT/DELETE (and Basic auth)
    could be redirected off-host.
  * Vaultwarden server URLs were passed to `bw config server` unvalidated.
  * DALL-E image downloads followed redirects onto private/link-local hops.
  * Cookbook SSH still interpolated host/script into a local shell string.
"""
from __future__ import annotations

import ast
from pathlib import Path
from urllib.parse import urlparse

import pytest

from routes.contacts_routes import _abs_url
from routes.cookbook_helpers import _ssh_argv
from routes.vault_routes import _validate_vault_server_url
from src.url_security import fetch_public_http_bytes, is_public_http_url


REPO = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


@pytest.fixture
def carddav_origin(monkeypatch):
    monkeypatch.setattr(
        "routes.contacts_routes._get_carddav_config",
        lambda: {
            "url": "https://dav.example.com:5232/user/contacts/",
            "username": "alice",
            "password": "secret",
        },
    )


def test_carddav_abs_url_joins_relative_href(carddav_origin):
    assert _abs_url("/user/contacts/abc.vcf") == "https://dav.example.com:5232/user/contacts/abc.vcf"
    assert _abs_url("xyz.vcf") == "https://dav.example.com:5232/xyz.vcf"


def test_carddav_abs_url_allows_same_origin(carddav_origin):
    href = "https://dav.example.com:5232/user/contacts/abc.vcf"
    assert _abs_url(href) == href


def test_carddav_abs_url_rejects_off_host_absolute_href(carddav_origin):
    assert _abs_url("https://evil.example/steal") is None
    assert _abs_url("http://dav.example.com:5232/user/contacts/abc.vcf") is None
    assert _abs_url("//evil.example/steal") is None
    assert _abs_url("https://alice:secret@dav.example.com:5232/x.vcf") is None


def test_carddav_abs_url_rejects_empty_or_unconfigured(monkeypatch):
    monkeypatch.setattr("routes.contacts_routes._get_carddav_config", lambda: {"url": ""})
    assert _abs_url("/x.vcf") is None
    monkeypatch.setattr(
        "routes.contacts_routes._get_carddav_config",
        lambda: {"url": "https://dav.example.com/contacts"},
    )
    assert _abs_url("") is None
    assert _abs_url("   ") is None


@pytest.mark.parametrize(
    "url",
    [
        "ftp://vault.example.com",
        "file:///etc/passwd",
        "https://alice:secret@vault.example.com",
        "https://vault.example.com/path#frag",
        "http://localhost:8080",
        "http://127.0.0.1:8080",
        "http://[::1]:8080",
        "http://169.254.169.254/latest",
        "http://metadata.google.internal/",
        "http://vault.localhost/",
    ],
)
def test_vault_server_url_rejects_unsafe(url):
    with pytest.raises(ValueError):
        _validate_vault_server_url(url)


def test_vault_server_url_allows_lan_and_public():
    assert _validate_vault_server_url(" https://vault.example.com/ ") == "https://vault.example.com"
    assert (
        _validate_vault_server_url("http://10.0.0.5:8000/vw")
        == "http://10.0.0.5:8000/vw"
    )
    assert _validate_vault_server_url("") == ""


def test_ssh_argv_keeps_host_and_script_out_of_a_local_shell_string():
    argv = _ssh_argv("user@box", "2222", "echo 'WARNING: tmux missing'; nvidia-smi")
    assert argv[0] == "ssh"
    assert "-p" in argv and "2222" in argv
    assert argv[-2] == "user@box"
    assert argv[-1] == "echo 'WARNING: tmux missing'; nvidia-smi"
    joined = " ".join(argv)
    assert "user@box '" not in joined
    default = _ssh_argv("user@box", "22", "true")
    assert "-p" not in default


def test_is_public_http_url_blocks_loopback_and_link_local():
    assert is_public_http_url("http://127.0.0.1/latest") is False
    assert is_public_http_url("http://169.254.169.254/latest") is False
    assert is_public_http_url("http://93.184.216.34/") is True


def test_fetch_public_http_bytes_rejects_redirect_to_link_local(monkeypatch):
    hops = []

    class _Resp:
        def __init__(self, status, location=None, url="http://93.184.216.34/img", content=b""):
            self.status_code = status
            self.headers = {"location": location} if location else {}
            self.url = url
            self.content = content

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f"HTTP {self.status_code}")

    def fake_get(url, timeout=None, follow_redirects=None):
        hops.append((url, follow_redirects))
        if urlparse(url).hostname == "93.184.216.34":
            return _Resp(302, location="http://169.254.169.254/latest/meta-data/")
        return _Resp(200, url=url, content=b"secret")

    monkeypatch.setattr("httpx.get", fake_get)
    with pytest.raises(ValueError, match="public HTTP"):
        fetch_public_http_bytes("http://93.184.216.34/img", timeout=5)
    assert hops[0][1] is False
    assert all("169.254.169.254" not in url for url, _ in hops)


def test_fetch_public_http_bytes_returns_body_without_following_blindly(monkeypatch):
    class _Resp:
        status_code = 200
        headers = {}
        url = "http://93.184.216.34/img"
        content = b"png-bytes"

        def raise_for_status(self):
            return None

    monkeypatch.setattr("httpx.get", lambda *a, **k: _Resp())
    assert fetch_public_http_bytes("http://93.184.216.34/img") == b"png-bytes"


def test_cookbook_gpu_and_setup_use_ssh_argv_not_shell_interpolation():
    src = _read("routes/cookbook_routes.py")
    assert "_ssh_argv(" in src
    assert "ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no {pf}{host}" not in src
    assert "ssh {pf}{host} '{setup_script}'" not in src
    tree = ast.parse(src)
    assert tree.body


def test_personal_delete_confines_with_realpath():
    src = _read("routes/personal_routes.py")
    assert "os.path.realpath(filepath)" in src
    assert "os.path.realpath(UPLOADS_DIR)" in src
    assert "os.path.realpath(PERSONAL_DIR)" in src


def test_image_generation_uses_public_fetch_helper():
    src = _read("src/ai_interaction.py")
    assert "fetch_public_http_bytes" in src
    assert 'httpx.get(img["url"]' not in src
    assert 'image_url = img["url"]' not in src


def test_vault_config_handler_calls_validator():
    src = _read("routes/vault_routes.py")
    assert "_validate_vault_server_url(req.server_url)" in src
    ast.parse(src)
