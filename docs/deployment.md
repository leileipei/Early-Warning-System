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
SESSION_COOKIE_SECURE=false
LOGIN_MAX_FAILURES=5
LOGIN_FAILURE_WINDOW_SECONDS=900
LOGIN_LOCKOUT_SECONDS=900
```

### SCHEDULER_SYNC_INTERVAL_SECONDS

该值必须大于零，默认值为 10 秒，用于控制运行中的 Worker 多快反映规则变化。

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

## 11. 推荐生产运行方式

建议使用系统服务管理 Web 和 Worker，例如 systemd、Supervisor、Docker 或其他进程守护工具。

最低要求：

- Web 进程异常退出后自动重启。
- Worker 进程异常退出后自动重启。
- Web 和 Worker 使用同一份 `.env`。
- Web 和 Worker 使用同一份 SQLite 数据库或同一个 `DATABASE_URL`。
- 定期备份 SQLite 数据库文件。

## 12. 反向代理建议

如果使用 Nginx、Apache 或其他网关代理 Web 服务：

- 后端 Uvicorn 继续监听内网端口。
- 外部只开放 80/443。
- 生产环境建议使用 HTTPS。
- 代理需要转发 Cookie 和常规请求头。

Nginx 代理目标示例：

```text
http://127.0.0.1:8000
```

## 13. 首次后台配置流程

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

## 14. SQL Server 连接排查

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

## 15. SMTP 排查

检查项：

- SMTP 主机和端口是否正确。
- TLS/SSL 选项是否符合邮件服务器要求。
- 用户名和密码是否正确。
- 发件人邮箱是否允许该账号发送。
- 服务器是否允许应用所在主机连接 SMTP。
- 公司邮件服务是否要求应用专用密码或授权码。

可以在“配置”页面点击“测试发送”验证 SMTP 配置。

## 16. 日志和导出

后台“日志”页面展示：

- 执行日志。
- 邮件日志。

页面支持导出：

- 执行日志 CSV。
- 邮件日志 CSV。

CSV 使用 UTF-8 BOM，便于 Excel 打开中文。

## 17. 备份建议

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

## 18. 安全建议

- SQL Server 使用只读账号。
- 不要给预警系统数据库账号写入、DDL 或执行存储过程权限。
- SMTP 使用专用账号或应用密码。
- `.env` 不提交 Git。
- 生产环境开启 HTTPS。
- 限制后台访问来源。
- 定期更换管理员密码。
- 定期检查执行日志和邮件日志。

## 19. 常用命令

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

## 20. 生产加固配置

### 20.1 规则执行租约

Web 与 Worker 必须连接同一个 SQLite 数据库。默认配置如下：

```dotenv
RULE_EXECUTION_LEASE_SECONDS=7200
```

租约自成功获取时开始计时，并在 `RULE_EXECUTION_LEASE_SECONDS` 后过期。进程异常终止时不会重置计时，其他执行者在该租约剩余时间结束后可接管。租约时长必须大于规则的最大预期总运行时间；应在 Web 与 Worker 的环境中设置相同值并重启两个服务。本版本没有租约心跳。

### 20.2 SMTP 私有 CA

SMTP SSL 和 STARTTLS 始终校验证书链与主机名。企业私有 CA 应安装到操作系统信任库，或在启动 Web 与 Worker 前设置：

```bash
export SSL_CERT_FILE=/absolute/path/company-ca.pem
```

证书中的主机名必须与 SMTP 配置页面填写的主机一致。系统不提供跳过 SMTP 证书校验的开关。

### 20.3 SMTP 启用项升级

升级时，系统会自动整理旧 SQLite 数据库中的 SMTP 配置：若存在多个启用项，只保留 `updated_at` 最新的一项；更新时间相同时保留 ID 最大的一项，其余项会被禁用。升级完成后数据库保证最多只能有一个启用 SMTP 配置。
