# Dynamic Row Recipients Design

## Goal

Allow a per-row alert rule to choose recipients from SQL result fields, so one rule can notify each row's owner without duplicating rules.

## Decisions

- Add `dynamic_recipient_field` to alert rules. When present, the executor reads that field from each SQL result row and uses it as the message recipients.
- Add `dynamic_cc_field` to alert rules. When present, the executor reads that field from each SQL result row and uses it as the message CC list.
- Dynamic recipient routing is only valid for `per_row` send mode.
- Fixed recipients remain configured and act as the fallback when a row's dynamic recipient field is missing or empty.
- Fixed CC acts as the fallback when a row's dynamic CC field is missing or empty.
- Recipient parsing reuses the existing comma and semicolon split behavior.
- Summary mode ignores dynamic fields during execution, and the rule form rejects a non-empty dynamic recipient field when summary mode is selected.
- Mail logs continue storing the actual recipient and CC values used for each generated message.

## Non-Goals

- Do not add email address format validation in this change.
- Do not merge fixed recipients with dynamic recipients; use dynamic values first, then fixed fallback.
- Do not support template expressions inside recipient fields.
