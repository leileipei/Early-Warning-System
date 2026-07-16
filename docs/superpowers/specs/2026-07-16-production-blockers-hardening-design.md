# 生产阻断项加固设计

## 1. 目标

在不引入 Redis、任务队列或新的业务数据库的前提下，修复当前版本中阻碍单机生产部署的四项问题：

- 阻止邮件模板访问 Python 内部对象或操作系统能力。
- 为 SMTP SSL 和 STARTTLS 启用服务器证书与主机名校验。
- 防止同一规则被 Web、Worker 或多个进程并发执行。
- 将仪表盘从静态占位内容改为真实运行数据。

本阶段保持 FastAPI、SQLModel、SQLite、APScheduler、Jinja2 和 SMTP 的现有技术栈，不改变规则 JSON 格式，也不修改 SQL Server、SMTP 或预警规则表结构。

## 2. 范围

### 2.1 本阶段包含

- 模板渲染沙箱。
- SMTP TLS 证书校验。
- 基于 SQLite 的规则执行租约。
- 真实仪表盘统计和最近执行信息。
- 对应自动化测试、环境变量示例、部署文档和运维文档。

### 2.2 本阶段不包含

- SQL Server `TrustServerCertificate` 默认值调整。
- Redis、Celery、RabbitMQ 或其他任务队列。
- Worker 心跳、深度健康检查和外部监控平台。
- 日志分页、日志保留策略和数据库索引优化。
- 多角色权限、SSO、审批流或规则恢复通知。
- SQLite 迁移框架替换。

这些事项继续保留在后续生产完善阶段，避免本次安全与正确性修复扩大为架构重写。

## 3. 模板安全

### 3.1 渲染环境

`app.template_renderer` 使用 `jinja2.sandbox.ImmutableSandboxedEnvironment` 替代直接构造 `jinja2.Template`。

主题和正文分别使用独立环境：

- 主题关闭 HTML 自动转义。
- HTML 正文开启自动转义。
- 两个环境均使用 `StrictUndefined`。
- 清空环境默认 globals，避免暴露 `cycler`、`joiner`、`namespace` 等可被滥用的对象。
- 保留 Jinja2 沙箱允许的条件、循环、变量访问和安全过滤器。

汇总邮件中的结果表格继续由应用生成，并以可信 `Markup` 注入；SQL 返回值和列名仍逐项进行 HTML 转义。

### 3.2 拦截行为

访问私有属性、Python 类型层次、函数 globals、模块或其他沙箱禁止对象时，Jinja2 抛出的安全异常统一转换为现有 `TemplateRenderError`。

规则执行收到该错误后：

- 状态记为 `failed`。
- 不调用 SMTP。
- 不进入瞬时故障重试。
- 执行日志仅保存安全错误摘要，不保存敏感配置。

现有正常模板变量、条件、循环和 HTML 表格行为保持兼容。

## 4. SMTP 传输安全

### 4.1 证书校验

`app.execution_service.build_smtp_mailer()` 创建一个 `ssl.create_default_context()` 返回的客户端 Context，并在两种模式中复用：

- SSL 模式通过 `smtplib.SMTP_SSL(..., context=context)` 建立连接。
- STARTTLS 模式通过 `client.starttls(context=context)` 升级连接。

默认 Context 必须满足：

- `verify_mode == ssl.CERT_REQUIRED`。
- `check_hostname is True`。

系统不提供跳过 SMTP 证书校验的页面开关。内部自签名或私有 CA 证书通过操作系统信任库或 `SSL_CERT_FILE` 配置，避免长期保留不安全旁路。

### 4.2 错误处理

证书链、有效期或主机名校验失败时，现有邮件发送适配器返回失败结果。规则按现有 SMTP 瞬时失败策略处理并记录最终错误摘要，不输出 SMTP 密码。

## 5. 规则执行租约

### 5.1 数据模型

新增 `RuleExecutionLease` SQLModel 表：

- `rule_id`: 主键，同时关联 `AlertRule.id`。
- `owner_token`: 本次执行生成的随机不可预测标识。
- `acquired_at`: 获取租约的 UTC 时间。
- `expires_at`: 租约自动失效的 UTC 时间。

新表通过现有 `SQLModel.metadata.create_all()` 自动创建，旧 SQLite 文件无需手工执行迁移命令。

### 5.2 原子获取

新增独立模块 `app.execution_lock`，定义 `RuleExecutionInProgressError`，并提供上下文管理器 `rule_execution_lease(session: Session, rule_id: int, *, lease_seconds: int, now_fn: Callable[[], datetime] = utc_now) -> Iterator[None]`。

获取租约使用 SQLite 原子 `INSERT ... ON CONFLICT ... DO UPDATE ... WHERE expires_at <= now`。只有不存在租约或旧租约已经过期时才能成功写入。执行者保存自己的 `owner_token`，释放时必须同时匹配 `rule_id` 和 `owner_token`，避免旧执行误删新执行已经接管的租约。

### 5.3 生命周期

- `execute_rule_by_id()` 在任何 SQL、模板或 SMTP 操作前获取租约。
- 正常完成、业务失败或 Python 异常都在 `finally` 中释放租约。
- 进程被强制终止时租约保留，超过 `RULE_EXECUTION_LEASE_SECONDS` 后可被下一次执行原子接管。
- 默认租约时间为 `7200` 秒，配置必须是正整数。

本阶段不实现租约心跳。超过两小时的规则应通过调大环境变量支持；后续队列化阶段再引入续租机制。

### 5.4 冲突反馈

- 手动执行遇到租约占用时返回 HTTP `409`，规则页面显示“规则正在执行，请稍后重试”，不创建执行日志。
- Worker 遇到租约占用时记录包含规则 ID 的警告并跳过本次触发，不让异常影响其他任务。
- 租约只解决执行实例互斥；现有重复预警抑制继续处理 SQL 结果中的业务 Key。

## 6. 真实仪表盘

### 6.1 指标定义

仪表盘从系统数据库计算：

- `启用规则`: `archived_at IS NULL AND enabled = true` 的规则数量。
- `今日执行`: 按 `Asia/Shanghai` 自然日换算为 UTC 查询区间后的执行记录数量。
- `近 24 小时失败`: 最近 24 小时状态为 `failed` 或 `partial_failed` 的执行数量。
- `最近执行`: 按开始时间倒序显示最近 5 条执行记录，并显示规则名称、状态、触发方式、行数和邮件数。
- `邮件概览`: 最近 24 小时成功和失败邮件数量。

“待处理告警”改名为“近 24 小时失败”，因为当前模型没有确认或关闭告警的状态，不能声称这些记录仍待处理。

### 6.2 空状态

只有查询结果确实为空时才显示空状态。指标始终显示真实数字，不再在模板中硬编码 `0`。

仪表盘查询失败按现有 Web 错误处理返回服务错误，不用伪造零值掩盖数据库问题。

## 7. 配置与文档

新增配置：

```dotenv
RULE_EXECUTION_LEASE_SECONDS=7200
```

更新内容：

- `.env.example` 说明默认值。
- `docs/deployment.md` 说明租约用途、长任务配置和 SMTP 私有 CA 信任方式。
- `docs/operations.md` 说明 HTTP 409、过期租约和 SMTP 证书错误排查。
- `docs/project-requirements.md` 补充模板沙箱、SMTP 证书校验、跨入口执行互斥和真实仪表盘验收要求。

## 8. 测试策略

所有生产代码按测试驱动方式实现，并覆盖以下行为：

### 8.1 模板测试

- 现有主题、正文、循环、条件、行变量和汇总表格继续渲染。
- 访问 `__class__`、`__globals__` 或系统对象时抛出 `TemplateRenderError`。
- 危险模板不会调用邮件发送器。

### 8.2 SMTP 测试

- SSL 模式将校验 Context 传给 `SMTP_SSL`。
- STARTTLS 模式将同类 Context 传给 `starttls()`。
- Context 启用证书和主机名校验。

### 8.3 租约测试

- 首个执行者成功获取并释放租约。
- 未过期租约拒绝第二个执行者。
- 过期租约可被接管。
- 旧持有者不能释放新持有者租约。
- 执行异常后租约仍释放。
- 手动执行冲突返回 409。
- Worker 遇到冲突时跳过并记录警告。
- 两个并发执行请求最多一个进入业务执行逻辑。

### 8.4 仪表盘测试

- 真实启用规则数量。
- `Asia/Shanghai` 今日边界统计。
- 最近 24 小时失败统计。
- 最近 5 条执行排序和规则名称。
- 邮件成功/失败数量。
- 数据为空时的真实零值和空状态。

### 8.5 完整验证

- 运行完整 pytest 测试套件。
- 运行覆盖率报告，应用代码覆盖率不得低于当前 `93%` 基线。
- 运行 Ruff。
- 运行 `pip check`。
- 对本地 SQLite 执行 `PRAGMA integrity_check` 和 `PRAGMA foreign_key_check`。

## 9. 验收标准

满足以下条件后，本阶段完成：

1. 已验证的模板注入探针被沙箱拒绝。
2. 正常现有模板不需要人工迁移。
3. SMTP SSL 和 STARTTLS 均强制验证证书及主机名。
4. 手动、定时和多进程入口不能同时执行同一规则。
5. 异常退出遗留租约可在配置期限后接管。
6. 仪表盘所有数字和列表来自数据库查询。
7. 旧 SQLite 数据可以直接启动并自动创建租约表。
8. 文档说明新配置、证书和冲突排查方式。
9. 完整测试、覆盖率、Ruff、依赖和数据库完整性检查通过。
