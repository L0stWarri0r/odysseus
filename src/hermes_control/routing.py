from __future__ import annotations

from urllib.parse import urlparse
import ipaddress
import re


_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}
_ABBREV_IPV4_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){1,3}$")


def _expand_abbrev_ipv4(host: str) -> str | None:
    """Expand abbreviated IPv4 forms browsers accept (e.g. 127.1 → 127.0.0.1)."""
    if not _ABBREV_IPV4_RE.match(host):
        return None
    parts = [int(p) for p in host.split(".")]
    if any(p > 255 for p in parts):
        return None
    if len(parts) == 2:
        # a.b → a.0.0.b
        parts = [parts[0], 0, 0, parts[1]]
    elif len(parts) == 3:
        # a.b.c → a.b.0.c
        parts = [parts[0], parts[1], 0, parts[2]]
    elif len(parts) != 4:
        return None
    return ".".join(str(p) for p in parts)


def is_local_endpoint(endpoint_url: str | None) -> bool:
    """Return True for local/private model endpoints.

    Recognizes common loopback spellings (trailing-dot localhost, short
    127.x forms, IPv4-mapped IPv6) so Hermes local-lane policy cannot be
    bypassed by an alternate loopback URL.
    """
    if not endpoint_url:
        return False
    try:
        parsed = urlparse(str(endpoint_url))
    except Exception:
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        return False
    if host in _LOCAL_HOSTS or host.endswith(".localhost"):
        return True
    expanded = _expand_abbrev_ipv4(host)
    candidates = [host]
    if expanded and expanded != host:
        candidates.append(expanded)
    for candidate in candidates:
        try:
            ip = ipaddress.ip_address(candidate)
        except ValueError:
            continue
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        if ip.is_loopback or ip.is_unspecified:
            return True
    return False
