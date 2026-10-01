"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

Checklist for tenant-owned tables (scripts/check_rls.py enforces it in CI):
  - tenant_id uuid NOT NULL REFERENCES tenants(id)
  - ALTER TABLE ... ENABLE ROW LEVEL SECURITY
  - ALTER TABLE ... FORCE ROW LEVEL SECURITY
  - CREATE POLICY ... USING (...) WITH CHECK (...) using
    (select NULLIF(current_setting('app.tenant_id', true), '')::uuid)
Global tables must be added to the allowlist in scripts/check_rls.py.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
${imports if imports else ""}

revision: str = ${repr(up_revision)}
down_revision: str | Sequence[str] | None = ${repr(down_revision)}
branch_labels: str | Sequence[str] | None = ${repr(branch_labels)}
depends_on: str | Sequence[str] | None = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
