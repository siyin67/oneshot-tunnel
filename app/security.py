"""Authentication, sessions and CSRF primitives."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from fastapi import Request, Response

from .models import utcnow

SESSION_COOKIE = "sshport_session"
MESSAGE_INVALID_CREDENTIALS = "访问密钥无效，或登录尝试过于频繁，请稍后再试。"


def source_ip(request: Request) -> str:
    """Use the transport peer address, not a forgeable forwarded header."""
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def verify_access_key(keys: tuple[str, ...], candidate: str) -> bool:
    """Constant-time comparison against every configured key."""
    matched = False
    candidate_digest = hashlib.sha256(candidate.encode("utf-8")).digest()
    for key in keys:
        key_digest = hashlib.sha256(key.encode("utf-8")).digest()
        if hmac.compare_digest(key_digest, candidate_digest):
            matched = True
    return matched


def csrf_tokens_equal(expected: str, supplied: str | None) -> bool:
    if not supplied or not expected:
        return False
    return hmac.compare_digest(expected.encode("utf-8"), supplied.encode("utf-8"))


@dataclass
class Session:
    token_hash: str
    csrf_token: str
    created_at: datetime
    expires_at: datetime

    def is_valid(self, now: datetime) -> bool:
        return now < self.expires_at


class SessionManager:
    def __init__(
        self,
        keys: tuple[str, ...],
        ttl_seconds: int,
        cookie_secure: bool,
        *,
        now: Callable[[], datetime] = utcnow,
        token_factory: Callable[[], str] | None = None,
    ):
        self._keys = keys
        self.ttl_seconds = ttl_seconds
        self.cookie_secure = cookie_secure
        self._now = now
        self._token_factory = token_factory or (lambda: secrets.token_urlsafe(32))
        self._sessions: dict[str, Session] = {}

    def authenticate(self, candidate: str) -> tuple[str, Session] | None:
        if not verify_access_key(self._keys, candidate):
            return None
        token = self._token_factory()
        now = self._now()
        session = Session(
            token_hash=_digest(token),
            csrf_token=secrets.token_urlsafe(32),
            created_at=now,
            expires_at=now + timedelta(seconds=self.ttl_seconds),
        )
        self._prune(now)
        # Bound memory if the owner logs in repeatedly without logging out.
        if len(self._sessions) >= 256:
            oldest = min(self._sessions, key=lambda key: self._sessions[key].created_at)
            self._sessions.pop(oldest, None)
        self._sessions[session.token_hash] = session
        return token, session

    def get(self, token: str | None) -> Session | None:
        if not token:
            return None
        token_hash = _digest(token)
        session = self._sessions.get(token_hash)
        if session is None:
            return None
        now = self._now()
        if not session.is_valid(now):
            self._sessions.pop(token_hash, None)
            return None
        return session

    def revoke(self, token: str | None) -> None:
        if token:
            self._sessions.pop(_digest(token), None)

    def _prune(self, now: datetime) -> None:
        expired = [
            token_hash
            for token_hash, session in self._sessions.items()
            if not session.is_valid(now)
        ]
        for token_hash in expired:
            self._sessions.pop(token_hash, None)

    def set_cookie(self, response: Response, token: str) -> None:
        response.set_cookie(
            SESSION_COOKIE,
            token,
            max_age=self.ttl_seconds,
            httponly=True,
            secure=self.cookie_secure,
            samesite="lax",
            path="/",
        )

    def clear_cookie(self, response: Response) -> None:
        response.delete_cookie(
            SESSION_COOKIE,
            path="/",
            httponly=True,
            secure=self.cookie_secure,
            samesite="lax",
        )
