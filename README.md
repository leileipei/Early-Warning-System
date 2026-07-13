# SQL 预警系统

FastAPI + SQLite + SQL Server + SMTP 的独立预警系统。管理员可以配置 SQL Server 数据源、SMTP、预警规则和邮件模板，系统按 cron 调度或手动执行只读 SQL，并把预警邮件发送结果写入日志。

## 初始化

```bash
python -m venv .venv
source .venv/bin/activate
cp .env.example .env
pip install -e ".[dev]"
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
