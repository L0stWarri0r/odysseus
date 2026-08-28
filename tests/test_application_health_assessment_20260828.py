"""Health assessment 2026-08-28: IPv4-mapped SSRF + env-proxy pin.

web_fetch / deep research and API-token ``base_url`` validation already blocked
literal loopback and 169.254.169.254, but IPv4-mapped IPv6 forms
(``[::ffff:127.0.0.1]``, ``[::ffff:169.254.169.254]``) are not ``is_loopback`` /
``is_link_local`` in Python's ``ipaddress`` module. Those literals were treated
as public, so an agent web_fetch or a token chat ``base_url`` could reach
loopback and cloud metadata.

Webhook delivery already unwrapped mapped addresses; these tests pin the same
behavior on ``src.url_security`` and the search content fetchers, plus
``trust_env=False`` so HTTP(S)_PROXY cannot steer those clients.
"""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


MAPPED_AND_INTERNAL_URLS = [
    "http://[::ffff:127.0.0.1]/",
    "http://[::ffff:169.254.169.254]/latest/meta-data/",
    "http://[::]/",
    "http://100.64.0.1/",
    "http://intranet.local/",
    "http://foo.localhost/",
]


@pytest.mark.parametrize("url", MAPPED_AND_INTERNAL_URLS)
def test_url_security_blocks_ipv4_mapped_and_cgnat(url):
    from src.url_security import is_public_http_url

    assert is_public_http_url(url) is False


def test_url_security_nonstring_fails_closed():
    from src.url_security import is_public_http_url

    assert is_public_http_url(None) is False
    assert is_public_http_url(123) is False


@pytest.mark.parametrize("url", MAPPED_AND_INTERNAL_URLS)
def test_src_search_content_blocks_ipv4_mapped_and_cgnat(url):
    from src.search.content import _public_http_url

    assert _public_http_url(url) is False


def test_src_search_content_nonstring_fails_closed():
    from src.search.content import _public_http_url

    assert _public_http_url(None) is False
    assert _public_http_url(123) is False
    assert _public_http_url("") is False


@pytest.mark.parametrize("url", MAPPED_AND_INTERNAL_URLS)
def test_services_search_content_blocks_ipv4_mapped_and_cgnat(url):
    from services.search.content import _public_http_url

    assert _public_http_url(url) is False


def test_src_search_content_fetcher_disables_env_proxy():
    src = (ROOT / "src" / "search" / "content.py").read_text(encoding="utf-8")
    assert "trust_env=False" in src
    assert "follow_redirects=False" in src


def test_services_search_content_fetcher_disables_env_proxy():
    src = (ROOT / "services" / "search" / "content.py").read_text(encoding="utf-8")
    assert "trust_env=False" in src
    assert "follow_redirects=False" in src


def test_webhook_client_disables_env_proxy():
    src = (ROOT / "src" / "webhook_manager.py").read_text(encoding="utf-8")
    assert "follow_redirects=False, trust_env=False" in src or (
        "follow_redirects=False" in src and "trust_env=False" in src
    )


def test_url_security_still_allows_public_literal():
    from src.url_security import is_public_http_url
    from src.search.content import _public_http_url as src_public
    from services.search.content import _public_http_url as svc_public

    public = "http://93.184.216.34/"
    assert is_public_http_url(public) is True
    assert src_public(public) is True
    assert svc_public(public) is True
