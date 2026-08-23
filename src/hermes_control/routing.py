from __future__ import annotations

import ipaddress
from urllib.parse import urlparse


_LOCAL_HOSTS = {
    "127.0.0.1",
    "localhost",
    "::1",
    "0.0.0.0",
    "host.docker.internal",
    "ip6-localhost",
    "ip6-loopback",
}


def is_local_endpoint(endpoint_url: str | None) -> bool:
    """Return True for loopback / localhost-style model endpoints.

    Conservative on purpose: only the local lane (loopback and its aliases)
    gets web/research disabled. Public and general LAN hosts stay cloud-like
    unless they are clearly a loopback spelling such as ``127.1``,
    ``localhost.``, or IPv4-mapped ``::ffff:127.0.0.1``.
    """
    if not endpoint_url:
        return False
    try:
        parsed = urlparse(endpoint_url.strip())
    except Exception:
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        return False
    if host in _LOCAL_HOSTS or host.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(host)
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        return bool(ip.is_loopback or ip.is_unspecified)
    except ValueError:
        return host.startswith("127.")
