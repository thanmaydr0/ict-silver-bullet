"""Bounded, revocable single-process sessions; no browser-readable tokens."""
from collections import deque
from datetime import datetime, timedelta
import hashlib
import secrets
from threading import Lock
from zoneinfo import ZoneInfo
from brain.dashboard_models import SessionInfo

COOKIE = "ict_dashboard_session"
TTL_SECONDS = 8 * 60 * 60
UTC = ZoneInfo("UTC")


class LoginThrottled(Exception):
    pass


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).digest()


class Sessions:
    # NOTE: One worker, eight-hour sessions, maximum 1024 sessions/IP buckets.
    # Restart logs everyone out; no extra secret or Redis is required.
    def __init__(self, username, password, now=None):
        self.username = username
        self._user, self._password = _digest(username), _digest(password)
        self._now = now or (lambda: datetime.now(UTC))
        self._sessions, self._attempts = {}, {}
        self._lock = Lock()

    def _prune(self, now):
        self._sessions = {k: v for k, v in self._sessions.items() if v > now}
        for ip in list(self._attempts):
            times = self._attempts[ip]
            while times and (now - times[0]).total_seconds() >= 300:
                times.popleft()
            if not times:
                del self._attempts[ip]

    def login(self, username, password, ip, previous=None):
        with self._lock:
            now = self._now()
            self._prune(now)
            if ip not in self._attempts:
                if len(self._attempts) >= 1024:
                    raise LoginThrottled
                self._attempts[ip] = deque()
            attempts = self._attempts[ip]
            if len(attempts) >= 10:
                raise LoginThrottled
            attempts.append(now)
            user_ok = secrets.compare_digest(_digest(username), self._user)
            password_ok = secrets.compare_digest(_digest(password), self._password)
            if not (user_ok & password_ok):
                return None
            if previous and len(previous) <= 128:
                self._sessions.pop(_digest(previous), None)
            if len(self._sessions) >= 1024:
                del self._sessions[min(self._sessions, key=self._sessions.get)]
            token = secrets.token_urlsafe(32)
            expires = now + timedelta(seconds=TTL_SECONDS)
            self._sessions[_digest(token)] = expires
            return token, SessionInfo(username=self.username, expires_at=expires)

    def check(self, token):
        if not token or len(token) > 128:
            return None
        with self._lock:
            expires = self._sessions.get(_digest(token))
            if expires is None:
                return None
            if expires <= self._now():
                del self._sessions[_digest(token)]
                return None
            return SessionInfo(username=self.username, expires_at=expires)

    def logout(self, token):
        if token and len(token) <= 128:
            with self._lock:
                self._sessions.pop(_digest(token), None)
