"""Runtime configuration, read from the environment.

In AWS every value comes from ECS (plain env vars or Secrets Manager
injection). Locally it comes from `.env` (see `.env.example`). Nothing here
has a production default: a missing value should fail at startup, not at
the first request.
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field, PostgresDsn
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: Literal["local", "test", "dev", "prd"] = "local"

    # The app role: owns no tables, cannot CREATE, has no BYPASSRLS.
    database_url: PostgresDsn
    # The migration role: owns the schema. Used only by Alembic, never by the app.
    migration_database_url: PostgresDsn | None = None
    # The Agno role: owns only the `agno` schema, where Agno keeps its sessions,
    # memory and runs. It has no access to the business tables in `public`.
    agno_database_url: PostgresDsn | None = None

    aws_region: str = "eu-central-1"
    sqs_queue_url: str | None = None
    # Raw inbound emails (SES -> S3 -> this queue); no tenant until matched.
    sqs_intake_queue_url: str | None = None
    # Local dev: ElasticMQ (SQS-compatible). Unset in AWS.
    sqs_endpoint_url: str | None = None
    # Messages the worker cannot handle (no tenant, unknown type) go here.
    sqs_parking_queue_url: str | None = None

    anthropic_api_key: str | None = Field(default=None, repr=False)

    # Email. Outbound RFQs go from `rfq-<token>@<email_domain>`; replies come back there.
    email_domain: str = "quotes.usebido.com"
    email_transport: Literal["smtp", "ses"] = "smtp"
    smtp_host: str = "localhost"  # Mailpit locally
    smtp_port: int = 1025

    # Blob storage for raw emails and attachments.
    storage_backend: Literal["local", "s3"] = "local"
    storage_local_dir: str = ".data/blobs"
    s3_bucket: str | None = None

    # Auth (Clerk). `auth_dev_mode` lets local runs act as a seeded user via
    # the X-Dev-User header; it is refused unless environment == "local".
    clerk_issuer: str | None = None
    clerk_jwks_url: str | None = None
    clerk_authorized_parties: list[str] = []
    auth_dev_mode: bool = False

    # Observability. Both are off when unset (local runs and tests stay quiet).
    otel_exporter_otlp_endpoint: str | None = None
    otel_service_name: str = "bido-back"
    sentry_dsn: str | None = Field(default=None, repr=False)


@lru_cache
def get_settings() -> Settings:
    return Settings()  # pyright: ignore[reportCallIssue]
