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

    openai_api_key: str | None = Field(default=None, repr=False)

    # Email. `email_domain` names our Message-IDs and the Bido reply addresses.
    email_domain: str = "quotes.usebido.com"
    # How RFQ emails go out. "mailbox": from the requesting buyer's connected
    # Gmail / Microsoft 365 (the product behavior). "smtp" / "ses": through that
    # transport instead, From the buyer's address; only for local and test runs
    # (Mailpit locally), so no mailbox connection is needed to try the flow.
    email_transport: Literal["mailbox", "smtp", "ses"] = "mailbox"
    smtp_host: str = "localhost"  # Mailpit locally
    smtp_port: int = 1025

    # Buyer mailbox connections (OAuth). Bido reads and sends only in RFQ threads it
    # started; the rest of the mailbox is never listed or stored.
    google_client_id: str | None = None
    google_client_secret: str | None = Field(default=None, repr=False)
    microsoft_client_id: str | None = None
    microsoft_client_secret: str | None = Field(default=None, repr=False)
    microsoft_tenant: str = "common"
    oauth_redirect_base_url: str = "http://localhost:8080"
    # The frontend page the provider callback forwards `code` and `state` to (in
    # the URL fragment). It finishes the link with POST /v1/mailbox/complete as
    # the signed-in user, who must be the one who started it.
    mailbox_connect_return_url: str = "http://localhost:3000/settings/mailbox"
    # Encrypts stored OAuth tokens. Local: a Fernet key. AWS: KMS envelope
    # encryption under `oauth_token_kms_key_id`.
    token_encryption_key: str | None = Field(default=None, repr=False)
    oauth_token_kms_key_id: str | None = None
    mailbox_sync_interval_seconds: int = 120

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
