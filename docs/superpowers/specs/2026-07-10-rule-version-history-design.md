# Rule Version History Design

## Goal

Add an audit trail for alert rule edits so administrators can see what a rule looked like before each successful modification.

## Scope

- Record a version snapshot before every successful rule edit.
- Store the administrator username, version number, timestamp, and full rule configuration snapshot.
- Show a read-only history page for each rule.
- Add a history link in the rules list.
- Do not create versions on rule creation, copy form display, JSON import, or manual execution.
- Do not implement restore or diff comparison in this change.

## Data Model

Add `AlertRuleVersion` with:

- `id`
- `rule_id`
- `version_number`
- `changed_by`
- `changed_at`
- `snapshot_json`

`snapshot_json` stores the previous rule state as JSON. It includes all editable rule fields plus timestamps and rule ID. The snapshot captures the state before the new edit is applied.

## Behavior

When an admin submits a valid edit to `/rules/{rule_id}`:

1. Validate SQL, Cron, recipients, data source, dynamic recipients, and duplicate suppression exactly as today.
2. If validation fails, do not create a version.
3. If validation succeeds, create an `AlertRuleVersion` from the current rule before mutating it.
4. Assign the next `version_number` for that rule, starting at `1`.
5. Save the version and updated rule in the same transaction.

The history page `/rules/{rule_id}/versions` lists versions newest first and shows key snapshot fields:

- version number
- changed by
- changed at
- rule name
- data source ID
- SQL
- Cron
- recipients and CC
- send mode
- dynamic recipient fields
- duplicate suppression settings
- enabled state

## Error Handling

- Missing rule returns 404.
- Unauthenticated requests follow existing auth behavior.
- Invalid stored JSON is displayed as unavailable instead of breaking the whole page.

## Tests

- Database table creation includes `alertruleversion`.
- Persisting an `AlertRuleVersion` works.
- Updating a rule creates a snapshot of the pre-edit values.
- Multiple updates increment version numbers.
- Invalid edits do not create versions.
- The rules page links to version history.
- The history page requires login and renders stored snapshots.
