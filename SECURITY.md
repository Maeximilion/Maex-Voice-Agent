# Security

## Reporting Issues

Please **do not** create security issues as public issues. Instead report via "Report a vulnerability" in the Security tab of this repository (GitHub Private Vulnerability Reporting).

Helpful in your report: affected component, steps to reproduce, possible impact. You'll receive a response within seven days.

## Scope

The agent answers phone calls and processes names, phone numbers, addresses, and order data. Especially relevant:

- The agent API at `/v1/tools/*`, accessible only with Bearer token (`AGENT_API_TOKEN`)
- Team UI and its sessions
- Event queue for automation and its targets
- Database access, backups, and their storage locations

## Operating Principles

- Credentials exclusively via environment variables, never in repository. `.env` is in `.gitignore`.
- Every write transaction carries a `call_id` and lands in `audit_log`.
- Personal data carries a deletion deadline; a daily job applies it.
- All processing runs on EU servers, including transcription and analysis.
- Real call recordings are processed only after the legal checklist in `docs/09_OPERATIONS_LEGAL.md` is complete.
- Before any production migration, a backup is taken.

## Supported Version

The project is in development. Security reports refer to the current state of `main`.
