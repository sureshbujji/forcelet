"""TOTP (RFC 6238) two-factor helpers — stdlib only, no extra dependencies.

By Suresh Itha — part of the Forcelet platform.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time


def new_secret() -> str:
    """Random base32 secret suitable for authenticator apps."""
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def otpauth_url(secret: str, username: str, issuer: str = "Forcelet") -> str:
    padded = secret + "=" * (-len(secret) % 8)
    return (f"otpauth://totp/{issuer}:{username}"
            f"?secret={padded}&issuer={issuer}&digits=6&period=30")


def _hotp(secret: str, counter: int, digits: int = 6) -> str:
    padded = secret.upper() + "=" * (-len(secret) % 8)
    key = base64.b32decode(padded)
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(code % (10 ** digits)).zfill(digits)


def current_code(secret: str, for_time: float | None = None) -> str:
    counter = int((for_time if for_time is not None else time.time()) // 30)
    return _hotp(secret, counter)


def verify(secret: str, code: str, window: int = 1) -> bool:
    """Check a 6-digit code, allowing ±window 30-second steps for clock skew."""
    code = (code or "").strip().replace(" ", "")
    if not (code.isdigit() and len(code) == 6):
        return False
    now = int(time.time() // 30)
    for step in range(-window, window + 1):
        if hmac.compare_digest(_hotp(secret, now + step), code):
            return True
    return False
