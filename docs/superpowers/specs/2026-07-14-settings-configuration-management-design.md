# Settings Configuration Management Design

## Goal

Add SQL Server data-source deletion and SMTP configuration editing and deletion to the settings page. Keep the existing administrator-only access controls and use server-side validation for all destructive operations.

## Selected Approach

Extend the existing settings-page routes and HTML forms. Use browser confirmation prompts for destructive actions and enforce the same deletion rules in the server routes.

## SQL Server Data Sources

- Add a delete action to each data-source row.
- Before deletion, query alert rules that reference the data source.
- When one or more rules reference the data source, reject the deletion and display the rule names in the settings-page feedback message.
- When no rule references the data source, delete it and redirect to the settings page with a success message.
- Do not alter alert rules or historical execution data during a rejected or successful data-source deletion.

## SMTP Configurations

- Add an edit action that opens a dedicated SMTP configuration form.
- The edit form pre-populates all non-secret fields. An empty password preserves the existing encrypted password; a supplied password replaces it after encryption.
- Add a delete action to each SMTP configuration row.
- SMTP deletion is allowed after browser confirmation. Existing alert rules remain unchanged; later executions continue to report the existing missing-SMTP configuration error until a configuration is created.

## UI and Authorization

- Retain the existing test and edit actions for SQL Server data sources, and add delete beside them.
- Add edit and delete beside the existing SMTP test action.
- Use POST routes for all create, update, test, and delete actions.
- All routes require an administrator session and retain existing CSRF protection.

## Error Handling

- Missing SQL Server or SMTP configuration IDs return the existing not-found behavior.
- Deletion attempts blocked by data-source references return to settings with a specific, actionable message.
- Form validation errors re-render the relevant edit form without losing non-secret inputs.

## Tests

- Settings page includes the new edit and delete actions.
- Administrators can delete an unreferenced SQL Server data source.
- Referenced SQL Server data sources cannot be deleted and report the referencing rule names.
- Administrators can edit SMTP settings, preserving a blank password and encrypting a replacement password.
- Administrators can delete SMTP settings.
- New update and delete routes reject unauthenticated requests.
