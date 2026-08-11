"""Regression: validate_caldav_url must reject a non-string via its normal
ValueError path, not crash with TypeError.

It did `(raw_url or "").strip()`, so a non-string scalar (e.g. an int from a
mis-typed config) reached `.strip()` and raised TypeError instead of the
function\'s own ValueError.
"""
import socket

import pytest

from src.caldav_sync import validate_caldav_url


def test_non_string_raises_valueerror_not_typeerror():
    with pytest.raises(ValueError):
        validate_caldav_url(12345)
    with pytest.raises(ValueError):
        validate_caldav_url(None)


def test_valid_url_passes(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))],
    )
    out = validate_caldav_url("https://dav.example.com/calendars/")
    assert "example.com" in out
