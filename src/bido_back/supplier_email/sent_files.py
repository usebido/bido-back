"""Keep the files Bido attached to an email it sent, so the thread shows them.

An email that leaves a buyer's mailbox (the RFQ with its PDF, the purchase order
with its PDF and .xlsx) is stored as an outbound `messages` row. Its attachments
go where a supplier's attachments go: the exact bytes that were sent, in blob
storage under `attachments/<tenant>/<message>/<n>-<name>`, and one `attachments`
row each (tenant-scoped, forced RLS), so the invite's thread
(`bido_back.rfqs.messages`) lists them and `/v1/files/{id}/download` serves them.
The file library and its content index list only what suppliers sent
(`messages.direction = 'in'`).

Keeping them is best effort: the email has already gone out when this runs, and
the record of that send matters more than a copy of its files. A file that cannot
be stored is left out of the thread and logged with ids only.
"""

import logging
from collections.abc import Sequence
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from bido_back.db.models import Attachment
from bido_back.email.types import OutboundAttachment
from bido_back.intake.service import safe_filename
from bido_back.storage import BlobStorage

logger = logging.getLogger(__name__)


async def keep_sent_attachments(
    session: AsyncSession,
    storage: BlobStorage | None,
    tenant_id: UUID,
    message_row_id: UUID,
    attachments: Sequence[OutboundAttachment],
) -> int:
    """Store each sent file and add its `attachments` row; the number kept.

    Call it in the transaction that inserts the outbound `messages` row, only when
    that insert added the row (a retry that found it stored adds nothing). Without
    storage (some tests and scripts), nothing is kept.
    """
    if storage is None:
        return 0
    kept = 0
    for index, attachment in enumerate(attachments, start=1):
        key = (
            f"attachments/{tenant_id}/{message_row_id}/{index}-{safe_filename(attachment.filename)}"
        )
        try:
            await storage.put(key, attachment.data, attachment.content_type)
        except Exception as exc:
            logger.warning(
                "sent attachment not kept tenant=%s message=%s index=%d error=%s",
                tenant_id,
                message_row_id,
                index,
                type(exc).__name__,
            )
            continue
        session.add(
            Attachment(
                tenant_id=tenant_id,
                message_id=message_row_id,
                filename=attachment.filename,
                content_type=attachment.content_type,
                size_bytes=len(attachment.data),
                storage_key=key,
            )
        )
        kept += 1
    return kept
