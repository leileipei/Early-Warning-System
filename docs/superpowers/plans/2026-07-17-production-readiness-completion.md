# SQL 预警系统生产收口 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不改变单 Web、单 Worker、共享 SQLite 架构的前提下，完成执行事务、调度、健康检查、日志生命周期、Web 安全、配置约束、Python 3.13 兼容和 CI 收口，并在全部验收门槛通过后生成最终报告。

**Architecture:** 保留 FastAPI Web 与独立 APScheduler Worker，共享同一 SQLite 数据库；SQL Server 与 SMTP 继续作为外部适配器。新增小型领域模块承载健康检查和日志生命周期，数据库变更继续通过 `init_db()` 中的幂等 SQLite 迁移执行。

**Tech Stack:** Python 3.11-3.13、FastAPI、SQLModel/SQLAlchemy、SQLite、APScheduler 3、pyodbc、Jinja2、bcrypt、pytest、Ruff、GitHub Actions、pip-tools、pip-audit。

## Global Constraints

- 保留 FastAPI + SQLite + APScheduler + 单 Web + 单 Worker 架构，不引入 Redis、Celery、RabbitMQ、PostgreSQL 或多实例协调。
- 所有行为变更遵循 RED-GREEN-REFACTOR：先写失败测试，确认失败原因正确，再写最小实现。
- 数据库存储继续使用无时区 UTC `datetime`；统一通过 `app.models.utc_now()` 获取时间。
- SQLite 迁移必须幂等，可对旧库重复执行；迁移失败必须回滚整次 `init_db()`。
- 页面错误只显示脱敏摘要；完整异常进入 Python 日志，但不得记录密码、Fernet 密钥或完整连接字符串。
- 不修改或提交现存未跟踪文件 `docs/superpowers/plans/2026-07-14-alert-rule-archival.md` 与 `docs/superpowers/plans/2026-07-14-settings-configuration-management.md`。
- 每个任务只提交本任务列出的文件。提交前运行目标测试和 `git diff --check`。
- 最终报告只能填写实际命令和浏览器验收结果，不允许预先写“通过”。

---

### Task 1: 建立配置、UTC 与密码兼容基线

**Files:**
- Modify: `app/settings.py`
- Modify: `app/models.py`
- Modify: `app/execution_service.py`
- Modify: `app/security.py`
- Modify: `pyproject.toml`
- Modify: `.env.example`
- Modify: `tests/test_auth.py`
- Modify: `tests/test_admin_cli.py`
- Modify: `tests/test_routes.py`
- Create: `tests/test_settings.py`

- [ ] **Step 1: 写配置边界、UTC 和 bcrypt 兼容失败测试**

在 `tests/test_settings.py` 覆盖以下精确行为：

```python
def valid_settings_payload() -> dict[str, object]:
    return {
        "session_secret": "s" * 32,
        "secret_key": Fernet.generate_key().decode(),
    }


def test_session_secret_requires_at_least_32_bytes():
    with pytest.raises(ValidationError, match="at least 32 bytes"):
        Settings(**valid_settings_payload() | {"session_secret": "短" * 10})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("scheduler_misfire_grace_seconds", 0),
        ("worker_heartbeat_timeout_seconds", 0),
        ("log_retention_days", 0),
        ("log_cleanup_interval_seconds", 0),
        ("session_max_age_seconds", 0),
        ("session_idle_timeout_seconds", 0),
    ],
)
def test_positive_production_settings(field, value):
    with pytest.raises(ValidationError):
        Settings(**valid_settings_payload() | {field: value})


def test_utc_now_returns_naive_utc_datetime():
    value = utc_now()
    assert value.tzinfo is None
    assert abs((datetime.now(UTC).replace(tzinfo=None) - value).total_seconds()) < 1
```

在 `tests/test_auth.py` 和 `tests/test_admin_cli.py` 增加现有 `$2b$` 哈希验证、错误哈希返回 `False`、新哈希可验证的测试。

- [ ] **Step 2: 运行测试并确认 RED**

Run: `.venv/bin/pytest tests/test_settings.py tests/test_auth.py tests/test_admin_cli.py -q`

Expected: 因缺少新设置、`utc_now()` 仍调用 `datetime.utcnow()`、Passlib 行为与新测试不符而失败。

- [ ] **Step 3: 实现配置和 UTC 基线**

在 `app/settings.py` 增加：

```python
scheduler_misfire_grace_seconds: int = Field(default=300, gt=0)
worker_heartbeat_timeout_seconds: int = Field(default=60, gt=0)
log_retention_days: int = Field(default=180, gt=0)
log_cleanup_interval_seconds: int = Field(default=86400, gt=0)
session_max_age_seconds: int = Field(default=28800, gt=0)
session_idle_timeout_seconds: int = Field(default=1800, gt=0)
```

将 `session_secret` 单独验证为 UTF-8 编码后至少 32 字节，继续拒绝空值和 `REPLACE_ME`。在 `app/models.py` 保持旧库语义：

```python
from datetime import UTC, datetime


def utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)
```

将 `app/execution_service.py` 和 `tests/test_routes.py` 中剩余的 `datetime.utcnow()` 全部改为 `utc_now()`，确保应用和测试不再调用已弃用 API。

- [ ] **Step 4: 用现代 bcrypt 替换 Passlib**

`app/security.py` 使用完整的直接接口：

```python
import bcrypt


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("ascii"))
    except (UnicodeEncodeError, ValueError):
        return False
```

从 `pyproject.toml` 移除 `passlib[bcrypt]` 与 `bcrypt<4`，改为 `bcrypt>=4.1`；开发依赖将 `httpx>=0.27` 改为 `httpx2>=0.28.1`。在 `.env.example` 增加六个新设置及默认值。

Run: `.venv/bin/pip install -e '.[dev]'`

Expected: 安装成功，`bcrypt>=4.1` 和 `httpx2` 可导入，项目依赖中不再要求 Passlib。

- [ ] **Step 5: 运行测试并确认 GREEN**

Run: `.venv/bin/pytest tests/test_settings.py tests/test_auth.py tests/test_admin_cli.py tests/test_routes.py -q`

Expected: PASS，且不再出现 Passlib bcrypt 警告。

- [ ] **Step 6: 提交任务**

```bash
git add app/settings.py app/models.py app/execution_service.py app/security.py pyproject.toml .env.example tests/test_settings.py tests/test_auth.py tests/test_admin_cli.py tests/test_routes.py
git commit -m "feat: establish production compatibility settings"
```

---

### Task 2: 使抑制状态与执行日志原子持久化

**Files:**
- Modify: `app/execution_service.py`
- Modify: `tests/test_executor.py`
- Modify: `docs/operations.md`

- [ ] **Step 1: 写原子回滚失败测试**

在 `tests/test_executor.py` 增加数据库故障注入测试，构造开启重复抑制且产生一封邮件的执行结果，在 `session.flush()` 写入 `MailLog` 时抛出 `IntegrityError`，然后断言：

```python
assert session.exec(select(AlertSuppression)).all() == []
assert session.exec(select(ExecutionLog)).all() == []
assert session.exec(select(MailLog)).all() == []
assert session.get(RuleExecutionLease, rule.id) is None
```

再增加成功路径测试，断言抑制状态、一条执行日志和全部邮件日志在同一次提交后存在。

- [ ] **Step 2: 运行测试并确认 RED**

Run: `.venv/bin/pytest tests/test_executor.py -q`

Expected: 当前 `ExecutionLog` 已先提交，故障后仍残留，测试失败。

- [ ] **Step 3: 将执行结果和待写抑制状态一起返回**

在 `app/execution_service.py` 增加：

```python
@dataclass(frozen=True)
class ExecutionAttempt:
    result: ExecutionResult
    suppression_state: dict | None = None
```

把 `_execute_rule_once(session, rule, trigger_type) -> ExecutionAttempt` 改为只收集 `suppression_state`，不写数据库。重试循环使用 `attempt.result` 判断重试，并只保留最终尝试的抑制状态。

- [ ] **Step 4: 用单事务写入全部状态**

把 `_persist_suppression_state()` 中的 `session.commit()` 删除，并将 `persist_execution_result` 改为：

```python
def persist_execution_result(
    *,
    session: Session,
    rule: AlertRule,
    trigger_type: TriggerType,
    result: ExecutionResult,
    suppression_state: dict | None,
    started_at: datetime,
    finished_at: datetime,
) -> ExecutionLog:
    try:
        if suppression_state is not None and result.status == ExecutionStatus.SUCCESS:
            _persist_suppression_state(session, rule, suppression_state)
        execution_log = _build_execution_log(
            rule, trigger_type, result, started_at, finished_at
        )
        session.add(execution_log)
        session.flush()
        for mail_result in result.mail_results:
            session.add(_build_mail_log(execution_log.id, mail_result))
        session.commit()
        session.refresh(execution_log)
        return execution_log
    except Exception:
        session.rollback()
        raise
```

租约上下文保持独立 owner-token 释放逻辑，不把租约释放并入上述事务。

- [ ] **Step 5: 记录外部副作用语义并验证**

在 `docs/operations.md` 明确：SMTP 发送成功后若 SQLite 提交失败，邮件无法撤回，重试可能产生重复邮件，系统提供的是至少一次发送语义。

Run: `.venv/bin/pytest tests/test_executor.py tests/test_execution_lock.py -q`

Expected: PASS。

- [ ] **Step 6: 提交任务**

```bash
git add app/execution_service.py tests/test_executor.py docs/operations.md
git commit -m "fix: persist execution outcomes atomically"
```

---

### Task 3: 配置 Cron misfire 宽限策略

**Files:**
- Modify: `app/scheduler.py`
- Modify: `app/worker.py`
- Modify: `tests/test_scheduler.py`
- Modify: `tests/test_worker.py`

- [ ] **Step 1: 写调度参数失败测试**

在 `tests/test_scheduler.py` 使用记录 `add_job` 参数的 fake scheduler，断言新增和初始任务均满足：

```python
assert kwargs["misfire_grace_time"] == 300
assert kwargs["coalesce"] is True
assert kwargs["max_instances"] == 1
```

再测试 `build_scheduler(rules, execute_rule, misfire_grace_seconds=45)` 和动态同步沿用 45 秒。

- [ ] **Step 2: 运行测试并确认 RED**

Run: `.venv/bin/pytest tests/test_scheduler.py tests/test_worker.py -q`

Expected: 缺少 `misfire_grace_time` 参数而失败。

- [ ] **Step 3: 显式传递宽限时间**

将接口改为：

```python
def _add_rule_job(scheduler, rule, execute_rule, *, misfire_grace_seconds: int) -> None:
    scheduler.add_job(
        execute_rule,
        trigger=CronTrigger.from_crontab(rule.cron_expression),
        args=[rule.id],
        id=_job_id(rule.id),
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=misfire_grace_seconds,
    )
```

`RuleScheduleSynchronizer.__init__` 保存 `misfire_grace_seconds`；`build_scheduler` 接收同名关键字参数。`worker.main()` 从 settings 读取并同时传给二者。

- [ ] **Step 4: 验证和提交**

Run: `.venv/bin/pytest tests/test_scheduler.py tests/test_worker.py -q`

Expected: PASS。

```bash
git add app/scheduler.py app/worker.py tests/test_scheduler.py tests/test_worker.py
git commit -m "feat: configure scheduler misfire grace"
```

---

### Task 4: 增加 Worker 心跳模型和幂等迁移

**Files:**
- Modify: `app/models.py`
- Modify: `app/db.py`
- Create: `app/worker_health.py`
- Modify: `tests/test_db.py`
- Create: `tests/test_worker_health.py`

- [ ] **Step 1: 写旧库迁移和心跳服务失败测试**

测试从只含现有表的 SQLite 文件升级后：

```python
assert "workerheartbeat" in inspect(engine).get_table_names()
columns = {item["name"] for item in inspect(engine).get_columns("workerheartbeat")}
assert columns == {"id", "worker_id", "started_at", "last_seen_at", "last_sync_ok", "last_error"}
```

连续调用两次 `init_db(engine)`，断言不报错且仍只有单例记录。`tests/test_worker_health.py` 覆盖启动写入、成功覆盖错误、失败写入脱敏摘要和 `id=1`。

- [ ] **Step 2: 运行测试并确认 RED**

Run: `.venv/bin/pytest tests/test_db.py tests/test_worker_health.py -q`

Expected: 模型和模块不存在而失败。

- [ ] **Step 3: 增加模型和服务**

模型定义：

```python
class WorkerHeartbeat(SQLModel, table=True):
    id: int = Field(default=1, primary_key=True)
    worker_id: str
    started_at: datetime = Field(default_factory=utc_now)
    last_seen_at: datetime = Field(default_factory=utc_now)
    last_sync_ok: bool = True
    last_error: str = ""
```

`app/worker_health.py` 提供：

```python
def record_worker_start(session: Session, worker_id: str, *, now: datetime | None = None) -> None
def record_worker_sync(
    session: Session,
    worker_id: str,
    *,
    ok: bool,
    error: str = "",
    now: datetime | None = None,
) -> None
def summarize_worker_error(exc: BaseException) -> str
```

两种写入都使用 `session.get(WorkerHeartbeat, 1)` 后 upsert，提交失败时 rollback 并继续抛出。摘要只保留异常类型与固定安全描述，最长 300 字符。

- [ ] **Step 4: 扩展迁移并验证事务回滚**

`SQLModel.metadata.create_all()` 创建新表；`_migrate_sqlite_schema()` 校验表字段。迁移测试额外注入索引创建失败，断言整个 `BEGIN IMMEDIATE` 回滚。

Run: `.venv/bin/pytest tests/test_db.py tests/test_worker_health.py -q`

Expected: PASS。

- [ ] **Step 5: 提交任务**

```bash
git add app/models.py app/db.py app/worker_health.py tests/test_db.py tests/test_worker_health.py
git commit -m "feat: add worker heartbeat persistence"
```

---

### Task 5: 将心跳接入 Worker 并增加 readiness 接口

**Files:**
- Modify: `app/worker.py`
- Create: `app/health.py`
- Modify: `app/main.py`
- Modify: `tests/test_worker.py`
- Create: `tests/test_health.py`

- [ ] **Step 1: 写 Worker 心跳和 readiness 失败测试**

`tests/test_worker.py` 覆盖：启动立即写心跳；每次同步成功写 `last_sync_ok=True`；规则读取或同步异常写失败；心跳写失败只记录异常，不中止 scheduler 循环。

`tests/test_health.py` 覆盖：

```python
assert client.get("/health").status_code == 200
assert client.get("/health/ready").status_code == 503  # 无心跳
```

再构造新鲜成功心跳得到 200、过期心跳得到 503、`last_sync_ok=False` 得到 503、缺表或 SQLite 查询失败得到 503；响应不得包含数据库 URL、路径或异常堆栈。

- [ ] **Step 2: 运行测试并确认 RED**

Run: `.venv/bin/pytest tests/test_worker.py tests/test_health.py -q`

Expected: `/health/ready` 为 404 且 Worker 未写心跳。

- [ ] **Step 3: 接入 Worker 心跳**

`worker.main()` 启动生成 `worker_id = uuid4().hex` 并写初始心跳。将同步接口改为返回结构化结果：

```python
@dataclass(frozen=True)
class RuleSyncResult:
    ok: bool
    error: str = ""
```

`run_sync_loop()` 每轮调用后在独立 Session 中执行 `record_worker_sync`。同步失败不得删除已有调度任务；心跳失败不得结束调度循环。

- [ ] **Step 4: 实现 readiness 检查**

`app/health.py` 提供：

```python
@dataclass(frozen=True)
class ReadinessResult:
    ready: bool
    components: dict[str, str]


def check_readiness(engine: Engine, *, heartbeat_timeout_seconds: int) -> ReadinessResult:
    required_tables = {
        "adminuser",
        "alertrule",
        "executionlog",
        "maillog",
        "workerheartbeat",
    }
    try:
        with Session(engine) as session:
            session.exec(text("SELECT 1")).one()
            tables = set(inspect(engine).get_table_names())
            if not required_tables.issubset(tables):
                return ReadinessResult(False, {"database": "schema_incomplete", "worker": "unknown"})
            heartbeat = session.get(WorkerHeartbeat, 1)
    except Exception:
        return ReadinessResult(False, {"database": "unavailable", "worker": "unknown"})

    if heartbeat is None:
        return ReadinessResult(False, {"database": "ready", "worker": "missing"})
    age = (utc_now() - heartbeat.last_seen_at).total_seconds()
    if age > heartbeat_timeout_seconds:
        return ReadinessResult(False, {"database": "ready", "worker": "stale"})
    if not heartbeat.last_sync_ok:
        return ReadinessResult(False, {"database": "ready", "worker": "sync_failed"})
    return ReadinessResult(True, {"database": "ready", "worker": "ready"})
```

必要表至少包括 `adminuser`、`alertrule`、`executionlog`、`maillog`、`workerheartbeat`。`app/main.py` 返回：

```python
@app.get("/health/ready")
def readiness(response: Response):
    result = check_readiness(
        get_engine(),
        heartbeat_timeout_seconds=settings.worker_heartbeat_timeout_seconds,
    )
    if not result.ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ready" if result.ready else "not_ready", "components": result.components}
```

- [ ] **Step 5: 验证和提交**

Run: `.venv/bin/pytest tests/test_worker.py tests/test_health.py -q`

Expected: PASS。

```bash
git add app/worker.py app/health.py app/main.py tests/test_worker.py tests/test_health.py
git commit -m "feat: expose worker-aware readiness health"
```

---

### Task 6: 增加日志索引和独立分页

**Files:**
- Modify: `app/models.py`
- Modify: `app/db.py`
- Create: `app/log_service.py`
- Modify: `app/routes.py`
- Modify: `app/templates/logs.html`
- Modify: `app/static/styles.css`
- Modify: `tests/test_db.py`
- Create: `tests/test_log_service.py`
- Modify: `tests/test_routes.py`

- [ ] **Step 1: 写索引和分页失败测试**

断言旧库升级后存在以下索引名：

```python
EXPECTED_INDEXES = {
    "ix_executionlog_started_at",
    "ix_executionlog_status",
    "ix_executionlog_rule_id",
    "ix_maillog_sent_at",
    "ix_maillog_status",
    "ix_maillog_execution_log_id",
}
```

日志服务测试 `execution_page` 与 `mail_page` 独立，默认 `page_size=50`，允许 10 至 200，页码小于 1 归一为 1，超过末页归一到末页。路由测试筛选参数在前后页链接中保留，执行日志翻页不改变邮件日志页码。

- [ ] **Step 2: 运行测试并确认 RED**

Run: `.venv/bin/pytest tests/test_db.py tests/test_log_service.py tests/test_routes.py -q`

Expected: 索引、分页服务和模板分页信息缺失。

- [ ] **Step 3: 增加幂等索引**

模型字段加 `index=True`，并在 SQLite 迁移显式执行 `CREATE INDEX IF NOT EXISTS`，保证旧库补齐。不要建立多余复合索引。

- [ ] **Step 4: 实现日志查询服务**

`app/log_service.py` 定义以下分页值对象：

```python
T = TypeVar("T")


@dataclass(frozen=True)
class Page(Generic[T]):
    items: list[T]
    page: int
    page_size: int
    total: int
    total_pages: int
    has_previous: bool
    has_next: bool
```

查询接口固定为 `list_execution_logs(session: Session, filters: LogFilters, *, page: int, page_size: int) -> Page[ExecutionLog]` 和 `list_mail_logs(session: Session, filters: LogFilters, *, page: int, page_size: int) -> Page[MailLog]`。实现必须先构造筛选后的 statement，用 `SELECT count(*) FROM (<statement>)` 得到总数，再将页码归一化后执行 `order_by`、`offset` 和 `limit`；任何路径都不得加载全部记录。为兼容 Python 3.11，不使用 PEP 695 语法。

- [ ] **Step 5: 更新路由和模板**

参数名固定为 `execution_page`、`mail_page`、`page_size`。模板显示 `总计 N 条，第 X/Y 页`，并生成保留全部筛选条件及另一列表页码的上一页/下一页链接。`page_size` 输入设置 `min="10" max="200"`。

- [ ] **Step 6: 验证和提交**

Run: `.venv/bin/pytest tests/test_db.py tests/test_log_service.py tests/test_routes.py -q`

Expected: PASS。

```bash
git add app/models.py app/db.py app/log_service.py app/routes.py app/templates/logs.html app/static/styles.css tests/test_db.py tests/test_log_service.py tests/test_routes.py
git commit -m "feat: paginate and index audit logs"
```

---

### Task 7: 增加日志保留清理并接入 Worker

**Files:**
- Modify: `app/log_service.py`
- Modify: `app/worker.py`
- Modify: `tests/test_log_service.py`
- Modify: `tests/test_worker.py`
- Modify: `docs/operations.md`

- [ ] **Step 1: 写清理边界和失败隔离测试**

测试保留日边界：`finished_at < cutoff` 才过期，等于 cutoff 保留；运行中的执行日志不删除。每批最多 500 个执行 ID，先删关联 `MailLog` 再删 `ExecutionLog`。注入第二批提交失败，断言第一批已提交、第二批回滚、下一次调用可继续清理。

Worker 测试使用假时钟，断言启动后执行一次清理，之后仅在 `LOG_CLEANUP_INTERVAL_SECONDS` 到期时运行；清理异常被记录但 scheduler 继续同步。

- [ ] **Step 2: 运行测试并确认 RED**

Run: `.venv/bin/pytest tests/test_log_service.py tests/test_worker.py -q`

Expected: 清理接口不存在。

- [ ] **Step 3: 实现批次清理**

接口固定为 `cleanup_expired_logs(session_factory: Callable[[], Session], *, retention_days: int, now: datetime | None = None, batch_size: int = 500) -> int`。每批新建 Session，查询 `finished_at < cutoff` 且 status 不为 `running` 的执行 ID；使用 SQLAlchemy `delete()` 先删除这些 ID 对应的 `MailLog`，再删除 `ExecutionLog`，随后提交。每批捕获异常执行 rollback 后重新抛出，让 Worker 统一记录。循环至不足一批并返回删除的执行日志总数。

- [ ] **Step 4: 接入 Worker 周期**

`run_sync_loop()` 接收 `cleanup_logs`、`cleanup_interval_seconds` 和 `monotonic_fn`；维护 `next_cleanup_at`，清理失败不推进为永久完成，只安排下个正常周期重试。

- [ ] **Step 5: 文档、验证和提交**

在 `docs/operations.md` 说明保留设置、清理边界、500 条批次和失败重试行为。

Run: `.venv/bin/pytest tests/test_log_service.py tests/test_worker.py -q`

Expected: PASS。

```bash
git add app/log_service.py app/worker.py tests/test_log_service.py tests/test_worker.py docs/operations.md
git commit -m "feat: enforce audit log retention"
```

---

### Task 8: 将 CSV 导出改为安全流式响应

**Files:**
- Modify: `app/log_service.py`
- Modify: `app/routes.py`
- Modify: `tests/test_log_service.py`
- Modify: `tests/test_routes.py`

- [ ] **Step 1: 写 CSV 公式中和和批次读取失败测试**

覆盖所有危险前缀，并保持普通文本不变：

```python
@pytest.mark.parametrize("value", ["=1+1", "+cmd", "-2+3", "@SUM(A1:A2)"])
def test_csv_cell_neutralizes_formula_prefix(value):
    assert csv_safe_cell(value) == "'" + value
```

路由测试断言响应为 `StreamingResponse`、内容以 `\ufeff` 开始、现有列顺序不变、危险主题和错误信息已中和。构造 1200 条记录并记录查询批次，断言不会调用 `.all()` 一次加载所有日志。

- [ ] **Step 2: 运行测试并确认 RED**

Run: `.venv/bin/pytest tests/test_log_service.py tests/test_routes.py -q`

Expected: 当前 `_csv_response` 接收完整 rows 列表且不处理中和。

- [ ] **Step 3: 实现安全流生成器**

`app/log_service.py` 增加：

```python
def csv_safe_cell(value: object) -> object:
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def stream_csv(headers: Sequence[str], row_batches: Iterable[Iterable[Sequence[object]]]):
    yield "\ufeff"
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(headers)
    yield buffer.getvalue()
    for batch in row_batches:
        for row in batch:
            buffer.seek(0)
            buffer.truncate(0)
            writer.writerow([csv_safe_cell(cell) for cell in row])
            yield buffer.getvalue()
```

日志批次迭代器固定 `batch_size=500`，使用主键游标或 `offset/limit`，每批查询后立即生成文本。

- [ ] **Step 4: 替换两个导出路由**

返回以 `stream_csv(headers, row_batches)` 为内容、`media_type="text/csv; charset=utf-8"` 的 `StreamingResponse`，保留原文件名、列定义和“导出全部记录且不受页面筛选影响”的行为。

- [ ] **Step 5: 验证和提交**

Run: `.venv/bin/pytest tests/test_log_service.py tests/test_routes.py -q`

Expected: PASS。

```bash
git add app/log_service.py app/routes.py tests/test_log_service.py tests/test_routes.py
git commit -m "feat: stream safe csv log exports"
```

---

### Task 9: 增加 Session 绝对超时、空闲超时和版本失效

**Files:**
- Modify: `app/models.py`
- Modify: `app/db.py`
- Modify: `app/auth.py`
- Modify: `app/admin_cli.py`
- Modify: `app/main.py`
- Modify: `tests/test_db.py`
- Modify: `tests/test_auth.py`
- Modify: `tests/test_admin_cli.py`

- [ ] **Step 1: 写迁移和 Session 生命周期失败测试**

测试 `AdminUser.session_version` 旧库升级默认 `1` 且重复迁移不变。登录后 Session 必须包含：

```python
{
    "admin_user_id": user.id,
    "authenticated_at": epoch_seconds,
    "last_activity_at": epoch_seconds,
    "admin_session_version": user.session_version,
}
```

使用可注入 clock 覆盖 8 小时绝对超时、30 分钟空闲超时、每次有效请求刷新最后活动时间、篡改时间值、用户不存在、版本不一致和密码更新使旧 Session 失效。

- [ ] **Step 2: 运行测试并确认 RED**

Run: `.venv/bin/pytest tests/test_db.py tests/test_auth.py tests/test_admin_cli.py -q`

Expected: 缺少字段和生命周期校验而失败。

- [ ] **Step 3: 增加会话版本迁移**

`AdminUser` 增加 `session_version: int = 1`。SQLite 迁移使用：

```sql
ALTER TABLE adminuser ADD COLUMN session_version INTEGER NOT NULL DEFAULT 1
```

管理员密码更新时 `user.session_version += 1`；新用户保持 1。

- [ ] **Step 4: 集中实现 Session 校验**

在 `app/auth.py` 增加常量键名和：

```python
def establish_admin_session(request: Request, user: AdminUser, *, now: datetime | None = None) -> None
def validate_admin_session(
    request: Request,
    user: AdminUser,
    *,
    max_age_seconds: int,
    idle_timeout_seconds: int,
    now: datetime | None = None,
) -> bool
```

时间以 UTC epoch 整数存入签名 Cookie。任何缺失、类型错误、倒退时间、绝对超时、空闲超时或版本不匹配都 `request.session.clear()` 并返回 False。`require_admin()` 从 settings 读取边界并在成功后刷新 `last_activity_at`。

`SessionMiddleware` 增加 `max_age=settings.session_max_age_seconds`，使浏览器 Cookie 生命周期与绝对有效期一致。

- [ ] **Step 5: 验证和提交**

Run: `.venv/bin/pytest tests/test_db.py tests/test_auth.py tests/test_admin_cli.py -q`

Expected: PASS。

```bash
git add app/models.py app/db.py app/auth.py app/admin_cli.py app/main.py tests/test_db.py tests/test_auth.py tests/test_admin_cli.py
git commit -m "feat: expire and revoke admin sessions"
```

---

### Task 10: 增加安全响应头并收紧 CSP

**Files:**
- Modify: `app/web_security.py`
- Modify: `app/main.py`
- Modify: `app/templates/login.html`
- Modify: `app/templates/rule_form.html`
- Modify: `app/templates/settings.html`
- Modify: `app/static/app.js`
- Modify: `tests/test_web_security.py`
- Modify: `tests/test_routes.py`

- [ ] **Step 1: 写 HTTP 头和内联脚本失败测试**

对登录页、后台页、重定向、404 和 500 响应断言：

```python
assert response.headers["x-content-type-options"] == "nosniff"
assert response.headers["referrer-policy"] == "same-origin"
assert response.headers["permissions-policy"] == "camera=(), microphone=(), geolocation=()"
assert response.headers["x-frame-options"] == "DENY"
assert "default-src 'self'" in response.headers["content-security-policy"]
assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
```

当 `session_cookie_secure=True` 时还应有 `Strict-Transport-Security`；False 时不得出现。模板扫描断言不存在 `<script>` 内联内容和 `on*=` 事件属性。

- [ ] **Step 2: 运行测试并确认 RED**

Run: `.venv/bin/pytest tests/test_web_security.py tests/test_routes.py -q`

Expected: 响应头缺失或模板含内联行为。

- [ ] **Step 3: 实现响应头中间件**

```python
class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, *, enable_hsts: bool):
        super().__init__(app)
        self.enable_hsts = enable_hsts

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
            "base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["X-Frame-Options"] = "DENY"
        if self.enable_hsts:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response
```

在 `create_app()` 注册并以 `session_cookie_secure` 控制 HSTS。

- [ ] **Step 4: 移出内联 JavaScript**

将模板中的确认、显示/隐藏和 SQL 检测交互改为 `data-*` 属性，由 `app/static/app.js` 使用事件委托处理。保留无 JavaScript 时的服务器端表单能力。

- [ ] **Step 5: 验证和提交**

Run: `.venv/bin/pytest tests/test_web_security.py tests/test_routes.py -q`

Expected: PASS。

```bash
git add app/web_security.py app/main.py app/templates/login.html app/templates/rule_form.html app/templates/settings.html app/static/app.js tests/test_web_security.py tests/test_routes.py
git commit -m "feat: enforce browser security headers"
```

---

### Task 11: 收紧 SQL Server、规则和导入输入边界

**Files:**
- Modify: `app/models.py`
- Modify: `app/routes.py`
- Modify: `app/templates/settings.html`
- Modify: `app/templates/sql_server_form.html`
- Modify: `app/templates/rule_form.html`
- Modify: `app/static/styles.css`
- Modify: `tests/test_routes.py`
- Modify: `tests/test_sql_client.py`

- [ ] **Step 1: 写安全默认值和输入边界失败测试**

覆盖：新建 SQL 数据源默认 `encrypt="yes"`、`trust_server_certificate="no"`；编辑旧 `yes` 值不被静默改写；设置页对 `yes` 显示证书风险提示。服务器端拒绝：

- 端口不在 1 至 65535。
- 查询、连接、SMTP timeout 不在 1 至 600。
- `max_rows` 不在 1 至 5000。
- 上传体超过 1 MiB。
- 导入规则超过 500 条。

测试表单返回 400 和可操作中文摘要，且响应不回显密码。

- [ ] **Step 2: 运行测试并确认 RED**

Run: `.venv/bin/pytest tests/test_routes.py tests/test_sql_client.py -q`

Expected: 默认信任证书且多项范围未校验。

- [ ] **Step 3: 实现共享边界验证**

在 `app/routes.py` 增加小型纯函数，不新增大型框架：

```python
def _validate_bounded_int(value: int, *, label: str, minimum: int, maximum: int) -> str | None:
    if not minimum <= value <= maximum:
        return f"{label}必须在 {minimum} 至 {maximum} 之间"
    return None
```

在规则创建、编辑和导入，以及 SQL/SMTP 创建、编辑和连通性测试入口统一调用。上传先读取最多 `1_048_577` 字节，超过即拒绝；JSON `rules` 必须为列表且长度不超过 500。

- [ ] **Step 4: 更新 SQL Server 默认值与风险提示**

仅把模型和“新建表单”默认改为 `trust_server_certificate="no"`。迁移中现有列默认和已有行保持不变。列表和编辑页对值为 `yes` 的数据源显示明确风险状态，不显示连接字符串。

- [ ] **Step 5: 验证和提交**

Run: `.venv/bin/pytest tests/test_routes.py tests/test_sql_client.py -q`

Expected: PASS。

```bash
git add app/models.py app/routes.py app/templates/settings.html app/templates/sql_server_form.html app/templates/rule_form.html app/static/styles.css tests/test_routes.py tests/test_sql_client.py
git commit -m "feat: enforce safe sql input boundaries"
```

---

### Task 12: 保证最多一个 SMTP 配置启用

**Files:**
- Modify: `app/db.py`
- Modify: `app/execution_service.py`
- Modify: `app/routes.py`
- Modify: `tests/test_db.py`
- Modify: `tests/test_executor.py`
- Modify: `tests/test_routes.py`
- Modify: `docs/deployment.md`

- [ ] **Step 1: 写旧库整理、数据库约束和路由失败测试**

构造旧库三个 enabled SMTP，`updated_at` 最新且 ID 最大者应保留，其余禁用。重复升级结果不变。直接插入第二个 enabled 配置应触发 `IntegrityError`。

路由测试新建或编辑并启用一个 SMTP 后，同一事务中其余全部禁用；TLS 与 SSL 同时启用返回 400；只启用其中一种或都关闭允许保存。执行服务在不可能的多启用状态下必须抛 `ConfigurationError`，不得按时间静默选择。

- [ ] **Step 2: 运行测试并确认 RED**

Run: `.venv/bin/pytest tests/test_db.py tests/test_executor.py tests/test_routes.py -q`

Expected: 多个 SMTP 可启用且执行服务选最新一条。

- [ ] **Step 3: 迁移旧数据并建立部分唯一索引**

SQLite 迁移顺序固定：

```sql
UPDATE smtpconfig
SET enabled = 0
WHERE enabled = 1
  AND id <> (
      SELECT id FROM smtpconfig
      WHERE enabled = 1
      ORDER BY updated_at DESC, id DESC
      LIMIT 1
  );

CREATE UNIQUE INDEX IF NOT EXISTS uq_smtpconfig_single_enabled
ON smtpconfig(enabled)
WHERE enabled = 1;
```

升级测试验证任一语句失败时旧数据和索引整体回滚。

- [ ] **Step 4: 实现应用事务约束**

增加私有帮助函数：

```python
def _disable_other_smtp_configs(session: Session, *, keep_id: int | None) -> None:
    statement = update(SmtpConfig).where(SmtpConfig.enabled == True)  # noqa: E712
    if keep_id is not None:
        statement = statement.where(SmtpConfig.id != keep_id)
    session.exec(statement.values(enabled=False))
```

新建 enabled 配置先禁用全部再添加；编辑 enabled 配置先禁用其他再保存；两者只执行一次 `commit`，异常统一 rollback。`_get_enabled_smtp_config` 查询最多两条，零条报未配置，两条报配置冲突。

- [ ] **Step 5: 文档、验证和提交**

在 `docs/deployment.md` 记录升级时“保留最近更新启用项”的行为。

Run: `.venv/bin/pytest tests/test_db.py tests/test_executor.py tests/test_routes.py -q`

Expected: PASS。

```bash
git add app/db.py app/execution_service.py app/routes.py tests/test_db.py tests/test_executor.py tests/test_routes.py docs/deployment.md
git commit -m "feat: enforce one active smtp configuration"
```

---

### Task 13: 统一脱敏错误处理和日志记录

**Files:**
- Create: `app/error_reporting.py`
- Modify: `app/routes.py`
- Modify: `app/sql_client.py`
- Modify: `app/mailer.py`
- Modify: `app/worker.py`
- Create: `tests/test_error_reporting.py`
- Modify: `tests/test_routes.py`
- Modify: `tests/test_sql_client.py`
- Modify: `tests/test_mailer.py`
- Modify: `tests/test_worker.py`

- [ ] **Step 1: 写敏感信息不泄漏失败测试**

构造包含 `PWD=secret`、Fernet key、数据库 host/user 和 SMTP password 的异常。断言页面响应、heartbeat `last_error`、执行错误摘要均不含这些值。使用 `caplog` 断言内部日志包含异常类型和上下文 ID，但不含密码、key 或完整连接字符串。

- [ ] **Step 2: 运行测试并确认 RED**

Run: `.venv/bin/pytest tests/test_error_reporting.py tests/test_routes.py tests/test_sql_client.py tests/test_mailer.py tests/test_worker.py -q`

Expected: 当前部分路径直接使用 `str(exc)`，测试失败。

- [ ] **Step 3: 实现统一摘要与脱敏**

`app/error_reporting.py` 提供：

```python
SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)(pwd|password|secret|secret_key|session_secret)\s*=\s*([^;\s]+)"
)
FERNET_VALUE = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{43}=(?![A-Za-z0-9_-])")
ODBC_CONNECTION_STRING = re.compile(r"(?i)DRIVER=\{[^\n]+(?:;[^\n]+)+")


def redact_sensitive_text(value: object, *, limit: int = 300) -> str:
    text = ODBC_CONNECTION_STRING.sub("[REDACTED CONNECTION STRING]", str(value))
    text = SENSITIVE_ASSIGNMENT.sub(r"\1=[REDACTED]", text)
    text = FERNET_VALUE.sub("[REDACTED KEY]", text)
    return text[:limit]


def public_error_summary(exc: BaseException, *, fallback: str) -> str:
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return fallback
    return fallback


def log_exception_safely(logger: logging.Logger, message: str, exc: BaseException) -> None:
    rendered = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    logger.error("%s: error_type=%s\n%s", message, type(exc).__name__, redact_sensitive_text(rendered, limit=4000))
```

面向用户统一使用固定可操作摘要，例如“SQL Server 连接失败，请检查服务器、端口、证书和账号配置”。内部调用 `log_exception_safely` 保留脱敏后的堆栈；不得直接使用会自动附带原始异常文本的 `logger.exception`，也不得把原始连接字符串作为日志参数。

- [ ] **Step 4: 替换直接异常回显**

检查并替换 `routes.py`、`sql_client.py`、`mailer.py`、`worker.py` 中所有面向页面或数据库的 `str(exc)`。业务校验错误可保留精确中文文本；外部连接异常只返回摘要。

- [ ] **Step 5: 验证和提交**

Run: `.venv/bin/pytest tests/test_error_reporting.py tests/test_routes.py tests/test_sql_client.py tests/test_mailer.py tests/test_worker.py -q`

Expected: PASS。

```bash
git add app/error_reporting.py app/routes.py app/sql_client.py app/mailer.py app/worker.py tests/test_error_reporting.py tests/test_routes.py tests/test_sql_client.py tests/test_mailer.py tests/test_worker.py
git commit -m "fix: redact operational error details"
```

---

### Task 14: 锁定依赖并建立 Python 3.11-3.13 CI

**Files:**
- Modify: `pyproject.toml`
- Create: `requirements.lock`
- Create: `requirements-dev.lock`
- Create: `.github/workflows/ci.yml`
- Create: `.github/dependabot.yml`
- Modify: `README.md`
- Modify: `docs/deployment.md`

- [ ] **Step 1: 完成依赖元数据并生成锁文件**

在 dev extras 增加 `pip-audit>=2.7` 与 `pip-tools>=7.4`。使用 Python 3.11 兼容解析生成跨 3.11-3.13 可安装锁文件：

Run:

```bash
.venv/bin/python -m pip install -U pip-tools
.venv/bin/pip-compile --strip-extras --resolver=backtracking --output-file=requirements.lock pyproject.toml
.venv/bin/pip-compile --extra=dev --strip-extras --resolver=backtracking --output-file=requirements-dev.lock pyproject.toml
```

Expected: 两个锁文件生成，生产锁不包含 pytest/Ruff，开发锁包含生产和开发依赖；不再包含 Passlib 或 `bcrypt<4`。

- [ ] **Step 2: 验证锁文件可复现安装**

在临时虚拟环境安装生产锁并运行 `pip check`；另一个临时虚拟环境安装开发锁并运行测试收集。不得改写项目 `.venv` 作为验证依据。

Run:

```bash
python3 -m venv /tmp/ews-prod-lock
/tmp/ews-prod-lock/bin/pip install -r requirements.lock
/tmp/ews-prod-lock/bin/pip check
python3 -m venv /tmp/ews-dev-lock
/tmp/ews-dev-lock/bin/pip install -r requirements-dev.lock
/tmp/ews-dev-lock/bin/pytest --collect-only -q
```

Expected: 两次安装和检查均退出 0。

- [ ] **Step 3: 增加 CI 工作流**

`.github/workflows/ci.yml` 在 push 与 pull_request 运行：

```yaml
strategy:
  fail-fast: false
  matrix:
    python-version: ["3.11", "3.12", "3.13"]
steps:
  - uses: actions/checkout@v4
  - uses: actions/setup-python@v5
    with:
      python-version: ${{ matrix.python-version }}
      cache: pip
  - run: python -m pip install -r requirements-dev.lock
  - run: python -m pip check
  - run: ruff check .
  - run: pytest --cov=app --cov-report=term-missing --cov-fail-under=93
  - run: pip-audit -r requirements.lock --strict
```

如 `pip-audit --strict` 对已确认无修复版本的非高危漏洞失败，必须升级或记录正式忽略编号和到期日期；不得使用通配忽略。

- [ ] **Step 4: 增加 Dependabot**

`.github/dependabot.yml` 配置 `pip` 与 `github-actions`，每周检查，目标分支为仓库默认分支，每类最多 5 个开放 PR。

- [ ] **Step 5: 本地执行 CI 等价检查并更新文档**

Run:

```bash
.venv/bin/ruff check .
.venv/bin/pytest --cov=app --cov-report=term-missing --cov-fail-under=93
.venv/bin/pip check
.venv/bin/pip-audit -r requirements.lock --strict
git diff --check
```

Expected: 全部退出 0，覆盖率至少 93%，无 Passlib、`datetime.utcnow` 或 Starlette TestClient 弃用警告。

README 和部署文档改为优先安装锁文件，说明锁文件更新命令和 Python 支持范围。

- [ ] **Step 6: 提交任务**

```bash
git add pyproject.toml requirements.lock requirements-dev.lock .github/workflows/ci.yml .github/dependabot.yml README.md docs/deployment.md
git commit -m "ci: lock dependencies and test supported python"
```

---

### Task 15: 完成升级演练、浏览器验收、独立审查和最终报告

**Files:**
- Modify: `docs/deployment.md`
- Modify: `docs/operations.md`
- Modify: `docs/project-requirements.md`
- Create: `docs/project-acceptance-report.md`

- [ ] **Step 1: 补全停机升级与回滚文档**

文档必须给出可直接执行的顺序：停止 Web/Worker、备份 SQLite 和 `.env`、安装 `requirements.lock`、单独调用 `init_db()`、运行 SQLite 检查、启动 Web、检查 `/health`、启动 Worker、等待 `/health/ready` 为 200。记录 SMTP 去重迁移、会话版本字段、Worker heartbeat 表、日志索引以及至少一次邮件语义。

- [ ] **Step 2: 对旧库执行两次升级演练**

复制真实结构的脱敏旧库到 `/tmp/ews-upgrade.sqlite3`，不得直接操作工作区生产数据库。

Run:

```bash
DATABASE_URL=sqlite:////tmp/ews-upgrade.sqlite3 .venv/bin/python -c "from app.db import init_db; init_db()"
DATABASE_URL=sqlite:////tmp/ews-upgrade.sqlite3 .venv/bin/python -c "from app.db import init_db; init_db()"
sqlite3 /tmp/ews-upgrade.sqlite3 "PRAGMA integrity_check; PRAGMA foreign_key_check;"
```

Expected: 两次升级退出 0；输出第一行为 `ok`，`foreign_key_check` 无后续行；新增表、字段和索引存在，最多一个 SMTP enabled。

- [ ] **Step 3: 运行最终自动化验收**

先使用 `superpowers:verification-before-completion`，再运行：

```bash
.venv/bin/ruff check .
.venv/bin/pytest --cov=app --cov-report=term-missing --cov-fail-under=93
.venv/bin/pip check
.venv/bin/pip-audit -r requirements.lock --strict
! rg -n "datetime\.utcnow|passlib|bcrypt<4|REPLACE_ME" app tests pyproject.toml requirements*.lock
rg -n "REPLACE_ME" .env.example
git diff --check
```

Expected: 所有命令退出 0；第一个 `rg` 无匹配，第二个 `rg` 只显示 `.env.example` 中明确用于拒绝占位符的示例。

- [ ] **Step 4: 验证 GitHub Actions 兼容矩阵**

将实现分支推送到 GitHub 并创建或更新 PR，等待 `3.11`、`3.12`、`3.13` 三个矩阵任务全部通过：

```bash
git push -u origin HEAD
gh pr view --json url || gh pr create --draft --fill
gh pr checks --watch
```

Expected: Ruff、pytest、覆盖率、`pip check` 和 `pip-audit` 在三个 Python 版本均成功。网络或 GitHub 权限不可用时，不得把项目标记为完成，报告应明确仍待 CI 验证。

- [ ] **Step 5: 启动本地 Web 与 Worker 并做浏览器验收**

使用 `browser:control-in-app-browser` skill。以测试数据库和测试 SMTP/SQL adapter 启动 Web 与 Worker，检查桌面 1440x900 和移动 390x844：

- 登录成功、错误登录和 Session 失效重定向。
- 仪表盘无内容重叠。
- 日志两组独立分页、筛选保留、CSV 下载。
- SQL Server `TrustServerCertificate=yes` 风险提示。
- SMTP 只能启用一个，编辑和删除反馈正确。
- 规则手动执行成功，执行与邮件日志一致。
- `/health` 为 200；Worker 正常时 `/health/ready` 为 200；停止 Worker 并超过阈值后为 503。

保存桌面和移动关键截图到 `/tmp/ews-acceptance/`，检查页面无横向溢出、文字遮挡、空白主区域和控制失效。

- [ ] **Step 6: 执行独立代码审查并处理发现**

使用 `superpowers:requesting-code-review`。审查范围从本计划前基线提交到当前 HEAD，重点检查事务边界、迁移回滚、敏感信息、后台循环失败隔离和分页查询。Critical、Important 和 Minor 发现均需修复并重新运行相关测试；最终不得遗留未处理 Minor。

- [ ] **Step 7: 生成事实型验收报告**

仅在步骤 2 至 6 全部通过后创建 `docs/project-acceptance-report.md`，包含：

1. 项目范围与架构边界。
2. 设计第 3 至 13 节的需求覆盖矩阵，列出实现文件和测试文件。
3. SQLite 升级、重复迁移、完整性与外键检查实际结果。
4. Ruff、pytest 数量、应用覆盖率、pip check、pip-audit、Python 3.11/3.12/3.13 CI 的实际结果和日期。
5. 桌面与移动浏览器验收结果及截图路径。
6. 部署、备份恢复、存活与就绪检查步骤。
7. 至少一次邮件语义、单机 SQLite 和单 Worker 等已知边界。
8. 结论。只有全部门槛满足时写“完成并通过验收”。

- [ ] **Step 8: 最终全量复验和提交**

Run:

```bash
.venv/bin/ruff check .
.venv/bin/pytest --cov=app --cov-report=term-missing --cov-fail-under=93
.venv/bin/pip check
.venv/bin/pip-audit -r requirements.lock --strict
sqlite3 /tmp/ews-upgrade.sqlite3 "PRAGMA integrity_check; PRAGMA foreign_key_check;"
git diff --check
git status --short
```

Expected: 所有检查退出 0；SQLite 输出 `ok` 且无外键异常；`git status --short` 仅允许本任务待提交的四个文档和两份明确排除的历史未跟踪计划。

```bash
git add docs/deployment.md docs/operations.md docs/project-requirements.md docs/project-acceptance-report.md
git commit -m "docs: record production acceptance results"
```

提交后再次运行 `git status --short`，确认只剩两份明确排除的历史未跟踪计划。
