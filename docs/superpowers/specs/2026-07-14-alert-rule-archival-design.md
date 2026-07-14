# Alert Rule Archival Design

## Goal

Allow administrators to remove alert rules from active use without losing the execution, mail, version, or duplicate-suppression records that reference them.

## Selected Approach

Use soft deletion. An administrator's delete action archives a rule instead of physically deleting its database row.

## Data Model and Migration

- Add a nullable `archived_at` timestamp to `AlertRule`.
- Extend the existing SQLite schema migration to add `archived_at` to pre-existing `alertrule` tables when absent.
- Existing rows retain `archived_at = NULL` and remain active candidates, subject to their existing `enabled` state.

## Archiving Behavior

- Add a CSRF-protected POST archive route at `/rules/{rule_id}/delete`.
- The route requires an administrator session and only accepts a currently unarchived rule.
- Archiving sets `enabled = False`, records `archived_at = utc_now()`, updates `updated_at`, and commits the rule row.
- The route does not delete `AlertSuppression`, `AlertRuleVersion`, `ExecutionLog`, or `MailLog` rows.
- A missing or already archived rule returns the existing rule-not-found behavior.

## Active Rule Boundaries

- The rules list only displays rules where `archived_at IS NULL`.
- Rule export only includes unarchived rules.
- Application startup and periodic scheduler synchronization only receive unarchived rules, causing any existing scheduled job for an archived rule to be removed.
- Direct edit, copy, version-history, update, manual-run, and archive requests treat archived rules as unavailable.

## UI

- Add a Delete button to each rule row beside the existing rule actions.
- The button submits a POST form containing the existing CSRF input and asks for browser confirmation before archiving.
- Archived rules are absent from the normal rule-management list; no restore workflow is included in this scope.

## Tests

- Existing SQLite databases gain the nullable `archived_at` column.
- The rules page renders the archive action for active rules.
- Archiving removes a rule from the active list while preserving its related audit rows.
- Archived rules are not exported or scheduled.
- Archived rules cannot be edited, copied, viewed in history, run manually, updated, or archived again.
- Archive routes reject unauthenticated access.
