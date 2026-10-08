"""Who may read a company file. One rule, used by every file endpoint.

Decision (2026-10-08): **company-wide read**. Every member of a company may
list, preview, download and attach any file of that company: buyers' uploads
(whoever uploaded them) and suppliers' reply attachments. Buyers share files as
a team (an ERP export one colleague uploaded is the input another works from),
and RLS already confines every row to the caller's company. Writing stays with
the uploader: nothing here lets a member change or delete another's file.

Set `COMPANY_WIDE_READ` to False to go back to uploader-only uploads (supplier
attachments have no uploader and stay company-wide either way).
"""

from typing import Final
from uuid import UUID

from bido_back.auth import Principal

COMPANY_WIDE_READ: Final = True


def can_read_upload(uploader_id: UUID, principal: Principal) -> bool:
    """Whether the caller may read an upload of their own company (RLS checked the company)."""
    return COMPANY_WIDE_READ or uploader_id == principal.user_id
