# SQL 预警系统

FastAPI + SQLite + SQL Server + SMTP 的独立预警系统。

## 本地启动

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
uvicorn app.main:app --reload
```
