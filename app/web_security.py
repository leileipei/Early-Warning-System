import math
import secrets
import time
from collections import OrderedDict, deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from hashlib import sha256
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
    gate: Lock = field(default_factory=Lock, repr=False)
    references: int = 0


_LimiterKey = tuple[str, bytes]


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
        max_states: int = 10_000,
        cleanup_interval_seconds: float = 60.0,
        cleanup_batch_size: int = 64,
    ):
        if max_states < 1:
            raise ValueError("max_states must be positive")
        if cleanup_interval_seconds <= 0:
            raise ValueError("cleanup_interval_seconds must be positive")
        if cleanup_batch_size < 1:
            raise ValueError("cleanup_batch_size must be positive")

        self.max_failures = max_failures
        self.failure_window_seconds = failure_window_seconds
        self.lockout_seconds = lockout_seconds
        self.clock = clock
        self.max_states = max_states
        self.cleanup_interval_seconds = cleanup_interval_seconds
        self.cleanup_batch_size = cleanup_batch_size
        self._states: OrderedDict[_LimiterKey, _AttemptState] = OrderedDict()
        self._lock = Lock()
        self._next_cleanup_at = self.clock() + self.cleanup_interval_seconds

    @contextmanager
    def attempt(self, client_id: str, username: str) -> Iterator[int]:
        key = self._key(client_id, username)
        with self._lock:
            now = self.clock()
            self._run_scheduled_cleanup(now)
            state = self._current_state(key, now)
            if state is None:
                state = self._reserve_state(key)
            if state is not None:
                state.references += 1

        if state is None:
            yield self._capacity_retry_after()
            return

        state.gate.acquire()
        try:
            with self._lock:
                now = self.clock()
                self._expire_state(state, now)
                retry_after = self._retry_after(state, now)
            yield retry_after
        finally:
            state.gate.release()
            with self._lock:
                state.references -= 1
                self._discard_if_inactive(key, state)

    def retry_after(self, client_id: str, username: str) -> int:
        with self._lock:
            now = self.clock()
            self._run_scheduled_cleanup(now)
            state = self._current_state(self._key(client_id, username), now)
            return self._retry_after(state, now) if state is not None else 0

    def record_failure(self, client_id: str, username: str) -> int:
        with self._lock:
            now = self.clock()
            self._run_scheduled_cleanup(now)
            key = self._key(client_id, username)
            state = self._current_state(key, now)
            if state is None:
                state = self._reserve_state(key)
            if state is None:
                return self._capacity_retry_after()
            state.failures.append(now)
            if len(state.failures) < self.max_failures:
                return 0
            state.locked_until = now + self.lockout_seconds
            return self.lockout_seconds

    def clear(self, client_id: str, username: str) -> None:
        with self._lock:
            key = self._key(client_id, username)
            state = self._states.get(key)
            if state is None:
                return
            state.failures.clear()
            state.locked_until = 0.0
            self._discard_if_inactive(key, state)

    def _key(self, client_id: str, username: str) -> _LimiterKey:
        normalized_username = username.strip().casefold().encode("utf-8")
        return client_id, sha256(normalized_username).digest()

    def _reserve_state(self, key: _LimiterKey) -> _AttemptState | None:
        if len(self._states) >= self.max_states:
            return None
        state = _AttemptState()
        self._states[key] = state
        return state

    def _current_state(self, key: _LimiterKey, now: float) -> _AttemptState | None:
        state = self._states.get(key)
        if state is None:
            return None
        self._expire_state(state, now)
        self._discard_if_inactive(key, state)
        return self._states.get(key)

    def _expire_state(self, state: _AttemptState, now: float) -> None:
        cutoff = now - self.failure_window_seconds
        if state.locked_until and state.locked_until <= now:
            state.locked_until = 0.0
            state.failures.clear()
        while state.failures and state.failures[0] <= cutoff:
            state.failures.popleft()

    def _retry_after(self, state: _AttemptState, now: float) -> int:
        if state.locked_until <= now:
            return 0
        return math.ceil(state.locked_until - now)

    def _discard_if_inactive(self, key: _LimiterKey, state: _AttemptState) -> None:
        if (
            self._states.get(key) is state
            and state.references == 0
            and not state.failures
            and state.locked_until == 0.0
        ):
            self._states.pop(key)

    def _run_scheduled_cleanup(self, now: float) -> None:
        if now < self._next_cleanup_at:
            return
        self._next_cleanup_at = now + self.cleanup_interval_seconds
        for _ in range(min(self.cleanup_batch_size, len(self._states))):
            key, state = self._states.popitem(last=False)
            self._expire_state(state, now)
            if state.references or state.failures or state.locked_until:
                self._states[key] = state

    def _capacity_retry_after(self) -> int:
        return max(1, math.ceil(self.cleanup_interval_seconds))


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


def _ascii_bytes(value: object) -> bytes | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return value.encode("ascii")
    except UnicodeEncodeError:
        return None


async def require_csrf(request: Request) -> None:
    if request.method.upper() not in UNSAFE_METHODS:
        return

    expected = request.session.get(CSRF_SESSION_KEY)
    try:
        form = await request.form()
        provided = form.get(CSRF_FORM_FIELD)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=CSRF_ERROR) from exc

    expected_bytes = _ascii_bytes(expected)
    provided_bytes = _ascii_bytes(provided)
    if (
        expected_bytes is None
        or provided_bytes is None
        or not secrets.compare_digest(expected_bytes, provided_bytes)
    ):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=CSRF_ERROR)
