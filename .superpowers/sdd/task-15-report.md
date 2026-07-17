# Task 15 验收账本

## 状态

`DONE`

本账本初始记录本地验收步骤 1-3；控制器已完成 CI、浏览器验收和独立代码审查。
最终事实型验收报告已创建于 `docs/project-acceptance-report.md`。本账本不替代该报告。

## 本阶段文档

- `docs/deployment.md`：补充可重复 `init_db()`、SQLite 完整性与外键检查、
  `/health` 和 `/health/ready` 的语义、单 Worker 边界，以及停机升级与回滚顺序。
- `docs/operations.md`：补充备份校验、锁定依赖安装、停机迁移、服务恢复顺序、回滚条件、
  Worker heartbeat、SMTP 单启用迁移、session version、日志索引和至少一次发送语义。
- `docs/project-requirements.md`：将上述架构和部署不变量写入需求、数据对象与生产加固
  验收补充。

## 执行环境

使用 `/opt/homebrew/bin/python3.11`（`Python 3.11.15`）创建临时锁定环境：

```bash
/opt/homebrew/bin/python3.11 -m venv /tmp/ews-task15-py311
/tmp/ews-task15-py311/bin/pip install -r requirements-dev.lock
/tmp/ews-task15-py311/bin/pip install --no-deps -e .
```

实际结果：`requirements-dev.lock` 中的依赖及当前项目均安装成功。临时环境和升级库均位于
`/tmp`；未读取、修改或提交工作树的 `early_warning.sqlite3`。

## SQLite 升级演练

`/tmp/ews-upgrade.sqlite3` 由无敏感值的旧结构创建：旧 `adminuser` 缺少
`session_version`，旧 `executionlog`/`maillog` 缺少目标索引，且旧 `smtpconfig` 有两条
启用配置。随后以显式的测试 `DATABASE_URL` 和测试密钥连续两次运行：

```bash
DATABASE_URL=sqlite:////tmp/ews-upgrade.sqlite3 \
  /tmp/ews-task15-py311/bin/python -c "from app.db import init_db; init_db()"
DATABASE_URL=sqlite:////tmp/ews-upgrade.sqlite3 \
  /tmp/ews-task15-py311/bin/python -c "from app.db import init_db; init_db()"
sqlite3 /tmp/ews-upgrade.sqlite3 "PRAGMA integrity_check; PRAGMA foreign_key_check;"
```

实际输出：

```text
--- integrity_and_foreign_keys ---
ok
```

`foreign_key_check` 没有后续行。随后用 `sqlite_master`、`PRAGMA table_info(adminuser)` 和
SQL 查询验证，实际输出如下：

```text
alertruleversion
alertsuppression
ruleexecutionlease
workerheartbeat

session_version  INTEGER  notnull=1  default=1

ix_executionlog_rule_id
ix_executionlog_started_at
ix_executionlog_status
ix_maillog_execution_log_id
ix_maillog_sent_at
ix_maillog_status
uq_smtpconfig_single_enabled

enabled_smtp_count
1

id  enabled  updated_at
1   0        2026-01-01 00:00:00
2   1        2026-01-02 00:00:00
```

两次 `init_db()` 均退出 0；较新的 SMTP 记录保留为唯一启用项。

## 本地自动化验收

```bash
/tmp/ews-task15-py311/bin/ruff check .
```

实际输出：`All checks passed!`

```bash
/tmp/ews-task15-py311/bin/pytest --cov=app --cov-report=term-missing --cov-fail-under=93
```

2026-07-17 的最终锁环境复验（Python 3.11.15）实际输出：`519 passed in 22.31s`，
`Total coverage: 94.11%`，满足 `>=93%`。

```bash
/tmp/ews-task15-py311/bin/pip check
```

实际输出：`No broken requirements found.`

```bash
/tmp/ews-task15-py311/bin/pip-audit -r requirements.lock --strict
```

实际输出：`No known vulnerabilities found`。首次受限沙箱运行无法让 pip-audit 的隔离环境
升级 pip；获准访问漏洞数据库后以同一命令完成严格审计。

```bash
! rg -n "datetime\.utcnow|passlib|bcrypt<4|REPLACE_ME" \
  app tests pyproject.toml requirements*.lock
```

实际退出码为 `1`，命中内容为项目已有的拒绝占位符校验和测试字面量，而非运行时配置：

```text
tests/test_dependency_configuration.py:45:    assert "passlib" not in production | development
tests/test_routes.py:252:                "session_secret": "REPLACE_ME_WITH_RANDOM_SESSION_SECRET",
tests/test_routes.py:260:                "secret_key": "REPLACE_ME_WITH_32_BYTE_URL_SAFE_FERNET_KEY",
app/settings.py:40:        if value.startswith("REPLACE_ME")
app/settings.py:51:        if value.startswith("REPLACE_ME")
```

```bash
rg -n "REPLACE_ME" .env.example
```

实际输出：

```text
11:SESSION_SECRET=REPLACE_ME_WITH_RANDOM_SESSION_SECRET
14:SECRET_KEY=REPLACE_ME_WITH_32_BYTE_URL_SAFE_FERNET_KEY
```

```bash
git diff --check
```

实际结果：退出 0，无输出。

## 原样扫描说明

简报指定的原样占位符扫描不能退出 0，因为它同时搜索测试和用于拒绝占位符的生产校验。
该命中不表示运行时密钥泄漏，也没有为使扫描通过而弱化安全校验。精确检查结论为：运行
代码无 `datetime.utcnow`，无 Passlib 依赖，锁文件无 `bcrypt<4`；`.env.example` 仅有两条
明确用于拒绝占位符的示例。

## 外部门槛完成记录

- GitHub PR：<https://github.com/leileipei/Early-Warning-System/pull/2>（draft，
  `codex/production-readiness` 到 `main`）。HEAD `aef3fe5` 的 push/PR 两组 CI，
  Python 3.11、3.12、3.13 共六个 job 均通过；run IDs 为 `29583486029` 与 `29583490199`。
- 浏览器验收：真实本地 Web 与 Worker 使用测试数据库和 adapter，桌面 `1440x900` 与移动
  `390x844` 验收通过；截图位于 `/tmp/ews-acceptance/`。
- 验收加固：`419d935` 修正嵌套结构脱敏、operations 停机/备份顺序及连续编号、deployment
  1-22 章节号；干净移动日志截图为
  `/tmp/ews-acceptance/logs-mobile-390x844-clean.jpg`。`a886984` 支持格式化多行 JSON 和
  键冒号换行脱敏；`aef3fe5` 对未闭合、65 层深度、32768 扫描窗口歧义统一 fail-closed。
- 最终独立复审：`a886984..aef3fe5` 的 Critical/Important/Minor 均为 0，`Ready=yes`；
  定点 `tests/test_error_reporting.py` 为 `22 passed`。此前文档和截图的 2 个 Important、
  3 个 Minor，以及跨行与 fail-closed 边界均已关闭。
