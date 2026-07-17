# SQL 预警系统运维手册

## 1. 运维目标

本文档面向系统管理员和值班人员，说明 SQL 预警系统上线后的日常巡检、备份恢复、升级发布和故障处理方法。

部署安装、首次配置和外部 IP 访问说明见：

[deployment.md](deployment.md)

## 2. 服务组成

系统生产环境通常包含两个进程：

- Web 服务：提供后台页面、配置管理、手动执行、日志查看、CSV 导出和规则 JSON 导入导出。
- Worker 服务：加载启用规则，并按 Cron 表达式定时执行。

两者必须使用同一份 `.env` 和同一份 `DATABASE_URL`。如果 Worker 未运行，手动执行仍可用，但定时预警不会触发。

## 3. 日常巡检清单

建议每天或每个工作日执行一次巡检。

### 3.1 Web 服务

检查项：

- 登录页可以访问。
- 管理员可以登录后台。
- 仪表盘可以正常打开。
- “配置”“规则”“日志”页面无异常报错。

命令示例：

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/health
```

预期结果：

```text
200
```

### 3.2 Worker 服务

检查项：

- Worker 进程正在运行。
- 定时规则按计划产生执行日志。
- 最近一次定时执行时间符合 Cron 预期。

如果使用 systemd，可以查看：

```bash
systemctl status early-warning-worker
```

如果没有使用 systemd，可以用进程命令检查：

```bash
ps aux | grep "app.worker"
```

除进程状态外，还应检查就绪端点：

```bash
curl -sS -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/health/ready
```

预期为 `200`。该端点会检查数据库架构和 Worker 心跳：Worker 缺失、心跳超过
`WORKER_HEARTBEAT_TIMEOUT_SECONDS` 或最近一次同步失败时返回 `503`。`/health`
只证明 Web 可以响应，不能替代 `/health/ready`。

SQLite 部署仅允许一个 Worker；不应以多个 Web/Worker 进程或多个主机共享同一个
SQLite 文件来扩容。Worker 的启动和每次同步会更新 `workerheartbeat` 表，值班时可将
该表作为心跳故障的排查依据。

### 3.3 规则执行结果

后台进入“日志”页面，重点检查：

- 最近执行状态是否大量 `failed`。
- 是否出现 `partial_failed`。
- 错误信息是否集中在同一个数据源、SMTP 或规则。
- 邮件日志是否有连续发送失败。

页面支持按状态、触发方式、规则 ID 和关键词筛选。

### 3.4 日志保留清理

Worker 启动时会立即清理一次过期审计日志，之后按
`LOG_CLEANUP_INTERVAL_SECONDS` 定期执行；默认间隔为 86400 秒。保留天数由
`LOG_RETENTION_DAYS` 控制，默认保留 180 天。

仅在执行日志的 `finished_at` 早于保留截止时间时才会删除；恰好等于截止时间的
日志会保留，状态为 `running` 的日志不会删除。清理以每批 500 条执行日志处理，
先删除关联邮件日志再删除执行日志，并为每批单独提交事务。

单批失败会回滚该批并记录 Worker 错误，已经提交的前序批次不会回滚；Worker 将在
下一个正常清理周期再次尝试，规则同步和定时调度不受清理失败影响。

### 3.5 外部依赖

检查项：

- SQL Server 网络可达。
- SQL Server 只读账号未过期、未锁定。
- SMTP 账号未过期、未锁定。
- 服务器到 SQL Server 和 SMTP 的防火墙规则未变更。
- Microsoft ODBC Driver 仍可被 Python 识别。

ODBC 驱动检查：

```bash
python3 -c "import pyodbc; print(pyodbc.drivers())"
```

## 4. 备份策略

### 4.1 必备备份对象

默认 SQLite 部署至少备份：

- `early_warning.sqlite3`
- `.env`
- 规则导出 JSON 文件

说明：

- SQLite 中保存系统配置、规则、执行日志和加密后的密码。
- `.env` 中的 `SECRET_KEY` 用于解密 SQL Server 和 SMTP 密码。
- 数据库文件和 `.env` 必须配套保存；只备份数据库而丢失 `SECRET_KEY`，旧密码将无法解密。

### 4.2 备份频率

建议：

- 每天备份 SQLite 数据库。
- 每次调整规则、数据源或 SMTP 后手动导出规则 JSON。
- 每次升级发布前做一次完整备份。

### 4.3 SQLite 备份命令

如果系统写入量不大，可以直接复制数据库文件：

```bash
cp early_warning.sqlite3 backups/early_warning_$(date +%F_%H%M%S).sqlite3
```

更稳妥的方式是使用 SQLite 在线备份：

```bash
sqlite3 early_warning.sqlite3 ".backup 'backups/early_warning_$(date +%F_%H%M%S).sqlite3'"
```

备份 `.env`：

```bash
cp .env backups/env_$(date +%F_%H%M%S)
```

备份完成后至少执行一次完整性检查；`integrity_check` 首行必须为 `ok`，
`foreign_key_check` 必须无输出：

```bash
sqlite3 backups/early_warning_2026-01-01_000000.sqlite3 \
  "PRAGMA integrity_check; PRAGMA foreign_key_check;"
```

### 4.4 规则导出

进入后台“规则”页面，点击“导出规则”。

导出文件用于迁移和恢复规则，但不包含 SQL Server 密码和 SMTP 配置。恢复到新环境时，必须先创建同名数据源，再导入规则 JSON。

## 5. 恢复流程

### 5.1 原服务器恢复

1. 停止 Web 和 Worker。
2. 恢复 `early_warning.sqlite3`。
3. 恢复匹配的 `.env`。
4. 启动 Web。
5. 登录后台，检查配置和规则。
6. 启动 Worker。
7. 手动执行一条低风险规则，确认 SQL 和邮件链路正常。

### 5.2 新服务器迁移

1. 按 `deployment.md` 完成环境安装。
2. 放置 `.env`，确认 `SECRET_KEY` 与旧环境一致。
3. 恢复 SQLite 数据库，或先创建数据源和 SMTP 后导入规则 JSON。
4. 安装 SQL Server ODBC 驱动。
5. 启动 Web 和 Worker。
6. 在“配置”页面测试 SQL Server 连接和 SMTP 发送。
7. 在“日志”页面确认执行记录正常产生。

## 6. 升级发布流程

建议按以下顺序升级。以下 systemd 服务名仅为示例，使用其他守护器时需执行等价操作，
并在窗口内禁止自动重启：

1. 通知相关人员，确认升级窗口。
2. 备份 SQLite 数据库和 `.env`。
3. 导出规则 JSON。
4. 停止 Worker，避免升级过程中定时任务执行；再停止 Web。
6. 拉取最新代码。
7. 安装或更新依赖。
8. 在 Web 和 Worker 均保持停止的状态下，由单一进程执行一次 `init_db()`。
9. 确认 `init_db()` 成功退出，运行 `PRAGMA integrity_check` 和
   `PRAGMA foreign_key_check`，分别得到 `ok` 和无外键异常。
10. 启动 Web，并确认 `/health` 返回 200。
11. 启动唯一的 Worker，等待 `/health/ready` 返回 200。
12. 手动执行一条测试规则。
13. 检查日志页面是否有异常。

命令示例：

```bash
systemctl stop early-warning-worker
systemctl stop early-warning-web
sqlite3 early_warning.sqlite3 ".backup 'backups/early_warning_$(date +%F_%H%M%S).sqlite3'"
cp .env backups/env_$(date +%F_%H%M%S)
git pull
.venv/bin/pip install -r requirements.lock
.venv/bin/pip install --no-deps .
.venv/bin/python -c "from app.db import init_db; init_db()"
sqlite3 early_warning.sqlite3 "PRAGMA integrity_check; PRAGMA foreign_key_check;"
systemctl start early-warning-web
curl -fsS http://127.0.0.1:8000/health
systemctl start early-warning-worker
curl -fsS -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/health/ready
```

只有 `init_db()` 成功后才能重新启动 Web 和 Worker。不要依赖两个服务进程并发启动来完成数据库升级。如果使用 systemd 或 Supervisor，请使用对应服务管理命令替代手工启动，并确保升级期间不会自动拉起服务。

### 6.1 升级失败回滚

出现迁移异常、SQLite 完整性/外键检查失败、Web `/health` 非 200，或 Worker 启动后
`/health/ready` 在升级窗口内仍非 200 时，保持 Web 和 Worker 停止并执行回滚：

1. 检出上一已知可用的代码版本，并使用该版本的 `requirements.lock` 重新安装依赖。
2. 恢复同一时间点的 SQLite 备份与 `.env`；两者必须配对，尤其不能丢失或替换用于
   解密历史密码的 `SECRET_KEY`。
3. 对恢复后的数据库运行 `PRAGMA integrity_check; PRAGMA foreign_key_check;`。
4. 先启动 Web 并确认 `/health` 为 200，再启动唯一 Worker 并确认
   `/health/ready` 为 200。

不要在失败的新库上手工删表、删索引或手改迁移状态来继续上线；保留副本后按变更流程
排查，并在隔离副本上复现。

## 7. 常见故障处理

### 7.1 无法通过服务器 IP 访问

检查项：

- Web 是否使用 `--host 0.0.0.0` 启动。
- 服务器防火墙是否开放端口。
- 云服务器安全组是否开放端口。
- 反向代理是否指向正确后端。

### 7.2 登录页打不开

检查项：

- Web 进程是否运行。
- `.env` 是否存在。
- `SESSION_SECRET` 是否已配置。
- `SECRET_KEY` 是否是有效 Fernet key。
- 数据库文件是否可读写。

### 请求返回 403

刷新页面后重试，确认提交来自系统页面并携带当前 Session 的 CSRF Token。检查反向代理是否保留 Cookie。

### 登录返回 429

默认同一客户端 IP 和用户名在 15 分钟内失败 5 次后锁定 15 分钟。等待 `Retry-After` 指定时间，并检查是否存在错误密码或自动化尝试。重启 Web 会清空当前单进程限流状态，但不应作为常规解锁方式。

### 7.3 定时规则不执行

检查项：

- Worker 是否运行。
- 规则是否启用。
- Cron 表达式是否符合预期时区。
- Web 和 Worker 是否使用同一数据库。
- 规则最近是否被编辑或导入为停用状态。
- Worker 会按 `SCHEDULER_SYNC_INTERVAL_SECONDS` 周期同步规则；修改后请等待至少一个同步周期。
- 如果 Worker 日志出现“读取预警规则失败”，检查 SQLite 文件权限和并发锁定情况；已有任务会保留，系统会在下一周期重试。

### 7.4 SQL Server 连接失败

检查项：

- 数据源主机、端口、数据库名是否正确。
- ODBC 驱动名称是否与服务器安装一致。
- SQL Server 是否允许远程连接。
- 只读账号密码是否正确。
- `Encrypt` 和 `TrustServerCertificate` 是否符合 SQL Server 配置。
- 服务器到 SQL Server 的网络是否可达。

后台“配置”页面的“测试连接”可以用于快速验证。

### 7.5 SQL 检测失败

检查项：

- SQL 是否为单条 `SELECT` 或 `WITH` 查询。
- SQL 是否包含被禁止的写入、DDL 或执行关键字。
- 所选数据源是否指向正确数据库。
- 数据库账号是否有读取相关表或视图的权限。

### 7.6 邮件发送失败

检查项：

- SMTP 主机和端口是否正确。
- TLS/SSL 开关是否符合公司邮件服务器要求。
- 用户名、密码或授权码是否正确。
- 发件人地址是否允许该账号发送。
- SMTP 账号是否被锁定或过期。
- 公司邮件服务是否限制服务器来源 IP。

后台“配置”页面的“测试发送”可以用于快速验证。

### SMTP 与执行记录一致性

抑制状态、执行日志和邮件日志会在同一个 SQLite 事务中写入；任一写入失败时，这些本地记录会全部回滚。SMTP 发送属于不可回滚的外部副作用：邮件发送成功后，如果 SQLite 提交失败，已经发出的邮件无法撤回。后续重试可能产生重复邮件，因此系统提供的是至少一次发送语义。

数据库迁移会清理历史上多个启用 SMTP 的记录，只保留 `updated_at` 最新（再以 ID 最大为准）
的一条，并以部分唯一索引强制最多一个 `enabled` SMTP 配置。迁移前后均不得通过直接
修改 SQLite 绕过该约束。

管理员修改密码或由命令行重置管理员密码时，`session_version` 会递增，旧浏览器会话在
下一次请求时失效并需重新登录。这是预期安全行为，不应通过手工修改数据库规避。

`executionlog` 的规则、状态、开始时间索引，以及 `maillog` 的执行记录、状态、发送时间
索引由 `init_db()` 补齐，用于后台日志筛选和分页；不应在生产库中随意删除这些索引。

### 7.7 执行日志显示已重试

系统会对瞬时 SQL 查询失败、SMTP 发送失败和客户端构建异常默认尝试 3 次。

如果错误信息包含“已重试 2 次”，说明本次执行 3 次都失败。应优先检查：

- SQL Server 或 SMTP 是否短时间不可用。
- 网络是否波动。
- 密码或证书配置是否刚发生变更。
- 规则 SQL 是否偶发超时。

### 7.8 重复预警没有再次发送

如果规则启用了“重复预警抑制”，系统会按配置的去重字段和抑制窗口过滤重复行。

检查项：

- 规则表单中的“去重字段”是否与 SQL 返回字段完全一致。
- 抑制窗口是否仍在有效时间内。
- 上一次包含同一去重 Key 的执行是否成功发送。
- 如果全部返回行都被抑制，本次执行会成功，但邮件数量为 0。

### 7.9 动态收件人没有生效

动态收件人只在“每行一封”模式下生效。

检查项：

- 规则发送方式是否为“每行一封”。
- “动态收件人字段”是否与 SQL 返回列名完全一致。
- SQL 预览结果中该字段是否有邮箱值。
- 多个邮箱是否使用英文逗号或分号分隔。
- 如果该字段为空，系统会使用固定收件人作为兜底。

### 7.10 规则被修改后执行异常

如果规则编辑后出现 SQL、收件人、Cron 或模板异常，可以在“规则”页面点击对应规则的“历史”查看编辑前快照。

检查项：

- 最近一次版本历史中的 SQL 是否与当前 SQL 不同。
- Cron 表达式是否被改动。
- 固定收件人、动态收件人字段或抄送字段是否被改动。
- 邮件主题和正文模板是否引用了不存在的 SQL 字段。
- 重复抑制字段或窗口是否被改动。

版本历史用于追溯编辑前配置；当前版本不提供一键恢复，需人工复制历史配置回编辑表单。

### 7.11 导入规则失败

检查项：

- JSON 文件是否由系统“导出规则”功能生成。
- 文件 `version` 是否为 `1`。
- 目标环境是否存在同名数据源。
- 导入规则中的 SQL 是否通过安全校验。
- Cron 表达式是否有效。

导入是全量校验后写入，任意一条规则失败时，不会导入任何规则。

### 7.12 手动执行返回 409

HTTP 409 和“规则正在执行，请稍后重试”表示同一规则已有执行租约，不是 SQL 或 SMTP 故障。先查看执行日志和 Worker 日志；正常执行结束后租约会自动释放。租约自成功获取时开始计时，并在 `RULE_EXECUTION_LEASE_SECONDS` 后过期。进程被强制终止时不会重置计时，其他执行者在该租约剩余时间结束后可接管。不要直接删除未过期租约。

Worker 日志中的“规则正在执行，跳过本次调度”是预期的互斥跳过，不会创建失败执行记录。租约时长必须大于规则的最大预期总运行时间；应同时为 Web 和 Worker 设置相同的 `RULE_EXECUTION_LEASE_SECONDS` 并重启两个服务。

### 7.13 SMTP 证书校验失败

出现证书链、证书过期或主机名不匹配错误时，确认 SMTP 主机名与证书一致并检查系统时间。私有 CA 应加入操作系统信任库，或在 Web 与 Worker 启动环境中设置 `SSL_CERT_FILE=/absolute/path/company-ca.pem`。不得通过关闭证书校验规避错误。

## 8. 安全运维要求

- `.env` 只能授权给运维人员和服务进程读取。
- `SECRET_KEY` 必须和数据库备份一起保管。
- 不要把 `.env`、SQLite 备份或规则导出文件提交到 Git。
- SQL Server 账号必须使用只读权限。
- SMTP 建议使用专用账号或应用密码。
- 生产环境建议使用 HTTPS。
- 限制后台访问来源。
- 管理员离职或职责变更时及时修改密码。

## 9. 例行维护建议

每周：

- 查看失败和部分失败日志。
- 检查磁盘空间。
- 抽查备份文件是否存在。
- 手动执行一条低风险规则验证链路。

每月：

- 做一次备份恢复演练。
- 检查 SQL Server 和 SMTP 账号有效期。
- 检查 ODBC 驱动和系统依赖版本。
- 清理不再使用的规则和无效收件人。

每次重大调整后：

- 导出规则 JSON。
- 备份 SQLite 和 `.env`。
- 记录变更内容和变更人。
