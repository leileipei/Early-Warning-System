# Execution Retry Design

## Goal

Add a default retry policy for transient rule execution failures so temporary SQL Server or SMTP problems do not immediately fail an alert run.

## Scope

- Retry rule execution up to three total attempts by default.
- Keep one final execution log per rule run.
- Avoid retrying deterministic validation and configuration failures.
- Do not add rule form fields or database columns in this version.

## Retry Policy

- Total attempts: `3` by default.
- Retry delay: `1` second between failed retryable attempts.
- Retryable failures:
  - SQL query exceptions surfaced as runtime-style failures.
  - SMTP send failures surfaced as `MailSendError`.
  - unexpected SQL client or SMTP mailer construction exceptions.
- Non-retryable failures:
  - `ConfigurationError`, including missing or disabled data source and missing SMTP configuration.
  - SQL safety validation failures.
  - template rendering failures.
  - recipient validation failures.
  - partial mail failures, to avoid sending duplicate successful emails.

## Logging

- Only one `ExecutionLog` is persisted for each requested rule run.
- If a later attempt succeeds, the final log is successful and does not keep the earlier transient error.
- If every retryable attempt fails, the final log records the last failure and appends `已重试 2 次` to the error message.
- Mail logs are only persisted from the final attempt.

## Interfaces

`execute_rule_by_id` accepts optional keyword-only controls for tests and future callers:

```python
execute_rule_by_id(
    session,
    rule_id,
    trigger_type=TriggerType.MANUAL,
    max_attempts=3,
    retry_delay_seconds=1.0,
    sleep_fn=time.sleep,
)
```

Existing callers can continue using the current positional arguments.

## Testing

- A SQL query failure followed by success should produce one successful execution log after two attempts.
- A repeated SQL query failure should produce one failed execution log with retry count in the error message.
- Configuration errors should not retry.
- Partial mail failure should not retry.

