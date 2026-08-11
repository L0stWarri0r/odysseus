from __future__ import annotations

from urllib.parse import urlparse


_LOCAL_HOSTS = {
    "127.0.0.1",
    "localhost",
    "localhost.",
    "::1",
    "0.0.0.0",
    "127.1",
    "ip6-localhost",
}


def is_local_endpoint(endpoint_url: str | None) -> bool:
    """Return True for local/private model endpoints.

    Recognizes common localhost aliases (trailing-dot DNS form, short
    ``127.1``, IPv4-mapped IPv6) so Hermes private_mode cannot be skipped
    by a minor URL spelling difference.
    """
    if not endpoint_url:
        return False
    try:
        parsed = urlparse(endpoint_url)
    except Exception:
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    if host in _LOCAL_HOSTS or host.endswith(".localhost"):
        return True
    # IPv4-mapped IPv6 loopback: ::ffff:127.0.0.1
    if host.startswith("::ffff:"):
        mapped = host.split("::ffff:", 1)[-1]
        if mapped in {"127.0.0.1", "127.1"} or mapped.startswith("127."):
            return True
    if host.startswith("127."):
        return True
    return False
