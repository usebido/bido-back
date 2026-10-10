"""Runtime configuration, read from the environment.

In AWS every value comes from ECS (plain env vars or Secrets Manager
injection). Locally it comes from `.env` (see `.env.example`). Nothing here
has a production default: a missing value should fail at startup, not at
the first request.
"""

import re
from functools import lru_cache
from typing import Final, Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, PostgresDsn, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASIS_THEORY_TEST_HOST: Final = "api.test.basistheory.com"


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

    aws_region: str = "us-east-2"
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
    # A Clerk organization is a company (tenant). A signed-in member of an
    # organization that is already linked to a tenant always gets a membership
    # there; `auth_auto_provision` also creates the tenant on the first login
    # of an organization nobody linked yet. `clerk_allowed_org_ids`, when set,
    # restricts both to those organizations.
    auth_auto_provision: bool = False
    clerk_allowed_org_ids: list[str] = []
    # Optional: Clerk Backend API key, used only when provisioning, to read the
    # organization's display name and the user's e-mail (fail-soft without it).
    clerk_secret_key: str | None = Field(default=None, repr=False)

    # CORS: the browser origins allowed to call the api (the frontend runs on
    # another origin, e.g. Vercel). Exact origins, plus an optional regex for
    # preview deployments. The frontend authenticates with a bearer token, so
    # no credentials (cookies) are allowed cross-origin. "*" is refused outside
    # local.
    cors_allowed_origins: list[str] = []
    cors_allowed_origin_regex: str | None = None

    # Observability. Both are off when unset (local runs and tests stay quiet).
    otel_exporter_otlp_endpoint: str | None = None
    otel_service_name: str = "bido-back"
    sentry_dsn: str | None = Field(default=None, repr=False)
    # Logs: one JSON object per line on stdout (CloudWatch reads the fields).
    # "text" is for a terminal; unset means text locally and JSON elsewhere.
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["json", "text"] | None = None
    # The commit the image was built from (set by the deploy); tags logs and
    # Sentry events with the release.
    git_sha: str | None = None
    # LLM traces to Langfuse (bido_back/agents/tracing.py), on only when all
    # three are set. Content (prompts, outputs, tool arguments and results) is
    # added only with LANGFUSE_CAPTURE_CONTENT=true, and never when
    # ENVIRONMENT=prd.
    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = Field(default=None, repr=False)
    langfuse_host: str | None = None
    langfuse_capture_content: bool = False

    # Payments (bido_back.payments): a buyer pays an issued PO by card, Bido mints
    # its stablecoin into the company's invisible wallet and funds the PO's escrow
    # on Solana. Off until every required value below is set; devnet only.
    #
    # Card: Basis Theory Agentic Payments (vault + `spt` rail), charged on Bido's
    # Stripe account. The private key stays in the backend; the public key only
    # tokenizes cards in the browser (Basis Theory Elements).
    basis_theory_api_url: str = "https://api.test.basistheory.com"
    basis_theory_api_key: str | None = Field(default=None, repr=False)
    basis_theory_public_key: str | None = None
    # "simulated": the Basis Theory test tenant mints mock Stripe tokens that
    # Stripe cannot charge, so the charge is recorded as simulated (marked as
    # such everywhere). Only with the test tenant, never in prd.
    payments_card_charge: Literal["stripe", "simulated"] = "stripe"
    stripe_secret_key: str | None = Field(default=None, repr=False)
    # Bido's Stripe Business Network profile (`profile_...`): the Shared Payment
    # Token Basis Theory mints is granted to it.
    stripe_network_business_profile: str | None = None
    stripe_api_version: str | None = None
    # The merchant the card allowance is scoped to (Bido collects the payment).
    payments_merchant_name: str = "Bido"
    payments_merchant_url: str = "https://usebido.com"
    payments_merchant_country: str = "US"
    # Basis Theory's consumer country for enrolled cards.
    payments_consumer_country: str = "US"
    # How long a payment's quote (amount, FX rate) may wait for its approval.
    payments_quote_ttl_minutes: int = Field(default=24 * 60, ge=5, le=7 * 24 * 60)
    # The escrow returns the unreleased balance to the buyer only after this.
    payments_escrow_refund_days: int = Field(default=90, ge=1, le=3650)
    # Suppliers' bank details are sealed in Bido's database until the payout provider
    # (Tazapay, next step) holds them. Local: a Fernet key. AWS: KMS envelope
    # encryption under `payout_details_kms_key_id` (required in prd).
    payout_details_encryption_key: str | None = Field(default=None, repr=False)
    payout_details_kms_key_id: str | None = None
    # Wallets: one Privy server wallet per company, owned by a Privy key quorum
    # whose P-256 authorization key Bido holds (PKCS#8 DER, base64).
    privy_api_url: str = "https://api.privy.io"
    privy_app_id: str | None = None
    privy_app_secret: str | None = Field(default=None, repr=False)
    privy_authorization_key: str | None = Field(default=None, repr=False)
    privy_wallet_owner_id: str | None = None
    privy_wallet_policy_id: str | None = None
    # Solana: our stablecoin mint (Bido holds its mint authority) and the
    # `bido_escrow` program (solana/). Keys are base58 64-byte secret keys:
    # the relayer pays every fee and rent, the operator signs `create_po`.
    solana_cluster: Literal["devnet"] = "devnet"
    solana_rpc_url: str | None = Field(default=None, repr=False)
    solana_escrow_program_id: str = "7992GKVq51ChvsVJQ9GFto2aBozUGuMcuY93SUSxexJw"
    solana_stablecoin_mint: str | None = None
    solana_stablecoin_decimals: int = Field(default=6, ge=0, le=9)
    solana_mint_authority_key: str | None = Field(default=None, repr=False)
    solana_operator_key: str | None = Field(default=None, repr=False)
    solana_relayer_key: str | None = Field(default=None, repr=False)
    # Where released milestones are paid: the payout provider's deposit wallet.
    solana_payout_owner: str | None = None
    # Who attests each milestone's gate (Solana Attestation Service signer).
    solana_gate_attester: str | None = None
    solana_priority_fee_micro_lamports: int = Field(default=1_000, ge=0, le=1_000_000)

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("cors_allowed_origin_regex")
    @classmethod
    def _regex_compiles(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                re.compile(value)
            except re.error as exc:
                raise ValueError(f"CORS_ALLOWED_ORIGIN_REGEX is not a valid regex: {exc}") from exc
        return value

    @model_validator(mode="after")
    def _no_wildcard_cors_outside_local(self) -> Self:
        if self.environment != "local" and "*" in self.cors_allowed_origins:
            raise ValueError('CORS_ALLOWED_ORIGINS may contain "*" only when ENVIRONMENT=local')
        return self

    @model_validator(mode="after")
    def _simulated_charges_only_on_the_test_tenant(self) -> Self:
        if self.payments_card_charge == "simulated" and (
            self.environment == "prd"
            or urlsplit(self.basis_theory_api_url).hostname != BASIS_THEORY_TEST_HOST
        ):
            raise ValueError(
                "PAYMENTS_CARD_CHARGE=simulated needs the Basis Theory test tenant "
                f"({BASIS_THEORY_TEST_HOST}) and ENVIRONMENT other than prd"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()  # pyright: ignore[reportCallIssue]
