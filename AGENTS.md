# AGENTS.md

Rules for everyone who changes this repository: people and coding agents alike.
They apply to the whole repo. The architecture behind them is in
[`usebido/engineering` → `b2b/infrastructure.md`](https://github.com/usebido/engineering/blob/main/b2b/infrastructure.md).

`bido-back` is a production service holding many companies' procurement data in
one database. Tenant isolation, approval of anything that commits, and
no duplicate side effects are the properties every change must preserve.

## Workflow

1. **English everywhere:** code, identifiers, comments, docs, tests, branch
   names, commit messages, and PR titles and descriptions. End-user copy may be
   localized; the engineering around it is English.
2. **One pull request per change, branched from `origin/main`.** Never push to
   `main`. Keep the PR focused: no drive-by refactors, dependency bumps, or
   formatting churn. The PR description states the problem, the behavior,
   the validation run, known limitations, and any rollout or migration steps.
3. Before editing, check `git status` and leave unrelated local work alone.
4. **Never commit secrets** or put them in logs, tests, fixtures, or PR text.
   Local values go in `.env` (git-ignored); document new settings in
   `.env.example` and `src/bido_back/settings.py`. In AWS, secrets come from
   Secrets Manager. The passwords in `docker/postgres/init.sql` and
   `.env.example` are for throwaway local and CI databases only.
5. Do not run anything against a shared or production environment (migrations,
   queues, email, the Anthropic API with real data) without explicit approval.

## Code

- **Strict typing.** pyright runs in `strict` mode on `src`, `tests`, and
  `scripts`. Avoid `Any`; where an SDK forces it, contain it at the boundary and
  convert to our own types immediately. Do not weaken the pyright config or
  scatter `# pyright: ignore` to get green.
- `ruff check` and `ruff format` must pass.
- Add or update tests for every behavior change. Assert on structure, not on
  model prose.

## Database and tenancy

- **Every new tenant-owned table** gets, in the same migration that creates it:
  a non-null `tenant_id`, `ENABLE ROW LEVEL SECURITY`,
  `FORCE ROW LEVEL SECURITY`, and a tenant policy. CI enforces this with
  `scripts/check_rls.py`.
- **Set the tenant only through the `db` helper** (`tenant_session`), which uses
  `set_config('app.tenant_id', ..., true)` per transaction. **Never use plain
  `SET` / `SET SESSION` for the tenant**: it outlives the transaction and leaks
  into the next request on a pooled connection.
- **The app role never owns tables.** `bido_app` owns nothing, cannot `CREATE`,
  and has no `BYPASSRLS`. All DDL goes through Alembic migrations run as
  `bido_migrator`, including tables a library would like to create itself.
- Migrations are append-only. Never edit one that has been merged; add a new one.
- Missing tenant means fail closed. No code path may fall back to "all tenants".

## Agents and side effects

- **No Anthropic SDK types outside `src/bido_back/agents/llm.py`.** Everything
  else uses our neutral message, tool, and event types.
- **`act` tools are never executed by the runner.** A tool that sends, accepts,
  pays, or otherwise commits is declared `kind="act"`; the runner emits an
  approval request and stops. Execution happens elsewhere, after a person
  approves the exact payload.
- Tools take the tenant from `ToolContext`, never from model-supplied arguments.
- **Side effects go through the outbox** with a unique idempotency key, written
  in the same transaction as the state change. Workers and providers redeliver;
  handlers must be idempotent.
- Prompts are versioned files under `prompts/` (`<name>.v<N>.md`). Change a
  prompt by adding a new version, not by editing a released one.

## API contract

- `openapi.json` is committed and must match the code. After changing the API,
  run `uv run python scripts/export_openapi.py` and commit the result.
- **API changes must be backward compatible.** CI runs `oasdiff breaking`
  against the base branch's `openapi.json` and fails on breaking changes. A
  deliberate break needs a new version of the endpoint, not an edit in place.

## Validate before opening a PR

```bash
uv sync --locked
uv run ruff check
uv run ruff format --check
uv run pyright
docker compose up -d db
uv run alembic upgrade head
uv run python scripts/check_rls.py
uv run pytest
uv run python scripts/export_openapi.py && git diff --exit-code openapi.json
docker build -t bido-back:local .
```

If a check cannot run (no Docker, no network), say exactly what was and was not
verified in the PR.

## CI

`.github/workflows/ci.yml` runs lint, tests (with migrations and the RLS check),
the OpenAPI contract check, and an arm64 image build. The `ci-ok` job is the one
required status check. Workflow hardening rules:

- Pin every third-party action by full commit SHA with a `# vX.Y.Z` comment.
- Declare least-privilege `permissions:`; the default is `contents: read`.
- Dependabot keeps actions, Python dependencies, and base images current.
