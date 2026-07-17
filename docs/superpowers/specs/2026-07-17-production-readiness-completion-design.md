# SQL 预警系统生产收口设计

## 1. 目标

在保留 FastAPI、SQLite、APScheduler、SQL Server 和独立 Worker 架构的前提下，完成生产可靠性、安全、长期运维和工程化收口，并生成可签署的项目验收报告。

本轮完成后，系统应满足：

- 抑制状态、执行记录和邮件记录保持数据库事务一致。
- Cron 短暂错过有明确宽限策略。
- 监控可以区分 Web 存活、数据库就绪和 Worker 健康。
- 日志具备分页、索引、保留和安全导出能力。
- Session、SQL Server 证书、SMTP 激活规则和响应头达到生产基线。
- Python 3.11、3.12、3.13 均通过 CI。
- 生产和开发依赖可复现，并执行依赖漏洞扫描。
- 全量测试、浏览器验收和数据库升级演练通过后生成验收报告。

## 2. 架构边界

继续采用单服务器部署：

- 一个 FastAPI Web 进程提供后台管理和手动执行。
- 一个 Worker 进程负责 Cron 调度、心跳和日志清理。
- Web 与 Worker 共享同一 SQLite 数据库和环境配置。
- SQL Server 与 SMTP 保持外部适配器边界。

本轮不引入 Redis、Celery、RabbitMQ、PostgreSQL 或多主机高可用。多 Web/Worker、队列化执行和停机期间全量补跑仍属于后续扩展。

## 3. 原子执行持久化

### 3.1 当前问题

当前抑制状态、执行记录和邮件记录分多次提交。邮件记录提交失败时可能留下执行记录，造成审计数据不一致。

### 3.2 目标行为

- SQL 查询和 SMTP 发送完成后，在一个 SQLite 事务中写入抑制状态、执行记录和全部邮件记录。
- 先 `flush` 执行记录取得主键，再添加邮件记录，最后统一 `commit`。
- 任一数据库操作失败时统一 `rollback`，不得留下部分抑制或日志记录。
- 租约获取与释放保持独立的 owner-token 保护，事务失败后仍必须释放当前执行者的租约。
- SMTP 已发送属于外部副作用，数据库回滚不能撤回邮件。系统维持“至少一次发送”语义，并在运维文档中说明极端情况下可能重复发送。

## 4. 调度可靠性

新增设置：

```dotenv
SCHEDULER_MISFIRE_GRACE_SECONDS=300
```

要求：

- 必须是正整数。
- 每个规则任务显式设置 `misfire_grace_time`。
- 保持 `coalesce=True` 和 `max_instances=1`。
- Worker 在宽限时间内只补最近一次错过执行，不追放大量历史任务。
- 规则新增、Cron 修改、启停和归档继续由动态同步机制处理。

## 5. Worker 心跳与健康检查

### 5.1 数据模型

新增单例 `WorkerHeartbeat` 表，至少包含：

- `id`，固定为 `1`。
- `worker_id`，每次 Worker 启动生成。
- `started_at`。
- `last_seen_at`。
- `last_sync_ok`。
- `last_error`，仅保存脱敏摘要。

新增设置：

```dotenv
WORKER_HEARTBEAT_TIMEOUT_SECONDS=60
```

Worker 启动后写入初始心跳；每次规则同步后更新状态。规则读取或同步失败时记录失败状态，下一周期成功后恢复。

### 5.2 HTTP 接口

- `/health` 保持 Web 进程存活检查，返回 `200`。
- `/health/ready` 检查 SQLite 可用性、必要表结构和 Worker 心跳。
- 就绪成功返回 `200`；数据库不可用、心跳过期或最近同步失败时返回 `503`。
- 响应只包含组件状态和安全摘要，不返回数据库路径、连接字符串、密码或堆栈。

## 6. 日志生命周期

### 6.1 分页

- 执行日志和邮件日志使用独立页码参数。
- 默认每页 50 条，允许范围 10 至 200 条。
- 现有状态、触发方式、规则 ID 和关键词筛选继续生效。
- 页面显示总数、当前页和前后翻页操作。

### 6.2 索引

为常用筛选和排序字段增加幂等索引：

- `ExecutionLog.started_at`
- `ExecutionLog.status`
- `ExecutionLog.rule_id`
- `MailLog.sent_at`
- `MailLog.status`
- `MailLog.execution_log_id`

### 6.3 保留清理

新增设置：

```dotenv
LOG_RETENTION_DAYS=180
LOG_CLEANUP_INTERVAL_SECONDS=86400
```

Worker 定期执行清理：先删除过期邮件日志，再删除对应执行日志。清理采用批次事务，单批失败不得影响规则调度，失败信息写入日志并在下一周期重试。

### 6.4 CSV 安全

- CSV 改为分批流式生成，避免一次加载全部记录。
- 所有文本单元格统一处理中和公式前缀 `=`、`+`、`-`、`@`。
- 保留 UTF-8 BOM 和现有列定义。
- CSV 仍导出全部日志，不受页面筛选条件影响。

## 7. Session 与 Web 安全

新增设置：

```dotenv
SESSION_MAX_AGE_SECONDS=28800
SESSION_IDLE_TIMEOUT_SECONDS=1800
```

要求：

- `SESSION_SECRET` 至少 32 字节且不能使用占位值。
- 登录 Session 记录认证时间、最后活动时间和管理员会话版本。
- 超过绝对有效期或空闲超时后清除 Session 并要求重新登录。
- 管理员密码更新时递增会话版本，使旧 Session 失效。
- HTTPS Cookie 继续由 `SESSION_COOKIE_SECURE` 控制。

应用增加以下响应头：

- Content Security Policy
- `X-Content-Type-Options: nosniff`
- `Referrer-Policy`
- `Permissions-Policy`
- 禁止页面被其他站点嵌入
- HTTPS 模式下启用 HSTS

不符合 CSP 的内联脚本迁移到静态资源文件。

## 8. SQL Server 与 SMTP 安全

### 8.1 SQL Server 证书

- 新数据源默认使用 `Encrypt=yes`、`TrustServerCertificate=no`。
- 现有数据源不自动覆盖，避免企业自签名环境升级后立即中断。
- 配置页面对 `TrustServerCertificate=yes` 显示风险提示。
- 私有 CA 继续通过操作系统信任库或正式证书链处理。

### 8.2 SMTP 唯一启用

- 新建或编辑 SMTP 时，启用当前配置会在同一事务中停用其他 SMTP。
- 数据库增加“最多一个启用 SMTP”的约束。
- 旧库升级时保留 `updated_at` 最新的启用配置，其余自动停用，并记录升级说明。
- 执行服务不再静默按更新时间从多个启用项中选择。

## 9. 输入边界与错误处理

服务器端统一验证：

- 端口范围 1 至 65535。
- 查询、连接和 SMTP 超时范围为 1 至 600 秒。
- `max_rows` 范围为 1 至 5000。
- 规则导入文件最大 1 MiB，单次最多导入 500 条规则。
- SMTP 的 TLS 与 SSL 组合必须有效。

面向页面的错误只返回可操作摘要。完整异常写入服务日志，且不得包含密码、Fernet 密钥或完整连接字符串。

## 10. Python 3.13 兼容

- 将 `datetime.utcnow()` 替换为兼容写法，同时继续以无时区 UTC 值存储，避免改变现有数据库语义。
- 移除 Passlib，直接使用现代 `bcrypt` 生成和验证密码，并兼容现有 `$2b$` 哈希。
- 开发测试依赖切换到 `httpx2`，消除 Starlette TestClient 弃用警告。
- CI 覆盖 Python 3.11、3.12、3.13。

## 11. 依赖与持续集成

产物包括：

- 生产依赖锁文件。
- 开发依赖锁文件。
- GitHub Actions 工作流。
- Dependabot 配置。

CI 必须执行：

- Ruff
- pytest 全量测试
- 应用覆盖率不低于 93%
- `pip check`
- `pip-audit`
- Python 3.11、3.12、3.13 兼容矩阵

高危依赖漏洞、测试失败或覆盖率不达标时不得通过 CI。

## 12. 数据库升级

继续使用现有停机单进程升级流程：

1. 停止 Web 和 Worker。
2. 备份 SQLite 与 `.env`。
3. 更新代码和依赖。
4. 单独执行 `init_db()`。
5. 验证 `integrity_check`、外键和新增表/索引。
6. 启动 Web，验证存活和就绪接口。
7. 启动 Worker，等待心跳变为健康。

所有新增表、字段、约束和索引必须幂等，并覆盖旧数据库、重复升级和异常回滚测试。

## 13. 测试与验收

所有生产行为按 RED-GREEN-REFACTOR 实施。测试至少覆盖：

- 日志和抑制状态原子回滚。
- misfire 宽限配置。
- Worker 心跳正常、过期和错误状态。
- 数据库就绪失败。
- 日志分页、索引、保留边界和批次清理。
- CSV 流式导出与公式中和。
- Session 密钥强度、绝对超时、空闲超时和会话版本。
- 安全响应头和 CSP。
- SQL Server 证书安全默认值。
- SMTP 唯一启用及旧库升级。
- Python 3.11 至 3.13 兼容。

浏览器验收覆盖桌面和移动端的登录、仪表盘、日志分页、配置风险提示、规则手动执行和健康状态。

最终完成门槛：

- 全量测试通过。
- 应用覆盖率不低于 93%。
- Ruff、`pip check`、`pip-audit` 和 `git diff --check` 通过。
- SQLite `integrity_check` 返回 `ok`，外键检查无异常。
- 独立代码审查无 Critical、Important 或未处理 Minor。
- 工作树干净。

## 14. 最终报告

验收通过后生成 `docs/project-acceptance-report.md`，内容包括：

- 项目范围和架构。
- 需求覆盖矩阵。
- 数据库升级和兼容说明。
- 自动化测试、覆盖率、静态检查和漏洞扫描结果。
- 桌面与移动端浏览器验收结果。
- 部署、备份恢复和健康检查步骤。
- 已知架构边界和后续扩展建议。

只有全部完成门槛满足后，报告才可将项目标记为“完成并通过验收”。
