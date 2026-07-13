from io import BytesIO

import pytest
from fastapi import Depends, FastAPI, File, Request, UploadFile
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

from app.web_security import (
    LoginRateLimiter,
    client_identifier,
    ensure_csrf_token,
    require_csrf,
    rotate_csrf_token,
)


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture()
def limiter_and_clock():
    clock = FakeClock()
    limiter = LoginRateLimiter(
        max_failures=5,
        failure_window_seconds=900,
        lockout_seconds=900,
        clock=clock,
    )
    return limiter, clock


@pytest.fixture()
def csrf_client():
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="csrf-test-secret")
    app.state.submit_calls = 0

    @app.get("/token")
    def token(request: Request):
        return {"token": ensure_csrf_token(request)}

    @app.post("/submit", dependencies=[Depends(require_csrf)])
    def submit(request: Request):
        request.app.state.submit_calls += 1
        return {"ok": True}

    @app.post("/upload", dependencies=[Depends(require_csrf)])
    async def upload(file: UploadFile = File(...)):
        return {"filename": file.filename, "content": (await file.read()).decode()}

    @app.post("/rotate", dependencies=[Depends(require_csrf)])
    def rotate(request: Request):
        return {"token": rotate_csrf_token(request)}

    return TestClient(app)


def test_csrf_accepts_matching_session_form_token(csrf_client):
    token = csrf_client.get("/token").json()["token"]

    response = csrf_client.post("/submit", data={"_csrf_token": token})

    assert response.status_code == 200


@pytest.mark.parametrize("data", [{}, {"_csrf_token": "wrong"}])
def test_csrf_rejects_missing_or_wrong_token(csrf_client, data):
    csrf_client.get("/token")

    response = csrf_client.post("/submit", data=data)

    assert response.status_code == 403
    assert response.json() == {"detail": "请求安全校验失败"}
    assert csrf_client.app.state.submit_calls == 0


def test_csrf_rejects_token_from_another_session(csrf_client):
    other = TestClient(csrf_client.app)
    foreign_token = other.get("/token").json()["token"]
    csrf_client.get("/token")

    response = csrf_client.post("/submit", data={"_csrf_token": foreign_token})

    assert response.status_code == 403


def test_csrf_accepts_multipart_upload(csrf_client):
    token = csrf_client.get("/token").json()["token"]

    response = csrf_client.post(
        "/upload",
        data={"_csrf_token": token},
        files={"file": ("rules.json", BytesIO(b"{}"), "application/json")},
    )

    assert response.status_code == 200
    assert response.json() == {"filename": "rules.json", "content": "{}"}


def test_rotate_csrf_token_invalidates_previous_token(csrf_client):
    original = csrf_client.get("/token").json()["token"]
    rotated = csrf_client.post("/rotate", data={"_csrf_token": original}).json()["token"]

    assert rotated != original
    assert csrf_client.post("/submit", data={"_csrf_token": original}).status_code == 403
    assert csrf_client.post("/submit", data={"_csrf_token": rotated}).status_code == 200


def test_limiter_locks_on_fifth_failure_and_reports_retry_after(limiter_and_clock):
    limiter, _ = limiter_and_clock

    assert [limiter.record_failure("10.0.0.1", " Admin ") for _ in range(4)] == [0, 0, 0, 0]
    assert limiter.record_failure("10.0.0.1", "admin") == 900
    assert limiter.retry_after("10.0.0.1", "ADMIN") == 900


def test_limiter_rounds_retry_after_up(limiter_and_clock):
    limiter, clock = limiter_and_clock
    for _ in range(5):
        limiter.record_failure("10.0.0.1", "admin")

    clock.advance(0.1)

    assert limiter.retry_after("10.0.0.1", "admin") == 900


def test_limiter_lock_expires_and_stale_state_is_removed(limiter_and_clock):
    limiter, clock = limiter_and_clock
    for _ in range(5):
        limiter.record_failure("10.0.0.1", "admin")

    clock.advance(901)

    assert limiter.retry_after("10.0.0.1", "admin") == 0
    assert limiter.record_failure("10.0.0.1", "admin") == 0


def test_expired_lock_clears_failures_even_when_window_is_longer():
    clock = FakeClock()
    limiter = LoginRateLimiter(
        max_failures=5,
        failure_window_seconds=900,
        lockout_seconds=60,
        clock=clock,
    )
    for _ in range(5):
        limiter.record_failure("10.0.0.1", "admin")

    clock.advance(61)

    assert limiter.retry_after("10.0.0.1", "admin") == 0
    assert limiter.record_failure("10.0.0.1", "admin") == 0


def test_limiter_discards_failures_outside_window(limiter_and_clock):
    limiter, clock = limiter_and_clock
    for _ in range(4):
        limiter.record_failure("10.0.0.1", "admin")
    clock.advance(901)

    assert limiter.record_failure("10.0.0.1", "admin") == 0


def test_limiter_separates_client_and_username_keys(limiter_and_clock):
    limiter, _ = limiter_and_clock
    for _ in range(5):
        limiter.record_failure("10.0.0.1", "admin")

    assert limiter.retry_after("10.0.0.2", "admin") == 0
    assert limiter.retry_after("10.0.0.1", "other") == 0


def test_limiter_clear_removes_failures(limiter_and_clock):
    limiter, _ = limiter_and_clock
    for _ in range(4):
        limiter.record_failure("10.0.0.1", "admin")

    limiter.clear("10.0.0.1", "admin")

    assert limiter.record_failure("10.0.0.1", "admin") == 0


def test_client_identifier_uses_request_client_host():
    request = Request(
        {
            "type": "http",
            "client": ("203.0.113.8", 50000),
            "headers": [(b"x-forwarded-for", b"198.51.100.4")],
        }
    )

    assert client_identifier(request) == "203.0.113.8"


def test_client_identifier_has_stable_missing_client_fallback():
    request = Request({"type": "http", "client": None})

    assert client_identifier(request) == "unknown-client"
