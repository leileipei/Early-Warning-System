# Production Blockers Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复模板注入和 SMTP 证书校验问题，为同一规则增加跨进程执行租约，并让仪表盘展示真实运行数据。

**Architecture:** 保持 FastAPI、SQLModel、SQLite、APScheduler 和现有同步执行模式。模板安全和 SMTP TLS 在现有适配器边界内加固；新增 SQLite 租约表和独立 `execution_lock` 模块提供跨 Web/Worker 互斥；新增 `dashboard` 查询模块为页面提供可测试的真实统计。

**Tech Stack:** Python 3.11+、FastAPI、SQLModel/SQLAlchemy、SQLite、APScheduler、Jinja2 sandbox、smtplib、pytest、Ruff。

## Global Constraints

- 不引入 Redis、Celery、RabbitMQ、PostgreSQL 或新的外部服务。
- 不改变规则 JSON 格式，也不修改现有 SQL Server、SMTP 或预警规则表结构。
- SMTP TLS 不提供跳过证书校验的开关；私有 CA 使用系统信任库或 `SSL_CERT_FILE`。
- 规则执行租约默认 `7200` 秒，配置必须为正整数。
- 租约冲突不创建执行日志；手动入口返回 `409`，Worker 记录警告后跳过。
- 仪表盘“今日”按 `Asia/Shanghai` 自然日计算，“近 24 小时失败”包含 `failed` 和 `partial_failed`。
- 所有生产代码遵循 RED-GREEN-REFACTOR；必须先看到目标测试因缺少行为而失败。
- 完成后应用代码覆盖率不得低于当前 `93%` 基线。
- 不暂存或提交现有未跟踪文件 `docs/superpowers/plans/2026-07-14-alert-rule-archival.md` 和 `docs/superpowers/plans/2026-07-14-settings-configuration-management.md`。

---

### Task 1: Sandbox Email Templates

**Files:**
- Modify: `tests/test_template_renderer.py`
- Modify: `tests/test_executor.py`
- Modify: `app/template_renderer.py`

**Interfaces:**
- Consumes: existing `render_summary(...)`, `render_per_row(...)`, `TemplateRenderError`.
- Produces: module-level sandbox environments used by `_render(template_text: str, context: dict, *, autoescape: bool) -> str`.

- [ ] **Step 1: Write failing sandbox tests**

Append tests that prove dangerous attribute traversal is rejected while normal template features remain available:

```python
@pytest.mark.parametrize(
    "template_text",
    [
        "{{ ''.__class__.__mro__ }}",
        "{{ cycler.__init__.__globals__ }}",
    ],
)
def test_template_renderer_rejects_unsafe_python_access(template_text):
    with pytest.raises(TemplateRenderError):
        render_per_row("预警", template_text, {"id": 1}, {"rule_name": "测试"})


def test_template_renderer_keeps_safe_conditions_and_loops():
    rendered = render_summary(
        "{{ rule_name }}",
        "{% if row_count %}{% for row in rows %}{{ row.id }}{% endfor %}{% endif %}",
        [{"id": 1}, {"id": 2}],
        {"rule_name": "测试", "row_count": 2, "rows": [{"id": 1}, {"id": 2}]},
    )

    assert rendered.subject == "测试"
    assert rendered.html_body == "12"
```

Add an executor regression proving a dangerous body never reaches the mailer:

```python
def test_unsafe_template_fails_without_sending_email():
    mailer = FakeMailer()
    executor = RuleExecutor(sql_client=FakeSqlClient([{"id": 1}]), mailer=mailer)

    result = executor.execute(make_rule(body_template="{{ ''.__class__.__mro__ }}"))

    assert result.status == ExecutionStatus.FAILED
    assert result.error_type == "TemplateRenderError"
    assert mailer.messages == []
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_template_renderer.py::test_template_renderer_rejects_unsafe_python_access \
  tests/test_executor.py::test_unsafe_template_fails_without_sending_email -q
```

Expected: the unsafe templates render successfully or expose Python objects, so at least one test fails for the intended reason.

- [ ] **Step 3: Replace direct Template construction with immutable sandboxes**

Update `app/template_renderer.py` imports and rendering setup:

```python
from jinja2 import StrictUndefined, TemplateError
from jinja2.sandbox import ImmutableSandboxedEnvironment


def _build_environment(*, autoescape: bool) -> ImmutableSandboxedEnvironment:
    environment = ImmutableSandboxedEnvironment(
        undefined=StrictUndefined,
        autoescape=autoescape,
    )
    environment.globals.clear()
    return environment


_SUBJECT_ENVIRONMENT = _build_environment(autoescape=False)
_BODY_ENVIRONMENT = _build_environment(autoescape=True)


def _render(template_text: str, context: dict, *, autoescape: bool) -> str:
    environment = _BODY_ENVIRONMENT if autoescape else _SUBJECT_ENVIRONMENT
    try:
        return environment.from_string(template_text).render(**context)
    except TemplateError as exc:
        raise TemplateRenderError(str(exc)) from exc
```

Keep `_table`, `render_summary`, `render_per_row`, `StrictUndefined`, HTML escaping, and trusted `Markup` behavior unchanged.

- [ ] **Step 4: Run focused and module tests and verify GREEN**

Run:

```bash
.venv/bin/python -m pytest tests/test_template_renderer.py tests/test_executor.py -q
```

Expected: all template and executor tests pass; unsafe access raises `TemplateRenderError`.

- [ ] **Step 5: Commit template sandbox**

```bash
git add app/template_renderer.py tests/test_template_renderer.py tests/test_executor.py
git commit -m "fix: sandbox alert email templates"
```

---

### Task 2: Verify SMTP TLS Certificates

**Files:**
- Modify: `tests/test_executor.py`
- Modify: `app/execution_service.py`

**Interfaces:**
- Consumes: `build_smtp_mailer(config: SmtpConfig) -> SmtpMailer`.
- Produces: SMTP clients configured with a shared `ssl.SSLContext` whose `verify_mode` is `CERT_REQUIRED` and `check_hostname` is `True`.

- [ ] **Step 1: Write failing SSL and STARTTLS tests**

Add lightweight SMTP fakes and two tests in `tests/test_executor.py`:

```python
class FakeCipher:
    def __init__(self, plaintext):
        self.plaintext = plaintext

    def decrypt(self, encrypted_value):
        return self.plaintext


class RecordingSmtpClient:
    def __init__(self):
        self.starttls_context = None

    def starttls(self, *, context):
        self.starttls_context = context

    def login(self, username, password):
        return None


@pytest.mark.parametrize("use_ssl", [False, True])
def test_build_smtp_mailer_uses_verified_tls_context(monkeypatch, use_ssl):
    from ssl import CERT_REQUIRED

    captured = {}
    client = RecordingSmtpClient()

    def fake_smtp(*args, **kwargs):
        captured.update(kwargs)
        return client

    monkeypatch.setattr(execution_service.smtplib, "SMTP", fake_smtp)
    monkeypatch.setattr(execution_service.smtplib, "SMTP_SSL", fake_smtp)
    monkeypatch.setattr(execution_service, "_cipher", lambda: FakeCipher("secret"))
    config = SmtpConfig(
        host="smtp.example.com",
        port=465 if use_ssl else 587,
        username="mailer",
        encrypted_password="encrypted",
        sender="alerts@example.com",
        use_ssl=use_ssl,
        use_tls=not use_ssl,
    )

    mailer = execution_service.build_smtp_mailer(config)
    mailer.client_factory()

    context = captured["context"] if use_ssl else client.starttls_context
    assert context.verify_mode == CERT_REQUIRED
    assert context.check_hostname is True
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_executor.py -k "verified_tls_context" -q
```

Expected: SSL has no `context` keyword and STARTTLS receives no context, causing the assertions to fail.

- [ ] **Step 3: Create and pass the default verified context**

Update `app/execution_service.py`:

```python
import ssl


def build_smtp_mailer(config: SmtpConfig) -> SmtpMailer:
    password = _cipher().decrypt(config.encrypted_password)
    tls_context = ssl.create_default_context()

    def client_factory():
        if config.use_ssl:
            client = smtplib.SMTP_SSL(
                config.host,
                config.port,
                timeout=config.timeout_seconds,
                context=tls_context,
            )
        else:
            client = smtplib.SMTP(config.host, config.port, timeout=config.timeout_seconds)
            if config.use_tls:
                client.starttls(context=tls_context)

        client.login(config.username, password)
        return client

    return SmtpMailer(sender=config.sender, client_factory=client_factory)
```

- [ ] **Step 4: Run focused mail tests and verify GREEN**

Run:

```bash
.venv/bin/python -m pytest tests/test_executor.py tests/test_mailer.py -q
```

Expected: all tests pass and both secure transport modes verify certificates and hostnames.

- [ ] **Step 5: Commit SMTP transport hardening**

```bash
git add app/execution_service.py tests/test_executor.py
git commit -m "fix: verify smtp server certificates"
```

---

### Task 3: Add Rule Execution Lease Primitives

**Files:**
- Create: `app/execution_lock.py`
- Create: `tests/test_execution_lock.py`
- Modify: `app/models.py`
- Modify: `app/settings.py`
- Modify: `tests/test_db.py`
- Modify: `tests/test_routes.py`
- Modify: `.env.example`

**Interfaces:**
- Produces: `RuleExecutionLease` SQLModel.
- Produces: `RuleExecutionInProgressError`.
- Produces: `rule_execution_lease(session: Session, rule_id: int, *, lease_seconds: int, now_fn: Callable[[], datetime] = utc_now) -> Iterator[None]`.
- Produces: `Settings.rule_execution_lease_seconds: int` with default `7200` and `gt=0` validation.

- [ ] **Step 1: Write failing schema and settings tests**

Extend `tests/test_db.py` imports and table assertions:

```python
from app.models import RuleExecutionLease


def test_init_db_creates_rule_execution_lease_table(engine):
    assert "ruleexecutionlease" in inspect(engine).get_table_names()


def test_rule_execution_lease_uses_rule_as_primary_key(session):
    rule = _create_rule(session)
    lease = RuleExecutionLease(
        rule_id=rule.id,
        owner_token="owner-a",
        expires_at=utc_now(),
    )
    session.add(lease)
    session.commit()

    assert session.get(RuleExecutionLease, rule.id).owner_token == "owner-a"


def test_init_db_adds_rule_execution_lease_table_to_existing_database(tmp_path):
    from app.db import create_db_engine, init_db

    database_path = tmp_path / "legacy.sqlite3"
    legacy_engine = create_db_engine(f"sqlite:///{database_path}")
    with legacy_engine.begin() as connection:
        connection.execute(text("CREATE TABLE legacy_marker (id INTEGER PRIMARY KEY)"))

    init_db(legacy_engine)

    assert "ruleexecutionlease" in inspect(legacy_engine).get_table_names()
```

Add `from app.settings import Settings` to `tests/test_routes.py`, then extend its settings tests:

```python
def test_rule_execution_lease_defaults_to_two_hours():
    settings = Settings(session_secret="valid-session-secret", secret_key=VALID_FERNET_KEY)
    assert settings.rule_execution_lease_seconds == 7200


@pytest.mark.parametrize("value", [0, -1])
def test_rule_execution_lease_rejects_non_positive_values(value):
    with pytest.raises(ValidationError):
        Settings(
            session_secret="valid-session-secret",
            secret_key=VALID_FERNET_KEY,
            rule_execution_lease_seconds=value,
        )
```

- [ ] **Step 2: Run schema/settings tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_db.py -k "execution_lease" \
  tests/test_routes.py -k "execution_lease" -q
```

Expected: model, table, and settings field do not exist.

- [ ] **Step 3: Add the lease model and setting**

Add to `app/models.py` after `AlertRule`:

```python
class RuleExecutionLease(SQLModel, table=True):
    rule_id: int = Field(foreign_key="alertrule.id", primary_key=True)
    owner_token: str
    acquired_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime
```

Add to `app/settings.py`:

```python
rule_execution_lease_seconds: int = Field(default=7200, gt=0)
```

Add to `.env.example`:

```dotenv
RULE_EXECUTION_LEASE_SECONDS=7200
```

- [ ] **Step 4: Add failing lease behavior tests**

Create `tests/test_execution_lock.py` with the complete persistence fixture and imports:

```python
from datetime import datetime, timedelta

import pytest
from sqlmodel import Session

from app.execution_lock import RuleExecutionInProgressError, rule_execution_lease
from app.models import AlertRule, RuleExecutionLease, SendMode, SqlDataSource


@pytest.fixture
def persisted_rule_id(engine):
    with Session(engine) as session:
        source = SqlDataSource(
            name="lease-source",
            host="db.example.com",
            database="erp",
            username="readonly",
            encrypted_password="encrypted",
        )
        session.add(source)
        session.commit()
        session.refresh(source)
        rule = AlertRule(
            name="lease-rule",
            data_source_id=source.id,
            sql_text="select 1 as ok",
            cron_expression="0 9 * * *",
            recipients="ops@example.com",
            subject_template="预警",
            body_template="{{table}}",
            send_mode=SendMode.SUMMARY,
        )
        session.add(rule)
        session.commit()
        session.refresh(rule)
        return rule.id


def test_rule_execution_lease_is_released_after_context(engine, persisted_rule_id):
    with Session(engine) as session:
        with rule_execution_lease(session, persisted_rule_id, lease_seconds=60):
            assert session.get(RuleExecutionLease, persisted_rule_id) is not None
        assert session.get(RuleExecutionLease, persisted_rule_id) is None


def test_rule_execution_lease_rejects_second_session(engine, persisted_rule_id):
    with Session(engine) as first, Session(engine) as second:
        with rule_execution_lease(first, persisted_rule_id, lease_seconds=60):
            with pytest.raises(RuleExecutionInProgressError):
                with rule_execution_lease(second, persisted_rule_id, lease_seconds=60):
                    pytest.fail("second execution must not enter")


def test_expired_rule_execution_lease_can_be_replaced(engine, persisted_rule_id):
    now = datetime(2026, 7, 16, 0, 0, 0)
    with Session(engine) as session:
        session.add(
            RuleExecutionLease(
                rule_id=persisted_rule_id,
                owner_token="stale-owner",
                acquired_at=now - timedelta(minutes=2),
                expires_at=now - timedelta(minutes=1),
            )
        )
        session.commit()
        with rule_execution_lease(
            session,
            persisted_rule_id,
            lease_seconds=60,
            now_fn=lambda: now,
        ):
            assert session.get(RuleExecutionLease, persisted_rule_id).owner_token != "stale-owner"


def test_rule_execution_lease_releases_after_exception(engine, persisted_rule_id):
    with Session(engine) as session:
        with pytest.raises(RuntimeError, match="boom"):
            with rule_execution_lease(session, persisted_rule_id, lease_seconds=60):
                raise RuntimeError("boom")
        assert session.get(RuleExecutionLease, persisted_rule_id) is None


def test_expired_owner_cannot_release_replacement_lease(engine, persisted_rule_id):
    current = [datetime(2026, 7, 16, 0, 0, 0)]
    first_session = Session(engine)
    second_session = Session(engine)
    first = rule_execution_lease(
        first_session,
        persisted_rule_id,
        lease_seconds=10,
        now_fn=lambda: current[0],
    )
    second = rule_execution_lease(
        second_session,
        persisted_rule_id,
        lease_seconds=10,
        now_fn=lambda: current[0],
    )
    first_entered = False
    second_entered = False
    try:
        first.__enter__()
        first_entered = True
        current[0] += timedelta(seconds=11)
        second.__enter__()
        second_entered = True
        replacement_token = second_session.get(
            RuleExecutionLease,
            persisted_rule_id,
        ).owner_token

        first.__exit__(None, None, None)
        first_entered = False

        second_session.expire_all()
        lease = second_session.get(RuleExecutionLease, persisted_rule_id)
        assert lease is not None
        assert lease.owner_token == replacement_token
    finally:
        if second_entered:
            second.__exit__(None, None, None)
        if first_entered:
            first.__exit__(None, None, None)
        first_session.close()
        second_session.close()
```

- [ ] **Step 5: Run lease tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_execution_lock.py -q
```

Expected: import fails because `app.execution_lock` does not exist.

- [ ] **Step 6: Implement atomic SQLite lease acquisition and owner-checked release**

Create `app/execution_lock.py`:

```python
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import delete
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import Session

from app.models import RuleExecutionLease, utc_now


class RuleExecutionInProgressError(Exception):
    pass


@contextmanager
def rule_execution_lease(
    session: Session,
    rule_id: int,
    *,
    lease_seconds: int,
    now_fn: Callable[[], datetime] = utc_now,
) -> Iterator[None]:
    if lease_seconds < 1:
        raise ValueError("lease_seconds must be positive")

    acquired_at = now_fn()
    owner_token = uuid4().hex
    expires_at = acquired_at + timedelta(seconds=lease_seconds)
    statement = sqlite_insert(RuleExecutionLease).values(
        rule_id=rule_id,
        owner_token=owner_token,
        acquired_at=acquired_at,
        expires_at=expires_at,
    )
    statement = statement.on_conflict_do_update(
        index_elements=[RuleExecutionLease.rule_id],
        set_={
            "owner_token": owner_token,
            "acquired_at": acquired_at,
            "expires_at": expires_at,
        },
        where=RuleExecutionLease.expires_at <= acquired_at,
    )

    try:
        result = session.execute(statement)
        session.commit()
    except Exception:
        session.rollback()
        raise
    if result.rowcount != 1:
        raise RuleExecutionInProgressError(f"rule {rule_id} is already running")

    try:
        yield
    finally:
        session.rollback()
        session.execute(
            delete(RuleExecutionLease).where(
                RuleExecutionLease.rule_id == rule_id,
                RuleExecutionLease.owner_token == owner_token,
            )
        )
        session.commit()
```

- [ ] **Step 7: Run lease, schema, and settings tests and verify GREEN**

Run:

```bash
.venv/bin/python -m pytest tests/test_execution_lock.py tests/test_db.py tests/test_routes.py -k "lease or init_db" -q
```

Expected: all selected tests pass, including old-database `create_all()` behavior.

- [ ] **Step 8: Commit lease primitives**

```bash
git add .env.example app/models.py app/settings.py app/execution_lock.py \
  tests/test_db.py tests/test_routes.py tests/test_execution_lock.py
git commit -m "feat: add cross-process rule execution leases"
```

---

### Task 4: Enforce Leases in Manual and Scheduled Execution

**Files:**
- Modify: `app/execution_service.py`
- Modify: `app/routes.py`
- Modify: `app/worker.py`
- Modify: `tests/test_executor.py`
- Modify: `tests/test_routes.py`
- Modify: `tests/test_worker.py`

**Interfaces:**
- Consumes: `rule_execution_lease(...)`, `RuleExecutionInProgressError`, `Settings.rule_execution_lease_seconds`.
- Preserves: `execute_rule_by_id(...) -> ExecutionLog` for successful acquisition.
- Produces: HTTP 409 manual conflict and Worker warning/skip behavior.

- [ ] **Step 1: Write failing execution-service lease tests**

Add these imports to `tests/test_executor.py`:

```python
from app.execution_lock import RuleExecutionInProgressError, rule_execution_lease
from app.models import RuleExecutionLease
```

Add tests proving the business executor runs inside one lease and releases it on failure:

```python
def test_execute_rule_by_id_rejects_existing_execution_lease(monkeypatch, session):
    data_source = persist_data_source(session)
    persist_smtp_config(session)
    rule = persist_rule(session, data_source)

    with rule_execution_lease(session, rule.id, lease_seconds=60):
        with pytest.raises(RuleExecutionInProgressError):
            execution_service.execute_rule_by_id(session, rule.id, retry_delay_seconds=0)

    assert session.exec(select(ExecutionLog)).all() == []


def test_execute_rule_by_id_releases_lease_after_result_is_persisted(monkeypatch, session):
    data_source = persist_data_source(session)
    persist_smtp_config(session)
    rule = persist_rule(session, data_source)
    monkeypatch.setattr(
        execution_service,
        "build_sql_client",
        lambda source: FakeSqlClient([]),
    )
    monkeypatch.setattr(execution_service, "build_smtp_mailer", lambda config: FakeMailer())

    execution_service.execute_rule_by_id(session, rule.id, retry_delay_seconds=0)

    assert session.get(RuleExecutionLease, rule.id) is None
```

- [ ] **Step 2: Write failing route and Worker conflict tests**

Add `from unittest.mock import Mock` to `tests/test_routes.py`, then add the route conflict test:

```python
def test_run_rule_returns_conflict_when_execution_lease_is_busy(monkeypatch, session):
    from app.execution_lock import RuleExecutionInProgressError

    data_source = _create_data_source(session)
    rule = _create_rule(session, data_source)
    monkeypatch.setattr(
        "app.routes.execute_rule_by_id",
        Mock(side_effect=RuleExecutionInProgressError("busy")),
    )
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        response = client.post(f"/rules/{rule.id}/run")

        assert response.status_code == 409
        assert "规则正在执行，请稍后重试" in response.text
        assert session.exec(select(ExecutionLog)).all() == []
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()
```

Add the Worker callback test:

```python
def test_worker_skips_rule_when_execution_lease_is_busy(caplog):
    from app.execution_lock import RuleExecutionInProgressError
    from app.worker import build_execute_rule_callback

    session_context = Mock()
    session_context.__enter__ = Mock(return_value=session_context)
    session_context.__exit__ = Mock(return_value=False)
    execute_rule = Mock(side_effect=RuleExecutionInProgressError("busy"))
    callback = build_execute_rule_callback(
        session_factory=lambda: session_context,
        execute_rule_by_id_fn=execute_rule,
    )

    with caplog.at_level("WARNING"):
        callback(42)

    execute_rule.assert_called_once()
    assert "rule_id=42" in caplog.text
```

- [ ] **Step 3: Run focused tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_executor.py -k "execution_lease" \
  tests/test_routes.py -k "run_rule and execution" \
  tests/test_worker.py -k "lease" -q
```

Expected: `execute_rule_by_id` ignores the existing lease, the route does not return 409, and Worker propagates the exception.

- [ ] **Step 4: Wrap the complete retry lifecycle in one lease**

Import the lease API in `app/execution_service.py`:

```python
from app.execution_lock import rule_execution_lease
```

Replace `execute_rule_by_id` with the complete retry lifecycle wrapped by one lease:

```python
def execute_rule_by_id(
    session: Session,
    rule_id: int,
    trigger_type: TriggerType = TriggerType.MANUAL,
    *,
    max_attempts: int = 3,
    retry_delay_seconds: float = 1.0,
    sleep_fn=time.sleep,
) -> ExecutionLog:
    rule = session.get(AlertRule, rule_id)
    if rule is None or rule.archived_at is not None:
        raise RuleNotFoundError(f"rule {rule_id} not found")

    with rule_execution_lease(
        session,
        rule_id,
        lease_seconds=get_settings().rule_execution_lease_seconds,
    ):
        started_at = datetime.utcnow()
        total_attempts = max(1, max_attempts)
        result = None
        attempts_used = 0
        for attempt in range(1, total_attempts + 1):
            attempts_used = attempt
            result = _execute_rule_once(session, rule, trigger_type)
            if not _is_retryable_result(result) or attempt == total_attempts:
                break
            if retry_delay_seconds > 0:
                sleep_fn(retry_delay_seconds)

        if result is None:
            result = ExecutionResult(
                status=ExecutionStatus.FAILED,
                error_type="RuntimeError",
                error_message="规则执行失败",
            )

        result = _with_exhausted_retry_message(result, attempts_used)
        return persist_execution_result(
            session=session,
            rule=rule,
            trigger_type=trigger_type,
            result=result,
            started_at=started_at,
            finished_at=datetime.utcnow(),
        )
```

The lease must be acquired after validating that the rule exists and is not archived, but before building SQL/SMTP clients or running the first query. Keep one lease across all retries.

- [ ] **Step 5: Return an actionable 409 from manual execution**

Extend the execution-lock import in `app/routes.py`:

```python
from app.execution_lock import RuleExecutionInProgressError
```

Replace `run_rule` with:

```python
@router.post("/rules/{rule_id}/run")
def run_rule(
    request: Request,
    rule_id: int,
    admin: AdminUser = Depends(require_admin),
    session: Session = Depends(get_session),
):
    _get_active_rule_or_404(session, rule_id)
    try:
        execute_rule_by_id(session, rule_id, trigger_type=TriggerType.MANUAL)
    except RuleExecutionInProgressError:
        return _template_response(
            request,
            "rules.html",
            _rules_page_context(
                request,
                admin,
                session,
                error="规则正在执行，请稍后重试",
            ),
            status_code=409,
        )
    return RedirectResponse("/logs", status_code=303)
```

- [ ] **Step 6: Skip busy scheduled executions without failing the job**

Import the conflict exception in `app/worker.py`:

```python
from app.execution_lock import RuleExecutionInProgressError
```

Replace `build_execute_rule_callback` with:

```python
def build_execute_rule_callback(
    session_factory: Callable[[], Session] | type[Session] | None = None,
    execute_rule_by_id_fn: Callable[[Session, int, TriggerType], object] = execute_rule_by_id,
) -> Callable[[int], None]:
    def execute_rule(rule_id: int) -> None:
        factory = session_factory or (lambda: Session(get_engine()))
        with factory() as session:
            try:
                execute_rule_by_id_fn(session, rule_id, TriggerType.SCHEDULED)
            except RuleExecutionInProgressError:
                logger.warning("规则正在执行，跳过本次调度: rule_id=%s", rule_id)

    return execute_rule
```

Do not catch other exceptions; APScheduler must continue to report unexpected failures.

- [ ] **Step 7: Run execution integration tests and verify GREEN**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_execution_lock.py tests/test_executor.py tests/test_routes.py tests/test_worker.py -q
```

Expected: all tests pass; only one execution enters, manual conflicts return 409, and scheduled conflicts log then return.

- [ ] **Step 8: Commit lease enforcement**

```bash
git add app/execution_service.py app/routes.py app/worker.py \
  tests/test_executor.py tests/test_routes.py tests/test_worker.py
git commit -m "fix: prevent concurrent rule executions"
```

---

### Task 5: Replace the Static Dashboard with Real Metrics

**Files:**
- Create: `app/dashboard.py`
- Create: `tests/test_dashboard.py`
- Modify: `app/routes.py`
- Modify: `app/templates/dashboard.html`
- Modify: `tests/test_routes.py`

**Interfaces:**
- Produces: `build_dashboard_context(session: Session, *, now: datetime | None = None) -> dict`.
- Consumes: `AlertRule`, `ExecutionLog`, `MailLog`, status enums, and `utc_now()`.
- Produces template keys: `enabled_rule_count`, `today_execution_count`, `recent_failure_count`, `recent_executions`, `mail_success_count`, `mail_failure_count`.

- [ ] **Step 1: Write failing dashboard query tests**

Create `tests/test_dashboard.py`:

```python
from datetime import datetime, timedelta

from app.dashboard import build_dashboard_context
from app.models import (
    AlertRule,
    ExecutionLog,
    ExecutionStatus,
    MailLog,
    MailStatus,
    SendMode,
    SqlDataSource,
    TriggerType,
)


def _create_source(session) -> SqlDataSource:
    source = SqlDataSource(
        name="dashboard-source",
        host="db.example.com",
        database="erp",
        username="readonly",
        encrypted_password="encrypted",
    )
    session.add(source)
    session.commit()
    session.refresh(source)
    return source


def _create_rule(
    session,
    source: SqlDataSource,
    name: str,
    *,
    enabled: bool = True,
    archived_at: datetime | None = None,
) -> AlertRule:
    rule = AlertRule(
        name=name,
        data_source_id=source.id,
        sql_text="select 1 as warning",
        cron_expression="0 9 * * *",
        recipients="ops@example.com",
        subject_template="预警",
        body_template="{{ table }}",
        send_mode=SendMode.SUMMARY,
        enabled=enabled,
        archived_at=archived_at,
    )
    session.add(rule)
    session.commit()
    session.refresh(rule)
    return rule


def _create_execution(
    session,
    rule: AlertRule,
    *,
    started_at: datetime,
    status: ExecutionStatus,
    trigger_type: TriggerType = TriggerType.SCHEDULED,
    row_count: int = 1,
    email_count: int = 1,
) -> ExecutionLog:
    execution = ExecutionLog(
        rule_id=rule.id,
        trigger_type=trigger_type,
        status=status,
        started_at=started_at,
        finished_at=started_at + timedelta(seconds=1),
        row_count=row_count,
        email_count=email_count,
    )
    session.add(execution)
    session.commit()
    session.refresh(execution)
    return execution


def _create_mail(
    session,
    execution: ExecutionLog,
    *,
    status: MailStatus,
    sent_at: datetime,
) -> None:
    session.add(
        MailLog(
            execution_log_id=execution.id,
            recipients="ops@example.com",
            subject="预警",
            status=status,
            sent_at=sent_at,
        )
    )
    session.commit()


def test_dashboard_context_uses_real_counts_and_recent_rows(session):
    now = datetime(2026, 7, 16, 2, 0, 0)  # 10:00 Asia/Shanghai
    source = _create_source(session)
    latest_rule = _create_rule(session, source, "最新规则")
    second_rule = _create_rule(session, source, "第二规则")
    _create_rule(session, source, "停用规则", enabled=False)
    _create_rule(session, source, "已归档规则", archived_at=now - timedelta(days=1))

    latest = _create_execution(
        session,
        latest_rule,
        started_at=now - timedelta(hours=1),
        status=ExecutionStatus.FAILED,
    )
    second = _create_execution(
        session,
        second_rule,
        started_at=now - timedelta(hours=2),
        status=ExecutionStatus.PARTIAL_FAILED,
    )
    third = _create_execution(
        session,
        latest_rule,
        started_at=now - timedelta(hours=3),
        status=ExecutionStatus.SUCCESS,
    )
    old = _create_execution(
        session,
        latest_rule,
        started_at=now - timedelta(hours=25),
        status=ExecutionStatus.FAILED,
    )
    for execution in (latest, second, third, latest):
        _create_mail(
            session,
            execution,
            status=MailStatus.SUCCESS,
            sent_at=now - timedelta(minutes=30),
        )
    _create_mail(
        session,
        latest,
        status=MailStatus.FAILED,
        sent_at=now - timedelta(minutes=20),
    )
    _create_mail(
        session,
        old,
        status=MailStatus.FAILED,
        sent_at=now - timedelta(hours=25),
    )

    context = build_dashboard_context(session, now=now)

    assert context["enabled_rule_count"] == 2
    assert context["today_execution_count"] == 3
    assert context["recent_failure_count"] == 2
    assert context["mail_success_count"] == 4
    assert context["mail_failure_count"] == 1
    assert len(context["recent_executions"]) <= 5
    assert context["recent_executions"][0]["rule_name"] == "最新规则"


def test_dashboard_today_count_uses_shanghai_midnight(session):
    now = datetime(2026, 7, 15, 16, 30, 0)
    source = _create_source(session)
    rule = _create_rule(session, source, "边界规则")
    _create_execution(
        session,
        rule,
        started_at=datetime(2026, 7, 15, 16, 0, 0),
        status=ExecutionStatus.SUCCESS,
    )
    _create_execution(
        session,
        rule,
        started_at=datetime(2026, 7, 15, 15, 59, 59),
        status=ExecutionStatus.SUCCESS,
    )
    _create_execution(
        session,
        rule,
        started_at=datetime(2026, 7, 16, 16, 0, 0),
        status=ExecutionStatus.SUCCESS,
    )

    context = build_dashboard_context(session, now=now)

    assert context["today_execution_count"] == 1


def test_dashboard_recent_executions_returns_latest_five(session):
    now = datetime(2026, 7, 16, 2, 0, 0)
    source = _create_source(session)
    rule = _create_rule(session, source, "排序规则")
    for minutes_ago in range(6):
        _create_execution(
            session,
            rule,
            started_at=now - timedelta(minutes=minutes_ago),
            status=ExecutionStatus.SUCCESS,
        )

    context = build_dashboard_context(session, now=now)

    returned_times = [item["log"].started_at for item in context["recent_executions"]]
    assert returned_times == [now - timedelta(minutes=value) for value in range(5)]


def test_dashboard_empty_database_returns_real_zeroes(session):
    context = build_dashboard_context(
        session,
        now=datetime(2026, 7, 16, 2, 0, 0),
    )

    assert context == {
        "enabled_rule_count": 0,
        "today_execution_count": 0,
        "recent_failure_count": 0,
        "recent_executions": [],
        "mail_success_count": 0,
        "mail_failure_count": 0,
    }
```

- [ ] **Step 2: Run query tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard.py -q
```

Expected: import fails because `app.dashboard` does not exist.

- [ ] **Step 3: Implement focused dashboard queries**

Create `app/dashboard.py` with:

```python
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_
from sqlmodel import Session, select

from app.models import (
    AlertRule,
    ExecutionLog,
    ExecutionStatus,
    MailLog,
    MailStatus,
    utc_now,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")


def _shanghai_day_bounds_utc(now: datetime) -> tuple[datetime, datetime]:
    aware_utc = now.replace(tzinfo=UTC)
    local_now = aware_utc.astimezone(SHANGHAI)
    local_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    local_end = local_start + timedelta(days=1)
    return (
        local_start.astimezone(UTC).replace(tzinfo=None),
        local_end.astimezone(UTC).replace(tzinfo=None),
    )


def build_dashboard_context(session: Session, *, now: datetime | None = None) -> dict:
    current = now or utc_now()
    today_start, today_end = _shanghai_day_bounds_utc(current)
    recent_start = current - timedelta(hours=24)

    enabled_rule_count = session.exec(
        select(func.count()).select_from(AlertRule).where(
            AlertRule.archived_at.is_(None),
            AlertRule.enabled == True,  # noqa: E712
        )
    ).one()
    today_execution_count = session.exec(
        select(func.count()).select_from(ExecutionLog).where(
            ExecutionLog.started_at >= today_start,
            ExecutionLog.started_at < today_end,
        )
    ).one()
    recent_failure_count = session.exec(
        select(func.count()).select_from(ExecutionLog).where(
            ExecutionLog.started_at >= recent_start,
            or_(
                ExecutionLog.status == ExecutionStatus.FAILED,
                ExecutionLog.status == ExecutionStatus.PARTIAL_FAILED,
            ),
        )
    ).one()
    recent_rows = session.exec(
        select(ExecutionLog, AlertRule.name)
        .join(AlertRule, AlertRule.id == ExecutionLog.rule_id)
        .order_by(ExecutionLog.started_at.desc())
        .limit(5)
    ).all()

    def mail_count(status: MailStatus) -> int:
        return session.exec(
            select(func.count()).select_from(MailLog).where(
                MailLog.sent_at >= recent_start,
                MailLog.status == status,
            )
        ).one()

    return {
        "enabled_rule_count": enabled_rule_count,
        "today_execution_count": today_execution_count,
        "recent_failure_count": recent_failure_count,
        "recent_executions": [
            {"log": log, "rule_name": rule_name} for log, rule_name in recent_rows
        ],
        "mail_success_count": mail_count(MailStatus.SUCCESS),
        "mail_failure_count": mail_count(MailStatus.FAILED),
    }
```

Keep all timestamps naive UTC to match the existing schema; only the day boundary conversion uses timezone-aware values.

- [ ] **Step 4: Run query tests and verify GREEN**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard.py -q
```

Expected: all metric, ordering, limit, and timezone tests pass.

- [ ] **Step 5: Write a failing dashboard route/template test**

Add this test to `tests/test_routes.py`:

```python
def test_dashboard_uses_real_metrics(monkeypatch, session):
    data_source = _create_data_source(session)
    rule = _create_rule(session, data_source, name="实时规则")
    session.add(
        ExecutionLog(
            rule_id=rule.id,
            trigger_type=TriggerType.MANUAL,
            status=ExecutionStatus.FAILED,
            started_at=datetime.utcnow(),
            row_count=3,
            email_count=1,
        )
    )
    session.commit()
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        response = client.get("/")

        assert response.status_code == 200
        assert 'data-testid="enabled-rule-count">1</strong>' in response.text
        assert 'data-testid="recent-failure-count">1</strong>' in response.text
        assert "实时规则" in response.text
        assert "暂无执行记录" not in response.text
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()
```

Run:

```bash
.venv/bin/python -m pytest tests/test_routes.py -k "dashboard_uses_real" -q
```

Expected: template still renders hard-coded zero values and empty states.

- [ ] **Step 6: Connect the route and render real dashboard data**

Import the query helper in `app/routes.py`:

```python
from app.dashboard import build_dashboard_context
```

Replace the dashboard route with:

```python
@router.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    admin: AdminUser = Depends(require_admin),
    session: Session = Depends(get_session),
):
    return _template_response(
        request,
        "dashboard.html",
        {
            "admin": admin,
            "title": "仪表盘",
            **build_dashboard_context(session),
        },
    )
```

Replace `app/templates/dashboard.html` with:

```html
{% extends "base.html" %}

{% block content %}
<section class="page-header">
  <div class="page-heading">
    <p class="eyebrow">后台</p>
    <h1>仪表盘</h1>
  </div>
</section>

<section class="metric-grid" aria-label="运行概览">
  <article class="panel metric-card">
    <span class="metric-label status-text">启用规则</span>
    <strong class="metric-value" data-testid="enabled-rule-count">{{ enabled_rule_count }}</strong>
  </article>
  <article class="panel metric-card">
    <span class="metric-label status-text">今日执行</span>
    <strong class="metric-value" data-testid="today-execution-count">{{ today_execution_count }}</strong>
  </article>
  <article class="panel metric-card">
    <span class="metric-label status-text">近 24 小时失败</span>
    <strong class="metric-value" data-testid="recent-failure-count">{{ recent_failure_count }}</strong>
  </article>
</section>

<section class="two-column">
  <article class="panel table-panel">
    <h2 class="section-heading">最近执行</h2>
    {% if recent_executions %}
    <div class="table-shell">
      <table>
        <thead>
          <tr>
            <th>规则</th>
            <th>开始时间</th>
            <th>触发</th>
            <th>状态</th>
            <th>行数</th>
            <th>邮件</th>
          </tr>
        </thead>
        <tbody>
          {% for item in recent_executions %}
          <tr>
            <td>{{ item.rule_name }}</td>
            <td>{{ item.log.started_at.strftime("%Y-%m-%d %H:%M:%S") }}</td>
            <td>{{ item.log.trigger_type.value }}</td>
            <td>
              <span class="status-text {% if item.log.status.value == 'success' %}status-success{% elif item.log.status.value == 'running' %}status-warning{% else %}status-danger{% endif %}">
                {{ item.log.status.value }}
              </span>
            </td>
            <td>{{ item.log.row_count }}</td>
            <td>{{ item.log.email_count }}</td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
    {% else %}
    <p class="empty-state">暂无执行记录</p>
    {% endif %}
  </article>
  <article class="panel table-panel">
    <h2 class="section-heading">近 24 小时邮件</h2>
    {% if mail_success_count or mail_failure_count %}
    <div class="table-shell">
      <table>
        <thead>
          <tr>
            <th>状态</th>
            <th>数量</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <td><span class="status-text status-success">成功</span></td>
            <td data-testid="mail-success-count">{{ mail_success_count }}</td>
          </tr>
          <tr>
            <td><span class="status-text status-danger">失败</span></td>
            <td data-testid="mail-failure-count">{{ mail_failure_count }}</td>
          </tr>
        </tbody>
      </table>
    </div>
    {% else %}
    <p class="empty-state">暂无邮件日志</p>
    {% endif %}
  </article>
</section>
{% endblock %}
```

- [ ] **Step 7: Run dashboard and route tests and verify GREEN**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard.py tests/test_routes.py -k "dashboard or navigation" -q
```

Expected: real metrics render and existing authentication/navigation behavior remains intact.

- [ ] **Step 8: Commit the real dashboard**

```bash
git add app/dashboard.py app/routes.py app/templates/dashboard.html \
  tests/test_dashboard.py tests/test_routes.py
git commit -m "feat: show live operational dashboard metrics"
```

---

### Task 6: Document, Verify, and Review the Hardening Release

**Files:**
- Modify: `README.md`
- Modify: `docs/project-requirements.md`
- Modify: `docs/deployment.md`
- Modify: `docs/operations.md`

**Interfaces:**
- Consumes all completed behavior from Tasks 1-5.
- Produces operator guidance and final verification evidence.

- [ ] **Step 1: Update requirements and operator documentation**

Append this section to `README.md`:

```markdown
## 生产加固说明

- 邮件模板运行在不可变 Jinja 沙箱中，禁止访问 Python 内部对象；拦截后规则失败且不会发送邮件。
- SMTP SSL 和 STARTTLS 校验服务器证书及主机名。私有 CA 使用系统信任库或启动前设置 `SSL_CERT_FILE`。
- 同一规则通过 SQLite 租约避免 Web、Worker 和多进程并发执行。租约默认 `7200` 秒，可用 `RULE_EXECUTION_LEASE_SECONDS` 调整。
- 仪表盘展示数据库中的启用规则、上海自然日执行次数、近 24 小时失败、最近执行及邮件结果。
```

Append this acceptance section to `docs/project-requirements.md`:

```markdown
## 10. 生产加固验收补充

1. 主题和正文模板使用 `ImmutableSandboxedEnvironment` 与 `StrictUndefined`；访问私有属性、类型层次或函数全局对象时执行失败，且不调用 SMTP。
2. SMTP SSL 和 STARTTLS 使用默认可信 CA 并校验证书链、有效期及主机名；系统不提供跳过校验的配置开关。
3. 同一规则在手动、定时或多个进程入口中最多只有一个执行者进入 SQL 查询；冲突不创建执行日志。
4. 手动冲突返回 HTTP 409 和“规则正在执行，请稍后重试”；Worker 冲突记录规则 ID 后跳过。
5. 仪表盘显示真实启用规则数、按 `Asia/Shanghai` 自然日计算的今日执行数、近 24 小时失败数、最近 5 次执行和近 24 小时邮件成功/失败数。
6. 旧 SQLite 数据库启动时自动创建 `ruleexecutionlease` 表，无需手工迁移。
```

Append this configuration section to `docs/deployment.md`:

````markdown
## 20. 生产加固配置

### 20.1 规则执行租约

Web 与 Worker 必须连接同一个 SQLite 数据库。默认配置如下：

```dotenv
RULE_EXECUTION_LEASE_SECONDS=7200
```

该值表示进程异常终止后，其他执行者可接管同一规则的等待秒数。规则可能运行超过两小时时，应在 Web 与 Worker 的环境中设置更大的相同值并重启两个服务。本版本没有租约心跳。

### 20.2 SMTP 私有 CA

SMTP SSL 和 STARTTLS 始终校验证书链与主机名。企业私有 CA 应安装到操作系统信任库，或在启动 Web 与 Worker 前设置：

```bash
export SSL_CERT_FILE=/absolute/path/company-ca.pem
```

证书中的主机名必须与 SMTP 配置页面填写的主机一致。系统不提供跳过 SMTP 证书校验的开关。
````

Append these troubleshooting entries to `docs/operations.md`:

```markdown
### 7.12 手动执行返回 409

HTTP 409 和“规则正在执行，请稍后重试”表示同一规则已有执行租约，不是 SQL 或 SMTP 故障。先查看执行日志和 Worker 日志；正常执行结束后租约会自动释放。进程被强制终止时，租约会在 `RULE_EXECUTION_LEASE_SECONDS` 后允许接管。不要直接删除未过期租约。

Worker 日志中的“规则正在执行，跳过本次调度”是预期的互斥跳过，不会创建失败执行记录。若规则正常耗时超过租约时长，应同时调大 Web 和 Worker 的 `RULE_EXECUTION_LEASE_SECONDS` 并重启服务。

### 7.13 SMTP 证书校验失败

出现证书链、证书过期或主机名不匹配错误时，确认 SMTP 主机名与证书一致并检查系统时间。私有 CA 应加入操作系统信任库，或在 Web 与 Worker 启动环境中设置 `SSL_CERT_FILE=/absolute/path/company-ca.pem`。不得通过关闭证书校验规避错误。
```

Do not add claims about Worker heartbeat, deep health checks, log pagination, SQL Server certificate default changes, or queue-based high availability.

- [ ] **Step 2: Run the complete test suite**

Run:

```bash
.venv/bin/python -m pytest -q
```

Expected: all tests pass with zero failures.

- [ ] **Step 3: Run coverage and enforce the baseline**

Run:

```bash
COVERAGE_FILE=/tmp/early-warning-coverage .venv/bin/python -m pytest --cov=app --cov-report=term-missing:skip-covered -q
```

Expected: all tests pass and total application coverage is at least `93%`; no `.coverage` file is created in the repository.

- [ ] **Step 4: Run static and dependency checks**

Run:

```bash
.venv/bin/ruff check app tests
.venv/bin/python -m pip check
git diff --check
```

Expected: Ruff reports `All checks passed!`, pip reports no broken requirements, and `git diff --check` produces no output.

- [ ] **Step 5: Verify the local database and automatic table creation**

Run:

```bash
.venv/bin/python -c "from app.db import init_db; init_db()"
sqlite3 early_warning.sqlite3 "PRAGMA integrity_check; PRAGMA foreign_key_check;"
sqlite3 early_warning.sqlite3 ".tables"
```

Expected: integrity check returns `ok`, foreign-key check returns no rows, and `.tables` includes `ruleexecutionlease`.

- [ ] **Step 6: Run a harmless security regression probe**

Run:

```bash
.venv/bin/python -c '
from app.template_renderer import TemplateRenderError, render_per_row

try:
    render_per_row("x", "{{ \"\".__class__.__mro__ }}", {}, {})
except TemplateRenderError:
    print("sandbox blocked unsafe access")
else:
    raise SystemExit("unsafe template was not blocked")
'
```

Expected: `sandbox blocked unsafe access`.

- [ ] **Step 7: Review scope and working tree**

Run:

```bash
git status -sb
git diff --stat origin/main...HEAD
git log --oneline --decorate -8
```

Expected: only planned implementation/documentation files differ; the two pre-existing untracked plan files remain untouched.

- [ ] **Step 8: Commit documentation and final verification updates**

```bash
git add README.md docs/project-requirements.md docs/deployment.md docs/operations.md
git commit -m "docs: document production blocker hardening"
```

- [ ] **Step 9: Request final code review**

Invoke `superpowers:requesting-code-review` against the complete branch. Address any P0/P1 findings through new RED-GREEN cycles, rerun the full verification commands, and only then use `superpowers:finishing-a-development-branch` to choose merge or push handling.
