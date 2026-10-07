"""Inbound lookup: find an invite's tenant from a reply token, Message-ID or RFQ ref.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-06

Why these functions exist
-------------------------
An inbound email has no tenant until it is matched to an invite. Every
tenant-owned table has FORCEd row-level security, and the app role sees no row
without `app.tenant_id`, so the app cannot find the invite that tells it which
tenant to use. Three narrow lookups break that circle. (The primary inbound
path is the buyer's own mailbox, where the tenant and invite are already known
and none of this is needed; these serve the Bido-domain reply address, plan B.)

  * `resolve_invite_by_token(token)`: the invite whose `reply_token` is the
    token from the reply address `rfq-<token>@<email_domain>`.
  * `resolve_invite_by_message_id(message_id)`: the invite whose RFQ thread
    (`invites.thread_message_id`) or one of whose outbound messages
    (`messages.message_id_header`, direction 'out') has that Message-ID. Used
    when the reply address was lost (In-Reply-To / References fallback).
  * `resolve_invite_by_rfq_ref(p_ref, p_sender)`: the invite of RFQ `p_ref`
    (from an `[RFQ-<n>]` marker in the subject or body) whose supplier
    `p_sender` is a known contact of: the invite's own contact, or a contact
    linked to its supplier. RFQ refs are short, guessable and unique only per
    tenant, so the ref alone never matches; the caller treats more than one
    row as no match.

All three return ONLY `(tenant_id, invite_id)`, nothing else from the rows, and
only for an exact match on values the caller already holds. They are
SECURITY DEFINER, so they run as their owner (the
migration role), are STABLE (read-only), and pin `search_path` to
`public, pg_temp` so a caller cannot shadow the tables. EXECUTE is revoked
from PUBLIC and granted to the app role only.

How they get past RLS
---------------------
FORCE ROW LEVEL SECURITY applies to the table owner too, and the migration
role has no BYPASSRLS, so SECURITY DEFINER alone still sees zero rows. A
SELECT-only policy `inbound_lookup` on `invites`, `messages`, `rfqs`,
`contacts` and `supplier_contacts` admits rows
only to the owner role (the policy is `TO` that role, never to the app role)
and only while the transaction-local flag `app.inbound_lookup` is 'on'. Each
function turns the flag on with `set_config(..., true)` right before its one
query and off right after it. So the bypass exists only inside these three
functions: the app role gains nothing by setting the flag itself (the policy
does not apply to it), and the migration role does not see cross-tenant rows
by accident during migrations or test cleanup.

(The flag cannot be a `SET app.inbound_lookup = 'on'` clause on the function:
Postgres 15+ only lets a superuser attach an unregistered custom parameter to
a function, and the migration role is not one.)
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "bido_app"
LOOKUP_FLAG = "(select current_setting('app.inbound_lookup', true) = 'on')"
_POLICY_TABLES = ("invites", "messages", "rfqs", "contacts", "supplier_contacts")
_SIGNATURES = (
    "resolve_invite_by_token(text)",
    "resolve_invite_by_message_id(text)",
    "resolve_invite_by_rfq_ref(text, text)",
)


def upgrade() -> None:
    # The policy and the functions are owned by whoever runs the migration
    # (bido_migrator locally/CI, the migration role in AWS).
    for table in _POLICY_TABLES:
        op.execute(
            f"CREATE POLICY inbound_lookup ON {table} FOR SELECT TO CURRENT_USER "
            f"USING ({LOOKUP_FLAG})"
        )

    op.execute(
        "CREATE INDEX invites_thread_message_id_idx ON invites (thread_message_id) "
        "WHERE thread_message_id IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX messages_outbound_message_id_header_idx ON messages (message_id_header) "
        "WHERE direction = 'out' AND message_id_header IS NOT NULL"
    )

    op.execute(
        """
        CREATE FUNCTION resolve_invite_by_token(token text)
        RETURNS TABLE (tenant_id uuid, invite_id uuid)
        LANGUAGE plpgsql
        STABLE
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
        BEGIN
            IF token IS NULL OR length(token) > 64 THEN
                RETURN;
            END IF;
            PERFORM set_config('app.inbound_lookup', 'on', true);
            RETURN QUERY
                SELECT i.tenant_id, i.id
                FROM public.invites AS i
                WHERE i.reply_token = lower(token);
            PERFORM set_config('app.inbound_lookup', 'off', true);
        END
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION resolve_invite_by_message_id(message_id text)
        RETURNS TABLE (tenant_id uuid, invite_id uuid)
        LANGUAGE plpgsql
        STABLE
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
        BEGIN
            IF message_id IS NULL OR length(message_id) > 998 THEN
                RETURN;
            END IF;
            PERFORM set_config('app.inbound_lookup', 'on', true);
            RETURN QUERY
                SELECT i.tenant_id, i.id
                FROM public.invites AS i
                WHERE i.thread_message_id = message_id
                UNION
                SELECT m.tenant_id, m.invite_id
                FROM public.messages AS m
                WHERE m.direction = 'out'
                  AND m.message_id_header = message_id
                  AND m.invite_id IS NOT NULL;
            PERFORM set_config('app.inbound_lookup', 'off', true);
        END
        $$
        """
    )

    op.execute(
        """
        CREATE FUNCTION resolve_invite_by_rfq_ref(p_ref text, p_sender text)
        RETURNS TABLE (tenant_id uuid, invite_id uuid)
        LANGUAGE plpgsql
        STABLE
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
        BEGIN
            IF p_ref IS NULL OR p_sender IS NULL
               OR length(p_ref) > 64 OR length(p_sender) > 320 THEN
                RETURN;
            END IF;
            PERFORM set_config('app.inbound_lookup', 'on', true);
            RETURN QUERY
                SELECT i.tenant_id, i.id
                FROM public.rfqs AS r
                JOIN public.invites AS i
                  ON i.rfq_id = r.id AND i.tenant_id = r.tenant_id
                WHERE r.ref = p_ref
                  AND (
                    EXISTS (
                        SELECT 1 FROM public.contacts AS c
                        WHERE c.id = i.contact_id
                          AND c.tenant_id = i.tenant_id
                          AND lower(c.email) = lower(p_sender)
                    )
                    OR EXISTS (
                        SELECT 1
                        FROM public.supplier_contacts AS sc
                        JOIN public.contacts AS c
                          ON c.id = sc.contact_id AND c.tenant_id = sc.tenant_id
                        WHERE sc.supplier_id = i.supplier_id
                          AND sc.tenant_id = i.tenant_id
                          AND lower(c.email) = lower(p_sender)
                    )
                  );
            PERFORM set_config('app.inbound_lookup', 'off', true);
        END
        $$
        """
    )

    for signature in _SIGNATURES:
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO {APP_ROLE}")


def downgrade() -> None:
    for signature in reversed(_SIGNATURES):
        op.execute(f"DROP FUNCTION {signature}")
    op.execute("DROP INDEX messages_outbound_message_id_header_idx")
    op.execute("DROP INDEX invites_thread_message_id_idx")
    for table in reversed(_POLICY_TABLES):
        op.execute(f"DROP POLICY inbound_lookup ON {table}")
