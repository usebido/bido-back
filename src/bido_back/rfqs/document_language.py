"""Which language an RFQ's emails and documents use, as the RFQ API shows it.

The language of an invite's thread is decided once, when its RFQ email is
prepared (`bido_back.rfqs.service.prepare_send`, `invite_language`), and recorded
in the approved document. The API mirrors that:

- an invite whose RFQ email was proposed shows the language recorded in that
  proposal's document (`recorded_languages`);
- a draft invite shows the language the send would pick now (`invite_language`);
- the RFQ itself shows what a supplier with nothing known about it would get
  (`default_languages`): the buyer's own template, else the RFQ creator's
  language, else Bido's standard template.

The buyer's language is the RFQ creator's latest copilot conversation's, so the
decision depends on the RFQ, not on who looks at it.
"""

from collections.abc import Iterable, Sequence
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession

from bido_back.db.models import Approval, CopilotConversation, Rfq, RfqTemplate, Supplier
from bido_back.onboarding.standard import STANDARD_TEMPLATE
from bido_back.rfqs.language import LanguageDecision, decide_email_language
from bido_back.rfqs.send import SEND_RFQ_EMAIL, send_proposal_ids
from bido_back.templates.service import STANDARD_VERSION, ResolvedTemplate

# Proposals whose document still stands: waiting, approved, or sent.
_STANDING = ("pending", "approved", "executed")


def invite_language(
    template: ResolvedTemplate,
    buyer_language: str | None,
    supplier: Supplier,
    contact_email: str | None,
) -> LanguageDecision:
    """The language an invite's thread is written in, decided at its first send.

    The approved document records it (`RfqDocument.language`); every later email
    of the thread reuses it (`bido_back.rfqs.language.thread_language`).
    """
    content = template.content
    own_template = template.template_id is not None
    return decide_email_language(
        supplier_language=supplier.language,
        supplier_country=supplier.country,
        supplier_name=supplier.name,
        email_addresses=(contact_email, supplier.email_domain),
        buyer_template_language=content.language if own_template else None,
        buyer_language=buyer_language,
        standard_template_language=None if own_template else content.language,
    )


async def conversation_languages(
    session: AsyncSession, user_ids: Iterable[UUID]
) -> dict[UUID, str]:
    """Each user's language, from their latest copilot conversation the titler labeled."""
    ids = list(set(user_ids))
    if not ids:
        return {}
    rows = await session.execute(
        select(CopilotConversation.user_id, CopilotConversation.title_language)
        .where(
            CopilotConversation.user_id.in_(ids),
            CopilotConversation.title_language.is_not(None),
        )
        .order_by(CopilotConversation.user_id, CopilotConversation.last_activity_at.desc())
        .ext(distinct_on(CopilotConversation.user_id))
    )
    return {user_id: language for user_id, language in rows if language}


async def recorded_languages(session: AsyncSession, invite_ids: Sequence[UUID]) -> dict[UUID, str]:
    """The language each invite's latest standing RFQ-email proposal was rendered in: the
    one its arguments record, else its document's (`bido_back.rfqs.language.sent_language`)."""
    by_call = {send_proposal_ids(invite_id)[1]: invite_id for invite_id in invite_ids}
    if not by_call:
        return {}
    rows = await session.execute(
        select(
            Approval.tool_call_id,
            func.coalesce(
                Approval.args["language"].astext, Approval.args["document"]["language"].astext
            ),
        )
        .where(
            Approval.tool == SEND_RFQ_EMAIL,
            Approval.tool_call_id.in_(list(by_call)),
            Approval.status.in_(_STANDING),
        )
        .order_by(Approval.created_at)
    )
    # Oldest first, so the latest proposal of an invite wins.
    return {by_call[call_id]: language for call_id, language in rows if language}


async def default_languages(session: AsyncSession, rfqs: Sequence[Rfq]) -> dict[UUID, str]:
    """What a supplier with nothing known about it gets on each RFQ.

    The buyer's own template (the one the RFQ was sent with, else the tenant's
    general default), else the RFQ creator's language, else Bido's standard
    template. A template chosen per item category only applies once the RFQ is
    sent; before that the general default stands in for it.
    """
    pinned = {rfq.template_id for rfq in rfqs if rfq.template_id is not None}
    templates: dict[UUID, str | None] = {}
    if pinned:
        templates = {
            row_id: language
            for row_id, language in await session.execute(
                select(RfqTemplate.id, RfqTemplate.language).where(RfqTemplate.id.in_(pinned))
            )
        }
    general = await session.scalar(
        select(RfqTemplate.language)
        .where(
            RfqTemplate.is_default,
            RfqTemplate.status == "ready",
            RfqTemplate.category.is_(None),
        )
        .limit(1)
    )
    creators = await conversation_languages(session, (rfq.created_by for rfq in rfqs))
    defaults: dict[UUID, str] = {}
    for rfq in rfqs:
        if rfq.template_id is not None:
            own = templates.get(rfq.template_id)
        elif rfq.template_version == STANDARD_VERSION:
            own = None  # Bido's standard template
        else:
            own = general
        defaults[rfq.id] = decide_email_language(
            supplier_language=None,
            supplier_country=None,
            supplier_name="",
            buyer_template_language=own,
            buyer_language=creators.get(rfq.created_by),
            standard_template_language=STANDARD_TEMPLATE.language,
        ).language
    return defaults
