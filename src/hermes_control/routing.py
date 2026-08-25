from __future__ import annotations

import ipaddress
from urllib.parse import urlparse


_LOCAL_HOSTS = {
    "localhost",
    "localhost.",
    "ip6-localhost",
    "ip6-loopback",
    "host.docker.internal",
}


def _parse_ip(host: str) -> ipaddress._BaseAddress | None:
    """Parse a hostname that is already an IP literal, including abbreviated IPv4."""
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    parts = host.split(".")
    if not parts or not all(p.isdigit() for p in parts) or not (1 <= len(parts) <= 4):
        return None
    try:
        nums = [int(p) for p in parts]
        if any(n > 255 for n in nums):
            return None
        if len(nums) == 1:
            packed = nums[0]
        elif len(nums) == 2:
            packed = (nums[0] << 24) | nums[1]
        elif len(nums) == 3:
            packed = (nums[0] << 24) | (nums[1] << 16) | nums[2]
        else:
            packed = (nums[0] << 24) | (nums[1] << 16) | (nums[2] << 8) | nums[3]
        return ipaddress.IPv4Address(packed)
    except (ValueError, ipaddress.AddressValueError):
        return None


def _is_loopback_host(host: str) -> bool:
    hostname = (host or "").strip().lower()
    if not hostname:
        return False
    if hostname in _LOCAL_HOSTS or hostname.endswith(".localhost"):
        return True
    ip = _parse_ip(hostname)
    if ip is None:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return bool(ip.is_loopback or ip.is_unspecified)


def is_local_endpoint(endpoint_url: str | None) -> bool:
    """Return True for loopback / Docker-host model endpoints.

    Conservative on purpose: loopback, unspecified, localhost aliases, and
    host.docker.internal count as local. LAN/cloud hosts stay non-local so a
    home-lab reverse proxy is not silently treated as an opaque local lane.
    """
    if not endpoint_url:
        return False
    try:
        parsed = urlparse(endpoint_url)
    except Exception:
        return False
    return _is_loopback_host(parsed.hostname or "")
