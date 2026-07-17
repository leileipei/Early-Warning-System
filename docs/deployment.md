# SQL 预警系统部署运维指南

## 1. 部署目标

本文档用于将 SQL 预警系统部署到服务器，并说明日常启动、配置、备份和排障方法。

系统包含两个运行入口：

- Web 服务：提供后台页面、配置、手动执行、日志查看和导出。
- Worker 服务：加载启用的预警规则，并按 Cron 表达式定时执行。

生产环境建议 Web 和 Worker 都保持运行。

## 2. 环境要求

- Python 3.11、3.12 或 3.13。
- 可访问 SQL Server 的网络环境。
- 可访问公司 SMTP 邮件服务器的网络环境。
- Microsoft ODBC Driver for SQL Server。
- 一个 SQL Server 只读账号。
- 一个可发送邮件的 SMTP 账号。

## 3. 获取代码

```bash
git clone https://github.com/leileipei/Early-Warning-System.git
cd Early-Warning-System
```

如果已经在服务器上部署过，可以进入目录后更新代码：

```bash
git pull
```

## 4. 创建 Python 环境

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.lock
pip install --no-deps -e .
```

生产环境不需要测试工具时，安装生产锁文件与当前项目：

```bash
pip install -r requirements.lock
pip install --no-deps .
```

锁文件由 Python 3.11 环境生成，适用于受支持的 Python 3.11-3.13。升级依赖时使用开发环境更新两份锁文件，并一同提交：

```bash
.venv/bin/python -m pip install -U pip-tools
.venv/bin/pip-compile --strip-extras --resolver=backtracking --output-file=requirements.lock pyproject.toml
.venv/bin/pip-compile --extra=dev --strip-extras --resolver=backtracking --output-file=requirements-dev.lock pyproject.toml
```

## 5. 配置环境变量

复制示例配置：

```bash
cp .env.example .env
```

`.env` 支持以下配置：

```dotenv
APP_NAME=SQL 预警系统
DATABASE_URL=sqlite:///./early_warning.sqlite3
SESSION_SECRET=REPLACE_ME_WITH_RANDOM_SESSION_SECRET
SECRET_KEY=REPLACE_ME_WITH_32_BYTE_URL_SAFE_FERNET_KEY
SCHEDULER_SYNC_INTERVAL_SECONDS=10
SCHEDULER_MISFIRE_GRACE_SECONDS=300
WORKER_HEARTBEAT_TIMEOUT_SECONDS=60
LOG_RETENTION_DAYS=180
LOG_CLEANUP_INTERVAL_SECONDS=86400
SESSION_MAX_AGE_SECONDS=28800
SESSION_IDLE_TIMEOUT_SECONDS=1800
SESSION_COOKIE_SECURE=false
LOGIN_MAX_FAILURES=5
LOGIN_FAILURE_WINDOW_SECONDS=900
LOGIN_LOCKOUT_SECONDS=900
```

### SCHEDULER_SYNC_INTERVAL_SECONDS

该值必须大于零，默认值为 10 秒，用于控制运行中的 Worker 多快反映规则变化。

### 调度、心跳、日志与 Session 参数

以下参数都必须大于 0。修改后需要重启表中列出的进程，使新配置生效。

| 参数 | 默认值与单位 | 用途 | 修改后重启 |
| --- | --- | --- | --- |
| `SCHEDULER_MISFIRE_GRACE_SECONDS` | 300 秒 | 允许延迟调度任务补执行的时间窗口 | Worker |
| `WORKER_HEARTBEAT_TIMEOUT_SECONDS` | 60 秒 | Worker 心跳超过该时间后，Web 就绪检查返回不可用 | Web |
| `LOG_RETENTION_DAYS` | 180 天 | 执行日志和邮件日志的保留天数 | Worker |
| `LOG_CLEANUP_INTERVAL_SECONDS` | 86400 秒 | Worker 执行日志清理的间隔 | Worker |
| `SESSION_MAX_AGE_SECONDS` | 28800 秒 | 登录 Session 的绝对最长有效期 | Web |
| `SESSION_IDLE_TIMEOUT_SECONDS` | 1800 秒 | 登录 Session 的最大空闲时间 | Web |

`SCHEDULER_SYNC_INTERVAL_SECONDS` 修改后也需要重启 Worker。若一次修改同时涉及
Web 与 Worker 使用的参数，应同时重启两个进程。

### SESSION_SECRET

`SESSION_SECRET` 用于保护登录会话。生产环境必须替换成随机字符串。

示例：

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

### SECRET_KEY

`SECRET_KEY` 用于加密 SQL Server 密码和 SMTP 密码，必须是有效的 Fernet key。

生成命令：

```bash
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

注意：

- 不要使用 `.env.example` 中的 `REPLACE_ME` 占位值。
- `SECRET_KEY` 一旦用于加密已有密码，不要随意更换；更换后旧密码将无法解密。
- `.env` 不应提交到 Git。

### Web 安全配置

当前直接通过 HTTP 访问时使用 `SESSION_COOKIE_SECURE=false`，否则浏览器不会在 HTTP 请求中发送登录 Cookie。完成 HTTPS 反向代理后必须改为 `SESSION_COOKIE_SECURE=true`。Session Cookie 始终使用 `HttpOnly` 和 `SameSite=Lax`。

登录限流默认按“客户端 IP + 去除首尾空白并忽略大小写的用户名”计数：15 分钟内失败 5 次后锁定 15 分钟。状态仅保存在单个 Web 进程内，重启 Web 会清空；不要配置多个 Web Worker，否则各进程不会共享计数。

应用只使用 Uvicorn 校验后的 `request.client.host`，不会直接信任 `X-Forwarded-For`。反向代理与 Uvicorn 在同一服务器时，按实际代理地址启动，例如：

```bash
uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000 --proxy-headers --forwarded-allow-ips=127.0.0.1
```

`--forwarded-allow-ips` 必须只列出真实反向代理地址，不要设置为 `*`。

## 6. 初始化数据库

系统默认使用 SQLite：

```bash
python3 -c "from app.db import init_db; init_db()"
```

默认数据库文件为：

```text
early_warning.sqlite3
```

如果修改了 `DATABASE_URL`，请确认 Web 和 Worker 使用同一份配置。

`init_db()` 是可重复执行的 SQLite 架构升级入口。升级时必须在 Web 和 Worker
均停止后由单一进程调用，不要依赖两个服务启动时并发完成升级。它会保留已有数据，
补充管理员的 `session_version`、Worker 心跳表和执行/邮件日志索引；对历史 SMTP
数据会只保留更新时间最新的一条启用配置，并创建约束，保证最多一条 SMTP 配置为启用。

升级完成后在服务仍停止时检查数据库：

```bash
sqlite3 early_warning.sqlite3 "PRAGMA integrity_check; PRAGMA foreign_key_check;"
```

第一行必须是 `ok`，且 `foreign_key_check` 不应有任何后续输出。若任一检查失败，不要
启动服务，按“升级失败回滚”恢复备份。

## 7. 创建管理员

创建或更新管理员账号：

```bash
python3 -m app.admin_cli admin
```

也可以直接传入密码：

```bash
python3 -m app.admin_cli admin --password 'your-password'
```

建议生产环境使用交互输入，避免密码留在 shell 历史中。

## 8. 安装 SQL Server ODBC 驱动

生产环境需要安装 Microsoft ODBC Driver for SQL Server。

系统默认驱动名称为：

```text
ODBC Driver 18 for SQL Server
```

如果服务器上安装的是其他版本，可以在后台 SQL Server 数据源配置中修改 `ODBC 驱动` 字段，例如：

```text
ODBC Driver 17 for SQL Server
```

排查命令：

```bash
python3 -c "import pyodbc; print(pyodbc.drivers())"
```

如果 `pyodbc` 导入失败或找不到 ODBC 动态库，需要先安装系统级 ODBC 依赖和 Microsoft SQL Server 驱动。

## 9. 启动 Web 服务

开发或本地测试：

```bash
uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000 --reload
```

服务器内网访问：

```bash
uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000
```

访问后台：

```text
http://服务器IP:8000/login
```

健康检查：

```text
http://服务器IP:8000/health
```

如果只能通过 `127.0.0.1` 访问，通常是因为启动时绑定了 `127.0.0.1`。需要改为：

```bash
--host 0.0.0.0
```

同时确认服务器防火墙、安全组或反向代理允许访问对应端口。

## 10. 启动 Worker

Worker 用于执行启用规则的 Cron 调度：

```bash
python3 -m app.worker
```

注意：

- Web 服务负责配置和手动操作。
- Worker 负责定时执行。
- 如果 Worker 没有运行，Cron 规则不会自动触发。
- 手动执行规则不依赖 Worker。
- 新建规则、修改 Cron、启用或停用规则后无需重启 Worker。
- Worker 启动和每次规则同步都会写入同一个 `workerheartbeat` 记录；Web 的
  `/health/ready` 据此确认 Worker 是否存在、未超时且最近同步成功。
- SQLite 部署只支持一个 Worker。不要同时启动两个 Worker 或将 SQLite 放在多个主机
  共享使用；如需多 Worker/多实例，需要先迁移至具备并发协调能力的存储方案。

## 11. 健康与就绪检查

`/health` 仅表示 Web 进程可以响应，预期始终返回 HTTP 200：

```bash
curl -fsS http://127.0.0.1:8000/health
```

`/health/ready` 同时检查数据库架构和 Worker 心跳。启动 Worker 并等待至少一个同步
周期后才应返回 HTTP 200；Worker 未启动、心跳过期或最近同步失败时预期返回 HTTP 503，
此时不得把服务接入流量：

```bash
curl -sS -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/health/ready
```

## 12. 停机升级与回滚

以下以 systemd 服务名 `early-warning-web` 和 `early-warning-worker` 为例；使用
Supervisor、Docker 或其他守护器时，以等价的停止和启动命令替换。升级期间应临时
禁止守护器自动拉起服务。

1. 停止 Worker，再停止 Web：

   ```bash
   systemctl stop early-warning-worker
   systemctl stop early-warning-web
   ```

2. 备份 SQLite 和 `.env`，并保留本次规则 JSON 导出；数据库与 `.env` 中的
   `SECRET_KEY` 必须成对保管。
3. 更新已审核的代码版本，在锁定的 Python 3.11-3.13 环境中安装依赖：

   ```bash
   .venv/bin/pip install -r requirements.lock
   .venv/bin/pip install --no-deps .
   ```

4. 仅调用一次迁移入口并完成 SQLite 检查：

   ```bash
   .venv/bin/python -c "from app.db import init_db; init_db()"
   sqlite3 early_warning.sqlite3 "PRAGMA integrity_check; PRAGMA foreign_key_check;"
   ```

5. 启动 Web，确认 `/health` 为 200；随后启动唯一的 Worker，等待
   `/health/ready` 为 200 后再恢复正常流量。

升级失败、数据库检查异常或就绪检查在预定窗口内无法恢复时：保持两个服务停止，检出
上一已知可用代码版本，恢复同一时间点的 SQLite 与 `.env` 备份，使用该版本的
`requirements.lock` 安装依赖；随后依次启动 Web、确认 `/health`，启动唯一 Worker，
并确认 `/health/ready` 为 200。不得把新库与旧 `.env` 或旧库与新 `SECRET_KEY` 混用。

## 13. 推荐生产运行方式

建议使用系统服务管理 Web 和 Worker，例如 systemd、Supervisor、Docker 或其他进程守护工具。

最低要求：

- Web 进程异常退出后自动重启。
- Worker 进程异常退出后自动重启。
- Web 和 Worker 使用同一份 `.env`。
- Web 和 Worker 使用同一份 SQLite 数据库或同一个 `DATABASE_URL`。
- 定期备份 SQLite 数据库文件。

## 14. 反向代理建议

如果使用 Nginx、Apache 或其他网关代理 Web 服务：

- 后端 Uvicorn 继续监听内网端口。
- 外部只开放 80/443。
- 生产环境建议使用 HTTPS。
- 代理需要转发 Cookie 和常规请求头。

Nginx 代理目标示例：

```text
http://127.0.0.1:8000
```

## 15. 首次后台配置流程

1. 登录后台。
2. 进入“配置”页面。
3. 新增 SQL Server 数据源。
4. 点击“测试连接”，确认数据库连接成功。
5. 新增 SMTP 配置。
6. 点击“测试发送”，确认邮件可以发出。
7. 进入“规则”页面。
8. 新建预警规则。
9. 填写 SQL、Cron、收件人和邮件模板。
10. 点击“检测 SQL”，确认 SQL Server 语法可解析。
11. 点击“预览结果”，确认 SQL 返回字段符合邮件模板。
12. 保存规则。
13. 手动执行规则，确认邮件和日志结果。
14. 启动 Worker，让规则按 Cron 自动执行。

## 16. SQL Server 连接排查

### 连接失败

检查项：

- 主机、端口、数据库名是否正确。
- SQL Server 是否允许远程连接。
- 服务器防火墙是否开放 SQL Server 端口。
- 用户名和密码是否正确。
- 数据库账号是否有连接权限。
- ODBC 驱动名称是否与服务器实际安装一致。
- `Encrypt` 和 `TrustServerCertificate` 是否符合 SQL Server 配置。

### 实例名或特殊地址

如果 SQL Server 使用实例名或需要特殊 `SERVER` 写法，可以使用“服务器地址覆盖值”。

示例：

```text
SERVER\INSTANCE
```

或：

```text
192.168.1.10,1433
```

### SQL 可以在数据库工具中执行，但系统检测失败

检查项：

- 系统配置的数据源是否指向同一个数据库。
- SQL 中是否包含写入、DDL、多语句或被禁止关键字。
- SQL Server 账号是否只有只读权限。
- 查询超时时间是否太短。

## 17. SMTP 排查

检查项：

- SMTP 主机和端口是否正确。
- TLS/SSL 选项是否符合邮件服务器要求。
- 用户名和密码是否正确。
- 发件人邮箱是否允许该账号发送。
- 服务器是否允许应用所在主机连接 SMTP。
- 公司邮件服务是否要求应用专用密码或授权码。

可以在“配置”页面点击“测试发送”验证 SMTP 配置。

## 18. 日志和导出

后台“日志”页面展示：

- 执行日志。
- 邮件日志。

页面支持导出：

- 执行日志 CSV。
- 邮件日志 CSV。

CSV 使用 UTF-8 BOM，便于 Excel 打开中文。

## 19. 备份建议

如果使用默认 SQLite，至少备份以下文件：

```text
early_warning.sqlite3
.env
```

建议：

- 定期备份数据库文件。
- 备份前确认没有正在进行的大量写入。
- 备份文件妥善保存，因为其中包含加密后的连接密码。
- `.env` 中的 `SECRET_KEY` 必须和数据库配套保存，否则加密密码无法解密。

## 20. 安全建议

- SQL Server 使用只读账号。
- 不要给预警系统数据库账号写入、DDL 或执行存储过程权限。
- SMTP 使用专用账号或应用密码。
- `.env` 不提交 Git。
- 生产环境开启 HTTPS。
- 限制后台访问来源。
- 定期更换管理员密码。
- 定期检查执行日志和邮件日志。

## 21. 常用命令

运行测试：

```bash
pytest
ruff check .
```

查看当前 Git 状态：

```bash
git status
```

启动 Web：

```bash
uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000
```

启动 Worker：

```bash
python3 -m app.worker
```

创建管理员：

```bash
python3 -m app.admin_cli admin
```

## 22. 生产加固配置

### 22.1 规则执行租约

Web 与 Worker 必须连接同一个 SQLite 数据库。默认配置如下：

```dotenv
RULE_EXECUTION_LEASE_SECONDS=7200
```

租约自成功获取时开始计时，并在 `RULE_EXECUTION_LEASE_SECONDS` 后过期。进程异常终止时不会重置计时，其他执行者在该租约剩余时间结束后可接管。租约时长必须大于规则的最大预期总运行时间；应在 Web 与 Worker 的环境中设置相同值并重启两个服务。本版本没有租约心跳。

### 22.2 SMTP 私有 CA

SMTP SSL 和 STARTTLS 始终校验证书链与主机名。企业私有 CA 应安装到操作系统信任库，或在启动 Web 与 Worker 前设置：

```bash
export SSL_CERT_FILE=/absolute/path/company-ca.pem
```

证书中的主机名必须与 SMTP 配置页面填写的主机一致。系统不提供跳过 SMTP 证书校验的开关。

### 22.3 SMTP 启用项升级

升级时，系统会自动整理旧 SQLite 数据库中的 SMTP 配置：若存在多个启用项，只保留 `updated_at` 最新的一项；更新时间相同时保留 ID 最大的一项，其余项会被禁用。升级完成后数据库保证最多只能有一个启用 SMTP 配置。
