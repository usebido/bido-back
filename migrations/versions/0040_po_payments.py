"""Pay an issued purchase order by card, mint our stablecoin and fund its escrow on Solana.

Revision ID: 0040
Revises: 0039
Create Date: 2026-10-09

* `supplier_payout_accounts`: where a supplier is paid (a CNY bank account, for
  one). The payout provider (Tazapay, next step) pays that account, so the
  details end up there as a beneficiary (`beneficiary_ref`, its id, set then);
  until then they are sealed here (`details_ciphertext`: KMS envelope in AWS,
  Fernet locally, bound to the tenant and supplier). In clear Bido keeps only the
  fingerprint that binds a payment to the account and a masked view. Changing an
  account means disabling it and adding another, never an update.
* `payment_methods`: a buyer's card, vaulted at Basis Theory and enrolled for
  agentic payments (`payment_method_ref` = `pm_...`). No card data here.
* `tenant_wallets`: the company's invisible Solana wallet (a Privy server
  wallet), one per tenant.
* `po_payments`: one payment of one PO, from the request (`pending_approval`)
  through the card charge to the funded escrow:
  `pending_approval -> charging -> charged -> minted -> escrow_created -> funded`,
  or `rejected`, `expired`, `declined`, `failed`. `approval_id` has no FK on
  purpose: the executor holds the approval's row lock while the handler
  writes here (see 0015). At most one live payment per PO; a payment that
  failed after the card was charged stays live (it needs a person, and the
  PO must not be charged twice).
* `payment_chain_transactions`: every Solana transaction sent for a payment,
  recorded BEFORE it is sent (signature + last valid block height), so a
  retry asks the chain instead of sending twice.

Every table has `tenant_id`, forced RLS and the tenant policy. The app role may
not DELETE or TRUNCATE any of them: they are the payment's record.

`payment_retry_tenants(min_age, max_age)`: a SECURITY DEFINER function
returning ONLY tenant ids with a payment stuck between the charge and the
funded escrow, for the scheduled sweep (same pattern as 0030).
"""

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0040"
down_revision: str | Sequence[str] | None = "0039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "bido_app"
CURRENT_TENANT = "(select NULLIF(current_setting('app.tenant_id', true), '')::uuid)"
LOOKUP_FLAG = "(select current_setting('app.payment_lookup', true) = 'on')"
SIGNATURE = "payment_retry_tenants(interval, interval)"
TABLES = (
    "supplier_payout_accounts",
    "payment_methods",
    "tenant_wallets",
    "po_payments",
    "payment_chain_transactions",
)
PAYMENT_STATES = (
    "'pending_approval', 'charging', 'charged', 'minted', 'escrow_created', 'funded', "
    "'rejected', 'expired', 'declined', 'failed'"
)
CHARGED_STATES = "'charged', 'minted', 'escrow_created', 'funded'"
HEX64 = "'^[0-9a-f]{64}$'"
BASE58 = "'^[1-9A-HJ-NP-Za-km-z]{32,88}$'"


def _id() -> sa.Column[UUID]:
    return sa.Column(
        "id",
        postgresql.UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    )


def _tenant() -> sa.Column[UUID]:
    return sa.Column(
        "tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False
    )


def _fk(name: str, target: str, *, nullable: bool = False) -> sa.Column[UUID]:
    return sa.Column(name, postgresql.UUID(as_uuid=True), sa.ForeignKey(target), nullable=nullable)


def _timestamp(name: str, *, now: bool = False) -> sa.Column[datetime]:
    if now:
        return sa.Column(
            name, sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        )
    return sa.Column(name, sa.DateTime(timezone=True), nullable=True)


def upgrade() -> None:
    op.create_table(
        "supplier_payout_accounts",
        _id(),
        _tenant(),
        _fk("supplier_id", "suppliers.id"),
        sa.Column("method", sa.Text(), nullable=False, server_default="bank_transfer"),
        sa.Column("country", sa.Text(), nullable=False),
        sa.Column("currency", sa.Text(), nullable=False),
        sa.Column("details_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("beneficiary_ref", sa.Text(), nullable=True),
        sa.Column("bank_fingerprint", sa.Text(), nullable=False),
        sa.Column("beneficiary_display", sa.Text(), nullable=False),
        sa.Column("bank_name", sa.Text(), nullable=False),
        sa.Column("swift_bic", sa.Text(), nullable=True),
        sa.Column("account_last4", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        _fk("created_by", "users.id"),
        _timestamp("created_at", now=True),
        _fk("disabled_by", "users.id", nullable=True),
        _timestamp("disabled_at"),
        sa.CheckConstraint(
            "method IN ('bank_transfer')", name="supplier_payout_accounts_method_check"
        ),
        sa.CheckConstraint("country ~ '^[A-Z]{2}$'", name="supplier_payout_accounts_country_check"),
        sa.CheckConstraint(
            "currency ~ '^[A-Z]{3}$'", name="supplier_payout_accounts_currency_check"
        ),
        sa.CheckConstraint(
            f"bank_fingerprint ~ {HEX64}", name="supplier_payout_accounts_fingerprint_check"
        ),
        sa.CheckConstraint(
            "account_last4 ~ '^[0-9A-Za-z]{1,4}$'", name="supplier_payout_accounts_last4_check"
        ),
        sa.CheckConstraint(
            "status IN ('active', 'disabled')", name="supplier_payout_accounts_status_check"
        ),
        sa.CheckConstraint(
            "beneficiary_ref IS NULL OR length(beneficiary_ref) BETWEEN 1 AND 120",
            name="supplier_payout_accounts_beneficiary_ref_check",
        ),
        sa.CheckConstraint(
            "(status = 'disabled') = (disabled_at IS NOT NULL)",
            name="supplier_payout_accounts_disabled_check",
        ),
    )
    op.create_index(
        "supplier_payout_accounts_tenant_id_supplier_id_idx",
        "supplier_payout_accounts",
        ["tenant_id", "supplier_id"],
    )
    # The same account twice for one supplier only once it was disabled.
    op.create_index(
        "supplier_payout_accounts_active_fingerprint_key",
        "supplier_payout_accounts",
        ["tenant_id", "supplier_id", "bank_fingerprint"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )

    op.create_table(
        "payment_methods",
        _id(),
        _tenant(),
        _fk("created_by", "users.id"),
        sa.Column("provider", sa.Text(), nullable=False, server_default="basis_theory"),
        sa.Column("payment_method_ref", sa.Text(), nullable=False),
        sa.Column("fingerprint", sa.Text(), nullable=False),
        sa.Column("brand", sa.Text(), nullable=True),
        sa.Column("last4", sa.Text(), nullable=True),
        sa.Column("exp_month", sa.SmallInteger(), nullable=True),
        sa.Column("exp_year", sa.SmallInteger(), nullable=True),
        sa.Column("funding", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        _timestamp("created_at", now=True),
        _timestamp("updated_at", now=True),
        _timestamp("removed_at"),
        sa.UniqueConstraint(
            "tenant_id", "payment_method_ref", name="payment_methods_tenant_id_ref_key"
        ),
        sa.CheckConstraint("provider IN ('basis_theory')", name="payment_methods_provider_check"),
        sa.CheckConstraint(
            "last4 IS NULL OR last4 ~ '^[0-9]{4}$'", name="payment_methods_last4_check"
        ),
        sa.CheckConstraint(
            "exp_month IS NULL OR exp_month BETWEEN 1 AND 12",
            name="payment_methods_exp_month_check",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'pending', 'failed', 'removed')",
            name="payment_methods_status_check",
        ),
        sa.CheckConstraint(
            "(status = 'removed') = (removed_at IS NOT NULL)",
            name="payment_methods_removed_check",
        ),
    )
    # One enrollment per card per company, until it is removed.
    op.create_index(
        "payment_methods_live_fingerprint_key",
        "payment_methods",
        ["tenant_id", "fingerprint"],
        unique=True,
        postgresql_where=sa.text("status <> 'removed'"),
    )

    op.create_table(
        "tenant_wallets",
        _id(),
        _tenant(),
        sa.Column("provider", sa.Text(), nullable=False, server_default="privy"),
        sa.Column("chain", sa.Text(), nullable=False, server_default="solana"),
        sa.Column("wallet_ref", sa.Text(), nullable=False),
        sa.Column("address", sa.Text(), nullable=False),
        _timestamp("created_at", now=True),
        sa.UniqueConstraint("tenant_id", "chain", name="tenant_wallets_tenant_id_chain_key"),
        sa.UniqueConstraint("provider", "wallet_ref", name="tenant_wallets_provider_ref_key"),
        sa.CheckConstraint("provider IN ('privy')", name="tenant_wallets_provider_check"),
        sa.CheckConstraint("chain IN ('solana')", name="tenant_wallets_chain_check"),
        sa.CheckConstraint(f"address ~ {BASE58}", name="tenant_wallets_address_check"),
    )

    op.create_table(
        "po_payments",
        _id(),
        _tenant(),
        _fk("purchase_order_id", "issued_purchase_orders.id"),
        # The `pay_purchase_order` approval. No FK on purpose (module docstring).
        sa.Column("approval_id", postgresql.UUID(as_uuid=True), nullable=False),
        _fk("requested_by", "users.id"),
        _fk("payment_method_id", "payment_methods.id"),
        _fk("payout_account_id", "supplier_payout_accounts.id"),
        sa.Column("state", sa.Text(), nullable=False, server_default="pending_approval"),
        sa.Column("po_currency", sa.Text(), nullable=False),
        sa.Column("po_total", sa.Numeric(18, 2), nullable=False),
        sa.Column("charge_currency", sa.Text(), nullable=False),
        sa.Column("charge_amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("fx_rate", sa.Numeric(18, 8), nullable=True),
        sa.Column("fx_rate_date", sa.Date(), nullable=True),
        sa.Column("quote_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("token_amount", sa.Numeric(20, 0), nullable=False),
        sa.Column("chain_po_id", sa.Text(), nullable=False),
        sa.Column("po_hash", sa.Text(), nullable=False),
        sa.Column("milestones", postgresql.JSONB(), nullable=False),
        sa.Column("charge_mode", sa.Text(), nullable=False),
        sa.Column("card_authorization_ref", sa.Text(), nullable=True),
        sa.Column("charge_reference", sa.Text(), nullable=True),
        _timestamp("charged_at"),
        _fk("wallet_id", "tenant_wallets.id", nullable=True),
        sa.Column("wallet_address", sa.Text(), nullable=True),
        sa.Column("escrow_address", sa.Text(), nullable=True),
        sa.Column("vault_address", sa.Text(), nullable=True),
        _timestamp("refund_after"),
        _timestamp("funded_at"),
        sa.Column("failure_code", sa.Text(), nullable=True),
        _timestamp("created_at", now=True),
        _timestamp("updated_at", now=True),
        sa.UniqueConstraint("approval_id", name="po_payments_approval_id_key"),
        sa.UniqueConstraint(
            "tenant_id", "chain_po_id", name="po_payments_tenant_id_chain_po_id_key"
        ),
        sa.CheckConstraint(f"state IN ({PAYMENT_STATES})", name="po_payments_state_check"),
        sa.CheckConstraint("po_currency ~ '^[A-Z]{3}$'", name="po_payments_po_currency_check"),
        sa.CheckConstraint("po_total > 0", name="po_payments_po_total_check"),
        sa.CheckConstraint("charge_currency IN ('USD')", name="po_payments_charge_currency_check"),
        sa.CheckConstraint("charge_amount > 0", name="po_payments_charge_amount_check"),
        sa.CheckConstraint("fx_rate IS NULL OR fx_rate > 0", name="po_payments_fx_rate_check"),
        sa.CheckConstraint(
            "token_amount > 0 AND token_amount <= 18446744073709551615",
            name="po_payments_token_amount_check",
        ),
        sa.CheckConstraint(
            "chain_po_id ~ '^[A-Z0-9-]{6,32}$'", name="po_payments_chain_po_id_check"
        ),
        sa.CheckConstraint(f"po_hash ~ {HEX64}", name="po_payments_po_hash_check"),
        sa.CheckConstraint(
            "jsonb_typeof(milestones) = 'array'", name="po_payments_milestones_check"
        ),
        sa.CheckConstraint(
            "charge_mode IN ('stripe', 'simulated')", name="po_payments_charge_mode_check"
        ),
        sa.CheckConstraint(
            f"state NOT IN ({CHARGED_STATES}) OR charged_at IS NOT NULL",
            name="po_payments_charged_check",
        ),
        sa.CheckConstraint(
            "state <> 'funded' OR funded_at IS NOT NULL", name="po_payments_funded_check"
        ),
        sa.CheckConstraint(
            "failure_code IS NULL OR failure_code ~ '^[a-z0-9_]{1,64}$'",
            name="po_payments_failure_code_check",
        ),
        sa.CheckConstraint(
            f"wallet_address IS NULL OR wallet_address ~ {BASE58}",
            name="po_payments_wallet_address_check",
        ),
    )
    op.create_index("po_payments_tenant_id_state_idx", "po_payments", ["tenant_id", "state"])
    op.create_index("po_payments_purchase_order_id_idx", "po_payments", ["purchase_order_id"])
    # One live payment per PO. A payment that failed AFTER the charge stays live.
    op.create_index(
        "po_payments_one_live_per_po_key",
        "po_payments",
        ["purchase_order_id"],
        unique=True,
        postgresql_where=sa.text(
            "NOT (state IN ('rejected', 'expired', 'declined') "
            "OR (state = 'failed' AND charged_at IS NULL))"
        ),
    )

    op.create_table(
        "payment_chain_transactions",
        _id(),
        _tenant(),
        _fk("payment_id", "po_payments.id"),
        sa.Column("step", sa.Text(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("signature", sa.Text(), nullable=False),
        sa.Column("last_valid_block_height", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False, server_default="sent"),
        sa.Column("error", sa.Text(), nullable=True),
        _timestamp("created_at", now=True),
        _timestamp("updated_at", now=True),
        sa.UniqueConstraint("signature", name="payment_chain_transactions_signature_key"),
        sa.UniqueConstraint(
            "payment_id", "step", "attempt", name="payment_chain_transactions_attempt_key"
        ),
        sa.CheckConstraint(
            "step IN ('mint', 'create_escrow', 'fund')",
            name="payment_chain_transactions_step_check",
        ),
        sa.CheckConstraint("attempt >= 1", name="payment_chain_transactions_attempt_check"),
        sa.CheckConstraint(
            f"signature ~ {BASE58}", name="payment_chain_transactions_signature_check"
        ),
        sa.CheckConstraint(
            "state IN ('sent', 'confirmed', 'failed', 'expired')",
            name="payment_chain_transactions_state_check",
        ),
        sa.CheckConstraint(
            "error IS NULL OR length(error) <= 120", name="payment_chain_transactions_error_check"
        ),
    )
    op.create_index(
        "payment_chain_transactions_payment_id_idx",
        "payment_chain_transactions",
        ["payment_id"],
    )

    for table in TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            f"USING (tenant_id = {CURRENT_TENANT}) "
            f"WITH CHECK (tenant_id = {CURRENT_TENANT})"
        )
        # A payment's record is never deleted by the app.
        op.execute(f"REVOKE DELETE, TRUNCATE ON {table} FROM {APP_ROLE}")

    # The scheduled sweep has no tenant: tenant ids only, past FORCEd RLS (0030).
    op.execute(
        "CREATE POLICY payment_retry_lookup ON po_payments FOR SELECT "
        f"TO CURRENT_USER USING ({LOOKUP_FLAG})"
    )
    op.execute(
        """
        CREATE FUNCTION payment_retry_tenants(min_age interval, max_age interval)
        RETURNS TABLE (tenant_id uuid)
        LANGUAGE plpgsql
        STABLE
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
        BEGIN
            PERFORM set_config('app.payment_lookup', 'on', true);
            RETURN QUERY
                SELECT DISTINCT p.tenant_id
                FROM public.po_payments AS p
                WHERE p.state IN ('charged', 'minted', 'escrow_created')
                  AND p.updated_at < now() - min_age
                  AND p.updated_at > now() - max_age
                ORDER BY p.tenant_id;
            PERFORM set_config('app.payment_lookup', 'off', true);
        END
        $$
        """
    )
    op.execute(f"REVOKE ALL ON FUNCTION {SIGNATURE} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {SIGNATURE} TO {APP_ROLE}")


def downgrade() -> None:
    op.execute(f"DROP FUNCTION {SIGNATURE}")
    op.execute("DROP POLICY payment_retry_lookup ON po_payments")
    for table in reversed(TABLES):
        op.drop_table(table)
