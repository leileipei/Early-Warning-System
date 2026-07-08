# Rule Import Export Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add JSON import and export for alert rules.

**Architecture:** Keep the feature inside the existing FastAPI route and Jinja template structure. Use small helper functions in `app/routes.py` for payload serialization and validation so the HTTP handlers stay readable.

**Tech Stack:** FastAPI, SQLModel, Jinja2, APScheduler `CronTrigger`, pytest, ruff.

## Global Constraints

- Export format version is exactly `1`.
- Export uses `data_source_name`, not `data_source_id`.
- Import is all-or-nothing.
- Import must run local SQL safety validation and Cron validation before inserting any rule.
- CSV log export behavior is unchanged.

---

### Task 1: Add Failing Route Tests

**Files:**
- Modify: `tests/test_routes.py`

**Interfaces:**
- Consumes: existing `_client_with_admin`, `_create_data_source`, `_create_rule`, `_valid_rule_form`.
- Produces: tests for `/rules/export.json` and `/rules/import`.

- [ ] **Step 1: Write failing tests**

Add tests near the rule route tests:

```python
def test_export_rules_json_includes_data_source_name(monkeypatch, session):
    data_source = _create_data_source(session)
    rule = _create_rule(session, data_source, notes="迁移备注")
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        response = client.get("/rules/export.json")

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/json")
        assert 'filename="alert-rules.json"' in response.headers["content-disposition"]
        payload = response.json()
        assert payload["version"] == 1
        assert payload["rules"][0]["name"] == rule.name
        assert payload["rules"][0]["data_source_name"] == data_source.name
        assert payload["rules"][0]["notes"] == "迁移备注"
        assert "data_source_id" not in payload["rules"][0]
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()
```

Add import success, unknown data source, unsafe SQL, and login-required tests using `files={"file": ("rules.json", json.dumps(payload), "application/json")}`.

- [ ] **Step 2: Run tests to verify failure**

Run: `.venv/bin/python -m pytest tests/test_routes.py -k "export_rules_json or import_rules" -v`

Expected: FAIL because the routes do not exist.

### Task 2: Implement JSON Export

**Files:**
- Modify: `app/routes.py`
- Modify: `app/templates/rules.html`

**Interfaces:**
- Produces: `_rule_export_payload(session: Session) -> dict` and `GET /rules/export.json`.

- [ ] **Step 1: Add export helper**

Add a helper that fetches rules ordered by `created_at desc`, resolves data source names, and returns the documented payload.

- [ ] **Step 2: Add route**

Add `@router.get("/rules/export.json")` that requires admin login and returns `JSONResponse` with a `Content-Disposition` attachment header.

- [ ] **Step 3: Add UI link**

Add `导出规则` in `app/templates/rules.html` header linking to `/rules/export.json`.

- [ ] **Step 4: Run export tests**

Run: `.venv/bin/python -m pytest tests/test_routes.py -k "export_rules_json" -v`

Expected: PASS.

### Task 3: Implement JSON Import

**Files:**
- Modify: `app/routes.py`
- Modify: `app/templates/rules.html`

**Interfaces:**
- Produces: `_build_imported_rules(payload: dict, session: Session) -> list[AlertRule]` and `POST /rules/import`.

- [ ] **Step 1: Add import validation helper**

Validate payload version, list shape, required fields, data source name, send mode, Cron expression, SQL safety, query timeout, and max rows.

- [ ] **Step 2: Add import route**

Add `@router.post("/rules/import")` using `UploadFile`. Read the file as UTF-8 JSON. If validation fails, render `rules.html` with `error`. If validation passes, insert every rule and redirect to `/rules?imported=<count>`.

- [ ] **Step 3: Add UI upload form**

Add a small multipart form in `rules.html` with a file input named `file` and submit button `导入规则`.

- [ ] **Step 4: Run import tests**

Run: `.venv/bin/python -m pytest tests/test_routes.py -k "import_rules" -v`

Expected: PASS.

### Task 4: Update Docs And Verify

**Files:**
- Modify: `docs/project-requirements.md`

**Interfaces:**
- Produces: updated requirements that mark rule import/export as supported.

- [ ] **Step 1: Update requirement text**

Add bullets under `4.4 预警规则管理` for JSON export/import and remove `增加规则导入导出` from later optimization suggestions.

- [ ] **Step 2: Run full verification**

Run: `.venv/bin/python -m pytest`

Expected: all tests pass.

Run: `.venv/bin/ruff check .`

Expected: all checks pass.

- [ ] **Step 3: Commit and push**

Commit message: `feat: import and export rules`

