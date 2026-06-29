# SQL 预警系统

FastAPI + SQLite + SQL Server + SMTP 的独立预警系统。

## 本地启动

```bash
python -m venv .venv
source .venv/bin/activate
cp .env.example .env
pip install -e ".[dev]"
uvicorn app.main:app --reload
```

启动前请替换 `.env` 中的 `SESSION_SECRET` 和 `SECRET_KEY`。
