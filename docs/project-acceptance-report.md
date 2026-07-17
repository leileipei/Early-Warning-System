# SQL 预警系统生产验收报告

**验收日期：** 2026-07-17
**验收提交：** `77b362d fix: surface production readiness failures`
**结论：** 完成并通过验收。

## 1. 项目范围与架构边界

系统由一个 FastAPI Web 进程和一个 Worker 进程组成，二者共享同一单机 SQLite 数据库和
环境配置；Worker 负责 Cron 调度、心跳和日志清理，Web 提供后台管理、手动执行和日志
查看。SQL Server 与 SMTP 均位于外部适配器边界。

本次不引入 Redis、队列、多 Worker、多 Web 或多主机高可用。生产运行边界为单 Web、
单 Worker 和共享的单机 SQLite。

## 2. 设计需求覆盖矩阵

| 设计章节 | 已验收实现 | 主要自动化测试 |
| --- | --- | --- |
| 3. 原子执行持久化 | `app/executor.py`、`app/execution_service.py`、`app/execution_lock.py` | `tests/test_executor.py`、`tests/test_execution_lock.py` |
| 4. 调度可靠性 | `app/scheduler.py`、`app/worker.py`、`app/settings.py` | `tests/test_scheduler.py`、`tests/test_worker.py`、`tests/test_settings.py` |
| 5. Worker 心跳与健康检查 | `app/models.py`、`app/worker_health.py`、`app/health.py`、`app/main.py` | `tests/test_worker_health.py`、`tests/test_health.py` |
| 6. 日志生命周期 | `app/log_service.py`、`app/routes.py`、`app/db.py` | `tests/test_log_service.py`、`tests/test_routes.py`、`tests/test_db.py` |
| 7. Session 与 Web 安全 | `app/auth.py`、`app/settings.py`、`app/web_security.py`、`app/admin_cli.py` | `tests/test_auth.py`、`tests/test_settings.py`、`tests/test_web_security.py`、`tests/test_admin_cli.py` |
| 8. SQL Server 与 SMTP 安全 | `app/sql_client.py`、`app/routes.py`、`app/execution_service.py`、`app/db.py` | `tests/test_sql_client.py`、`tests/test_routes.py`、`tests/test_db.py`、`tests/test_executor.py` |
| 9. 输入边界与错误处理 | `app/routes.py`、`app/error_reporting.py`、`app/settings.py` | `tests/test_routes.py`、`tests/test_error_reporting.py`、`tests/test_settings.py` |
| 10. Python 3.13 兼容 | `app/models.py`、`app/crypto.py`、`pyproject.toml` | `tests/test_dependency_configuration.py` 与 Python 3.11/3.12/3.13 CI |
| 11. 依赖与持续集成 | `requirements.lock`、`requirements-dev.lock`、`.github/workflows/ci.yml`、`.github/dependabot.yml` | CI 的 Ruff、pytest、覆盖率、`pip check`、`pip-audit` job |
| 12. 数据库升级 | `app/db.py`、`docs/deployment.md`、`docs/operations.md` | `tests/test_db.py` 与本报告的隔离 SQLite 升级演练 |
| 13. 测试与验收 | `tests/`、`docs/project-requirements.md` | 本地自动化、浏览器验收、CI 与独立审查 |

## 3. SQLite 升级与兼容性

使用仅含无敏感测试值的旧结构在 `/tmp/ews-upgrade.sqlite3` 演练，未读取、写入或提交
工作区数据库。连续两次调用 `init_db()` 均退出 0。

```text
PRAGMA integrity_check;  -> ok
PRAGMA foreign_key_check; -> 无输出
```

已确认新增 `alertruleversion`、`alertsuppression`、`ruleexecutionlease` 和
`workerheartbeat` 表；`adminuser.session_version` 字段；执行/邮件日志索引；以及
`uq_smtpconfig_single_enabled` 约束。旧库中两条启用 SMTP 迁移后仅保留更新时间较新的
一条启用记录，`enabled SMTP = 1`。

## 4. 最终本地自动化验收

2026-07-17 在 Python 3.11.15 锁定环境中完成：

| 检查 | 实际结果 |
| --- | --- |
| `ruff check .` | `All checks passed!` |
| `pytest --cov=app --cov-report=term-missing --cov-fail-under=93` | `507 passed in 22.49s`；总覆盖率 `94.55%`（门槛 93%） |
| `pip check` | 无破损依赖 |
| `pip-audit -r requirements.lock --strict` | `No known vulnerabilities found` |
| SQLite 再检 | `integrity_check` 为 `ok`；外键检查无行 |
| `git diff --check` | 通过 |

原计划的粗粒度命令
`rg -n "datetime\.utcnow|passlib|bcrypt<4|REPLACE_ME" app tests pyproject.toml requirements*.lock`
会命中 `app/settings.py` 的防御性 `REPLACE_ME` 校验和 `tests/` 中的测试字面量，不能如实
报告为“无匹配”。这些命中不是运行时依赖或泄漏；运行代码无 `datetime.utcnow`，无 Passlib
依赖，锁文件无 `bcrypt<4`。`.env.example` 仅有两条 `REPLACE_ME` 示例。

## 5. GitHub 持续集成

GitHub PR 为 <https://github.com/leileipei/Early-Warning-System/pull/2>，处于 draft 状态，
分支为 `codex/production-readiness` 到 `main`。HEAD `77b362d` 的 push 与 PR 两组 CI 均
完成：Python 3.11、3.12、3.13 共六个 job 均通过。GitHub Actions run IDs：
`29580393575`、`29580395226`。

## 6. 浏览器验收

验收使用真实本地 Web 和 Worker，测试数据库及测试 SQL/SMTP adapter。已在桌面
`1440x900` 和移动 `390x844` 验证：

- 错误登录显示固定“用户名或密码错误”且不回显密码；正确登录成功；增加
  `session_version` 后访问受保护页面重定向到 `/login`。
- 仪表盘无重叠或横向溢出；移动宽度 390 复验无横向溢出。
- 执行日志和邮件日志各有 120 条、各 1/3 页；执行日志翻到 2/3 页时邮件日志保持 1/3 页，
  URL 使用独立参数；筛选值保留，CSV download 事件成功。
- SQL Server 的 `trust_server_certificate=yes` 显示“证书校验风险”。SMTP 列表只有一条
  启用项；编辑停用项后列表显示新值，确认删除后记录消失。
- 手动执行规则后，执行和邮件日志各由 120 增至 121；首条时间一致，执行为
  `success`、`row_count=2`、`email_count=1`，邮件为 `success`。
- `/health` 返回 200；Worker 运行时 `/health/ready` 返回 200；停止 Worker 超过 3 秒后
  `/health/ready` 返回 503，组件状态为 `worker=stale`。最终提交 `77b362d` 再复验同样
  得到 200/503 行为。

关键截图：

- `/tmp/ews-acceptance/dashboard-desktop-1440x900.jpg`
- `/tmp/ews-acceptance/dashboard-mobile-390x844.jpg`
- `/tmp/ews-acceptance/settings-mobile-390x844.jpg`
- `/tmp/ews-acceptance/logs-mobile-390x844.jpg`

## 7. 部署、备份与健康检查

生产升级必须停机：先停止 Worker 和 Web，备份 SQLite 与配套 `.env`，使用发布版本的
`requirements.lock` 安装依赖，由单一进程执行 `init_db()`，再运行 SQLite 完整性和外键
检查。随后先启动 Web 并确认 `/health` 为 200，再启动唯一 Worker，等待
`/health/ready` 为 200 后恢复流量。

升级或健康检查失败时，保持服务停止，恢复同一时间点的 SQLite 与 `.env` 备份，检出
上一已知可用版本并按其锁文件安装依赖；再次通过完整性、外键、存活和就绪检查后恢复。
详细操作见 `docs/deployment.md` 与 `docs/operations.md`。

## 8. 已知边界

- 系统仅支持单 Web、单 Worker 与共享的单机 SQLite，不支持多 Worker、多 Web 或多主机
  SQLite 共享部署。
- 邮件是不可回滚的外部副作用，系统提供至少一次发送语义；在数据库提交失败或进程极端
  崩溃窗口中，重试可能造成重复邮件。
- 生产升级必须按停机、备份、检查和回滚流程执行。
- 浏览器验收使用测试 SQL/SMTP adapter；真实生产 SQL Server、SMTP 凭据、网络和证书链
  的连通性仍需由部署方在其环境中确认。

## 9. 独立代码审查

独立审查范围 `182048e..8285d66` 发现 3 个 Important 和 1 个 Minor：局部调度失败造成
健康误报、Session 占位密钥可被空白或大小写绕过、JSON/字典敏感值未完全脱敏、部署文档
遗漏六个参数。上述问题均由 `77b362d fix: surface production readiness failures` 修复。

复审范围 `8285d66..77b362d` 的 Critical、Important、Minor 均为 0，结论为
`Ready to merge=yes`；定点 63 个测试通过。

## 10. 最终结论

SQLite 升级、重复迁移、完整性与外键检查、本地自动化、Python 3.11/3.12/3.13 CI、桌面与
移动浏览器验收，以及独立代码审查均已完成并满足门槛。项目完成并通过验收。
