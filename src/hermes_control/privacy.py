from __future__ import annotations

import re
from typing import List

from .models import HermesFinding


_WINDOWS_LOCAL_PATH_RE = re.compile(
    r"[A-Za-z]:\\(?:Users|Documents|Projects|Games|Downloads|Desktop)\\[^\s`'\"]+",
    re.IGNORECASE,
)
_WSL_LOCAL_PATH_RE = re.compile(
    r"/(?:mnt/)?[cC]/Users/[^\s`'\"]+",
    re.IGNORECASE,
)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"\b[A-Za-z0-9_-]*(?:api[_-]?key|secret|token|password|passwd|pwd|client[_-]?secret|access[_-]?token|refresh[_-]?token)\b\s*[:=]\s*['\"]?[^'\"\s]{8,}",
    re.IGNORECASE,
)
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN (?:OPENSSH|RSA|EC|DSA)? ?PRIVATE KEY-----",
    re.IGNORECASE,
)
_GITHUB_TOKEN_RE = re.compile(r"\b(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]{12,}\b")
_BEARER_RE = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}\b", re.IGNORECASE)
_DATABASE_URL_RE = re.compile(r"\b(?:postgres|postgresql|mysql|mongodb)://[^\s:@]+:[^\s@]+@[^\s]+", re.IGNORECASE)


def _preview(value: str, max_len: int = 80) -> str:
    if len(value) <= max_len:
        return value
    return value[: max_len - 3] + "..."


def find_privacy_signals(text: str) -> List[HermesFinding]:
    """Find privacy/security signals in visible, non-private content.

    Do not call this for private/local opaque mode; the whole point of that
    lane is that Hermes policy does not inspect prompt/output text.
    """
    findings: List[HermesFinding] = []

    for regex, label in (
        (_PRIVATE_KEY_RE, "Private key block"),
        (_SECRET_ASSIGNMENT_RE, "Secret or credential assignment"),
        (_GITHUB_TOKEN_RE, "GitHub token"),
        (_BEARER_RE, "Bearer token"),
        (_DATABASE_URL_RE, "Database URL with credentials"),
    ):
        for match in regex.finditer(text or ""):
            findings.append(
                HermesFinding(
                    type="secret",
                    severity="critical",
                    label=label,
                    preview="[REDACTED]",
                )
            )

    for regex, label in (
        (_WINDOWS_LOCAL_PATH_RE, "Windows local path"),
        (_WSL_LOCAL_PATH_RE, "WSL/MSYS local path"),
    ):
        for match in regex.finditer(text or ""):
            findings.append(
                HermesFinding(
                    type="local_path",
                    severity="info",
                    label=label,
                    preview=_preview(match.group(0)),
                )
            )

    return findings
