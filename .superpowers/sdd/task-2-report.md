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

## Review Fixes

Commit: `55c61d2 fix: reconcile existing scheduler jobs`

- Seeded synchronization state from existing managed `rule-{id}` scheduler jobs using their
  normalized Cron trigger signatures. This prevents unchanged jobs from being rescheduled and
  makes pre-existing disabled, invalid, or deleted rule jobs eligible for removal.
- Moved `get_job()` and the unchanged-job check into the per-rule exception boundary. A lookup
  failure is now logged without blocking other rules, and state remains unchanged for retry.
- Added regression coverage for adopting unchanged pre-existing jobs, removing pre-existing stale
  jobs, and isolating/retrying a per-rule `get_job()` failure.

### Review RED Evidence

Command:

```text
/Users/leo.cui/Documents/Early Warning System/.venv/bin/python -m pytest \
  tests/test_scheduler.py::test_rule_synchronizer_adopts_existing_unchanged_job_without_rescheduling \
  tests/test_scheduler.py::test_rule_synchronizer_removes_preexisting_disabled_or_deleted_rules \
  tests/test_scheduler.py::test_rule_synchronizer_isolates_get_job_failure_and_retries_rule -q
```

Exact summary:

```text
FFF                                                                      [100%]
FAILED tests/test_scheduler.py::test_rule_synchronizer_adopts_existing_unchanged_job_without_rescheduling
FAILED tests/test_scheduler.py::test_rule_synchronizer_removes_preexisting_disabled_or_deleted_rules
FAILED tests/test_scheduler.py::test_rule_synchronizer_isolates_get_job_failure_and_retries_rule
3 failed, 16 warnings in 0.06s
```

The failures were the expected `add_job.call_count == 1`, stale managed jobs still present, and an
uncaught `RuntimeError("scheduler unavailable")` from `get_job()`.

### Review GREEN Evidence

Regression tests:

```text
...                                                                      [100%]
3 passed, 20 warnings in 0.04s
```

Covering scheduler suite:

```text
..............                                                           [100%]
14 passed, 62 warnings in 0.07s
```

Final full suite:

```text
........................................................................ [ 32%]
........................................................................ [ 65%]
........................................................................ [ 98%]
....                                                                     [100%]
220 passed, 463 warnings in 4.43s
```

Lint and diff checks:

```text
All checks passed!
```

`git diff --check` also exited successfully with no output.
