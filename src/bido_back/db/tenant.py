"""Tenant context errors.

There is deliberately no module-level "current tenant" here (no contextvar,
no global). The tenant is passed explicitly to `tenant_session` for every
unit of work, so forgetting it is a visible bug at the call site rather than
a silent leak from whatever request ran last.
"""


class TenantContextMissing(RuntimeError):
    """Raised when tenant-scoped database work is attempted without a tenant.

    Fail closed: no tenant means no query, never "all tenants".
    """
