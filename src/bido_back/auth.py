"""Who is making a request, in our own terms.

A `Principal` is produced only by authentication (the API's `get_principal`
dependency; Clerk verification is not built yet). It carries identity only:
the tenant the request acts in and the user making it. Roles are NOT part of
it on purpose: every authorization decision reads the user's membership from
our database at the moment of the decision, never from a token claim or a
value cached at login.
"""

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class Principal:
    tenant_id: UUID
    user_id: UUID
