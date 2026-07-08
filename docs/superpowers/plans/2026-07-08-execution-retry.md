# Execution Retry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add default retry behavior for transient rule execution failures.

**Architecture:** Implement retry orchestration in `app/execution_service.py` around client construction and `RuleExecutor.execute`. Keep `RuleExecutor` focused on one attempt, and persist only the final `ExecutionResult`.

**Tech Stack:** FastAPI service layer, SQLModel persistence, pytest, ruff.

## Global Constraints

- Default total attempts is `3`.
- Default retry delay is `1` second.
- Configuration errors do not retry.
- Partial mail failures do not retry.
- One requested rule run produces one final execution log.

---

### Task 1: Add Retry Tests

**Files:**
- Modify: `tests/test_executor.py`

**Interfaces:**
- Consumes: `execution_service.execute_rule_by_id`.
- Produces: tests for transient success after retry, exhausted retries, and non-retryable failures.

- [ ] **Step 1: Write failing tests**

Add tests that use fake builders and pass `retry_delay_seconds=0` to avoid sleeping.

- [ ] **Step 2: Verify red**

Run: `.venv/bin/python -m pytest tests/test_executor.py -k "retry or partial_mail_results or data_source_disabled" -v`

Expected: retry tests fail because no retry behavior exists.

### Task 2: Implement Retry Orchestration

**Files:**
- Modify: `app/execution_service.py`

**Interfaces:**
- Produces: `execute_rule_by_id(..., max_attempts=3, retry_delay_seconds=1.0, sleep_fn=time.sleep)`.
- Produces: `_is_retryable_result(result: ExecutionResult) -> bool`.

- [ ] **Step 1: Add one-attempt helper**

Move existing client construction and executor execution into a helper that returns one `ExecutionResult`.

- [ ] **Step 2: Add retry loop**

Loop up to `max_attempts`, sleep between retryable failed attempts, and persist only the final result.

- [ ] **Step 3: Append retry count on exhausted failure**

When retryable failures are exhausted after more than one attempt, append `（已重试 N 次）` to the final error message.

- [ ] **Step 4: Verify green**

Run: `.venv/bin/python -m pytest tests/test_executor.py -k "retry or partial_mail_results or data_source_disabled" -v`

Expected: all selected tests pass.

### Task 3: Update Requirements And Verify

**Files:**
- Modify: `docs/project-requirements.md`

**Interfaces:**
- Produces: requirements text describing default retry behavior.

- [ ] **Step 1: Update docs**

Add retry bullets under rule execution and remove retry from later optimization suggestions.

- [ ] **Step 2: Full verification**

Run: `.venv/bin/python -m pytest`

Expected: all tests pass.

Run: `.venv/bin/ruff check .`

Expected: all checks pass.

- [ ] **Step 3: Commit and push**

Commit message: `feat: retry transient execution failures`

