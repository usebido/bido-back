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
  agents/            the agents adapter: Agno behind our own types and tools
  copilot/           the procurement copilot: chat agent, its tools, SSE stream
  purchasing/        data imports (BOM, stock, purchase history, plan) and the purchase analysis
  db/                SQLAlchemy models, sessions, tenant-scoped transactions
  observability.py   OpenTelemetry tracing and Sentry; off when unset
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

Demo data for the purchase analysis: `uv run python scripts/seed_motor_demo.py`
creates the "Motori Demo S.p.A." tenant (an EV motor's BOM, stock, 12 months of
purchase history, a production plan) through the same spreadsheet importer as
`POST /v1/data/imports`; the workbooks are in `scripts/demo_data/`. Then, as
`X-Dev-User: user_motori_buyer`, `POST /v1/analyses {"product": "motor"}` or ask
the copilot "faz uma análise de motor".

## Sign-in: Clerk organizations are companies

- The frontend signs users in with Clerk and every session has an active
  organization. The api verifies the session token and treats that
  organization as the company: `tenants.clerk_org_id` links the two.
- First request of a member of a **linked** organization: their membership is
  created (Clerk admins become owners, everyone else members; a role is never
  lowered). An **unlinked** organization gets a new company only with
  `AUTH_AUTO_PROVISION=true`; otherwise it is refused (403 "organization not
  enabled"). `CLERK_ALLOWED_ORG_IDS` restricts both. A token without an
  organization is refused (403 "select an organization").
- Link an organization ahead of time (local or dev):
  `uv run python scripts/link_demo_org.py --clerk-org-id org_... --with-motor-demo`
  (or `--tenant-id <uuid>`, or `--name "<company>"` for a new empty one).
- Clerk dashboard: organizations enabled and required for sessions; sign-up
  restricted (invitations or allowlist) outside dev; optionally a session token
  claim `"email": "{{user.primary_email_address}}"`, or set `CLERK_SECRET_KEY`
  so provisioning can read the e-mail and the organization's name.
- Locally, `AUTH_DEV_MODE=true` still accepts `X-Dev-User: <clerk_user_id>` for
  the seeded users, with `X-Tenant-Id` to pick among several memberships.

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

## Agents

[Agno](https://github.com/agno-agi/agno) does the agent part: model calls, the
tool loop, sessions and memory. Bido keeps everything that commits: auth,
tenants, approvals, the outbox, and the business endpoints.

- **Models:** OpenAI, through the Responses API (`OPENAI_API_KEY`).
  `gpt-6-luna` by default, `gpt-6.1-sol` for quote extraction; each agent's
  model is set in `agents/catalog/definitions.py`. Requests are stateless
  (`store=False`): OpenAI keeps no conversation state between turns.
- **Pinned exactly** (`agno==<version>` in `pyproject.toml`). Upgrades are
  deliberate, in their own PR, after reading the changelog.
- **Contained:** Agno and OpenAI types live only in
  `src/bido_back/agents/agno_adapter.py`. Everything else goes through `run()`
  and sees our own tool, run and event types, so replacing Agno stays a change
  to one module.
- **Only our tools:** typed async functions that take the tenant from our
  context, never from model arguments. No shell, Python, file-system or MCP
  toolkits.
- **Telemetry off:** `AGNO_TELEMETRY=false` (image, compose, CI) and
  `telemetry=False` in code.
- **Its own storage:** Agno keeps its sessions, memory and runs in schema
  `agno` through role `bido_agno` (`AGNO_DATABASE_URL`). That role has no
  access to the business tables in `public`. Agno's tables have no RLS, so
  every Agno user and session id is namespaced by tenant.
- **Approvals are ours:** a tool that sends, accepts or pays is an `act` tool,
  registered with Agno's `external_execution` so Agno can never run it. The run
  stops with a proposal, stored as a `pending` row in our `approvals` table. A
  person decides through `POST /v1/approvals/{id}/decision`; the guard checks
  tenant, who may decide and the payload hash, and the worker executes the
  action once. By default the buyer who asked approves what the AI prepared
  (`self_approved`); four-eyes (requester ≠ approver) is an opt-in tenant
  policy (`tenants.allow_self_approval = false`). AgentOS endpoints are not mounted, and paused runs
  are never resumed from client-supplied state (see the open upstream bug
  [agno-agi/agno#10589](https://github.com/agno-agi/agno/issues/10589)).
- **Prompts are versioned files** in `prompts/`.

## Scheduled jobs

Long waits are database rows; nothing sleeps. EventBridge Scheduler puts these
jobs on the jobs queue with the all-zeros tenant id (= every tenant):

| Job | Every | What |
|---|---|---|
| `mailbox.sync` | 2 min | new messages in the RFQ threads of connected mailboxes |
| `followups.sweep` | 15 min | chases due (business days, supplier holidays, auto-reply pauses), clarifications for offers with missing fields, cleanup of stale proposals |
| `negotiation.sweep` | 15 min | counter-offers and best-and-final requests, early close, closing at the deadline |

Every chase, clarification or counter-offer is proposed as a pending approval
and sent in the RFQ's thread once approved. Under an approved negotiation
mandate (`POST /v1/rfqs/{id}/negotiation/mandate`), the sweeps also approve the
in-bounds ones on the buyer's behalf right away; out-of-bounds drafts are held
for a person, and `POST .../negotiation/mandate/pause` stops it all. Locally,
trigger a sweep with `curl -X POST localhost:8080/dev/followups/sweep`.

## Spreadsheets

Buyers send what they have. `POST /v1/data/workbook-imports` (and the
copilot's `import_spreadsheet`) reads every sheet of an `.xlsx` or a `.csv`:
rules find the header row and columns (Portuguese, English, Italian), the
`sheet_mapper` agent proposes a mapping for sheets the rules cannot read, and
code validates every value. When unsure it answers `needs_confirmation` with
questions and writes nothing until confirmed. Supplier emails next to supplier
names become supplier contacts.

## Observability

- **Traces:** OpenTelemetry, exported over OTLP/HTTP to
  `OTEL_EXPORTER_OTLP_ENDPOINT` (in AWS, an ADOT collector sidecar that
  forwards to CloudWatch). Requests, outbound HTTP, AWS calls, and queries on
  engines passed to `instrument_engine` are traced; `/health` is not.
- **Errors:** Sentry when `SENTRY_DSN` is set, for errors only (no Sentry
  tracing).
- Both are off when unset, so local runs and tests export nothing.
- **IDs, never content.** Logs, spans and Sentry events carry `tenant_id`,
  `run_id`, the job type, the model and token counts. They never carry prompts,
  model output, documents, emails, request bodies, query strings, cookies, or
  auth headers. Sentry runs with `send_default_pii=False`, no request bodies, no
  local variables, and a `before_send` scrubber; span URLs lose their query
  strings, and SQL spans record statements without parameters.

## Deploy (dev)

`.github/workflows/deploy-dev.yml` deploys to the `dev` environment (AWS
account `b2b-dev`, Terraform in `bido-infra/b2b/dev`). It runs after CI
succeeds on a push to `main`, or by hand from the Actions tab.

1. Build the arm64 image once and push it to ECR, tagged with the git SHA.
   ECR tags are immutable; an SHA that is already there is reused.
2. Register new `api`, `worker` and `migrate` task definitions, changing only
   the image. Everything else stays as Terraform defined it.
3. Run `alembic upgrade head` as a one-off `migrate` task. If it fails, the
   deploy stops and prints the task's log; the services keep the old image.
4. Roll the `api` and `worker` services and wait until they are stable.

**Rollback:** run the workflow by hand with `image_tag` set to an earlier
SHA. Nothing is rebuilt. Migrations are not rolled back: keep them backward
compatible with the previous image.

It authenticates with GitHub OIDC; no AWS keys are stored. The role trusts
only this repository in the `dev` GitHub environment, so that environment's
protection rules (required reviewers, allowed branches) decide who deploys.
Until the variables below exist, the job logs a notice and does nothing.

| `dev` environment variable | Value (`terraform output` in `bido-infra/b2b/dev`) |
|---|---|
| `AWS_DEPLOY_ROLE` | `github_deploy_back_role_arn` |
| `ECR_REPOSITORY` | `ecr_repository_url` |
| `RUN_TASK_NETWORK_CONFIGURATION` | `run_task_network_configuration` (the JSON as is) |
| `AWS_REGION` (optional) | default `us-east-2` |
| `ECS_CLUSTER` (optional) | default `bido-dev` |
| `TASK_FAMILY_PREFIX` (optional) | default `bido-dev` |

## Not here yet (deliberately)

- **Production deploy:** a `prd` environment and promotion of the same image
  come with `bido-infra/b2b/prd`.
- **Staff (cross-tenant) access** and roles managed from the app (roles come
  from Clerk on first sign-in and from the database).
- **Product features:** this is the scaffold that features build on.

See [AGENTS.md](AGENTS.md) for the rules every change must follow.
