# Worker Dynamic Rule Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a running Worker reflect rule creation, Cron changes, enablement changes, and removals within a configurable interval without resetting unchanged jobs.

**Architecture:** Keep APScheduler's in-memory `BackgroundScheduler`. Add a focused synchronizer in `app/scheduler.py` that reconciles rule state into scheduler jobs, while `app/worker.py` owns database polling and process lifecycle. A failed database poll preserves the current jobs, and individual scheduler operation failures are logged and retried during the next poll.

**Tech Stack:** Python 3.11+, FastAPI settings via pydantic-settings, SQLModel/SQLite, APScheduler 3.x, pytest, Ruff.

## Global Constraints

- `SCHEDULER_SYNC_INTERVAL_SECONDS` is a positive number with default value `10`.
- Rule changes must be reflected within one configured polling interval.
- Unchanged rules must not be rescheduled because doing so resets their next run time.
- Scheduler job IDs remain `rule-{rule_id}`.
- Scheduled rule jobs retain `max_instances=1`, `coalesce=True`, and `replace_existing=True`.
- Database polling failures preserve existing jobs and are retried on the next cycle.
- Individual job synchronization failures must not stop other rules from synchronizing.
- No Redis, queue, persistent APScheduler JobStore, distributed lock, schema change, or Web UI is added.

---

### Task 1: Configurable Synchronization Interval

**Files:**
- Modify: `app/settings.py`
- Modify: `tests/test_routes.py`
- Modify: `.env.example`

**Interfaces:**
- Consumes: Existing `Settings` and `get_settings()`.
- Produces: `Settings.scheduler_sync_interval_seconds: float` with default `10.0` and strict `gt=0` validation.

- [ ] **Step 1: Write failing settings tests**

Add the import and tests alongside the existing settings tests in `tests/test_routes.py`:

```python
def test_scheduler_sync_interval_defaults_to_ten_seconds():
    from app.settings import Settings

    settings = Settings(
        session_secret="valid-session-secret",
        secret_key=VALID_FERNET_KEY,
    )

    assert settings.scheduler_sync_interval_seconds == 10.0


def test_scheduler_sync_interval_reads_environment(monkeypatch):
    from app.settings import Settings

    monkeypatch.setenv("SCHEDULER_SYNC_INTERVAL_SECONDS", "2.5")

    settings = Settings(
        session_secret="valid-session-secret",
        secret_key=VALID_FERNET_KEY,
    )

    assert settings.scheduler_sync_interval_seconds == 2.5


@pytest.mark.parametrize("value", ["0", "-1"])
def test_scheduler_sync_interval_rejects_non_positive_values(value, monkeypatch):
    from pydantic import ValidationError
    from app.settings import Settings

    monkeypatch.setenv("SCHEDULER_SYNC_INTERVAL_SECONDS", value)

    with pytest.raises(ValidationError) as exc_info:
        Settings(
            session_secret="valid-session-secret",
            secret_key=VALID_FERNET_KEY,
        )

    assert any(error["loc"] == ("scheduler_sync_interval_seconds",) for error in exc_info.value.errors())
```

- [ ] **Step 2: Run the settings tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_routes.py::test_scheduler_sync_interval_defaults_to_ten_seconds \
  tests/test_routes.py::test_scheduler_sync_interval_reads_environment \
  tests/test_routes.py::test_scheduler_sync_interval_rejects_non_positive_values -q
```

Expected: FAIL because `Settings` does not expose `scheduler_sync_interval_seconds` and non-positive values are accepted.

- [ ] **Step 3: Add the validated setting**

Update imports and fields in `app/settings.py`:

```python
from pydantic import Field, ValidationInfo, field_validator


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    app_name: str = "SQL 预警系统"
    database_url: str = "sqlite:///./early_warning.sqlite3"
    scheduler_sync_interval_seconds: float = Field(default=10.0, gt=0)
    session_secret: str
    secret_key: str
```

Add this line to `.env.example` after `DATABASE_URL`:

```dotenv
SCHEDULER_SYNC_INTERVAL_SECONDS=10
```

- [ ] **Step 4: Run the settings tests and verify GREEN**

Run the command from Step 2.

Expected: all four parametrized test cases pass.

- [ ] **Step 5: Commit the configuration slice**

```bash
git add app/settings.py tests/test_routes.py .env.example
git commit -m "feat: configure worker rule sync interval"
```

---

### Task 2: Incremental Scheduler Reconciliation

**Files:**
- Modify: `app/scheduler.py`
- Modify: `tests/test_scheduler.py`

**Interfaces:**
- Consumes: `AlertRule`, APScheduler methods `get_job`, `add_job`, and `remove_job`, and `execute_rule(rule_id: int) -> None`.
- Produces: `RuleScheduleSynchronizer(scheduler, execute_rule, logger=None)` and `RuleScheduleSynchronizer.sync(rules: Iterable[AlertRule]) -> None`.
- Preserves: `build_scheduler(rules, execute_rule) -> BackgroundScheduler` for existing callers and tests.

- [ ] **Step 1: Write failing add/no-op/change/remove tests**

Import `Mock` and `RuleScheduleSynchronizer`, then add these tests to `tests/test_scheduler.py`:

```python
from unittest.mock import Mock

from app.scheduler import RuleScheduleSynchronizer, build_scheduler


def test_rule_synchronizer_adds_new_rule_without_rescheduling_unchanged_rule():
    scheduler = build_scheduler([], execute_rule=lambda rule_id: None)
    add_job = Mock(wraps=scheduler.add_job)
    scheduler.add_job = add_job
    synchronizer = RuleScheduleSynchronizer(scheduler, execute_rule=lambda rule_id: None)

    synchronizer.sync([make_rule(id=7)])
    synchronizer.sync([make_rule(id=7)])

    assert add_job.call_count == 1
    assert scheduler.get_job("rule-7") is not None


def test_rule_synchronizer_replaces_job_when_cron_changes():
    scheduler = build_scheduler([], execute_rule=lambda rule_id: None)
    synchronizer = RuleScheduleSynchronizer(scheduler, execute_rule=lambda rule_id: None)

    synchronizer.sync([make_rule(id=7, cron_expression="0 9 * * *")])
    first_trigger = str(scheduler.get_job("rule-7").trigger)
    synchronizer.sync([make_rule(id=7, cron_expression="30 10 * * *")])

    assert str(scheduler.get_job("rule-7").trigger) != first_trigger
    assert "10" in str(scheduler.get_job("rule-7").trigger)
    assert "30" in str(scheduler.get_job("rule-7").trigger)


def test_rule_synchronizer_removes_disabled_or_deleted_rules():
    scheduler = build_scheduler([], execute_rule=lambda rule_id: None)
    synchronizer = RuleScheduleSynchronizer(scheduler, execute_rule=lambda rule_id: None)
    synchronizer.sync([make_rule(id=7), make_rule(id=8)])

    synchronizer.sync([make_rule(id=7, enabled=False)])

    assert scheduler.get_job("rule-7") is None
    assert scheduler.get_job("rule-8") is None
```

- [ ] **Step 2: Run the reconciliation tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_scheduler.py::test_rule_synchronizer_adds_new_rule_without_rescheduling_unchanged_rule \
  tests/test_scheduler.py::test_rule_synchronizer_replaces_job_when_cron_changes \
  tests/test_scheduler.py::test_rule_synchronizer_removes_disabled_or_deleted_rules -q
```

Expected: collection FAIL because `RuleScheduleSynchronizer` does not exist.

- [ ] **Step 3: Implement the synchronizer and shared job helper**

Add standard logging, a helper for adding jobs, and this synchronizer to `app/scheduler.py`. Keep `valid_scheduled_rules` unchanged and update `build_scheduler` to call `_add_rule_job`:

```python
import logging


def _job_id(rule_id: int) -> str:
    return f"rule-{rule_id}"


def _add_rule_job(scheduler, rule: AlertRule, execute_rule: Callable[[int], None]) -> None:
    scheduler.add_job(
        execute_rule,
        trigger=CronTrigger.from_crontab(rule.cron_expression),
        args=[rule.id],
        id=_job_id(rule.id),
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )


class RuleScheduleSynchronizer:
    def __init__(self, scheduler, execute_rule: Callable[[int], None], logger=None):
        self.scheduler = scheduler
        self.execute_rule = execute_rule
        self.logger = logger or logging.getLogger(__name__)
        self.known_cron_by_rule_id: dict[int, str] = {}

    def sync(self, rules: Iterable[AlertRule]) -> None:
        desired = {rule.id: rule for rule in valid_scheduled_rules(rules)}

        for rule_id in set(self.known_cron_by_rule_id) - set(desired):
            try:
                if self.scheduler.get_job(_job_id(rule_id)) is not None:
                    self.scheduler.remove_job(_job_id(rule_id))
            except Exception:
                self.logger.exception("移除规则调度任务失败: rule_id=%s", rule_id)
            else:
                self.known_cron_by_rule_id.pop(rule_id, None)

        for rule_id, rule in desired.items():
            unchanged = self.known_cron_by_rule_id.get(rule_id) == rule.cron_expression
            if unchanged and self.scheduler.get_job(_job_id(rule_id)) is not None:
                continue
            try:
                _add_rule_job(self.scheduler, rule, self.execute_rule)
            except Exception:
                self.logger.exception("同步规则调度任务失败: rule_id=%s", rule_id)
            else:
                self.known_cron_by_rule_id[rule_id] = rule.cron_expression
```

Update `build_scheduler`:

```python
def build_scheduler(rules, execute_rule):
    scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
    for rule in valid_scheduled_rules(rules):
        _add_rule_job(scheduler, rule, execute_rule)
    return scheduler
```

- [ ] **Step 4: Run all scheduler tests and verify GREEN**

Run:

```bash
.venv/bin/python -m pytest tests/test_scheduler.py -q
```

Expected: all scheduler tests pass.

- [ ] **Step 5: Write failing recovery and isolation tests**

Add to `tests/test_scheduler.py`:

```python
def test_rule_synchronizer_restores_missing_job():
    scheduler = build_scheduler([], execute_rule=lambda rule_id: None)
    synchronizer = RuleScheduleSynchronizer(scheduler, execute_rule=lambda rule_id: None)
    synchronizer.sync([make_rule(id=7)])
    scheduler.remove_job("rule-7")

    synchronizer.sync([make_rule(id=7)])

    assert scheduler.get_job("rule-7") is not None


def test_rule_synchronizer_retries_failed_rule_without_blocking_other_rules():
    scheduler = build_scheduler([], execute_rule=lambda rule_id: None)
    real_add_job = scheduler.add_job
    failed_once = {"rule-1": False}

    def flaky_add_job(*args, **kwargs):
        if kwargs["id"] == "rule-1" and not failed_once["rule-1"]:
            failed_once["rule-1"] = True
            raise RuntimeError("scheduler unavailable")
        return real_add_job(*args, **kwargs)

    scheduler.add_job = flaky_add_job
    synchronizer = RuleScheduleSynchronizer(scheduler, execute_rule=lambda rule_id: None)

    synchronizer.sync([make_rule(id=1), make_rule(id=2)])
    assert scheduler.get_job("rule-1") is None
    assert scheduler.get_job("rule-2") is not None

    synchronizer.sync([make_rule(id=1), make_rule(id=2)])
    assert scheduler.get_job("rule-1") is not None
```

- [ ] **Step 6: Run recovery tests and verify behavior**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_scheduler.py::test_rule_synchronizer_restores_missing_job \
  tests/test_scheduler.py::test_rule_synchronizer_retries_failed_rule_without_blocking_other_rules -q
```

Expected: PASS with the implementation from Step 3. If either fails, adjust only the per-rule state update so failed operations remain eligible for the next sync.

- [ ] **Step 7: Commit scheduler reconciliation**

```bash
git add app/scheduler.py tests/test_scheduler.py
git commit -m "feat: reconcile worker scheduler jobs"
```

---

### Task 3: Worker Database Polling and Failure Protection

**Files:**
- Modify: `app/worker.py`
- Modify: `tests/test_scheduler.py`

**Interfaces:**
- Consumes: `RuleScheduleSynchronizer.sync(rules)`, `Settings.scheduler_sync_interval_seconds`, `Session(get_engine())`, and `select(AlertRule)`.
- Produces: `sync_rules_once(synchronizer, session_factory=None, logger=None) -> bool`.
- Preserves: `build_execute_rule_callback(...) -> Callable[[int], None]`.

- [ ] **Step 1: Write failing one-shot polling tests**

Add to `tests/test_scheduler.py`:

```python
def test_worker_sync_rules_once_loads_all_rules_and_calls_synchronizer():
    worker = importlib.import_module("app.worker")
    rules = [make_rule(id=1), make_rule(id=2, enabled=False)]
    synchronizer = Mock()

    class QueryResult:
        def all(self):
            return rules

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return None

        def exec(self, statement):
            return QueryResult()

    result = worker.sync_rules_once(synchronizer, session_factory=FakeSession)

    assert result is True
    synchronizer.sync.assert_called_once_with(rules)


def test_worker_sync_rules_once_preserves_scheduler_when_database_read_fails():
    worker = importlib.import_module("app.worker")
    synchronizer = Mock()

    class FailingSession:
        def __enter__(self):
            raise RuntimeError("database is locked")

        def __exit__(self, exc_type, exc, traceback):
            return None

    result = worker.sync_rules_once(synchronizer, session_factory=FailingSession)

    assert result is False
    synchronizer.sync.assert_not_called()
```

- [ ] **Step 2: Run polling tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_scheduler.py::test_worker_sync_rules_once_loads_all_rules_and_calls_synchronizer \
  tests/test_scheduler.py::test_worker_sync_rules_once_preserves_scheduler_when_database_read_fails -q
```

Expected: FAIL because `sync_rules_once` does not exist.

- [ ] **Step 3: Implement one-shot database polling**

Update imports and add this function to `app/worker.py`:

```python
import logging

from app.scheduler import RuleScheduleSynchronizer, build_scheduler
from app.settings import get_settings

logger = logging.getLogger(__name__)


def sync_rules_once(synchronizer, session_factory=None, logger=None) -> bool:
    factory = session_factory or (lambda: Session(get_engine()))
    active_logger = logger or globals()["logger"]
    try:
        with factory() as session:
            rules = session.exec(select(AlertRule)).all()
    except Exception:
        active_logger.exception("读取预警规则失败，保留当前调度任务")
        return False

    synchronizer.sync(rules)
    return True
```

- [ ] **Step 4: Run polling tests and verify GREEN**

Run the command from Step 2.

Expected: both tests pass.

- [ ] **Step 5: Write the failing Worker lifecycle test**

Add to `tests/test_scheduler.py`:

```python
def test_worker_run_loop_syncs_immediately_then_uses_configured_interval(monkeypatch):
    worker = importlib.import_module("app.worker")
    scheduler = Mock()
    synchronizer = Mock()
    sync_calls = []
    sleeps = []

    def sync_once(target):
        sync_calls.append(target)
        if len(sync_calls) == 2:
            raise KeyboardInterrupt
        return True

    def sleep_fn(seconds):
        sleeps.append(seconds)

    worker.run_sync_loop(
        scheduler,
        synchronizer,
        interval_seconds=2.5,
        sync_once=sync_once,
        sleep_fn=sleep_fn,
    )

    assert sync_calls == [synchronizer, synchronizer]
    assert sleeps == [2.5]
    scheduler.start.assert_called_once_with()
    scheduler.shutdown.assert_called_once_with()
```

- [ ] **Step 6: Run the lifecycle test and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_scheduler.py::test_worker_run_loop_syncs_immediately_then_uses_configured_interval -q
```

Expected: FAIL because `run_sync_loop` does not exist.

- [ ] **Step 7: Implement the loop and wire `main()`**

Add the loop and replace the static startup logic in `app/worker.py`:

```python
def run_sync_loop(
    scheduler,
    synchronizer,
    *,
    interval_seconds: float,
    sync_once=sync_rules_once,
    sleep_fn=time.sleep,
) -> None:
    try:
        sync_once(synchronizer)
        scheduler.start()
        while True:
            sleep_fn(interval_seconds)
            sync_once(synchronizer)
    except KeyboardInterrupt:
        scheduler.shutdown()


def main() -> None:
    init_db()
    settings = get_settings()
    execute_rule = build_execute_rule_callback()
    scheduler = build_scheduler([], execute_rule)
    synchronizer = RuleScheduleSynchronizer(scheduler, execute_rule)
    run_sync_loop(
        scheduler,
        synchronizer,
        interval_seconds=settings.scheduler_sync_interval_seconds,
    )
```

- [ ] **Step 8: Run all scheduler/worker tests and verify GREEN**

Run:

```bash
.venv/bin/python -m pytest tests/test_scheduler.py -q
```

Expected: all scheduler and Worker tests pass, including the existing scheduled-trigger callback test.

- [ ] **Step 9: Commit Worker polling**

```bash
git add app/worker.py tests/test_scheduler.py
git commit -m "feat: sync worker rules while running"
```

---

### Task 4: Documentation and Full Verification

**Files:**
- Modify: `README.md`
- Modify: `docs/deployment.md`
- Modify: `docs/operations.md`
- Modify: `docs/project-requirements.md`

**Interfaces:**
- Consumes: Implemented `SCHEDULER_SYNC_INTERVAL_SECONDS` behavior.
- Produces: Operator-facing instructions that match the runtime implementation.

- [ ] **Step 1: Update the README Worker behavior**

Replace the existing Worker paragraph with:

```markdown
Worker 会按 Cron 调度启用规则，并默认每 10 秒从系统数据库同步规则变化。后台新增规则、修改 Cron、启用或停用规则后无需重启 Worker。可通过 `.env` 中的 `SCHEDULER_SYNC_INTERVAL_SECONDS` 调整同步间隔。
```

- [ ] **Step 2: Update deployment configuration and Worker notes**

Add this value to the `.env` example in `docs/deployment.md`:

```dotenv
SCHEDULER_SYNC_INTERVAL_SECONDS=10
```

Add a short configuration subsection stating that the value must be greater than zero, defaults to 10 seconds, and controls how quickly a running Worker reflects rule changes. In the Worker section, state that restarts are no longer required for rule creation, Cron edits, enablement changes, or disablement changes.

- [ ] **Step 3: Update operations and requirements**

In `docs/operations.md`, add these checks under “定时规则不执行”:

```markdown
- Worker 会按 `SCHEDULER_SYNC_INTERVAL_SECONDS` 周期同步规则；修改后请等待至少一个同步周期。
- 如果 Worker 日志出现“读取预警规则失败”，检查 SQLite 文件权限和并发锁定情况；已有任务会保留，系统会在下一周期重试。
```

In `docs/project-requirements.md`, add these reliability requirements:

```markdown
- Worker 应在可配置的同步周期内加载规则新增、Cron 修改和启停变化，无需重启进程。
- Worker 读取规则失败时应保留现有调度任务，并在下一同步周期重试。
```

- [ ] **Step 4: Run focused and full verification**

Run:

```bash
.venv/bin/python -m pytest tests/test_scheduler.py tests/test_routes.py -q
.venv/bin/python -m pytest
.venv/bin/ruff check .
git diff --check
```

Expected: all tests pass, Ruff reports `All checks passed!`, and `git diff --check` produces no output.

- [ ] **Step 5: Review the complete diff against the design**

Run:

```bash
git diff --stat
git diff -- app/settings.py app/scheduler.py app/worker.py tests/test_scheduler.py tests/test_routes.py .env.example README.md docs/deployment.md docs/operations.md docs/project-requirements.md
```

Verify explicitly:

- Unchanged jobs do not call `add_job` again.
- A database polling failure does not call `synchronizer.sync`.
- The initial synchronization happens before `scheduler.start()`.
- Every successful job keeps `max_instances=1` and `coalesce=True`.
- Documentation says default 10 seconds and matches the environment variable name exactly.

- [ ] **Step 6: Commit documentation and final verification state**

```bash
git add README.md docs/deployment.md docs/operations.md docs/project-requirements.md
git commit -m "docs: document dynamic worker rule sync"
```
