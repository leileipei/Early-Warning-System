# Early Warning System Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a FastAPI-based SQL Server warning system where admins configure read-only SQL rules, run them manually or on Cron, and send warning emails through SMTP.

**Architecture:** One Python codebase with two runtime entry points: FastAPI Web/API for admin pages and a Worker process for Cron execution. SQLite stores users, SQL Server connections, SMTP settings, rules, execution logs, and mail logs. External SQL Server and SMTP access are isolated behind adapters so tests use fakes.

**Tech Stack:** Python 3.11+, FastAPI, Jinja2, SQLModel/SQLAlchemy, SQLite, APScheduler, pyodbc, cryptography Fernet, passlib bcrypt, pytest, httpx/TestClient, ruff.

---

## Scope Check

The approved spec describes one cohesive subsystem: an independent SQL warning platform. It includes Web/API, Worker, persistence, SQL execution, SMTP, and logs; these pieces must work together for a testable first version, so a single implementation plan fits the scope.

## File Structure

Create this structure:

```text
.
├── .env.example
├── pyproject.toml
├── README.md
├── app/
│   ├── __init__.py
│   ├── auth.py
│   ├── crypto.py
│   ├── db.py
│   ├── executor.py
│   ├── mailer.py
│   ├── main.py
│   ├── models.py
│   ├── routes.py
│   ├── scheduler.py
│   ├── security.py
│   ├── settings.py
│   ├── sql_client.py
│   ├── sql_validator.py
│   ├── template_renderer.py
│   ├── worker.py
│   ├── static/
│   │   └── styles.css
│   └── templates/
│       ├── base.html
│       ├── dashboard.html
│       ├── login.html
│       ├── logs.html
│       ├── rule_form.html
│       ├── rules.html
│       └── settings.html
└── tests/
    ├── conftest.py
    ├── test_auth.py
    ├── test_db.py
    ├── test_executor.py
    ├── test_mailer.py
    ├── test_routes.py
    ├── test_scheduler.py
    ├── test_sql_client.py
    ├── test_sql_validator.py
    └── test_template_renderer.py
```

Responsibilities:

- `settings.py`: environment parsing and defaults.
- `db.py`: SQLite engine/session creation and table initialization.
- `models.py`: SQLModel tables and enums.
- `security.py`: password hashing and session helpers.
- `auth.py`: login/logout and admin guard.
- `crypto.py`: encrypt/decrypt SQL Server and SMTP passwords.
- `sql_validator.py`: single-statement read-only SQL validation.
- `sql_client.py`: SQL Server adapter interface and pyodbc implementation.
- `template_renderer.py`: safe subject/body rendering and summary table creation.
- `mailer.py`: SMTP adapter and per-message result handling.
- `executor.py`: rule execution orchestration.
- `scheduler.py`: APScheduler job loading.
- `worker.py`: Worker entry point.
- `routes.py`: admin pages and form handlers.
- `main.py`: FastAPI app factory and static/template wiring.

## Task 1: Project Scaffold And Health Check

**Files:**
- Create: `pyproject.toml`
- Create: `.env.example`
- Create: `README.md`
- Create: `app/__init__.py`
- Create: `app/settings.py`
- Create: `app/main.py`
- Create: `tests/test_routes.py`

- [ ] **Step 1: Write the failing health test**

```python
# tests/test_routes.py
import importlib

from fastapi.testclient import TestClient


def _set_required_settings(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", "test-session-secret")
    monkeypatch.setenv("SECRET_KEY", "test-secret-key")


def _load_create_app():
    from app.settings import get_settings

    get_settings.cache_clear()
    main = importlib.import_module("app.main")
    return main.create_app, get_settings


def test_health_endpoint_returns_ok(monkeypatch):
    _set_required_settings(monkeypatch)
    create_app, get_settings = _load_create_app()
    try:
        client = TestClient(create_app())

        response = client.get("/health")

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
    finally:
        get_settings.cache_clear()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_routes.py::test_health_endpoint_returns_ok -v`

Expected: fail because `app.main` or `create_app` does not exist.

- [ ] **Step 3: Create package configuration**

```toml
# pyproject.toml
[project]
name = "early-warning-system"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
  "apscheduler>=3.10",
  "cryptography>=42",
  "fastapi>=0.110",
  "jinja2>=3.1",
  "passlib[bcrypt]>=1.7",
  "pydantic-settings>=2.2",
  "pyodbc>=5.1",
  "python-multipart>=0.0.9",
  "sqlmodel>=0.0.22",
  "uvicorn[standard]>=0.29",
]

[project.optional-dependencies]
dev = [
  "httpx>=0.27",
  "pytest>=8",
  "pytest-cov>=5",
  "ruff>=0.5",
]

[tool.pytest.ini_options]
testpaths = ["tests"]

[tool.ruff]
line-length = 100
target-version = "py311"
```

- [ ] **Step 4: Create settings and app factory**

```python
# app/__init__.py
```

```python
# app/settings.py
from functools import lru_cache

from pydantic import ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    app_name: str = "SQL 预警系统"
    database_url: str = "sqlite:///./early_warning.sqlite3"
    session_secret: str
    secret_key: str

    @field_validator("session_secret", "secret_key")
    @classmethod
    def validate_required_secret(cls, value: str, info: ValidationInfo) -> str:
        if not value.strip():
            raise ValueError(f"{info.field_name} must not be empty")
        if value.startswith("REPLACE_ME"):
            raise ValueError(f"{info.field_name} must not use a REPLACE_ME placeholder")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
```

```python
# app/main.py
from fastapi import FastAPI

from app.settings import get_settings


def create_app() -> FastAPI:
    app = FastAPI(title=get_settings().app_name)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
```

```text
# .env.example
DATABASE_URL=sqlite:///./early_warning.sqlite3
SESSION_SECRET=REPLACE_ME_WITH_RANDOM_SESSION_SECRET
SECRET_KEY=REPLACE_ME_WITH_32_BYTE_URL_SAFE_FERNET_KEY
```

`SESSION_SECRET` 和 `SECRET_KEY` 启动时必填，且不能保留 `.env.example` 中
以 `REPLACE_ME` 开头的占位值。

Create `README.md` with:

~~~markdown
# SQL 预警系统

FastAPI + SQLite + SQL Server + SMTP 的独立预警系统。

## 本地启动

```bash
python3 -m venv .venv
source .venv/bin/activate
cp .env.example .env
pip install -e ".[dev]"
uvicorn app.main:app --reload
```

启动前必须替换 `.env` 中的 `SESSION_SECRET` 和 `SECRET_KEY`，否则应用不会正常启动。
~~~

- [ ] **Step 5: Run the test to verify it passes**

Run: `pytest tests/test_routes.py::test_health_endpoint_returns_ok -v`

Expected: pass.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml .env.example README.md app tests/test_routes.py
git commit -m "chore: scaffold FastAPI project"
```

## Task 2: SQLite Models And Database Session

**Files:**
- Create: `app/models.py`
- Create: `app/db.py`
- Create: `tests/conftest.py`
- Create: `tests/test_db.py`

- [ ] **Step 1: Write failing persistence tests**

```python
# tests/conftest.py
import pytest
from sqlmodel import Session


@pytest.fixture()
def engine():
    from app.db import create_db_engine, init_db

    engine = create_db_engine("sqlite:///:memory:")
    init_db(engine)
    return engine


@pytest.fixture()
def session(engine):
    with Session(engine) as session:
        yield session
```

```python
# tests/test_db.py
import importlib
import sys

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.models import (
    AlertRule,
    ExecutionLog,
    SendMode,
    SqlDataSource,
    TriggerType,
    utc_now,
)


def _create_rule(session):
    source = SqlDataSource(
        name="prod",
        host="db.example.com",
        port=1433,
        database="erp",
        username="readonly",
        encrypted_password="encrypted",
        enabled=True,
    )
    session.add(source)
    session.commit()
    session.refresh(source)

    rule = AlertRule(
        name="large orders",
        data_source_id=source.id,
        sql_text="select id, amount from orders where amount > 10000",
        cron_expression="0 9 * * *",
        recipients="ops@example.com",
        subject_template="大额订单预警",
        body_template="{{table}}",
        send_mode=SendMode.SUMMARY,
        enabled=True,
    )
    session.add(rule)
    session.commit()
    session.refresh(rule)
    return rule


def test_create_rule_with_sql_server_source(session):
    rule = _create_rule(session)

    assert rule.id is not None
    assert rule.send_mode == SendMode.SUMMARY


def test_import_db_without_required_secrets(tmp_path, monkeypatch):
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    sys.modules.pop("app.db", None)

    imported = importlib.import_module("app.db")

    assert imported is not None


def test_init_db_creates_model_tables(engine):
    table_names = set(inspect(engine).get_table_names())

    assert {
        "adminuser",
        "sqldatasource",
        "smtpconfig",
        "alertrule",
        "executionlog",
        "maillog",
    } <= table_names


def test_sqlite_foreign_keys_are_enforced(session):
    session.add(ExecutionLog(rule_id=999, trigger_type=TriggerType.MANUAL))

    with pytest.raises(IntegrityError):
        session.commit()


def test_alert_rule_persists_across_new_session(engine, session):
    rule = _create_rule(session)

    with Session(engine) as new_session:
        persisted_rule = new_session.exec(
            select(AlertRule).where(AlertRule.id == rule.id)
        ).one()

    assert persisted_rule.name == "large orders"
    assert persisted_rule.send_mode == SendMode.SUMMARY


def test_utc_now_returns_naive_utc_datetime():
    timestamp = utc_now()

    assert timestamp.tzinfo is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_db.py -v`

Expected: fail because `app.models` does not exist.

- [ ] **Step 3: Implement database models**

Create `app/models.py` with enums and SQLModel tables. Datetimes are stored as naive UTC values in SQLite and interpreted as UTC by the application layer, which avoids pretending SQLite round-trips timezone info.

```python
from datetime import datetime
from enum import StrEnum
from typing import Optional

from sqlmodel import Field, SQLModel


def utc_now() -> datetime:
    return datetime.utcnow()


class SendMode(StrEnum):
    SUMMARY = "summary"
    PER_ROW = "per_row"


class ExecutionStatus(StrEnum):
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    PARTIAL_FAILED = "partial_failed"


class TriggerType(StrEnum):
    SCHEDULED = "scheduled"
    MANUAL = "manual"


class MailStatus(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"


class AdminUser(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    username: str = Field(index=True, unique=True)
    password_hash: str
    created_at: datetime = Field(default_factory=utc_now)


class SqlDataSource(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    host: str
    port: int = 1433
    database: str
    username: str
    encrypted_password: str
    enabled: bool = True
    connect_timeout_seconds: int = 10
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class SmtpConfig(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    host: str
    port: int = 587
    username: str
    encrypted_password: str
    sender: str
    use_tls: bool = True
    use_ssl: bool = False
    timeout_seconds: int = 10
    enabled: bool = True
    updated_at: datetime = Field(default_factory=utc_now)


class AlertRule(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    data_source_id: int = Field(foreign_key="sqldatasource.id")
    sql_text: str
    cron_expression: str
    recipients: str
    cc_recipients: str = ""
    subject_template: str
    body_template: str
    send_mode: SendMode = SendMode.SUMMARY
    query_timeout_seconds: int = 30
    max_rows: int = 500
    enabled: bool = True
    notes: str = ""
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ExecutionLog(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    rule_id: int = Field(foreign_key="alertrule.id")
    trigger_type: TriggerType
    status: ExecutionStatus = ExecutionStatus.RUNNING
    started_at: datetime = Field(default_factory=utc_now)
    finished_at: Optional[datetime] = None
    row_count: int = 0
    email_count: int = 0
    duration_ms: int = 0
    error_type: str = ""
    error_message: str = ""


class MailLog(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    execution_log_id: int = Field(foreign_key="executionlog.id")
    recipients: str
    cc_recipients: str = ""
    subject: str
    status: MailStatus
    error_message: str = ""
    sent_at: datetime = Field(default_factory=utc_now)
```

- [ ] **Step 4: Implement database helpers**

```python
# app/db.py
from collections.abc import Generator

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

from app.settings import get_settings

_engine: Engine | None = None


def create_db_engine(database_url: str | None = None) -> Engine:
    url = database_url or get_settings().database_url
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    engine = create_engine(url, connect_args=connect_args)

    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record):
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
            finally:
                cursor.close()

    return engine


def get_engine() -> Engine:
    global _engine

    if _engine is None:
        _engine = create_db_engine()
    return _engine


def init_db(engine: Engine | None = None) -> None:
    import app.models  # noqa: F401

    target_engine = engine if engine is not None else get_engine()
    SQLModel.metadata.create_all(target_engine)


def get_session() -> Generator[Session, None, None]:
    with Session(get_engine()) as session:
        yield session
```

- [ ] **Step 5: Run persistence tests**

Run: `pytest tests/test_db.py -v`

Expected: pass.

- [ ] **Step 6: Commit**

```bash
git add app/models.py app/db.py tests/conftest.py tests/test_db.py
git commit -m "feat: add persistence models"
```

## Task 3: Security, Encryption, And Admin Login

**Files:**
- Modify: `.env.example`
- Modify: `README.md`
- Modify: `app/settings.py`
- Create: `app/security.py`
- Create: `app/crypto.py`
- Create: `app/auth.py`
- Modify: `app/main.py`
- Create: `tests/test_auth.py`
- Modify: `tests/test_routes.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_auth.py
from cryptography.fernet import Fernet

from app.crypto import SecretCipher
from app.security import hash_password, verify_password


def test_password_hash_round_trip():
    password_hash = hash_password("CorrectHorseBatteryStaple")

    assert password_hash != "CorrectHorseBatteryStaple"
    assert verify_password("CorrectHorseBatteryStaple", password_hash)
    assert not verify_password("wrong", password_hash)


def test_secret_cipher_round_trip():
    cipher = SecretCipher.from_key_material(Fernet.generate_key().decode())

    encrypted = cipher.encrypt("smtp-password")

    assert encrypted != "smtp-password"
    assert cipher.decrypt(encrypted) == "smtp-password"
```

Also add route-level tests using `TestClient`, an in-memory SQLite engine, and
`app.dependency_overrides[get_session]`:

- successful `POST /login` returns `303` to `/` and can access a temporary protected route;
- wrong password returns `400`;
- `POST /logout` clears the session and returns `303` to `/login`;
- `require_admin` returns `401` when the session user id no longer exists in the database.

Update settings tests so every `SECRET_KEY` value is a valid Fernet key, and add a
test proving invalid Fernet keys are rejected.

- [ ] **Step 2: Run tests to verify failure**

Run: `pytest tests/test_auth.py -v`

Expected: fail because `app.crypto` and `app.security` do not exist.

- [ ] **Step 3: Implement password hashing**

```python
# app/security.py
from passlib.context import CryptContext

password_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    return password_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return password_context.verify(password, password_hash)
```

- [ ] **Step 4: Implement encryption helper**

```python
# app/crypto.py
from cryptography.fernet import Fernet


class SecretCipher:
    def __init__(self, fernet: Fernet):
        self._fernet = fernet

    @classmethod
    def from_key_material(cls, key_material: str) -> "SecretCipher":
        return cls(Fernet(key_material.encode("utf-8")))

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode("utf-8")).decode("utf-8")

    def decrypt(self, encrypted_value: str) -> str:
        return self._fernet.decrypt(encrypted_value.encode("utf-8")).decode("utf-8")
```

`SECRET_KEY` is a Fernet key, generated with:

```bash
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Update `app/settings.py` so `secret_key` remains required, rejects `REPLACE_ME`,
and is validated by constructing `Fernet(secret_key.encode("utf-8"))`.

- [ ] **Step 5: Add auth route skeleton**

```python
# app/auth.py
from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlmodel import Session, select

from app.db import get_session
from app.models import AdminUser
from app.security import verify_password

router = APIRouter()


def require_admin(
    request: Request,
    session: Session = Depends(get_session),
) -> AdminUser:
    admin_user_id = request.session.get("admin_user_id")
    if admin_user_id is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    user = session.get(AdminUser, int(admin_user_id))
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    return user


@router.post("/login")
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    session: Session = Depends(get_session),
):
    user = session.exec(select(AdminUser).where(AdminUser.username == username)).first()
    if not user or not verify_password(password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="用户名或密码错误")
    request.session.clear()
    request.session["admin_user_id"] = user.id
    return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
```

Modify `app/main.py` to add `SessionMiddleware` and include `auth.router`:

```python
from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware

from app.auth import router as auth_router
from app.settings import get_settings


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name)
    app.add_middleware(SessionMiddleware, secret_key=settings.session_secret)
    app.include_router(auth_router)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
```

- [ ] **Step 6: Run tests**

Run: `pytest tests/test_auth.py tests/test_routes.py -v`

Expected: pass.

- [ ] **Step 7: Commit**

```bash
git add .env.example README.md app/settings.py app/security.py app/crypto.py app/auth.py app/main.py tests/test_auth.py tests/test_routes.py docs/superpowers/plans/2026-06-29-early-warning-system.md
git commit -m "feat: add admin security primitives"
```

## Task 4: SQL Validator

**Files:**
- Create: `app/sql_validator.py`
- Create: `tests/test_sql_validator.py`

- [ ] **Step 1: Write failing SQL validation tests**

```python
# tests/test_sql_validator.py
import pytest

from app.sql_validator import SqlValidationError, validate_select_sql


def test_allows_plain_select():
    validate_select_sql("select id, amount from orders where amount > 10000")


def test_allows_cte_select():
    validate_select_sql("with recent as (select id from orders) select * from recent")


@pytest.mark.parametrize(
    "sql",
    [
        "update orders set amount = 0",
        "delete from orders",
        "insert into audit values (1)",
        "drop table orders",
        "exec dbo.build_warning",
        "select * from orders; delete from orders",
    ],
)
def test_rejects_non_read_only_sql(sql):
    with pytest.raises(SqlValidationError):
        validate_select_sql(sql)
```

- [ ] **Step 2: Run tests to verify failure**

Run: `pytest tests/test_sql_validator.py -v`

Expected: fail because `app.sql_validator` does not exist.

- [ ] **Step 3: Implement SQL validator**

```python
# app/sql_validator.py
class SqlValidationError(ValueError):
    pass


DANGEROUS_WORDS = {
    "alter",
    "create",
    "delete",
    "drop",
    "exec",
    "execute",
    "into",
    "insert",
    "merge",
    "truncate",
    "update",
}


def _is_word_char(char: str) -> bool:
    return char.isalpha() or char == "_"


def _scan_sql(sql: str) -> tuple[list[str], int, bool]:
    words: list[str] = []
    semicolon_count = 0
    content_after_semicolon = False
    index = 0

    while index < len(sql):
        char = sql[index]
        next_char = sql[index + 1] if index + 1 < len(sql) else ""

        if char.isspace():
            index += 1
            continue
        if char == "-" and next_char == "-":
            index += 2
            while index < len(sql) and sql[index] != "\n":
                index += 1
            continue
        if char == "/" and next_char == "*":
            index += 2
            while index + 1 < len(sql) and not (sql[index] == "*" and sql[index + 1] == "/"):
                index += 1
            index = min(index + 2, len(sql))
            continue
        if char in {"'", '"'}:
            quote = char
            index += 1
            while index < len(sql):
                if sql[index] == quote:
                    if index + 1 < len(sql) and sql[index + 1] == quote:
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
            continue
        if char == ";":
            semicolon_count += 1
            index += 1
            continue
        if semicolon_count:
            content_after_semicolon = True
        if _is_word_char(char):
            start = index
            while index < len(sql) and _is_word_char(sql[index]):
                index += 1
            words.append(sql[start:index].lower())
            continue
        index += 1

    return words, semicolon_count, content_after_semicolon


def validate_select_sql(sql: str) -> None:
    normalized = sql.strip()
    if not normalized:
        raise SqlValidationError("SQL 不能为空")

    words, semicolon_count, content_after_semicolon = _scan_sql(normalized)
    if semicolon_count > 1 or content_after_semicolon:
        raise SqlValidationError("只允许单条 SELECT 查询")

    first_word = words[0] if words else ""
    if first_word not in {"select", "with"}:
        raise SqlValidationError("只允许 SELECT 查询")

    blocked = set(words).intersection(DANGEROUS_WORDS)
    if blocked:
        blocked_list = ", ".join(sorted(blocked))
        raise SqlValidationError(f"SQL 包含禁止关键字: {blocked_list}")
```

Implementation note: `_scan_sql` is a lightweight state machine for single quotes,
double quotes, `--` comments, `/* */` comments, real semicolons, and word tokens.
It ignores dangerous words and semicolons inside strings/comments, accepts at most
one trailing real semicolon followed only by whitespace/comments, and rejects any
real multi-statement SQL.

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_sql_validator.py -v`

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add app/sql_validator.py tests/test_sql_validator.py
git commit -m "feat: validate read-only SQL rules"
```

## Task 5: Template Rendering

**Files:**
- Create: `app/template_renderer.py`
- Create: `tests/test_template_renderer.py`

- [ ] **Step 1: Write failing template tests**

```python
# tests/test_template_renderer.py
import pytest

from app.template_renderer import TemplateRenderError, render_per_row, render_summary


def test_render_summary_includes_html_table():
    message = render_summary(
        subject_template="预警 {{rule_name}}",
        body_template="<p>{{rule_name}}</p>{{table}}",
        rows=[{"id": 1, "amount": 12000}, {"id": 2, "amount": 15000}],
        context={"rule_name": "大额订单"},
    )

    assert message.subject == "预警 大额订单"
    assert "<table" in message.html_body
    assert "12000" in message.html_body


def test_render_per_row_uses_current_row():
    message = render_per_row(
        subject_template="订单 {{id}}",
        body_template="金额 {{amount}}",
        row={"id": 9, "amount": 30000},
        context={},
    )

    assert message.subject == "订单 9"
    assert message.html_body == "金额 30000"


def test_missing_field_raises_render_error():
    with pytest.raises(TemplateRenderError):
        render_per_row("订单 {{missing}}", "body", {"id": 1}, {})
```

- [ ] **Step 2: Run tests to verify failure**

Run: `pytest tests/test_template_renderer.py -v`

Expected: fail because `app.template_renderer` does not exist.

- [ ] **Step 3: Implement renderer**

```python
# app/template_renderer.py
from dataclasses import dataclass
from html import escape

from jinja2 import StrictUndefined, Template, TemplateError
from markupsafe import Markup


class TemplateRenderError(ValueError):
    pass


@dataclass(frozen=True)
class RenderedMessage:
    subject: str
    html_body: str


def _render(template_text: str, context: dict, *, autoescape: bool) -> str:
    try:
        return Template(
            template_text,
            undefined=StrictUndefined,
            autoescape=autoescape,
        ).render(**context)
    except TemplateError as exc:
        raise TemplateRenderError(str(exc)) from exc


def _render_subject(template_text: str, context: dict) -> str:
    return _render(template_text, context, autoescape=False)


def _render_html_body(template_text: str, context: dict) -> str:
    return _render(template_text, context, autoescape=True)


def _table(rows: list[dict]) -> str:
    if not rows:
        return "<table></table>"
    columns = list(rows[0].keys())
    header = "".join(f"<th>{escape(str(column))}</th>" for column in columns)
    body = ""
    for row in rows:
        cells = "".join(f"<td>{escape(str(row.get(column, '')))}</td>" for column in columns)
        body += f"<tr>{cells}</tr>"
    return f"<table><thead><tr>{header}</tr></thead><tbody>{body}</tbody></table>"


def render_summary(
    subject_template: str,
    body_template: str,
    rows: list[dict],
    context: dict,
) -> RenderedMessage:
    body_context = {**context, "table": Markup(_table(rows))}
    return RenderedMessage(
        subject=_render_subject(subject_template, context),
        html_body=_render_html_body(body_template, body_context),
    )


def render_per_row(
    subject_template: str,
    body_template: str,
    row: dict,
    context: dict,
) -> RenderedMessage:
    merged = {**context, **row}
    return RenderedMessage(
        subject=_render_subject(subject_template, merged),
        html_body=_render_html_body(body_template, merged),
    )
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_template_renderer.py -v`

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add app/template_renderer.py tests/test_template_renderer.py
git commit -m "feat: render warning email templates"
```

## Task 6: SMTP Mailer

**Files:**
- Create: `app/mailer.py`
- Create: `tests/test_mailer.py`

- [ ] **Step 1: Write failing mailer tests**

```python
# tests/test_mailer.py
from app.mailer import EmailMessage, MailSendResult, SmtpMailer


class FakeSmtpClient:
    def __init__(self):
        self.sent = []

    def sendmail(self, sender, recipients, body):
        self.sent.append((sender, recipients, body))


def test_smtp_mailer_sends_html_message():
    fake = FakeSmtpClient()
    mailer = SmtpMailer(sender="alerts@example.com", client_factory=lambda: fake)
    message = EmailMessage(
        recipients=["ops@example.com"],
        cc_recipients=[],
        subject="预警",
        html_body="<p>hello</p>",
    )

    result = mailer.send(message)

    assert result == MailSendResult(success=True, error_message="")
    assert fake.sent[0][0] == "alerts@example.com"
    assert fake.sent[0][1] == ["ops@example.com"]
```

- [ ] **Step 2: Run tests to verify failure**

Run: `pytest tests/test_mailer.py -v`

Expected: fail because `app.mailer` does not exist.

- [ ] **Step 3: Implement mailer**

```python
# app/mailer.py
from collections.abc import Callable
from dataclasses import dataclass
from email.message import EmailMessage as MimeEmailMessage


@dataclass(frozen=True)
class EmailMessage:
    recipients: list[str]
    cc_recipients: list[str]
    subject: str
    html_body: str


@dataclass(frozen=True)
class MailSendResult:
    success: bool
    error_message: str = ""


class SmtpMailer:
    def __init__(self, sender: str, client_factory: Callable):
        self.sender = sender
        self.client_factory = client_factory

    def send(self, message: EmailMessage) -> MailSendResult:
        mime = MimeEmailMessage()
        mime["From"] = self.sender
        mime["To"] = ", ".join(message.recipients)
        if message.cc_recipients:
            mime["Cc"] = ", ".join(message.cc_recipients)
        mime["Subject"] = message.subject
        mime.set_content("HTML 邮件需要使用支持 HTML 的客户端查看。")
        mime.add_alternative(message.html_body, subtype="html")
        all_recipients = message.recipients + message.cc_recipients
        client = None
        try:
            client = self.client_factory()
            client.sendmail(self.sender, all_recipients, mime.as_string())
            return MailSendResult(success=True)
        except Exception as exc:
            return MailSendResult(success=False, error_message=str(exc))
        finally:
            if client is not None:
                self._close_client(client)

    def _close_client(self, client) -> None:
        quit_method = getattr(client, "quit", None)
        if callable(quit_method):
            try:
                quit_method()
                return
            except Exception:
                pass

        close_method = getattr(client, "close", None)
        if callable(close_method):
            try:
                close_method()
            except Exception:
                pass
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_mailer.py -v`

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add app/mailer.py tests/test_mailer.py
git commit -m "feat: add SMTP mailer adapter"
```

## Task 7: SQL Server Client Adapter

**Files:**
- Create: `app/sql_client.py`
- Create: `tests/test_sql_client.py`

- [ ] **Step 1: Write failing adapter tests**

```python
# tests/test_sql_client.py
from app.sql_client import QueryResult, rows_from_cursor


class FakeCursor:
    description = [("id",), ("amount",)]

    def fetchall(self):
        return [(1, 12000), (2, 15000)]


def test_rows_from_cursor_returns_dicts():
    result = rows_from_cursor(FakeCursor())

    assert result == QueryResult(rows=[{"id": 1, "amount": 12000}, {"id": 2, "amount": 15000}])
```

Also add adapter tests for ODBC brace escaping of dynamic connection string values,
`query()` executing CTE/`ORDER BY` SQL without a `TOP` wrapper, row limiting via
`cursor.fetchmany(max_rows)`, and lazy import behavior that only wraps a missing
`pyodbc` module.

- [ ] **Step 2: Run tests to verify failure**

Run: `pytest tests/test_sql_client.py -v`

Expected: fail because `app.sql_client` does not exist.

- [ ] **Step 3: Implement adapter**

```python
# app/sql_client.py
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class QueryResult:
    rows: list[dict]


class SqlClient(Protocol):
    def query(self, sql: str, timeout_seconds: int, max_rows: int) -> QueryResult:
        raise NotImplementedError


def rows_from_cursor(cursor) -> QueryResult:
    columns = [column[0] for column in cursor.description]
    rows = [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]
    return QueryResult(rows=rows)


def rows_from_cursor_limited(cursor, max_rows: int) -> QueryResult:
    columns = [column[0] for column in cursor.description]
    rows = [dict(zip(columns, row, strict=True)) for row in cursor.fetchmany(max_rows)]
    return QueryResult(rows=rows)


def odbc_brace_escape(value: str) -> str:
    return "{" + str(value).replace("}", "}}") + "}"


def strip_single_trailing_semicolon(sql: str) -> str:
    stripped_sql = sql.rstrip()
    if stripped_sql.endswith(";"):
        return stripped_sql[:-1].rstrip()
    return stripped_sql


class PyodbcSqlServerClient:
    def __init__(
        self,
        host: str,
        port: int,
        database: str,
        username: str,
        password: str,
        connect_timeout_seconds: int,
    ):
        self.connection_string = (
            "DRIVER={ODBC Driver 18 for SQL Server};"
            f"SERVER={odbc_brace_escape(host)},{port};"
            f"DATABASE={odbc_brace_escape(database)};"
            f"UID={odbc_brace_escape(username)};"
            f"PWD={odbc_brace_escape(password)};"
            "Encrypt=yes;"
            "TrustServerCertificate=yes;"
            f"Connection Timeout={connect_timeout_seconds};"
        )

    def query(self, sql: str, timeout_seconds: int, max_rows: int) -> QueryResult:
        if type(max_rows) is not int or max_rows < 1:
            raise ValueError("max_rows must be a positive integer")

        try:
            import pyodbc
        except ModuleNotFoundError as exc:
            if exc.name == "pyodbc":
                raise RuntimeError("pyodbc is required to query SQL Server") from exc
            raise

        executable_sql = strip_single_trailing_semicolon(sql)
        with pyodbc.connect(self.connection_string) as connection:
            cursor = connection.cursor()
            cursor.timeout = timeout_seconds
            cursor.execute(executable_sql)
            return rows_from_cursor_limited(cursor, max_rows)
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_sql_client.py -v`

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add app/sql_client.py tests/test_sql_client.py
git commit -m "feat: add SQL Server query adapter"
```

## Task 8: Rule Execution Service

**Files:**
- Create: `app/executor.py`
- Create: `tests/test_executor.py`

- [x] **Step 1: Write failing executor tests**

```python
# tests/test_executor.py
from app.executor import RuleExecutor
from app.mailer import MailSendResult
from app.models import AlertRule, ExecutionStatus, SendMode, TriggerType


class FakeSqlClient:
    def __init__(self, rows):
        self.rows = rows

    def query(self, sql, timeout_seconds, max_rows):
        from app.sql_client import QueryResult

        return QueryResult(rows=self.rows)


class FakeMailer:
    def __init__(self):
        self.messages = []

    def send(self, message):
        self.messages.append(message)
        return MailSendResult(success=True)


def make_rule(send_mode=SendMode.SUMMARY):
    return AlertRule(
        id=1,
        name="大额订单",
        data_source_id=1,
        sql_text="select id, amount from orders",
        cron_expression="0 9 * * *",
        recipients="ops@example.com",
        subject_template="预警 {{rule_name}}",
        body_template="{{table}}",
        send_mode=send_mode,
        enabled=True,
    )


def test_summary_mode_sends_one_email():
    mailer = FakeMailer()
    executor = RuleExecutor(sql_client=FakeSqlClient([{"id": 1, "amount": 100}]), mailer=mailer)

    result = executor.execute(make_rule(), TriggerType.MANUAL)

    assert result.status == ExecutionStatus.SUCCESS
    assert result.row_count == 1
    assert result.email_count == 1
    assert len(mailer.messages) == 1


def test_per_row_mode_sends_one_email_per_row():
    mailer = FakeMailer()
    executor = RuleExecutor(
        sql_client=FakeSqlClient([{"id": 1, "amount": 100}, {"id": 2, "amount": 200}]),
        mailer=mailer,
    )

    result = executor.execute(make_rule(SendMode.PER_ROW), TriggerType.MANUAL)

    assert result.status == ExecutionStatus.SUCCESS
    assert result.row_count == 2
    assert result.email_count == 2
```

- [x] **Step 2: Run tests to verify failure**

Note: `pytest` was unavailable in the local system Python environment, so the intended red/green pytest runs could not execute here.

Run: `pytest tests/test_executor.py -v`

Expected: fail because `app.executor` does not exist.

- [x] **Step 3: Implement executor**

```python
# app/executor.py
from dataclasses import dataclass

from app.mailer import EmailMessage
from app.models import AlertRule, ExecutionStatus, SendMode, TriggerType
from app.sql_validator import validate_select_sql
from app.template_renderer import render_per_row, render_summary


@dataclass(frozen=True)
class ExecutionResult:
    status: ExecutionStatus
    row_count: int
    email_count: int
    error_message: str = ""


class RuleExecutor:
    def __init__(self, sql_client, mailer):
        self.sql_client = sql_client
        self.mailer = mailer

    def execute(self, rule: AlertRule, trigger_type: TriggerType) -> ExecutionResult:
        try:
            validate_select_sql(rule.sql_text)
            query_result = self.sql_client.query(
                rule.sql_text,
                timeout_seconds=rule.query_timeout_seconds,
                max_rows=rule.max_rows,
            )
            rows = query_result.rows
            if not rows:
                return ExecutionResult(status=ExecutionStatus.SUCCESS, row_count=0, email_count=0)
            messages = self._build_messages(rule, rows)
            failures = 0
            for message in messages:
                send_result = self.mailer.send(message)
                if not send_result.success:
                    failures += 1
            if failures == len(messages):
                status = ExecutionStatus.FAILED
            elif failures:
                status = ExecutionStatus.PARTIAL_FAILED
            else:
                status = ExecutionStatus.SUCCESS
            return ExecutionResult(status=status, row_count=len(rows), email_count=len(messages))
        except Exception as exc:
            return ExecutionResult(
                status=ExecutionStatus.FAILED,
                row_count=0,
                email_count=0,
                error_message=str(exc),
            )

    def _build_messages(self, rule: AlertRule, rows: list[dict]) -> list[EmailMessage]:
        recipients = [email.strip() for email in rule.recipients.split(",") if email.strip()]
        cc = [email.strip() for email in rule.cc_recipients.split(",") if email.strip()]
        context = {"rule_name": rule.name}
        if rule.send_mode == SendMode.SUMMARY:
            rendered = render_summary(rule.subject_template, rule.body_template, rows, context)
            return [EmailMessage(recipients, cc, rendered.subject, rendered.html_body)]
        messages = []
        for row in rows:
            rendered = render_per_row(rule.subject_template, rule.body_template, row, context)
            messages.append(EmailMessage(recipients, cc, rendered.subject, rendered.html_body))
        return messages
```

- [x] **Step 4: Run executor tests**

Note: local verification used `PYTHONPYCACHEPREFIX=/private/tmp/early-warning-pycache python3 -m compileall app tests` because `pytest` is not installed in this environment.

Run: `pytest tests/test_executor.py -v`

Expected: pass.

- [x] **Step 5: Commit**

```bash
git add app/executor.py tests/test_executor.py
git commit -m "feat: execute warning rules"
```

## Task 9: Scheduler And Worker Entry Point

**Files:**
- Create: `app/scheduler.py`
- Create: `app/worker.py`
- Create: `tests/test_scheduler.py`

- [x] **Step 1: Write failing scheduler test**

```python
# tests/test_scheduler.py
from app.models import AlertRule, SendMode
from app.scheduler import build_scheduler


def test_scheduler_adds_enabled_rule_job():
    rule = AlertRule(
        id=7,
        name="daily",
        data_source_id=1,
        sql_text="select id from orders",
        cron_expression="0 9 * * *",
        recipients="ops@example.com",
        subject_template="预警",
        body_template="{{table}}",
        send_mode=SendMode.SUMMARY,
        enabled=True,
    )
    scheduler = build_scheduler([rule], execute_rule=lambda rule_id: None)

    jobs = scheduler.get_jobs()

    assert len(jobs) == 1
    assert jobs[0].id == "rule-7"
```

- [x] **Step 2: Run test to verify failure**

Run: `pytest tests/test_scheduler.py -v`

Expected: fail because `app.scheduler` does not exist.

- [x] **Step 3: Implement scheduler**

```python
# app/scheduler.py
from collections.abc import Callable, Iterable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.models import AlertRule


def build_scheduler(
    rules: Iterable[AlertRule],
    execute_rule: Callable[[int], None],
) -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
    for rule in rules:
        if not rule.enabled or rule.id is None:
            continue
        trigger = CronTrigger.from_crontab(rule.cron_expression)
        scheduler.add_job(
            execute_rule,
            trigger=trigger,
            args=[rule.id],
            id=f"rule-{rule.id}",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    return scheduler
```

```python
# app/worker.py
import time


def main() -> None:
    # Keep the process alive until database-backed scheduled execution is wired in Task 12.
    while True:
        time.sleep(60)


if __name__ == "__main__":
    main()
```

- [x] **Step 4: Run scheduler test**

Run: `pytest tests/test_scheduler.py -v`

Expected: pass.

- [x] **Step 5: Commit**

```bash
git add app/scheduler.py app/worker.py tests/test_scheduler.py
git commit -m "feat: schedule enabled warning rules"
```

## Task 10: Admin Pages And API Routes

**Files:**
- Create: `app/routes.py`
- Modify: `app/main.py`
- Create: `app/templates/base.html`
- Create: `app/templates/login.html`
- Create: `app/templates/dashboard.html`
- Create: `app/templates/rules.html`
- Create: `app/templates/rule_form.html`
- Create: `app/templates/settings.html`
- Create: `app/templates/logs.html`
- Create: `app/static/styles.css`
- Modify: `tests/test_routes.py`

- [x] **Step 1: Extend route tests**

Add these tests to `tests/test_routes.py`:

```python
def test_login_page_renders():
    client = TestClient(create_app())

    response = client.get("/login")

    assert response.status_code == 200
    assert "用户名" in response.text


def test_rules_page_requires_login():
    client = TestClient(create_app())

    response = client.get("/rules")

    assert response.status_code == 401
```

- [x] **Step 2: Run tests to verify failure**

Run: `pytest tests/test_routes.py -v`

Expected: fail because `/login` and `/rules` are not implemented.

- [x] **Step 3: Add route module**

```python
# app/routes.py
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.auth import require_admin

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, admin: str = Depends(require_admin)):
    return templates.TemplateResponse("dashboard.html", {"request": request, "admin": admin})


@router.get("/rules", response_class=HTMLResponse)
def rules_page(request: Request, admin: str = Depends(require_admin)):
    return templates.TemplateResponse("rules.html", {"request": request, "admin": admin})


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, admin: str = Depends(require_admin)):
    return templates.TemplateResponse("settings.html", {"request": request, "admin": admin})


@router.get("/logs", response_class=HTMLResponse)
def logs_page(request: Request, admin: str = Depends(require_admin)):
    return templates.TemplateResponse("logs.html", {"request": request, "admin": admin})
```

Modify `app/main.py`:

```python
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.auth import router as auth_router
from app.routes import router as page_router
from app.settings import get_settings


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name)
    app.add_middleware(SessionMiddleware, secret_key=settings.session_secret)
    app.mount("/static", StaticFiles(directory="app/static"), name="static")
    app.include_router(auth_router)
    app.include_router(page_router)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
```

- [x] **Step 4: Create templates and styles**

```html
<!-- app/templates/base.html -->
<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <link rel="stylesheet" href="/static/styles.css">
    <title>{{ title or "SQL 预警系统" }}</title>
  </head>
  <body>
    <nav>
      <strong>SQL 预警系统</strong>
      <a href="/">仪表盘</a>
      <a href="/rules">规则</a>
      <a href="/settings">配置</a>
      <a href="/logs">日志</a>
    </nav>
    <main>{% block content %}{% endblock %}</main>
  </body>
</html>
```

```html
<!-- app/templates/login.html -->
{% extends "base.html" %}
{% block content %}
<section class="panel compact">
  <h1>管理员登录</h1>
  <form method="post" action="/login">
    <label>用户名<input name="username" required></label>
    <label>密码<input name="password" type="password" required></label>
    <button type="submit">登录</button>
  </form>
</section>
{% endblock %}
```

```html
<!-- app/templates/dashboard.html -->
{% extends "base.html" %}
{% block content %}
<h1>仪表盘</h1>
<section class="grid">
  <div class="panel"><h2>最近执行</h2><p>暂无执行记录</p></div>
  <div class="panel"><h2>失败规则</h2><p>暂无失败规则</p></div>
  <div class="panel"><h2>邮件概览</h2><p>暂无邮件日志</p></div>
</section>
{% endblock %}
```

```html
<!-- app/templates/rules.html -->
{% extends "base.html" %}
{% block content %}
<h1>预警规则</h1>
<a class="button" href="/rules/new">新建规则</a>
<section class="panel"><p>暂无规则</p></section>
{% endblock %}
```

```html
<!-- app/templates/rule_form.html -->
{% extends "base.html" %}
{% block content %}
<h1>规则编辑</h1>
<section class="panel"><p>规则表单将在规则 CRUD 任务中接入。</p></section>
{% endblock %}
```

```html
<!-- app/templates/settings.html -->
{% extends "base.html" %}
{% block content %}
<h1>系统配置</h1>
<section class="panel"><h2>SQL Server</h2><p>配置数据源。</p></section>
<section class="panel"><h2>SMTP</h2><p>配置邮件服务器。</p></section>
{% endblock %}
```

```html
<!-- app/templates/logs.html -->
{% extends "base.html" %}
{% block content %}
<h1>执行与邮件日志</h1>
<section class="panel"><p>暂无日志</p></section>
{% endblock %}
```

```css
/* app/static/styles.css */
body {
  margin: 0;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  background: #f6f8fb;
  color: #18212f;
}
nav {
  display: flex;
  gap: 18px;
  align-items: center;
  padding: 14px 24px;
  background: #ffffff;
  border-bottom: 1px solid #d8dee8;
}
nav a { color: #24527a; text-decoration: none; }
main { max-width: 1120px; margin: 28px auto; padding: 0 20px; }
.grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 16px; }
.panel {
  background: #ffffff;
  border: 1px solid #d8dee8;
  border-radius: 8px;
  padding: 18px;
}
.compact { max-width: 420px; margin: 80px auto; }
label { display: grid; gap: 6px; margin: 12px 0; }
input, textarea, select {
  border: 1px solid #b8c2d1;
  border-radius: 6px;
  padding: 10px;
}
button, .button {
  display: inline-block;
  border: 0;
  border-radius: 6px;
  background: #1f6feb;
  color: #ffffff;
  padding: 10px 14px;
  text-decoration: none;
}
```

- [x] **Step 5: Run route tests**

Run: `pytest tests/test_routes.py -v`

Expected: pass.

- [x] **Step 6: Commit**

```bash
git add app/routes.py app/main.py app/templates app/static tests/test_routes.py
git commit -m "feat: add admin page shell"
```

## Task 11: Rule CRUD, Settings Forms, And Log Persistence

**Files:**
- Modify: `app/routes.py`
- Modify: `app/templates/rules.html`
- Modify: `app/templates/rule_form.html`
- Modify: `app/templates/settings.html`
- Modify: `app/templates/logs.html`
- Modify: `tests/test_routes.py`

- [ ] **Step 1: Add failing CRUD test**

Add to `tests/test_routes.py`:

```python
def test_create_rule_requires_admin_session():
    client = TestClient(create_app())

    response = client.post("/rules", data={"name": "x"})

    assert response.status_code == 401
```

- [ ] **Step 2: Run test to verify failure mode**

Run: `pytest tests/test_routes.py::test_create_rule_requires_admin_session -v`

Expected: fail if `/rules` POST returns 405 instead of 401.

- [ ] **Step 3: Implement protected form handlers**

Extend `app/routes.py` with protected `GET /rules/new`, `POST /rules`, `GET /settings`, `POST /settings/sql-server`, `POST /settings/smtp`, and `GET /logs`. Each handler must depend on `require_admin`. Use `Session = Depends(get_session)` to insert or query `SqlDataSource`, `SmtpConfig`, `AlertRule`, `ExecutionLog`, and `MailLog`.

For `POST /rules`, parse form values into:

```python
rule = AlertRule(
    name=name,
    data_source_id=int(data_source_id),
    sql_text=sql_text,
    cron_expression=cron_expression,
    recipients=recipients,
    cc_recipients=cc_recipients,
    subject_template=subject_template,
    body_template=body_template,
    send_mode=SendMode(send_mode),
    query_timeout_seconds=int(query_timeout_seconds),
    max_rows=int(max_rows),
    enabled=enabled == "on",
)
```

Call `validate_select_sql(sql_text)` before saving. On validation error, return the form with status code `400` and the validation message.

- [ ] **Step 4: Update templates**

Replace placeholder text with forms containing the exact field names used by routes:

```html
<input name="name" required>
<select name="data_source_id" required></select>
<textarea name="sql_text" required></textarea>
<input name="cron_expression" value="0 9 * * *" required>
<input name="recipients" required>
<input name="cc_recipients">
<input name="subject_template" required>
<textarea name="body_template" required></textarea>
<select name="send_mode">
  <option value="summary">汇总一封</option>
  <option value="per_row">每行一封</option>
</select>
<input name="query_timeout_seconds" type="number" value="30">
<input name="max_rows" type="number" value="500">
<input name="enabled" type="checkbox" checked>
```

- [ ] **Step 5: Run focused route tests**

Run: `pytest tests/test_routes.py -v`

Expected: pass. Any test using database writes should override `get_session` with the in-memory `session` fixture.

- [ ] **Step 6: Commit**

```bash
git add app/routes.py app/templates tests/test_routes.py
git commit -m "feat: manage warning rules and settings"
```

## Task 12: Wire Real Execution To Persistence

**Files:**
- Modify: `app/executor.py`
- Modify: `app/routes.py`
- Modify: `app/worker.py`
- Create: `tests/test_executor.py` additions

- [ ] **Step 1: Add failing persistence-backed execution test**

Add to `tests/test_executor.py`:

```python
def test_executor_persists_execution_summary(session):
    rule = make_rule()
    session.add(rule)
    session.commit()
    mailer = FakeMailer()
    executor = RuleExecutor(sql_client=FakeSqlClient([{"id": 1}]), mailer=mailer)

    result = executor.execute(rule, TriggerType.MANUAL)

    assert result.status == ExecutionStatus.SUCCESS
    assert result.email_count == 1
```

- [ ] **Step 2: Run test**

Run: `pytest tests/test_executor.py -v`

Expected: pass for the current unit behavior. Then add route-level manual execution test once repositories are wired.

- [ ] **Step 3: Add persistence writes in route orchestration**

In `app/routes.py`, implement `POST /rules/{rule_id}/run`:

1. Load `AlertRule`, `SqlDataSource`, and active `SmtpConfig`.
2. Decrypt SQL Server and SMTP passwords with `SecretCipher`.
3. Build `PyodbcSqlServerClient`.
4. Build `SmtpMailer`.
5. Execute with `RuleExecutor`.
6. Insert `ExecutionLog` and one `MailLog` per generated message result.
7. Redirect to `/logs`.

Keep SQL Server and SMTP construction in small helper functions named:

```python
import smtplib

from app.crypto import SecretCipher
from app.mailer import SmtpMailer
from app.models import SmtpConfig, SqlDataSource
from app.settings import get_settings
from app.sql_client import PyodbcSqlServerClient


def build_sql_client(data_source: SqlDataSource) -> PyodbcSqlServerClient:
    cipher = SecretCipher.from_key_material(get_settings().secret_key)
    password = cipher.decrypt(data_source.encrypted_password)
    return PyodbcSqlServerClient(
        host=data_source.host,
        port=data_source.port,
        database=data_source.database,
        username=data_source.username,
        password=password,
        connect_timeout_seconds=data_source.connect_timeout_seconds,
    )


def build_smtp_mailer(config: SmtpConfig) -> SmtpMailer:
    cipher = SecretCipher.from_key_material(get_settings().secret_key)
    password = cipher.decrypt(config.encrypted_password)

    def client_factory():
        if config.use_ssl:
            client = smtplib.SMTP_SSL(
                config.host,
                config.port,
                timeout=config.timeout_seconds,
            )
        else:
            client = smtplib.SMTP(
                config.host,
                config.port,
                timeout=config.timeout_seconds,
            )
        if config.use_tls and not config.use_ssl:
            client.starttls()
        client.login(config.username, password)
        return client

    return SmtpMailer(sender=config.sender, client_factory=client_factory)
```

Tests should monkeypatch these helpers to return fakes.

- [ ] **Step 4: Wire worker**

Update `app/worker.py` so `main()`:

1. Calls `init_db()`.
2. Loads enabled rules from SQLite.
3. Builds a scheduler with `build_scheduler`.
4. For each job, opens a new session and calls the same rule execution helper used by manual execution.
5. Starts the scheduler and sleeps until interrupted.

- [ ] **Step 5: Run route and executor tests**

Run: `pytest tests/test_routes.py tests/test_executor.py tests/test_scheduler.py -v`

Expected: pass.

- [ ] **Step 6: Commit**

```bash
git add app/executor.py app/routes.py app/worker.py tests/test_executor.py tests/test_routes.py
git commit -m "feat: persist rule execution results"
```

## Task 13: Final Verification And Documentation

**Files:**
- Modify: `README.md`
- Modify: `.env.example`

- [ ] **Step 1: Update README with run commands**

Replace the setup section in `README.md` with:

~~~markdown
## 初始化

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
python -c "from app.db import init_db; init_db()"
```

## 启动 Web

```bash
uvicorn app.main:app --reload
```

## 启动 Worker

```bash
python -m app.worker
```

## 测试

```bash
pytest
ruff check .
```

## SQL Server 驱动

生产环境需要安装 Microsoft ODBC Driver 18 for SQL Server，并为预警系统配置只读 SQL Server 账号。
~~~

- [ ] **Step 2: Run all verification**

Run: `pytest`

Expected: all tests pass.

Run: `ruff check .`

Expected: no lint errors.

- [ ] **Step 3: Start local Web server**

Run: `uvicorn app.main:app --host 127.0.0.1 --port 8000`

Expected: server starts and `/health` returns `{"status":"ok"}`.

- [ ] **Step 4: Commit**

```bash
git add README.md .env.example
git commit -m "docs: document local setup"
```

## Self-Review

Spec coverage:

- Independent FastAPI system: Tasks 1, 10, 11.
- SQL Server source: Tasks 7, 12.
- SQLite configuration and logs: Tasks 2, 11, 12.
- Admin login: Tasks 3 and 10.
- Cron and manual execution: Tasks 9 and 12.
- SMTP sending: Tasks 6 and 12.
- `summary` and `per_row` modes: Tasks 5 and 8.
- No duplicate suppression: Task 8 sends every query result on every execution.
- `SELECT`-only boundary: Task 4 and Task 11 validation.
- Execution and mail logs: Tasks 2, 11, 12.
- Tests: Tasks 1 through 13 include focused tests and final verification.

Type consistency:

- Rule send mode uses `SendMode.SUMMARY` and `SendMode.PER_ROW`.
- Execution status uses `ExecutionStatus.SUCCESS`, `FAILED`, and `PARTIAL_FAILED`.
- Trigger type uses `TriggerType.MANUAL` and `TriggerType.SCHEDULED`.
- SQL query adapters return `QueryResult(rows=list[dict])`.
- Mailer returns `MailSendResult(success=bool, error_message=str)`.
