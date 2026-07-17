# SQL 预警系统

FastAPI + SQLite + SQL Server + SMTP 的独立预警系统。管理员可以配置 SQL Server 数据源、SMTP、预警规则和邮件模板，系统按 cron 调度或手动执行只读 SQL，并把预警邮件发送结果写入日志。

## 初始化

项目支持 Python 3.11、3.12 和 3.13。开发环境使用开发锁文件安装，确保测试与 CI 使用相同的依赖版本：

```bash
python3 -m venv .venv
source .venv/bin/activate
cp .env.example .env
pip install -r requirements-dev.lock
pip install --no-deps -e .
```

继续之前，必须先替换 `.env` 中的 `SESSION_SECRET` 和 `SECRET_KEY`，否则应用不会正常启动。`SECRET_KEY` 必须是有效的 Fernet key，可用以下命令生成：

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

替换完成后初始化数据库并创建首个管理员：

```bash
python -c "from app.db import init_db; init_db()"
python -m app.admin_cli admin
```

## 启动 Web

```bash
uvicorn app.main:app --reload
```

打开 `http://127.0.0.1:8000/login` 进入后台。

## 启动 Worker

```bash
python -m app.worker
```

Worker 会按 Cron 调度启用规则，并默认每 10 秒从系统数据库同步规则变化。后台新增规则、修改 Cron、启用或停用规则后无需重启 Worker。可通过 `.env` 中的 `SCHEDULER_SYNC_INTERVAL_SECONDS` 调整同步间隔。

## 测试

```bash
pytest
ruff check .
```

## 依赖锁定

生产环境使用 `requirements.lock`，开发与 CI 使用 `requirements-dev.lock`。安装锁定依赖后安装当前项目本身，避免重新解析依赖：

```bash
pip install -r requirements.lock
pip install --no-deps .
```

更新依赖版本时，在 Python 3.11 环境执行以下命令，然后提交两份锁文件：

```bash
.venv/bin/python -m pip install -U pip-tools
.venv/bin/pip-compile --strip-extras --resolver=backtracking --output-file=requirements.lock pyproject.toml
.venv/bin/pip-compile --extra=dev --strip-extras --resolver=backtracking --output-file=requirements-dev.lock pyproject.toml
```

## SQL Server 驱动

生产环境需要安装 Microsoft ODBC Driver 18 for SQL Server，并为预警系统配置只读 SQL Server 账号。规则 SQL 只允许 `SELECT` 或 `WITH` 查询。

## 部署运维

生产部署、外部 IP 访问、Web/Worker 进程、ODBC 驱动、SMTP 排查和备份建议见：

[docs/deployment.md](docs/deployment.md)

日常巡检、备份恢复、升级发布和故障处理见：

[docs/operations.md](docs/operations.md)

### Web 安全

当前使用 HTTP 时保持 `SESSION_COOKIE_SECURE=false`；完成 HTTPS 反向代理后必须改为 `true`。后台所有修改类表单使用 Session CSRF Token。登录默认在 15 分钟内失败 5 次后锁定 15 分钟。

相关环境变量：`SESSION_COOKIE_SECURE`、`LOGIN_MAX_FAILURES`、`LOGIN_FAILURE_WINDOW_SECONDS`、`LOGIN_LOCKOUT_SECONDS`。

## 生产加固说明

- 邮件模板运行在不可变 Jinja 沙箱中，禁止访问 Python 内部对象；拦截后规则失败且不会发送邮件。
- SMTP SSL 和 STARTTLS 校验服务器证书及主机名。私有 CA 使用系统信任库或启动前设置 `SSL_CERT_FILE`。
- 同一规则通过 SQLite 租约避免 Web、Worker 和多进程并发执行。租约默认 `7200` 秒，可用 `RULE_EXECUTION_LEASE_SECONDS` 调整。
- 仪表盘展示数据库中的启用规则、上海自然日执行次数、近 24 小时失败、最近执行及邮件结果。
