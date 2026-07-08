---
name: check
description: |
  Use when verifying code changes before commit or PR (lint + format + types + tests).
  TRIGGER when: user says "check", "verify", "lint", "validate",
  or when completing implementation tasks that need verification.
  NOT a design/PR review — for that, use the built-in /review or /code-review.
allowed-tools: Read, Grep, Glob, Bash(cd backend && uv run *), Bash(cd frontend && npx biome *), Bash(cd frontend && npx tsc *), Bash(cd frontend && npm test*), Bash(make test-integration*)
---

# Code Verification Protocol

Run ALL applicable checks based on which executable files were changed.

## Documentation / spec-only changes

If the change only touches Markdown documentation or planning/spec files, do not
run backend or frontend code checks.

Examples that are documentation/spec-only:

- `README.md`
- `docs/**/*.md`
- `specs/**/*.md`
- `backend/specs/**/*.md`

For these changes, report that tests were not run because no executable code,
schema, dependency, or runtime configuration changed.

## Backend code changes

Run backend checks when executable backend files changed, including files under
`backend/app/`, `backend/tests/`, `backend/alembic/`, backend scripts,
`backend/pyproject.toml`, `backend/uv.lock`, Dockerfiles, compose files, or
backend-related runtime configuration.

Unit checks (ruff lint + format check + unit pytest):

```bash
cd backend && uv run ruff check app/ && uv run ruff format --check app/ && uv run pytest tests/ -x -q
```

DB integration tests (REQUIRED for any backend change):

```bash
make test-integration
```

`make test-integration` spins up an ephemeral `db-test` Postgres on a dynamic
loopback port, runs `pytest -m integration`, and tears everything down via
`trap`. Do not skip it — the unit suite does not cover DB interactions.

## Frontend code changes

Run frontend checks when executable frontend files changed, including files under
`frontend/src/`, `frontend/e2e/`, frontend scripts, `frontend/package.json`,
`frontend/package-lock.json`, config files, or generated TypeScript API types.

```bash
cd frontend && npx biome check src/ && npx tsc --noEmit && npm test
```

## Rules

- Run backend checks ONLY if backend code files changed
- Run frontend checks ONLY if frontend code files changed
- Do not treat `backend/specs/**/*.md` as a backend code change
- Do not run code checks for docs/spec-only changes
- If both changed, run both in parallel
- For backend, run unit checks first; on green, run `make test-integration`.
  Both must pass — integration is not optional
- Fix any errors found, then re-run checks until clean
- Report results concisely: pass/fail per check category
