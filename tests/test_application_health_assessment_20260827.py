"""Health assessment 2026-08-27 — image URL fetches, cache, ntfy, fonts.

Prior health PRs #16–#22 remain open/unmerged. This run does not re-land
those suites. It covers gaps they left on lost/personal-core:

  * Gallery inpaint/harmonize still did ``httpx.get(item["url"])`` with
    default redirect following (PR #19 only pinned the DALL-E path in
    ``src/ai_interaction.py``).
  * The MCP image-gen server returned provider URLs and inserted
    null-owner gallery rows.
  * Generated images and PDF page PNGs were cached with ``Cache-Control:
    public``, so shared caches could keep authenticated bytes.
  * Emoji CDN fetches followed redirects / trusted HTTP_PROXY.
  * Note reminder ntfy used the raw title as an HTTP header and interpolated
    the topic into the path.
  * Custom font family names were interpolated into ``@font-face`` CSS.
"""
from __future__ import annotations

import ast
from pathlib import Path
from urllib.parse import urlparse

import pytest

from routes.note_routes import _http_header_value, _ntfy_topic
from src.url_security import fetch_public_http_bytes, is_public_http_url


REPO = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


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

    def fake_get(url, timeout=None, follow_redirects=None, **kwargs):
        hops.append((url, follow_redirects, kwargs.get("trust_env")))
        if urlparse(url).hostname == "93.184.216.34":
            return _Resp(302, location="http://169.254.169.254/latest/meta-data/")
        return _Resp(200, url=url, content=b"secret")

    monkeypatch.setattr("httpx.get", fake_get)
    with pytest.raises(ValueError, match="public HTTP"):
        fetch_public_http_bytes("http://93.184.216.34/img", timeout=5)
    assert hops[0][1] is False
    assert hops[0][2] is False
    assert all("169.254.169.254" not in url for url, *_ in hops)


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


def test_gallery_image_url_downloads_use_public_fetch_helper():
    src = _read("routes/gallery_routes.py")
    assert src.count("fetch_public_http_bytes") >= 2
    assert 'c2.get(item["url"])' not in src
    ast.parse(src)


def test_mcp_image_gen_does_not_return_or_blindly_fetch_provider_urls():
    src = _read("mcp_servers/image_gen_server.py")
    assert "fetch_public_http_bytes" in src
    assert 'image_url = img["url"]' not in src
    assert "AUTH_ENABLED" in src
    ast.parse(src)


def test_generated_image_and_pdf_pages_use_private_cache():
    app = _read("app.py")
    docs = _read("routes/document_routes.py")
    assert 'Cache-Control": "private, max-age=31536000, immutable"' in app
    assert 'Cache-Control": "public, max-age=31536000, immutable"' not in app
    assert 'Cache-Control": "private, max-age=3600"' in docs
    assert 'Cache-Control": "public, max-age=3600"' not in docs


def test_emoji_cdn_fetch_does_not_follow_redirects_or_trust_proxy_env():
    src = _read("routes/emoji_routes.py")
    assert "follow_redirects=False" in src
    assert "trust_env=False" in src
    ast.parse(src)


def test_ntfy_header_strips_crlf_and_topic_rejects_path_escape():
    assert _http_header_value("Hello\r\nX-Injected: 1", "Reminder") == "HelloX-Injected: 1"
    assert "\n" not in _http_header_value("line\nbreak", "Reminder")
    assert _http_header_value("   ", "Reminder") == "Reminder"
    assert _ntfy_topic("Reminders") == "Reminders"
    assert _ntfy_topic("phone-alerts_1") == "phone-alerts_1"
    assert _ntfy_topic("../secret") == "reminders"
    assert _ntfy_topic("http://169.254.169.254") == "reminders"
    assert _ntfy_topic("foo/bar") == "reminders"


def test_note_routes_use_ntfy_sanitizers():
    src = _read("routes/note_routes.py")
    assert "_http_header_value(title, \"Reminder\")" in src
    assert "_ntfy_topic(" in src
    assert 'hdrs = {"Title": title or "Reminder"' not in src


def test_font_listing_quotes_filenames_and_skips_non_files():
    src = _read("routes/font_routes.py")
    assert "quote(f, safe='.-_')" in src
    assert "os.path.isfile(full)" in src
    ast.parse(src)


def test_theme_font_face_escapes_family_and_url():
    src = _read("static/js/theme.js")
    assert "export function _cssString(value)" in src
    assert "src: url('${_cssString(v.url)}')" in src
    assert "font-family: '${family}'" in src
    assert "family = \"'\" + _cssString(f) + \"', sans-serif\"" in src
    assert "src: url('${v.url}')" not in src
