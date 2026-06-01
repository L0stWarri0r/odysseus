from __future__ import annotations

from urllib.parse import urlparse


_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}


def is_local_endpoint(endpoint_url: str | None) -> bool:
    """Return True for local/private model endpoints.

    This is intentionally conservative: localhost-style hosts are local;
    everything else remains standard/cloud unless later configured otherwise.
    """
    if not endpoint_url:
        return False
    try:
        parsed = urlparse(endpoint_url)
    except Exception:
        return False
    host = (parsed.hostname or "").lower()
    return host in _LOCAL_HOSTS
