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
  logs.py            JSON log lines on stdout, request/job context, redaction
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

Suppliers saved before 2026-10-09 from just a name and an email have no country,
so their RFQ documents go out in English. `uv run python
scripts/backfill_supplier_locale.py --tenant <uuid>` prints what the email's
country TLD or the name's legal suffix (`.com.br`, `Ltda`, `GmbH`...) would set;
`--apply` writes it. Ambiguous ones (`S.A.`, a `.com` address) are left for the
buyer to settle in the chat.

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
  `gpt-6-luna` by default (the Copilot included); `gpt-6.1-sol` for quote
  extraction, the sheet and source mappers, the template builder, and the reply
  of a Copilot turn that ran the purchase analysis (`analysis_reply_writer`, a
  handoff: the Copilot stops after the analysis tool and the writer answers).
  Each agent's model is set in `agents/catalog/definitions.py` or
  `copilot/agent.py`. Requests are stateless (`store=False`): OpenAI keeps no
  conversation state between turns.
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

## Outbound recipient guard

A spreadsheet can carry made-up supplier addresses whose domains belong to real
companies. By default (`tenants.outbound_requires_confirmed_domains`), Bido
sends only to reserved test domains (`example.com`, `.test`, `.example`,
`.invalid`, `.localhost`) or domains someone in the company confirmed:

- `GET /v1/approvals` and a mandate proposal show each recipient with its DNS
  status (`real_domain`, `no_mail`, `unknown`, `test`) and whether it would go out;
- approving takes `confirmed_domains` (a 409 lists what is missing);
- `GET/POST /v1/outbound/domains` lists and confirms domains, and
  `POST /v1/outbound/check` classifies addresses.

The flag is set by seeds or the migration role, never by a request.

## Procurement sources

The copilot's `analyze_procurement_sources` reads related attachments together,
identifies direct material demand, inventory, purchase history, suppliers and
supplier evaluations, and returns evidence-backed purchase needs without
requiring a BOM. Source reading, semantic mapping and deterministic validation
are separate stages. Reported purchase quantities remain visible when conflicting
units or missing assumptions prevent a validated calculation.

XLSX/CSV tables and extracted PDF/DOCX/text use the same neutral evidence contract.
Unreadable, scanned or truncated sources are disclosed. Assessments are read-only;
the existing spreadsheet importer still persists its supported company-data kinds.
See [the architecture review and research](docs/source-understanding.md) for the
workflow, boundaries, procurement rules and remaining work.

## Observability

- **Logs:** one JSON object per line on stdout (CloudWatch reads the fields;
  `LOG_FORMAT=text` or a local run gives plain lines), at `LOG_LEVEL` (default
  INFO). Every line has `ts`, `level`, `logger`, `msg`, `service`, `release`
  (the image's commit), whatever is bound to the request or job
  (`request_id`, `tenant_id`, `user_id`, `conversation_id`, `run_id`, `job`),
  and `trace_id`/`span_id` when tracing is on. What is logged: every request
  (route template, status, duration), unhandled errors with a traceback
  (no local variables), HTTP errors and validation failures (field paths only),
  each Copilot turn (start and end: outcome, duration, tools), each agent run
  (agent, prompt version, model, outcome, error code), each tool call (status,
  duration, masked reason), each workbook import and section (type, row counts,
  error reasons), each job (type, outcome, duration, attempt), sends, recipient
  guard blocks, mandate holds and mailbox syncs.
- **Request ids:** every response has `X-Request-ID` (the caller's, when it
  sends a plain token) and every error body has `request_id`. Quote it to find
  the request's lines (queries below).
- **Traces:** OpenTelemetry, exported over OTLP/HTTP to
  `OTEL_EXPORTER_OTLP_ENDPOINT` (in AWS, an ADOT collector sidecar that
  forwards to CloudWatch). Requests, outbound HTTP, AWS calls, and queries on
  engines passed to `instrument_engine` are traced; `/health` is not.
- **Errors:** Sentry when `SENTRY_DSN` is set, for errors only (no Sentry
  tracing).
- **Tokens and cost:** every `agent run` line has the run's own
  `model_calls`, `input_tokens`, `cached_input_tokens`, `output_tokens`,
  `reasoning_tokens` and `cost_usd` (runs its tools started log theirs);
  `copilot turn finished` has the turn's totals, nested runs included. `cost_usd` is an estimate from the list
  prices in `bido_back/agents/usage.py` (`None`, with `unpriced_calls`, for a
  model missing there).
- **LLM traces (Langfuse, dev only):** with `LANGFUSE_HOST`,
  `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` set, each Copilot turn or
  top-level agent run is one trace: the run, each model request (model, tokens,
  cost, time to first token) and each tool call, with runs started by a tool
  nested under it. A trace's session is the Copilot conversation id; it also
  carries the user id, tenant, `request_id` (the buyer's "Referência"), job,
  environment and release. Prompts, outputs and tool arguments/results are
  added only with `LANGFUSE_CAPTURE_CONTENT=true`, which is refused when
  `ENVIRONMENT=prd`; without it a trace has names, timings, tokens, costs and
  statuses only. These spans have their own exporter and never mix with the
  request traces above (`bido_back/agents/tracing.py`).
- All of these are off when unset, so local runs and tests export nothing.
- **IDs, never content** (except the opt-in Langfuse content above, in dev,
  with synthetic data). Logs, spans and Sentry events carry `tenant_id`,
  `run_id`, the job type, the model and token counts. They never carry prompts,
  model output, documents, emails, request bodies, query strings, cookies, or
  auth headers. Sentry runs with `send_default_pii=False`, no request bodies, no
  local variables, and a `before_send` scrubber; span URLs lose their query
  strings, and SQL spans record statements without parameters.

## Evals (agents on synthetic spreadsheets)

`evals/` checks the agents end to end on what a buyer would upload: ten
synthetic workbooks generated by `evals/layouts.py` (tidy sheets, Italian
titles and a totals row, Portuguese decimal commas, two tables per sheet,
ERP headers only the sheet mapper can read, duplicates and bad rows, mixed
date formats, no BOM, no plan, two products) and four supplier replies
(`evals/quotes.py`). Supplier e-mails use reserved test domains only.

Scenarios: `import` (sections, valid rows, contacts, PO lines), `analysis`
(quantities, target prices and suggested suppliers vs the golden numbers in
`evals/golden.py`), `copilot` (a real conversation: attach, "faz uma
análise", "pode criar a RFQ"; checked by tool calls, the answer and the RFQ
it leaves) and `extraction` (prices, quantities, lead times, grounding,
extra offers).

```bash
uv run python -m evals.run --mock      # scripted model: checks the harness (free)
uv run python -m evals.run             # live model, k=3: pass^k per case
uv run python -m evals.run -k 1 --scenario copilot --fixture motor_tidy_en
```

Reports land in `evals/reports/<timestamp>/` (git-ignored): `report.md`,
`report.json` and the generated fixtures with their golden expectations.
Every run creates its own company, so any migrated database works. Tasks:
`eval: run (mock)` and `eval: run (live)`. The default test run excludes the
`eval` marker; `tests/evals` checks the harness in mock mode.

### Flow evals (the RFQ journey, with scripted suppliers)

`evals/flow.py` runs the whole flow in one process (`evals/world.py`): the real
services, approvals, mandates and job handlers against the database, with the
job queue drained in-process and a capturing mailbox transport under the real
recipient guard, so no email ever leaves the process. Scripted suppliers
(`evals/personas.py`) answer every captured email with a MIME reply fed into
the normal intake path (`ingest_for_invite`); their numbers are decided by
code (price per line per round, lead time, declined lines), their text is in
their language (de, it, fr, en), some put prices in a spreadsheet or a PDF,
one adds another department's offers. The clock is simulated (business days
for follow-ups, days for negotiation rounds).

Scenarios: `intake` (free text, a forwarded email, a pasted sheet -> lines,
quantities, need-by, Incoterm), `rfq_email` (the buyer's template rendered per
supplier language: required fields asked, numbers unchanged, test-domain
recipients), `followups` (chase on time, one clarification listing exactly
what is missing, out-of-office pause, a reply cancels the pending chase),
`negotiation` (five personas under one mandate: counter-offers and the
best-and-final request equal the code-decided asks, never above the maximum,
never revealing it or a competitor, no commitment language, round and daily
caps; early close; deadline close and the recommendation with the broker
rule; a committing draft held), `recipients` (a non-test domain held until
confirmed, test domains pass, the guarded sender as the net) and `award`
(purchase orders, PO email in the supplier's language, "not selected" notices
without prices). After the send, the buyer records the order's life in Bido
(`/v1/purchase-orders/{id}/acknowledge`, `/receipts`, `/close`, `/cancel`, or
the Copilot's purchase-order tools): `fulfilment.state` goes sent ->
acknowledged -> partially_received -> received -> closed (or cancelled), and
the RFQ shows as `completed` once every order is closed or cancelled.

```bash
uv run python -m evals.flow --mock     # scripted model: checks the harness (free)
uv run python -m evals.flow            # live models, k=3 (~45 min, small cost)
uv run python -m evals.flow -k 1 --scenario negotiation --case early_close
```

Reports land in `evals/reports/<timestamp>-flow-<mode>/`: pass^k per case, the
assertion pass rate per scenario, latencies and the top failure reasons.
Tasks: `eval: flow (mock)` and `eval: flow (live)`.

## Running the frontend against this backend

`bido-front` is expected next to this repository (`../bido-front`). Tasks:
`front: dev (local API)`, `front: dev (AWS dev API)` and `dev: full stack`
(everything local, frontend included).

The frontend signs in with the real Clerk dev instance, so the local backend
must verify Clerk tokens. In `.env`:

```
CLERK_ISSUER=https://<instance>.clerk.accounts.dev
CLERK_JWKS_URL=https://<instance>.clerk.accounts.dev/.well-known/jwks.json
CLERK_AUTHORIZED_PARTIES=["http://localhost:3000"]
AUTH_AUTO_PROVISION=true
```

The frontend reads `NEXT_PUBLIC_API_URL` (the tasks set it) and its own
`NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY` from the same Clerk instance.

## Working with the dev environment (AWS)

The `aws dev:` tasks in `.vscode/tasks.json` (health, status, logs, dead
letters, deploy) use an AWS CLI profile for the dev account. Sign in with
IAM Identity Center (`aws: login (SSO)`), then define the profile once in
`~/.aws/config`. Until AWS activates `b2b-dev`, dev runs temporarily in
account 045064752511:

```ini
[profile bido-dev-temp]
source_profile = bido-mgmt
role_arn = arn:aws:iam::045064752511:role/OrganizationAccountAccessRole
region = us-east-2
```

The tasks ask for the profile name and default to `bido-dev-temp`.

## Debugging in AWS

Logs are in CloudWatch: `/ecs/bido-dev/api` and `/ecs/bido-dev/worker`
(30 days). Open **CloudWatch → Logs Insights**, pick both groups, and paste a
query. Or from a terminal (`aws: login (SSO)` first):

```bash
aws logs start-query --profile bido-dev-temp \
  --log-group-names /ecs/bido-dev/api /ecs/bido-dev/worker \
  --start-time $(date -d '-1 hour' +%s) --end-time $(date +%s) \
  --query-string 'filter level in ["ERROR", "WARNING"] | sort @timestamp desc'
aws logs get-query-results --profile bido-dev-temp --query-id <queryId>
```

Errors and warnings in the last hour:

```text
fields @timestamp, level, logger, msg, request_id, tenant_id, job, reason, code, exc_type
| filter level in ["ERROR", "WARNING"]
| sort @timestamp desc
| limit 100
```

Everything one request did (the `request_id` from the error body or the
`X-Request-ID` header):

```text
fields @timestamp, level, logger, msg, route, status, tool, reason, exc
| filter request_id = "<request id>"
| sort @timestamp asc
```

One Copilot turn, or a whole conversation (`conversation_id` is in the
conversation's URL and in the stream's first event):

```text
fields @timestamp, msg, run_id, agent, tool, status, outcome, code, reason, duration_ms
| filter conversation_id = "<conversation id>"
| sort @timestamp asc
```

Copilot turns that did not end well:

```text
fields @timestamp, conversation_id, outcome, code, tool_errors, tools, duration_ms
| filter msg = "copilot turn finished" and outcome != "ok"
| sort @timestamp desc
```

Slowest tools and how often they fail:

```text
filter msg = "tool call"
| stats count(*) as calls, avg(duration_ms) as avg_ms, pct(duration_ms, 95) as p95_ms
  by tool, status
| sort p95_ms desc
```

Tool errors by reason (reasons are masked: quoted values show as `'…'`):

```text
filter msg = "tool call" and status != "ok"
| stats count(*) as n by tool, status, error_class, reason
| sort n desc
```

Spreadsheet sections with rejected rows or missing columns (logged as
warnings; sections are numbered because sheet names are the buyer's):

```text
fields @timestamp, upload_id, section, type, source, rows_total, rows_ok, rows_failed,
       missing_required, error_reasons
| filter msg = "workbook section" and level = "WARNING"
| sort @timestamp desc
```

Jobs that failed or retried:

```text
fields @timestamp, job, tenant_id, outcome, attempt, duration_ms
| filter msg = "job" and (outcome != "handled" or attempt > 1)
| sort @timestamp desc
```

Model spend per day, per agent and model (`cost_usd` is an estimate; each
`agent run` line counts that run's own requests, so the sum is the total):

```text
filter msg = "agent run"
| stats sum(cost_usd) as usd, sum(input_tokens) as input, sum(output_tokens) as output,
        count(*) as runs by bin(1d) as day, agent, model
| sort day desc, usd desc
```

Spend per tenant and day (all agents), and the Copilot's share (turn totals
include the runs its tools started):

```text
filter msg = "agent run"
| stats sum(cost_usd) as usd, count(*) as runs by bin(1d) as day, tenant_id
| sort day desc, usd desc
```

```text
filter msg = "copilot turn finished"
| stats sum(cost_usd) as usd, sum(input_tokens) as input, sum(output_tokens) as output,
        count(*) as turns by bin(1d) as day, tenant_id
| sort day desc, usd desc
```

In Langfuse (dev), find a turn by its conversation (Sessions) or filter traces
by the metadata `request_id` the buyer quotes.

Slow or failing endpoints:

```text
filter msg = "request"
| stats count(*) as n, pct(duration_ms, 95) as p95_ms by method, route, status
| sort status desc, p95_ms desc
```

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

## Solana escrow

The standalone devnet escrow program, LiteSVM tests, IDL and setup tooling live
in [`solana/`](solana/README.md). Run `pnpm --dir solana install --frozen-lockfile`
and `bash solana/scripts/check.sh` from this repository root. CI includes the
Solana checks in the required `ci-ok` result.
