# Task 15 本地验收阶段报告

## 状态

`DONE_WITH_CONCERNS`

本报告仅覆盖本地验收步骤 1-3 和本阶段文档提交。按控制器要求，未推送、未创建或更新
PR、未执行浏览器验收、未执行最终独立代码审查，且未创建
`docs/project-acceptance-report.md` 最终验收报告。

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

实际输出：`490 passed in 20.40s`，`Total coverage: 94.63%`，满足 `>=93%`。

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

## Concerns 与控制器后续门槛

1. 简报指定的原样占位符扫描不能退出 0，因为它同时搜索测试和用于拒绝占位符的生产校验。
   该命中不表示运行时密钥泄漏，也未在本任务中为使扫描通过而弱化安全校验；需由控制器
   确认是否接受该扫描范围，或在后续任务中调整扫描规则。
2. 待控制器完成 GitHub Actions 的 Python 3.11、3.12、3.13 CI 矩阵。
3. 待控制器完成桌面与移动浏览器验收及截图检查。
4. 待控制器完成最终独立代码审查。
5. 仅在上述外部门槛全部通过后，才能创建最终事实型验收报告
   `docs/project-acceptance-report.md`。
