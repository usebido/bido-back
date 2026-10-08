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
   queues, email, the OpenAI API with real data) without explicit approval.

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
  and has no `BYPASSRLS`. All DDL in `public` goes through Alembic migrations
  run as `bido_migrator`, including tables a library would like to create
  itself.
- **Agno's storage is the one exception, and it is fenced off.** Agno keeps its
  sessions, memory and runs in schema `agno`, owned by role `bido_agno`
  (`AGNO_DATABASE_URL`), which creates its own tables there and has no access
  to `public`. Agno's tables have no RLS, so every Agno `user_id` and
  `session_id` is namespaced by tenant, built only by the agents adapter.
- Migrations are append-only. Never edit one that has been merged; add a new one.
- Missing tenant means fail closed. No code path may fall back to "all tenants".

## Agents and side effects

- **Agno does the agent part** (model calls, the tool loop, sessions, memory).
  It is pinned to an exact version in `pyproject.toml`. Upgrade it deliberately,
  in its own PR, after reading the changelog; never inside a feature PR or a
  grouped dependency bump.
- **Agno and OpenAI types stay inside one module,**
  `src/bido_back/agents/agno_adapter.py`. Everything else, including the rest of
  the agents package, the api, the worker and the database layer, uses our own
  neutral tool, run and event types through `run()`.
- **Agno telemetry is off:** `AGNO_TELEMETRY=false` in the image, compose and
  CI, and `telemetry=False` passed explicitly in code (a `.env` file does not
  reach Agno).
- **No shell, Python, file-system or MCP toolkits.** Agents get only our typed
  tools. Adding any built-in Agno toolkit is a security decision, not a
  convenience.
- Tools take the tenant from our context, never from model-supplied arguments.
- **Bido keeps everything that commits.** Auth, tenants, approvals, the outbox
  and the business endpoints are ours, not Agno's. A tool that sends, accepts,
  pays, or otherwise commits is declared as an `act` tool. Agno registers it
  with `external_execution`, so Agno can never run it: the run stops with an
  `ApprovalRequested` proposal, recorded as a `pending` row in our `approvals`
  table.
- **Only our guard turns a proposal into an action.** A person decides every
  outbound action through `POST /v1/approvals/{id}/decision`. The guard checks
  the tenant, who may decide, and that the stored payload still matches its
  hash. By default the buyer who asked may approve what the AI prepared for
  them (`tenants.allow_self_approval`, recorded as `self_approved`); four-eyes
  (an approver or owner who is not the requester) is an opt-in tenant policy,
  enforced by the guard and by the database. Execution then happens once, in the
  worker, keyed by the approval id. Never mount Agno's AgentOS endpoints or
  resume a paused run from client-supplied state (see the open upstream bug
  [agno-agi/agno#10589](https://github.com/agno-agi/agno/issues/10589)). Agno's
  own `agno_approvals` table is never an authority.
- **A negotiation mandate is one approval for many emails.** Outbound actions
  need human approval either one by one, or through an approved
  `approve_negotiation_mandate` (`bido_back.mandates`): the person approves the
  suppliers, their addresses, per-line target and maximum, the deadline, the
  round and daily caps and the exact initial emails. After that the worker
  approves the RFQ's chases, clarifications and counter-offers on their behalf
  only when code verifies each one is inside those bounds (mandated address, no
  copies, prices within the maximum, no accepting or ordering language, caps
  respected, limits unchanged); anything else is held for a person. Every
  automatic approval still goes through the executor once, is audited with the
  mandate id, and stops at once when the mandate is paused. Awards and purchase
  orders are never covered by a mandate. Never widen what a mandate may approve
  without a code check behind it, and never let the buyer's maximum price or
  another supplier's name or price reach a writer agent's input.
- **Email goes only to test domains or confirmed domains.** With
  `tenants.outbound_requires_confirmed_domains` (the default), Bido writes from
  a buyer's mailbox only to reserved test domains (`example.com`, `.test`, ...)
  or to domains a person of the tenant confirmed (`bido_back.outbound`): with
  the approval (`confirmed_domains`), or via `/v1/outbound/domains`. The
  approval guard refuses unconfirmed recipients (409), mandates hold them, and
  the mailbox sender re-checks every email before it leaves. The decision uses
  the domain only; DNS (MX) is shown to people, never trusted to allow a send.
  Never add a send path that bypasses `rfqs.mailbox.mailbox_senders_for`.
- **Side effects go through the outbox** with a unique idempotency key, written
  in the same transaction as the state change. Workers and providers redeliver;
  handlers must be idempotent.
- Prompts are versioned files under `prompts/` (`<name>.v<N>.md`). Change a
  prompt by adding a new version, not by editing a released one.

## Observability

- Traces go out through OpenTelemetry (OTLP/HTTP to an ADOT collector, then
  CloudWatch); errors go to Sentry. Both are set up once per process by
  `bido_back.observability.init_observability` and are no-ops when
  `OTEL_EXPORTER_OTLP_ENDPOINT` / `SENTRY_DSN` are unset.
- **No customer content in logs, span attributes, or Sentry.** That means no
  prompts, model output, documents, emails, request or response bodies, query
  strings, cookies, or auth headers. Record IDs instead: `tenant_id`,
  `run_id`, idempotency keys, the job type, the model name, token counts.
- Sentry's `before_send` scrubber and the span URL redaction are a safety net,
  not permission: write log lines and attributes that are safe on their own,
  and keep exception messages free of payloads.
- Do not turn on body, header or message-content capture in any
  instrumentation (`send_default_pii`, request bodies, local variables,
  `OTEL_INSTRUMENTATION_*_CAPTURE_*`).

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
