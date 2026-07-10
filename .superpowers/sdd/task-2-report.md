# Task 2 Report: Incremental Scheduler Reconciliation

## Implementation

- Added `_job_id()` and `_add_rule_job()` so initial scheduler construction and incremental
  reconciliation use the same APScheduler job configuration.
- Added `RuleScheduleSynchronizer(scheduler, execute_rule, logger=None)` with
  `sync(rules)`. It schedules valid new rules, leaves unchanged existing jobs alone, replaces
  changed Cron jobs, removes disabled/deleted jobs, restores missing jobs, and isolates failures
  by rule so unsuccessful work remains eligible on the next sync.
- Preserved `build_scheduler(rules, execute_rule)` and its existing behavior.
- APScheduler queues jobs while a scheduler is stopped; in that state `replace_existing=True`
  does not replace an existing pending job. Before adding a changed or recovered job, the
  synchronizer therefore removes any existing job with the same ID.

## RED Evidence

- The specified `.venv/bin/python` command could not start because this worktree has no local
  `.venv/bin/python`.
- Using the repository virtual environment, all five new behavior tests failed during collection
  with the expected error: `ImportError: cannot import name 'RuleScheduleSynchronizer' from
  'app.scheduler'`.

## GREEN Evidence

- Focused scheduler suite:
  `/Users/leo.cui/Documents/Early Warning System/.venv/bin/python -m pytest tests/test_scheduler.py -q`
  -> `11 passed`.
- Full suite:
  `/Users/leo.cui/Documents/Early Warning System/.venv/bin/python -m pytest -q`
  -> `217 passed, 443 warnings`.

## Files Changed

- `app/scheduler.py`
- `tests/test_scheduler.py`
- `.superpowers/sdd/task-2-report.md`

## Self-Review

- Verified the exact public synchronizer interface and `build_scheduler` compatibility.
- Verified valid-rule filtering remains unchanged and is shared by both construction and sync.
- Verified failures during removal/addition are logged per rule and do not update state, allowing
  retries on a later sync.
- Ran `git diff --check`; no whitespace errors found.

## Concerns

- The full suite has pre-existing deprecation warnings from `datetime.utcnow()`, `crypt`, and
  Starlette's TestClient/httpx combination; this task does not alter those paths.
- The worktree lacks its own virtual environment, so test evidence used the repository root's
  `.venv`.
