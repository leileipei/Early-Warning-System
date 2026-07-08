# Duplicate Alert Suppression Design

## Goal

Add rule-level duplicate alert suppression so the system does not repeatedly email the same business item during a configured time window.

## Scope

- Add suppression settings to each alert rule.
- Support one suppression key field per rule.
- Store sent or suppressed keys in a new suppression table.
- Filter duplicate rows before email rendering.
- Keep CSV logs and existing rule execution behavior unchanged when suppression is disabled.

## Rule Settings

Each rule gets three new fields:

- `suppress_duplicates`: enables duplicate suppression.
- `suppression_key_field`: SQL result field used as the duplicate key, such as `order_id`.
- `suppression_window_hours`: number of hours during which the same key is suppressed.

When suppression is enabled, `suppression_key_field` is required and `suppression_window_hours` must be a positive integer.

## Execution Behavior

- Suppression is evaluated after SQL rows are returned and before emails are rendered.
- Rows missing the configured key field are not suppressed. They continue through normal email rendering.
- If all rows are suppressed, the execution succeeds with the original SQL row count and `email_count = 0`.
- In summary mode, only non-suppressed rows are included in the summary email.
- In per-row mode, only non-suppressed rows produce emails.
- Suppression records are updated only when the final execution status is `success`.
- Failed or partially failed executions do not mark new keys as sent.

## Persistence

Add an `AlertSuppression` table with:

- `rule_id`
- `suppression_key`
- `first_seen_at`
- `last_seen_at`
- `hit_count`

The table is used to decide whether a key is within the suppression window and to record repeated hits.

## Import And Export

Rule JSON import/export includes the three suppression settings so rules can migrate between environments.

## SQLite Migration

Existing SQLite deployments get the new `AlertRule` columns through the existing lightweight migration hook. The new table is created through `SQLModel.metadata.create_all`.

## Testing

- Database schema creates the new suppression table and migrates existing rule tables.
- Rule form can create and edit suppression settings.
- JSON export/import round-trips suppression settings.
- Execution suppresses repeated rows inside the window.
- All-suppressed runs succeed without sending email.
- Suppression is disabled by default and does not change existing behavior.

