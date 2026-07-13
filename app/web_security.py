import math
import secrets
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from threading import Lock

from fastapi import HTTPException, Request, status

CSRF_SESSION_KEY = "_csrf_token"
CSRF_FORM_FIELD = "_csrf_token"
CSRF_ERROR = "请求安全校验失败"
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


@dataclass
class _AttemptState:
    failures: deque[float] = field(default_factory=deque)
    locked_until: float = 0.0


def client_identifier(request: Request) -> str:
    return request.client.host if request.client is not None else "unknown-client"


class LoginRateLimiter:
    def __init__(
        self,
        *,
        max_failures: int,
        failure_window_seconds: int,
        lockout_seconds: int,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.max_failures = max_failures
        self.failure_window_seconds = failure_window_seconds
        self.lockout_seconds = lockout_seconds
        self.clock = clock
        self._states: dict[tuple[str, str], _AttemptState] = {}
        self._lock = Lock()

    def retry_after(self, client_id: str, username: str) -> int:
        with self._lock:
            now = self.clock()
            self._prune(now)
            state = self._states.get(self._key(client_id, username))
            if state is None or state.locked_until <= now:
                return 0
            return math.ceil(state.locked_until - now)

    def record_failure(self, client_id: str, username: str) -> int:
        with self._lock:
            now = self.clock()
            self._prune(now)
            key = self._key(client_id, username)
            state = self._states.setdefault(key, _AttemptState())
            state.failures.append(now)
            if len(state.failures) < self.max_failures:
                return 0
            state.locked_until = now + self.lockout_seconds
            return self.lockout_seconds

    def clear(self, client_id: str, username: str) -> None:
        with self._lock:
            self._states.pop(self._key(client_id, username), None)

    def _key(self, client_id: str, username: str) -> tuple[str, str]:
        return client_id, username.strip().casefold()

    def _prune(self, now: float) -> None:
        cutoff = now - self.failure_window_seconds
        for key, state in list(self._states.items()):
            if state.locked_until and state.locked_until <= now:
                state.locked_until = 0.0
                state.failures.clear()
            while state.failures and state.failures[0] <= cutoff:
                state.failures.popleft()
            if not state.failures and state.locked_until == 0.0:
                self._states.pop(key, None)


def ensure_csrf_token(request: Request) -> str:
    token = request.session.get(CSRF_SESSION_KEY)
    if not isinstance(token, str) or not token:
        token = secrets.token_urlsafe(32)
        request.session[CSRF_SESSION_KEY] = token
    return token


def rotate_csrf_token(request: Request) -> str:
    token = secrets.token_urlsafe(32)
    request.session[CSRF_SESSION_KEY] = token
    return token


async def require_csrf(request: Request) -> None:
    if request.method.upper() not in UNSAFE_METHODS:
        return

    expected = request.session.get(CSRF_SESSION_KEY)
    try:
        form = await request.form()
        provided = form.get(CSRF_FORM_FIELD)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=CSRF_ERROR) from exc

    if (
        not isinstance(expected, str)
        or not expected
        or not isinstance(provided, str)
        or not secrets.compare_digest(expected, provided)
    ):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=CSRF_ERROR)
