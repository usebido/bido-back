# syntax=docker/dockerfile:1.7
#
# One image, two processes:
#   api     (default)  uvicorn bido_back.api.app:create_app --factory on :8080
#   worker  (override) python -m bido_back.worker
#   migrate (override) alembic upgrade head   (needs MIGRATION_DATABASE_URL)
#
# Builds natively for linux/amd64 and linux/arm64 (ECS Fargate ARM). Base
# images are pinned by digest; Dependabot (docker ecosystem) bumps them.
# No secrets are baked in: every runtime value comes from the environment
# (ECS task definition / Secrets Manager in AWS, .env locally).

ARG PYTHON_IMAGE=python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f

FROM ghcr.io/astral-sh/uv:0.12.23@sha256:61d393e44e249f2e4b526b6c7ddcecce245946826e608e11c93ad4f5bba55b21 AS uv

# ---------------------------------------------------------------- builder
FROM ${PYTHON_IMAGE} AS builder

COPY --from=uv /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# Dependencies first, in their own layer, so source edits do not reinstall them.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-dev --no-install-project

COPY pyproject.toml uv.lock README.md ./
COPY src ./src

# Install the project in editable mode, pointing at /app/src. The runtime
# stage keeps the same layout because agents resolve the repo-root `prompts/`
# directory relative to the source tree (agents/definition.py).
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev \
    && python -m compileall -q /app/src

# ---------------------------------------------------------------- runtime
FROM ${PYTHON_IMAGE} AS runtime

RUN groupadd --system --gid 10001 bido \
    && useradd --system --uid 10001 --gid bido --no-create-home \
       --home-dir /nonexistent --shell /usr/sbin/nologin bido

WORKDIR /app

# Everything stays root-owned and read-only for the runtime user.
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src
COPY alembic.ini ./
COPY migrations ./migrations
COPY prompts ./prompts

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    AGNO_TELEMETRY=false

# The commit this image was built from (deploy-dev passes it); it tags logs and
# Sentry events with the release. Last, so a new commit reuses every layer above.
ARG GIT_SHA=""
ENV GIT_SHA=${GIT_SHA}

USER 10001:10001

EXPOSE 8080

CMD ["uvicorn", "bido_back.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080"]
