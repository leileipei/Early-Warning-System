# Rule Version History Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add read-only version history for alert rule edits.

**Architecture:** Add an `AlertRuleVersion` SQLModel table storing JSON snapshots of pre-edit rule state. Update the rule edit route to create a version only after validation succeeds and before mutating the rule. Add a Jinja history page and rule-list link.

**Tech Stack:** FastAPI, SQLModel, SQLite, Jinja2, pytest, ruff.

## Global Constraints

- 中文界面文案保持现有风格。
- Only successful edits create versions.
- Store the previous rule state, not the new submitted state.
- Do not implement restore in this change.
- Use TDD: add failing tests before production code.

---

### Task 1: Version Storage

**Files:**
- Modify: `app/models.py`
- Modify: `tests/test_db.py`

**Interfaces:**
- Produces: `AlertRuleVersion(rule_id: int, version_number: int, changed_by: str, snapshot_json: str)`

- [ ] **Step 1: Write failing tests**

Add tests asserting `alertruleversion` is created by `init_db` and an `AlertRuleVersion` row can persist for a rule.

- [ ] **Step 2: Run tests to verify red**

Run: `.venv/bin/python -m pytest tests/test_db.py::test_init_db_creates_model_tables tests/test_db.py::test_alert_rule_version_persists_for_rule -q`

Expected: FAIL because `AlertRuleVersion` does not exist.

- [ ] **Step 3: Implement model**

Add `AlertRuleVersion` to `app/models.py` with fields `id`, `rule_id`, `version_number`, `changed_by`, `changed_at`, and `snapshot_json`.

- [ ] **Step 4: Run tests to verify green**

Run: `.venv/bin/python -m pytest tests/test_db.py -q`

Expected: PASS.

### Task 2: Snapshot Creation on Edit

**Files:**
- Modify: `app/routes.py`
- Modify: `tests/test_routes.py`

**Interfaces:**
- Consumes: `AlertRuleVersion`
- Produces: `_rule_snapshot(rule: AlertRule) -> dict`
- Produces: `_create_rule_version(session: Session, rule: AlertRule, admin: AdminUser) -> AlertRuleVersion`

- [ ] **Step 1: Write failing tests**

Add route tests asserting successful update creates a version with old rule values, repeated updates increment version number, and invalid updates create no versions.

- [ ] **Step 2: Run tests to verify red**

Run: `.venv/bin/python -m pytest tests/test_routes.py::test_update_rule_creates_version_snapshot_before_changes -q`

Expected: FAIL because update does not create versions.

- [ ] **Step 3: Implement snapshot helpers and edit hook**

Serialize all editable rule fields into JSON with `ensure_ascii=False`; insert a version after validation and before assignments.

- [ ] **Step 4: Run route tests**

Run: `.venv/bin/python -m pytest tests/test_routes.py -q`

Expected: PASS.

### Task 3: History Page and Documentation

**Files:**
- Create: `app/templates/rule_versions.html`
- Modify: `app/templates/rules.html`
- Modify: `app/routes.py`
- Modify: `docs/project-requirements.md`
- Modify: `docs/operations.md`

- [ ] **Step 1: Write failing tests**

Add tests for `/rules/{rule_id}/versions` requiring login, rendering stored snapshots, and rules list including a history link.

- [ ] **Step 2: Implement page**

Add route, template, and list link.

- [ ] **Step 3: Update docs**

Move rule version history out of the version boundary and document it in rule management and operations.

- [ ] **Step 4: Full verification**

Run: `.venv/bin/python -m pytest`

Expected: all tests pass.

Run: `.venv/bin/ruff check .`

Expected: no lint errors.

- [ ] **Step 5: Commit and push**

Commit message: `feat: add rule version history`
