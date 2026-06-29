# SQL 预警系统

FastAPI + SQLite + SQL Server + SMTP 的独立预警系统。

## 本地启动

```bash
python3 -m venv .venv
source .venv/bin/activate
cp .env.example .env
pip install -e ".[dev]"
uvicorn app.main:app --reload
```

启动前必须替换 `.env` 中的 `SESSION_SECRET` 和 `SECRET_KEY`，否则应用不会正常启动。
`SECRET_KEY` 必须是有效的 Fernet key，可用以下命令生成：

```bash
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```
