"""Constant-time service Bearer authentication."""

from __future__ import annotations

import hmac


def verify_service_bearer(header: str | None, expected: str) -> bool:
    if not header or not expected:
        return False
    scheme, separator, token = header.partition(" ")
    if separator != " " or scheme.lower() != "bearer" or not token:
        return False
    return hmac.compare_digest(token, expected)
