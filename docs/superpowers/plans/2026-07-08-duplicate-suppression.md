# Duplicate Alert Suppression Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add rule-level duplicate alert suppression using one SQL result field as the suppression key.

**Architecture:** Store suppression settings on `AlertRule` and sent/suppressed keys in a new `AlertSuppression` table. Filter SQL result rows inside the execution service by passing a row filter into `RuleExecutor`, then persist suppression records only after a successful final execution.

**Tech Stack:** FastAPI, SQLModel, SQLite migration hook, Jinja2 templates, pytest, ruff.

## Global Constraints

- Suppression is disabled by default.
- Suppression uses one configured result field per rule.
- Suppression records update only after successful executions.
- Failed and partially failed executions do not mark new keys as sent.
- Existing callers and existing rule JSON files remain compatible.

---

### Task 1: Schema And Migration Tests

**Files:**
- Modify: `tests/test_db.py`
- Modify: `app/models.py`
- Modify: `app/db.py`

**Interfaces:**
- Produces: `AlertRule.suppress_duplicates`, `AlertRule.suppression_key_field`, `AlertRule.suppression_window_hours`.
- Produces: `AlertSuppression`.

- [ ] Write failing schema tests for new table and migrated columns.
- [ ] Run `pytest tests/test_db.py -k "suppression or init_db" -v` and confirm failure.
- [ ] Add models and SQLite migration columns.
- [ ] Re-run selected tests and confirm pass.

### Task 2: Rule Form And JSON Tests

**Files:**
- Modify: `tests/test_routes.py`
- Modify: `app/routes.py`
- Modify: `app/templates/rule_form.html`
- Modify: `app/templates/rules.html`

**Interfaces:**
- Consumes: new `AlertRule` suppression fields.
- Produces: create/edit/copy/import/export support for suppression settings.

- [ ] Write failing route tests for creating, editing, rendering, exporting, and importing suppression settings.
- [ ] Run selected route tests and confirm failure.
- [ ] Add form fields and route parsing/validation.
- [ ] Add import/export fields with backwards-compatible defaults.
- [ ] Re-run selected route tests and confirm pass.

### Task 3: Execution Suppression Tests

**Files:**
- Modify: `tests/test_executor.py`
- Modify: `app/executor.py`
- Modify: `app/execution_service.py`

**Interfaces:**
- Produces: optional `row_filter` parameter on `RuleExecutor.execute`.
- Produces: execution-service suppression filtering and record persistence.

- [ ] Write failing execution tests for first send, repeated suppression, all-suppressed success, and no records on partial failure.
- [ ] Run selected executor tests and confirm failure.
- [ ] Add row filtering support in `RuleExecutor`.
- [ ] Add suppression read/filter/persist helpers in `execution_service.py`.
- [ ] Re-run selected executor tests and confirm pass.

### Task 4: Documentation And Verification

**Files:**
- Modify: `docs/project-requirements.md`
- Modify: `docs/operations.md`

**Interfaces:**
- Produces: documented duplicate suppression behavior.

- [ ] Update requirements and operations notes.
- [ ] Run `.venv/bin/python -m pytest`.
- [ ] Run `.venv/bin/ruff check .`.
- [ ] Commit with message `feat: suppress duplicate alerts`.

