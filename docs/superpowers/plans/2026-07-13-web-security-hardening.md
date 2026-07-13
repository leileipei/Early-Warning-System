# Web Security Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Protect all state-changing Web forms with Session CSRF tokens, throttle repeated administrator login failures, and make Session Cookie security compatible with current HTTP and future HTTPS deployments.

**Architecture:** Add a focused `app/web_security.py` module for CSRF primitives, client identification, and a thread-safe in-memory login limiter. Wire CSRF once at both routers, inject tokens through the shared template response helper, and keep login orchestration in `app/auth.py`. Configure Cookie flags and limiter construction in `create_app()` so each application instance owns isolated runtime state.

**Tech Stack:** Python 3.11+, FastAPI, Starlette SessionMiddleware, Jinja2, pydantic-settings, pytest, Ruff, Python standard-library `secrets`, `threading`, `time`, `collections`, and `math`.

## Global Constraints

- Current HTTP deployments must remain usable with `SESSION_COOKIE_SECURE=false`.
- HTTPS deployments enable `Secure` cookies with `SESSION_COOKIE_SECURE=true`.
- Session cookies remain `HttpOnly`, use `SameSite=Lax`, and retain the existing cookie name and lifetime.
- Every `POST`, `PUT`, `PATCH`, and `DELETE` request in the auth and page routers requires a valid Session CSRF token in form field `_csrf_token`.
- CSRF failures return `403` with detail `请求安全校验失败` before business logic executes.
- CSRF tokens use cryptographically secure randomness and constant-time comparison; form protection must not depend on JavaScript.
- Login rate-limit keys are `request.client.host` plus stripped, case-folded username; application code does not trust `X-Forwarded-For` directly.
- Defaults are 5 failures in 900 seconds followed by a 900-second lockout.
- Attempts 1-4 return the existing `400`; attempt 5 and locked requests return `429`, detail `登录尝试过多，请稍后重试`, and `Retry-After`.
- Unknown usernames and wrong passwords have identical counting and response behavior.
- Successful login clears the matching limiter state, clears the old Session, creates a fresh CSRF token, and stores the administrator ID.
- Limiter state is thread-safe, process-local, reset by Web restart, and not shared between multiple Web workers.
- No Redis, database table, migration, CAPTCHA, MFA, SSO, LDAP, role system, or automatic trust of forwarding headers is added.

---

### Task 1: Security Settings and Session Cookie Flags

**Files:**
- Modify: `app/settings.py`
- Modify: `app/main.py`
- Modify: `tests/test_routes.py`
- Modify: `.env.example`

**Interfaces:**
- Consumes: Existing `Settings`, `get_settings()`, and `SessionMiddleware` construction.
- Produces: `Settings.session_cookie_secure: bool`, `login_max_failures: int`, `login_failure_window_seconds: int`, and `login_lockout_seconds: int`.

- [ ] **Step 1: Write failing settings and Cookie tests**

Add these tests near the existing settings tests in `tests/test_routes.py`:

```python
def test_web_security_settings_have_safe_compatible_defaults():
    from app.settings import Settings

    settings = Settings(session_secret="valid-session-secret", secret_key=VALID_FERNET_KEY)

    assert settings.session_cookie_secure is False
    assert settings.login_max_failures == 5
    assert settings.login_failure_window_seconds == 900
    assert settings.login_lockout_seconds == 900


@pytest.mark.parametrize(
    "field_name",
    ["login_max_failures", "login_failure_window_seconds", "login_lockout_seconds"],
)
@pytest.mark.parametrize("invalid_value", [0, -1])
def test_web_security_integer_settings_reject_non_positive_values(field_name, invalid_value):
    from app.settings import Settings

    values = {
        "session_secret": "valid-session-secret",
        "secret_key": VALID_FERNET_KEY,
        field_name: invalid_value,
    }

    with pytest.raises(ValidationError) as exc_info:
        Settings(**values)

    assert any(error["loc"] == (field_name,) for error in exc_info.value.errors())


@pytest.mark.parametrize(
    ("secure_value", "expects_secure"),
    [("false", False), ("true", True)],
)
def test_session_cookie_security_flags_follow_configuration(monkeypatch, secure_value, expects_secure):
    from fastapi import Request

    _set_required_settings(monkeypatch)
    monkeypatch.setenv("SESSION_COOKIE_SECURE", secure_value)
    create_app, get_settings = _load_create_app()
    app = create_app()

    @app.get("/session-cookie-test")
    def session_cookie_test(request: Request):
        request.session["probe"] = "value"
        return {"ok": True}

    try:
        response = TestClient(app).get("/session-cookie-test")
        cookie = response.headers["set-cookie"].lower()

        assert "httponly" in cookie
        assert "samesite=lax" in cookie
        assert ("secure" in cookie) is expects_secure
    finally:
        get_settings.cache_clear()
```

- [ ] **Step 2: Run focused tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_routes.py::test_web_security_settings_have_safe_compatible_defaults \
  tests/test_routes.py::test_web_security_integer_settings_reject_non_positive_values \
  tests/test_routes.py::test_session_cookie_security_flags_follow_configuration -q
```

Expected: failures because the four settings do not exist and the Cookie does not follow `SESSION_COOKIE_SECURE`.

- [ ] **Step 3: Add validated settings and configure SessionMiddleware**

Add fields to `Settings` in `app/settings.py`:

```python
session_cookie_secure: bool = False
login_max_failures: int = Field(default=5, gt=0)
login_failure_window_seconds: int = Field(default=900, gt=0)
login_lockout_seconds: int = Field(default=900, gt=0)
```

Replace the middleware call in `app/main.py` with:

```python
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret,
    https_only=settings.session_cookie_secure,
    same_site="lax",
)
```

Add to `.env.example` immediately after the existing application settings:

```dotenv
SESSION_COOKIE_SECURE=false
LOGIN_MAX_FAILURES=5
LOGIN_FAILURE_WINDOW_SECONDS=900
LOGIN_LOCKOUT_SECONDS=900
```

- [ ] **Step 4: Run focused tests and verify GREEN**

Run the Step 2 command.

Expected: all parametrized cases pass.

- [ ] **Step 5: Run relevant regression tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_routes.py::test_health_endpoint_returns_ok tests/test_auth.py -q
```

Expected: all existing route/authentication tests pass because CSRF is not wired yet.

- [ ] **Step 6: Commit Cookie configuration**

```bash
git add app/settings.py app/main.py tests/test_routes.py .env.example
git commit -m "feat: configure secure session cookies"
```

---

### Task 2: CSRF Security Primitives

**Files:**
- Create: `app/web_security.py`
- Create: `tests/test_web_security.py`

**Interfaces:**
- Consumes: Starlette `Request.session` and parsed form data.
- Produces: `CSRF_FORM_FIELD`, `CSRF_SESSION_KEY`, `ensure_csrf_token(request: Request) -> str`, `rotate_csrf_token(request: Request) -> str`, and `require_csrf(request: Request) -> None`.

- [ ] **Step 1: Write failing CSRF primitive tests**

Create `tests/test_web_security.py` with a small isolated application:

```python
from io import BytesIO

import pytest
from fastapi import Depends, FastAPI, File, Request, UploadFile
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

from app.web_security import ensure_csrf_token, require_csrf, rotate_csrf_token


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
```

- [ ] **Step 2: Run CSRF tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_web_security.py -q
```

Expected: collection error because `app.web_security` does not exist.

- [ ] **Step 3: Implement CSRF primitives**

Create the initial `app/web_security.py`:

```python
import secrets

from fastapi import HTTPException, Request, status

CSRF_SESSION_KEY = "_csrf_token"
CSRF_FORM_FIELD = "_csrf_token"
CSRF_ERROR = "请求安全校验失败"
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


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
```

- [ ] **Step 4: Run CSRF tests and verify GREEN**

Run the Step 2 command.

Expected: all CSRF primitive and multipart tests pass.

- [ ] **Step 5: Lint and commit primitives**

```bash
.venv/bin/ruff check app/web_security.py tests/test_web_security.py
git add app/web_security.py tests/test_web_security.py
git commit -m "feat: add csrf security primitives"
```

---

### Task 3: CSRF Application, Template, and JavaScript Integration

**Files:**
- Create: `app/templates/_csrf.html`
- Modify: `app/auth.py`
- Modify: `app/routes.py`
- Modify: `app/static/app.js`
- Modify: `app/templates/base.html`
- Modify: `app/templates/login.html`
- Modify: `app/templates/rules.html`
- Modify: `app/templates/rule_form.html`
- Modify: `app/templates/settings.html`
- Modify: `app/templates/sql_server_form.html`
- Modify: `tests/test_auth.py`
- Modify: `tests/test_routes.py`

**Interfaces:**
- Consumes: `ensure_csrf_token` and `require_csrf` from Task 2.
- Produces: Router-wide CSRF enforcement, `csrf_token` in every HTML template context, and hidden fields in every POST form.

- [ ] **Step 1: Write failing integration and template tests**

Add a token extractor to `tests/test_auth.py`:

```python
import re

CSRF_PATTERN = re.compile(r'name="_csrf_token" value="([^"]+)"')


def _csrf_token(client: TestClient) -> str:
    response = client.get("/login")
    match = CSRF_PATTERN.search(response.text)
    assert match is not None
    return match.group(1)


def _csrf_post(client: TestClient, path: str, *, data=None, **kwargs):
    payload = dict(data or {})
    payload["_csrf_token"] = _csrf_token(client)
    return client.post(path, data=payload, **kwargs)
```

Update the existing login and logout tests to use `_csrf_post`. Then add:

```python
def test_login_rejects_missing_csrf_token(auth_app):
    client = TestClient(auth_app)

    response = client.post("/login", data={"username": "admin", "password": "password"})

    assert response.status_code == 403
    assert response.json() == {"detail": "请求安全校验失败"}


def test_login_rotates_csrf_token(auth_app, auth_engine):
    _create_admin_user(auth_engine)
    client = TestClient(auth_app)
    old_token = _csrf_token(client)

    response = client.post(
        "/login",
        data={
            "username": "admin",
            "password": "correct-password",
            "_csrf_token": old_token,
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert client.post("/logout", data={"_csrf_token": old_token}).status_code == 403
```

In `tests/test_routes.py`, add the same `CSRF_PATTERN` and test the rendered forms:

```python
def test_login_page_renders_csrf_hidden_field(monkeypatch):
    _set_required_settings(monkeypatch)
    create_app, get_settings = _load_create_app()
    try:
        response = TestClient(create_app()).get("/login")

        assert response.status_code == 200
        assert CSRF_PATTERN.search(response.text)
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize(
    ("path", "minimum_form_count"),
    [("/rules", 2), ("/rules/new", 2), ("/settings", 3)],
)
def test_admin_pages_render_csrf_for_every_post_form(monkeypatch, session, path, minimum_form_count):
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        response = client.get(path)

        assert response.status_code == 200
        assert response.text.count('name="_csrf_token"') >= minimum_form_count
        assert response.text.count('name="_csrf_token"') == response.text.count('method="post"')
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()
```

The fixture state is intentionally empty. The expected counts are exact for that state: `/rules` has import plus logout, `/rules/new` has rule form plus logout, and `/settings` has SQL Server create, SMTP create, and logout.

Add a static JavaScript assertion:

```python
def test_sql_ajax_requests_include_csrf_token():
    script = Path("app/static/app.js").read_text(encoding="utf-8")

    assert script.count('payload.append("_csrf_token"') == 2
```

Import `Path` from `pathlib` in the test module.

- [ ] **Step 2: Run integration tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_auth.py::test_login_rejects_missing_csrf_token \
  tests/test_auth.py::test_login_rotates_csrf_token \
  tests/test_routes.py::test_login_page_renders_csrf_hidden_field \
  tests/test_routes.py::test_admin_pages_render_csrf_for_every_post_form \
  tests/test_routes.py::test_sql_ajax_requests_include_csrf_token -q
```

Expected: failures because routers do not require CSRF, templates lack hidden fields, and JavaScript omits the token.

- [ ] **Step 3: Wire router dependencies and template token injection**

In both `app/auth.py` and `app/routes.py`, import `require_csrf` and construct the router as:

```python
router = APIRouter(dependencies=[Depends(require_csrf)])
```

In `app/routes.py`, import `ensure_csrf_token` and change `_template_response` context to:

```python
{"request": request, "csrf_token": ensure_csrf_token(request), **context}
```

Create `app/templates/_csrf.html`:

```jinja2
{% macro csrf_input(token) -%}
<input type="hidden" name="_csrf_token" value="{{ token }}">
{%- endmacro %}
```

- [ ] **Step 4: Add hidden fields to every POST form**

In child templates, keep `{% extends "base.html" %}` as the first Jinja tag and put this import immediately after it. In `base.html`, put the import before `<!doctype html>`:

```jinja2
{% from "_csrf.html" import csrf_input %}
```

Insert `{{ csrf_input(csrf_token) }}` immediately after every opening POST `<form>` tag in:

- `base.html`: logout form.
- `login.html`: login form.
- `rules.html`: import form and each manual-run form.
- `rule_form.html`: create/edit form.
- `settings.html`: data-source create/test and SMTP create/test forms.
- `sql_server_form.html`: data-source edit form.

Do not add hidden inputs to the GET filter form in `logs.html`.

- [ ] **Step 5: Include CSRF in both SQL AJAX requests**

In each JavaScript handler, find the hidden field from the parent form and append it to the new `FormData`:

```javascript
const csrfInput = form?.querySelector('[name="_csrf_token"]');
payload.append("_csrf_token", csrfInput?.value || "");
```

Apply this once to SQL syntax validation and once to SQL preview.

- [ ] **Step 6: Preserve existing business tests while keeping dedicated CSRF coverage**

In `_client_with_admin` in `tests/test_routes.py`, import the exact dependency object and override it for business-logic tests:

```python
from app.web_security import require_csrf

app.dependency_overrides[require_csrf] = lambda: None
```

Dedicated CSRF tests must build an application without that override. Update unauthenticated POST tests that still need to reach `require_admin` to first fetch `/login`, extract the Token, and include `_csrf_token`; otherwise their correct new result is `403`.

Add this helper near `_client_with_admin`:

```python
def _csrf_token(client: TestClient) -> str:
    response = client.get("/login")
    match = CSRF_PATTERN.search(response.text)
    assert match is not None
    return match.group(1)


def _post_as_unauthenticated(client: TestClient, path: str, *, data=None, files=None):
    payload = dict(data or {})
    payload["_csrf_token"] = _csrf_token(client)
    return client.post(path, data=payload, files=files)
```

Use `_post_as_unauthenticated` in these existing tests so they continue asserting `401` from `require_admin`: `test_create_rule_requires_admin_session`, `test_import_rules_json_requires_login`, `test_validate_rule_sql_requires_admin_session`, `test_preview_rule_sql_requires_admin_session`, `test_test_sql_server_settings_requires_admin_session`, `test_test_smtp_settings_requires_admin_session`, `test_create_sql_server_settings_requires_admin_session`, and `test_run_rule_requires_login`. For the multipart import test, pass the existing `files` argument through the helper.

The replacement calls are:

```python
response = _post_as_unauthenticated(client, "/rules", data={"name": "x"})
response = _post_as_unauthenticated(
    client,
    "/rules/import",
    files={"file": ("rules.json", json.dumps({"version": 1, "rules": []}), "application/json")},
)
response = _post_as_unauthenticated(client, "/rules/validate-sql", data={"sql_text": "select 1"})
response = _post_as_unauthenticated(client, "/rules/preview-sql", data={"sql_text": "select 1"})
response = _post_as_unauthenticated(client, "/settings/sql-server/1/test")
response = _post_as_unauthenticated(client, "/settings/smtp/1/test")
response = _post_as_unauthenticated(client, "/settings/sql-server", data={"name": "生产库"})
response = _post_as_unauthenticated(client, "/rules/1/run")
```

In existing `tests/test_auth.py` tests, replace each login call with:

```python
login_response = _csrf_post(
    client,
    "/login",
    data={"username": "admin", "password": "correct-password"},
    follow_redirects=False,
)
```

Use the same `_csrf_post` call with `password="wrong-password"` in `test_login_with_wrong_password_returns_400`, and replace logout with:

```python
logout_response = _csrf_post(client, "/logout", follow_redirects=False)
```

- [ ] **Step 7: Run auth, route, and CSRF tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_web_security.py tests/test_auth.py tests/test_routes.py -q
```

Expected: all tests pass, including multipart import and SQL AJAX token assertions.

- [ ] **Step 8: Commit CSRF integration**

```bash
git add app/auth.py app/routes.py app/static/app.js app/templates tests/test_auth.py tests/test_routes.py
git commit -m "feat: enforce csrf protection"
```

---

### Task 4: Thread-Safe Login Rate Limiter

**Files:**
- Modify: `app/web_security.py`
- Modify: `tests/test_web_security.py`

**Interfaces:**
- Consumes: A monotonic `clock: Callable[[], float]` and configured positive integer limits.
- Produces: `client_identifier(request: Request) -> str` and `LoginRateLimiter(max_failures, failure_window_seconds, lockout_seconds, clock=time.monotonic)` with `retry_after`, `record_failure`, and `clear`.

- [ ] **Step 1: Write failing limiter tests with a fake clock**

Append to `tests/test_web_security.py`:

```python
from app.web_security import LoginRateLimiter, client_identifier


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


def test_limiter_locks_on_fifth_failure_and_reports_retry_after(limiter_and_clock):
    limiter, _ = limiter_and_clock

    assert [limiter.record_failure("10.0.0.1", " Admin ") for _ in range(4)] == [0, 0, 0, 0]
    assert limiter.record_failure("10.0.0.1", "admin") == 900
    assert limiter.retry_after("10.0.0.1", "ADMIN") == 900


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
```

- [ ] **Step 2: Run limiter tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_web_security.py -k "limiter or client_identifier" -q
```

Expected: import/attribute failures because the limiter does not exist.

- [ ] **Step 3: Implement limiter state and cleanup**

Extend `app/web_security.py` with:

```python
import math
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from threading import Lock


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
```

- [ ] **Step 4: Run limiter tests and verify GREEN**

Run the Step 2 command, then:

```bash
.venv/bin/python -m pytest tests/test_web_security.py -q
```

Expected: all CSRF and limiter tests pass.

- [ ] **Step 5: Commit limiter core**

```bash
git add app/web_security.py tests/test_web_security.py
git commit -m "feat: add login rate limiter"
```

---

### Task 5: Login Rate-Limit Integration

**Files:**
- Modify: `app/main.py`
- Modify: `app/auth.py`
- Modify: `tests/test_auth.py`

**Interfaces:**
- Consumes: Task 1 settings, Task 2 `rotate_csrf_token`, and Task 4 `LoginRateLimiter`/`client_identifier`.
- Produces: `app.state.login_rate_limiter` and login responses with `400`, `429`, `Retry-After`, clearing, and Session rotation semantics.

- [ ] **Step 1: Write failing login integration tests**

Add these test helpers near the existing CSRF helpers in `tests/test_auth.py`:

```python
class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def make_test_limiter(clock):
    from app.web_security import LoginRateLimiter

    return LoginRateLimiter(
        max_failures=5,
        failure_window_seconds=900,
        lockout_seconds=900,
        clock=clock,
    )
```

Add the integration tests:

```python
def test_login_locks_on_fifth_failure(auth_app, auth_engine):
    from app.web_security import LoginRateLimiter

    _create_admin_user(auth_engine)
    clock = FakeClock()
    auth_app.state.login_rate_limiter = LoginRateLimiter(
        max_failures=5,
        failure_window_seconds=900,
        lockout_seconds=900,
        clock=clock,
    )
    client = TestClient(auth_app)

    responses = [
        _csrf_post(client, "/login", data={"username": "admin", "password": "wrong"})
        for _ in range(5)
    ]

    assert [response.status_code for response in responses] == [400, 400, 400, 400, 429]
    assert responses[-1].headers["retry-after"] == "900"
    assert responses[-1].json() == {"detail": "登录尝试过多，请稍后重试"}


def test_locked_login_skips_password_verification(auth_app, auth_engine, monkeypatch):
    _create_admin_user(auth_engine)
    client = TestClient(auth_app)
    for _ in range(5):
        _csrf_post(client, "/login", data={"username": "admin", "password": "wrong"})

    monkeypatch.setattr("app.auth.verify_password", lambda *args: pytest.fail("must not verify"))

    response = _csrf_post(
        client,
        "/login",
        data={"username": "admin", "password": "correct-password"},
    )

    assert response.status_code == 429


def test_login_lock_expires(auth_app, auth_engine):
    _create_admin_user(auth_engine)
    clock = FakeClock()
    auth_app.state.login_rate_limiter = make_test_limiter(clock)
    client = TestClient(auth_app)
    for _ in range(5):
        _csrf_post(client, "/login", data={"username": "admin", "password": "wrong"})

    clock.advance(901)
    response = _csrf_post(
        client,
        "/login",
        data={"username": "admin", "password": "correct-password"},
        follow_redirects=False,
    )

    assert response.status_code == 303


def test_unknown_user_and_wrong_password_share_limit_behavior(auth_app, auth_engine):
    _create_admin_user(auth_engine)
    unknown_client = TestClient(auth_app, client=("10.0.0.1", 50000))
    wrong_client = TestClient(auth_app, client=("10.0.0.2", 50000))

    unknown = [
        _csrf_post(unknown_client, "/login", data={"username": "missing", "password": "wrong"})
        for _ in range(5)
    ]
    wrong = [
        _csrf_post(wrong_client, "/login", data={"username": "admin", "password": "wrong"})
        for _ in range(5)
    ]

    assert [response.status_code for response in unknown] == [400, 400, 400, 400, 429]
    assert [response.status_code for response in wrong] == [400, 400, 400, 400, 429]


def test_successful_login_clears_previous_failures(auth_app, auth_engine):
    _create_admin_user(auth_engine)
    client = TestClient(auth_app)
    for _ in range(4):
        response = _csrf_post(
            client,
            "/login",
            data={"username": "admin", "password": "wrong"},
        )
        assert response.status_code == 400

    login_response = _csrf_post(
        client,
        "/login",
        data={"username": "admin", "password": "correct-password"},
        follow_redirects=False,
    )
    assert login_response.status_code == 303

    logout_response = _csrf_post(client, "/logout", follow_redirects=False)
    assert logout_response.status_code == 303

    responses = [
        _csrf_post(client, "/login", data={"username": "admin", "password": "wrong"})
        for _ in range(4)
    ]
    assert [response.status_code for response in responses] == [400, 400, 400, 400]
```

- [ ] **Step 2: Run login limiter integration tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_auth.py -k "locks_on_fifth or skips_password or lock_expires or unknown_user or clears" -q
```

Expected: failures because the app does not construct or call the limiter.

- [ ] **Step 3: Construct limiter in `create_app()`**

Import `LoginRateLimiter` in `app/main.py` and add after app creation:

```python
app.state.login_rate_limiter = LoginRateLimiter(
    max_failures=settings.login_max_failures,
    failure_window_seconds=settings.login_failure_window_seconds,
    lockout_seconds=settings.login_lockout_seconds,
)
```

- [ ] **Step 4: Integrate limiter and Session rotation in login**

In `app/auth.py`, import `client_identifier`, `rotate_csrf_token`, and `LoginRateLimiter`. Add:

```python
LOGIN_LIMIT_DETAIL = "登录尝试过多，请稍后重试"


def _raise_login_limited(retry_after: int) -> None:
    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=LOGIN_LIMIT_DETAIL,
        headers={"Retry-After": str(retry_after)},
    )
```

Update `login()` in this order:

```python
limiter: LoginRateLimiter = request.app.state.login_rate_limiter
client_id = client_identifier(request)
retry_after = limiter.retry_after(client_id, username)
if retry_after:
    _raise_login_limited(retry_after)

user = session.exec(select(AdminUser).where(AdminUser.username == username)).first()
if not user or not verify_password(password, user.password_hash):
    retry_after = limiter.record_failure(client_id, username)
    if retry_after:
        _raise_login_limited(retry_after)
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="用户名或密码错误")

limiter.clear(client_id, username)
request.session.clear()
rotate_csrf_token(request)
request.session["admin_user_id"] = user.id
return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
```

- [ ] **Step 5: Run complete authentication and Web-security tests**

```bash
.venv/bin/python -m pytest tests/test_auth.py tests/test_web_security.py -q
```

Expected: all tests pass, including existing login/logout/session deletion behavior.

- [ ] **Step 6: Commit login integration**

```bash
git add app/main.py app/auth.py tests/test_auth.py
git commit -m "feat: throttle failed admin logins"
```

---

### Task 6: Documentation and Full Verification

**Files:**
- Modify: `README.md`
- Modify: `docs/deployment.md`
- Modify: `docs/operations.md`
- Modify: `docs/project-requirements.md`

**Interfaces:**
- Consumes: Implemented CSRF, limiter, and Cookie behavior.
- Produces: Deployment and operator instructions matching runtime behavior.

- [ ] **Step 1: Update README and environment documentation**

Add this paragraph to the README security/deployment guidance, followed by the exact variable list:

```markdown
当前使用 HTTP 时保持 `SESSION_COOKIE_SECURE=false`；完成 HTTPS 反向代理后必须改为 `true`。后台所有修改类表单使用 Session CSRF Token。登录默认在 15 分钟内失败 5 次后锁定 15 分钟。

相关环境变量：`SESSION_COOKIE_SECURE`、`LOGIN_MAX_FAILURES`、`LOGIN_FAILURE_WINDOW_SECONDS`、`LOGIN_LOCKOUT_SECONDS`。
```

- [ ] **Step 2: Update deployment guidance**

Add these four lines to the `.env` example in `docs/deployment.md`:

```dotenv
SESSION_COOKIE_SECURE=false
LOGIN_MAX_FAILURES=5
LOGIN_FAILURE_WINDOW_SECONDS=900
LOGIN_LOCKOUT_SECONDS=900
```

Add this deployment subsection after the environment-variable explanation:

````markdown
### Web 安全配置

当前直接通过 HTTP 访问时使用 `SESSION_COOKIE_SECURE=false`，否则浏览器不会在 HTTP 请求中发送登录 Cookie。完成 HTTPS 反向代理后必须改为 `SESSION_COOKIE_SECURE=true`。Session Cookie 始终使用 `HttpOnly` 和 `SameSite=Lax`。

登录限流默认按“客户端 IP + 去除首尾空白并忽略大小写的用户名”计数：15 分钟内失败 5 次后锁定 15 分钟。状态仅保存在单个 Web 进程内，重启 Web 会清空；不要配置多个 Web Worker，否则各进程不会共享计数。

应用只使用 Uvicorn 校验后的 `request.client.host`，不会直接信任 `X-Forwarded-For`。反向代理与 Uvicorn 在同一服务器时，按实际代理地址启动，例如：

```bash
uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000 --proxy-headers --forwarded-allow-ips=127.0.0.1
```

`--forwarded-allow-ips` 必须只列出真实反向代理地址，不要设置为 `*`。
````

- [ ] **Step 3: Update operations and requirements**

Add troubleshooting entries in `docs/operations.md`:

```markdown
### 请求返回 403

刷新页面后重试，确认提交来自系统页面并携带当前 Session 的 CSRF Token。检查反向代理是否保留 Cookie。

### 登录返回 429

默认同一客户端 IP 和用户名在 15 分钟内失败 5 次后锁定 15 分钟。等待 `Retry-After` 指定时间，并检查是否存在错误密码或自动化尝试。重启 Web 会清空当前单进程限流状态，但不应作为常规解锁方式。
```

Append these exact bullets to `docs/project-requirements.md` under security and deployment/reliability respectively:

```markdown
- 所有 `POST`、`PUT`、`PATCH` 和 `DELETE` 后台请求必须校验签名 Session 中的 CSRF Token；校验失败返回 `403`，且不得执行目标业务逻辑。
- Session Cookie 必须使用 `HttpOnly` 和 `SameSite=Lax`；HTTPS 部署必须设置 `SESSION_COOKIE_SECURE=true`。
- 同一客户端 IP 和规范化用户名默认在 15 分钟内失败 5 次后锁定 15 分钟；未知用户名和错误密码必须使用相同响应，锁定响应返回 `429` 和 `Retry-After`。
- 登录限流状态为线程安全的单 Web 进程内状态，Web 重启会清空，多 Web Worker 不共享；扩展为多实例部署前必须迁移到共享存储。
```

- [ ] **Step 4: Run focused and full verification**

```bash
.venv/bin/python -m pytest tests/test_web_security.py tests/test_auth.py tests/test_routes.py -q
.venv/bin/python -m pytest
.venv/bin/ruff check .
git diff --check
```

Expected: all tests pass, Ruff prints `All checks passed!`, and `git diff --check` has no output.

- [ ] **Step 5: Audit every POST form and route**

Run:

```bash
rg -n '<form[^>]*method="post"|method: "POST"' app/templates app/static/app.js
rg -n '@router.post' app/auth.py app/routes.py
rg -n '_csrf_token|require_csrf' app/templates app/static/app.js app/auth.py app/routes.py
```

Verify each HTML/JavaScript POST source includes `_csrf_token`, both routers include `require_csrf`, the GET log filter remains unchanged, and no password or CSRF token is logged.

- [ ] **Step 6: Review the complete diff against the design**

```bash
git diff --stat
git diff -- app/settings.py app/main.py app/web_security.py app/auth.py app/routes.py app/static/app.js app/templates tests .env.example README.md docs/deployment.md docs/operations.md docs/project-requirements.md
```

Confirm no Redis, database migration, schema field, CAPTCHA, forwarding-header trust, or unrelated UI redesign was introduced.

- [ ] **Step 7: Commit documentation**

```bash
git add README.md docs/deployment.md docs/operations.md docs/project-requirements.md
git commit -m "docs: document web security hardening"
```
