"""Resource-owner login for the /authorize consent screen (v0.12.0).

This is a *separate* authentication boundary from the MCP Bearer-token
check: it answers "is the human clicking through this browser actually
the Brain's owner", once per consent decision — it never gates an MCP
tool call itself (that's always the OAuth access token).

Password hashing uses `hashlib.scrypt` — a standard-library binding to a
real, audited scrypt implementation, not a hand-rolled KDF. The session
cookie is HMAC-signed (`hmac` + `hashlib.sha256`, both stdlib) with a
server secret generated once and stored in the gateway's own SQLite
(`storage.get_or_create_server_secret`) — a MAC, not an invented cipher.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time

SESSION_TTL_SECONDS = 15 * 60  # long enough to log in and click "approve", not longer


def hash_password(password: str) -> tuple[str, str]:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return base64.b64encode(digest).decode("ascii"), base64.b64encode(salt).decode("ascii")


def verify_password(password: str, password_hash_b64: str, salt_b64: str) -> bool:
    salt = base64.b64decode(salt_b64)
    expected = base64.b64decode(password_hash_b64)
    computed = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return hmac.compare_digest(computed, expected)


def issue_session_cookie(server_secret: str) -> str:
    expires = int(time.time()) + SESSION_TTL_SECONDS
    payload = f"owner:{expires}"
    mac = hmac.new(server_secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload}:{mac}"


def verify_session_cookie(cookie_value: str | None, server_secret: str) -> bool:
    if not cookie_value:
        return False
    try:
        subject, expires_s, mac = cookie_value.split(":", 2)
        expires = int(expires_s)
    except (ValueError, TypeError):
        return False
    if subject != "owner" or expires < time.time():
        return False
    payload = f"{subject}:{expires_s}"
    expected = hmac.new(server_secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(mac, expected)
