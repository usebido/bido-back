# bido-back

Bido's B2B backend: a procurement product for companies. One Python 3.12
codebase, one container image, two processes:

- **api**: FastAPI, served by uvicorn on port 8080.
- **worker**: consumes SQS jobs (agent runs, email, OCR, timers).

Both talk to one Postgres database (pgvector included) with pooled tenancy and
row-level security, and run on AWS ECS Fargate (ARM).

**Architecture:** the decisions behind this repository live in
[`usebido/engineering` → `b2b/infrastructure.md`](https://github.com/usebido/engineering/blob/main/b2b/infrastructure.md).
Read it before changing anything structural.

## Layout

```text
src/bido_back/
  settings.py        runtime configuration (env vars / .env)
  api/               FastAPI app (`create_app` factory)
  worker/            SQS worker (`python -m bido_back.worker`)
  agents/            the agent runner: definitions, tools, events, LLM seam
  db/                SQLAlchemy models, sessions, tenant-scoped transactions
prompts/             versioned agent prompts (`<name>.v<N>.md`)
migrations/          Alembic migrations (run as the migration role)
docker/postgres/     local/CI database bootstrap (roles, extensions)
scripts/             check_rls.py, export_openapi.py
tests/               api, agents, worker, db (`-m db` needs Postgres)
openapi.json         committed API contract; CI checks it is current
```

## Local setup

Prerequisites: [uv](https://docs.astral.sh/uv/), Docker with Compose v2.

```bash
uv sync                                  # Python 3.12 venv + all dependencies
cp .env.example .env                     # local settings; never commit .env
docker compose up -d db                  # Postgres 16 + pgvector on localhost:54320
uv run alembic upgrade head              # apply migrations as bido_migrator
```

Run the processes:

```bash
uv run uvicorn bido_back.api.app:create_app --factory --reload --port 8080
uv run python -m bido_back.worker
```

Or run everything in containers: `docker compose --profile app up --build`.

Validate before opening a PR:

```bash
uv run ruff check && uv run ruff format --check
uv run pyright
uv run python scripts/check_rls.py       # every tenant-owned table has forced RLS
uv run pytest                            # tests marked `db` need `docker compose up db`
uv run python scripts/export_openapi.py  # then commit openapi.json if it changed
```

To start over with an empty database: `docker compose down -v && docker compose up -d db`.

## Tenancy and row-level security

- Every tenant-owned table has a `tenant_id` column and, in the same migration,
  `ENABLE` + `FORCE ROW LEVEL SECURITY` and a tenant policy. CI runs
  `scripts/check_rls.py` and fails otherwise.
- The app role (`bido_app`) owns no tables, cannot `CREATE`, and has no
  `BYPASSRLS`. Only the migration role (`bido_migrator`) owns the schema, and
  only Alembic uses it.
- The tenant is set once per transaction with
  `set_config('app.tenant_id', ..., true)` through the `db` helper. Never with a
  plain `SET`, which would leak the tenant to the next user of a pooled connection.
- No tenant means no query: tenant-scoped work without a tenant fails closed.
  The api and the worker use the same app role; the worker carries the tenant
  in the job payload.
- Cross-tenant access goes only through a separate, audited staff role
  (not built yet).

## Agent runner seams

Agents are code-owned workflows on the Anthropic SDK plus our own thin runner.
Four seams keep a later move to another framework small:

- **One entry point:** `run(agent, input, context)` returns a typed result and
  a stream of our own events.
- **Typed tools** are plain async functions that take our `ToolContext`
  (tenant, user, roles). The tenant comes from the context, never from model
  arguments. Tools of kind `act` are never executed by the runner; it emits an
  approval request instead.
- **Our own schemas:** messages and UI events are ours. Anthropic SDK types
  never leave `agents/llm.py`.
- **Prompts are versioned files** in `prompts/`, and evals call `run()`.

## Not here yet (deliberately)

- **Deploy workflow:** images are built in CI but not pushed. ECR push and the
  ECS Fargate deploy via GitHub Actions + OIDC come in a later PR.
- **Authentication and authorization** for the api.
- **Product features:** this is the scaffold that features build on.

See [AGENTS.md](AGENTS.md) for the rules every change must follow.
