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

Worker 会加载启用的规则并按 cron 调度执行。Web 页面也支持在规则列表中手动执行规则。

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
