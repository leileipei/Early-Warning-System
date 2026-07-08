# Dynamic Row Recipients Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Support SQL result fields as per-row recipients and CC recipients for alert emails.

**Architecture:** Store two new string fields on `AlertRule`, migrate legacy SQLite databases, expose the fields in rule forms and JSON import/export, then resolve recipients while building per-row emails. Summary emails continue using fixed recipients only.

**Tech Stack:** FastAPI, SQLModel, SQLite migrations, Jinja templates, pytest, ruff.

## Global Constraints

- 中文界面文案保持现有风格。
- Dynamic recipient fields only apply to `SendMode.PER_ROW`.
- Fixed recipients remain the fallback recipients.
- Use TDD: add failing tests before production code.

---

### Task 1: Rule Storage and Form Persistence

**Files:**
- Modify: `tests/test_db.py`
- Modify: `tests/test_routes.py`
- Modify: `app/models.py`
- Modify: `app/db.py`
- Modify: `app/routes.py`
- Modify: `app/templates/rule_form.html`
- Modify: `app/templates/rules.html`

**Interfaces:**
- Produces: `AlertRule.dynamic_recipient_field: str`
- Produces: `AlertRule.dynamic_cc_field: str`

- [ ] **Step 1: Write failing tests**

Add tests that assert SQLite migration adds both columns, create/update preserve both fields, edit/copy forms prefill them, JSON export/import includes them, and summary mode rejects `dynamic_recipient_field`.

- [ ] **Step 2: Run focused tests to verify red**

Run: `.venv/bin/python -m pytest tests/test_db.py::test_init_db_adds_dynamic_recipient_columns_to_existing_sqlite_rule_table tests/test_routes.py::test_create_rule_persists_dynamic_recipient_fields -q`

Expected: FAIL because `dynamic_recipient_field` and `dynamic_cc_field` are not yet implemented.

- [ ] **Step 3: Implement storage and routing form support**

Add the two `AlertRule` fields, SQLite migration DDL, form plumbing, validation, JSON import/export fields, and rule-list display.

- [ ] **Step 4: Run focused tests to verify green**

Run: `.venv/bin/python -m pytest tests/test_db.py tests/test_routes.py -q`

Expected: PASS.

### Task 2: Executor Dynamic Recipient Resolution

**Files:**
- Modify: `tests/test_executor.py`
- Modify: `app/executor.py`

**Interfaces:**
- Consumes: `AlertRule.dynamic_recipient_field`
- Consumes: `AlertRule.dynamic_cc_field`

- [ ] **Step 1: Write failing tests**

Add tests for per-row dynamic recipients, fixed fallback when row value is empty, summary mode ignoring dynamic fields, and failure when neither dynamic nor fixed recipients are available.

- [ ] **Step 2: Run focused tests to verify red**

Run: `.venv/bin/python -m pytest tests/test_executor.py::test_per_row_mode_uses_dynamic_recipients_from_row -q`

Expected: FAIL because executor currently always uses fixed recipients.

- [ ] **Step 3: Implement executor resolution**

Parse fixed recipients once, read dynamic row fields per message, use dynamic values first and fixed values as fallback, then keep existing failure behavior if recipients are empty.

- [ ] **Step 4: Run focused tests to verify green**

Run: `.venv/bin/python -m pytest tests/test_executor.py -q`

Expected: PASS.

### Task 3: Documentation and Final Verification

**Files:**
- Modify: `docs/project-requirements.md`
- Modify: `docs/operations.md`

- [ ] **Step 1: Update docs**

Document dynamic recipient fields in requirements and troubleshooting.

- [ ] **Step 2: Run full verification**

Run: `.venv/bin/python -m pytest`

Expected: all tests pass.

Run: `.venv/bin/ruff check .`

Expected: no lint errors.

- [ ] **Step 3: Commit and push**

Commit message: `feat: support dynamic row recipients`
