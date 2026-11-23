# AeroDeck review rules for Bugbot

Review every pull request against the AeroDeck standards in these files:

- [PR review standards](rules/pr-review-standards.mdc): the rubric and its severity scale (BLOCKER, MAJOR, MINOR, NIT).
- [FastAPI architecture](rules/fastapi-architecture.mdc): applies to `apps/control-plane/`.
- [Next.js dashboard](rules/nextjs-dashboard.mdc): applies to `apps/dashboard/`.
- [Terraform on DigitalOcean](rules/terraform-digitalocean.mdc): applies to `packages/iac-engine/` and other Terraform.

## How to apply them

- Start each finding with its severity from the rubric.
- Ignore section 9 of the rubric (the fixed output format and verdict line). Report findings in your normal format.
- Apply a rule only to the language it is written for. The Pydantic and FastAPI rules do not apply to TypeScript, and the Next.js rules do not apply to Python.
- Before reporting a finding, read the whole function or file, not only the changed lines. Do not report a missing lock, cleanup, or error handler that exists outside the hunk.
- Report only what you can point to in the code. Do not report a length or parameter-count limit without counting.

## Project context

- The control plane runs its tests on SQLite locally and on PostgreSQL with asyncpg in CI. Row locks (`with_for_update`) are ignored on SQLite by design.
- Celery tasks that use the database inside `asyncio.run` create a private `NullPool` engine and dispose it in `finally`, because asyncpg connections are bound to one event loop.
- Never commit secrets (DigitalOcean token, database passwords, private keys). Treat any such value in a diff as a BLOCKER.
