# Rule Import Export Design

## Goal

Add JSON export and import for alert rules so administrators can back up rules and migrate them between environments.

## Scope

- Add a rule export endpoint that downloads all alert rules as JSON.
- Add a rule import form on the rules page.
- Add a rule import endpoint that accepts a JSON file upload.
- Validate every imported rule before writing any rows.
- Keep SQL Server data source passwords and SMTP settings outside the export.

## Format

The export payload uses this structure:

```json
{
  "version": 1,
  "exported_at": "2026-07-08T10:00:00",
  "rules": [
    {
      "name": "大额订单",
      "data_source_name": "生产库",
      "sql_text": "select id from orders",
      "cron_expression": "0 9 * * *",
      "recipients": "ops@example.com",
      "cc_recipients": "",
      "subject_template": "大额订单预警",
      "body_template": "{{table}}",
      "send_mode": "summary",
      "query_timeout_seconds": 30,
      "max_rows": 500,
      "enabled": true,
      "notes": ""
    }
  ]
}
```

`data_source_name` is used during import to map each rule to an existing local SQL Server data source. Rule IDs and data source IDs are not exported because they are environment-specific.

## Import Behavior

- The import endpoint requires admin login.
- The uploaded file must be valid JSON with `version` equal to `1` and `rules` as a list.
- Each rule must reference an existing data source by `data_source_name`.
- Each rule must pass the same local SQL safety validation and Cron validation used by the normal rule form.
- `send_mode` must be `summary` or `per_row`.
- Empty `name`, `sql_text`, `cron_expression`, or `recipients` are rejected.
- Numeric fields use the same practical bounds as the rule form: query timeout and max rows must be positive integers.
- Import is all-or-nothing. If any rule is invalid, no rules are inserted.
- Successful import redirects back to `/rules` with a short success message.
- Failed import returns the rules page with a readable error message.

## UI

The rules page gets two controls in the header:

- `导出规则` downloads `/rules/export.json`.
- `导入规则` uploads a `.json` file to `/rules/import`.

The controls follow existing button and inline form styling.

## Testing

- Export returns JSON with version, timestamp, data source names, and rule fields.
- Export requires login.
- Import creates rules from valid JSON.
- Import rejects unknown data sources without inserting any rule.
- Import rejects unsafe SQL without inserting any rule.
- Import requires login.

