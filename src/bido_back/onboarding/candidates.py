"""Sent-mail candidates: what the scan keeps, and how the frontend shows them.

A candidate is stored in `template_scans.candidates` as metadata only:

    {"id": "c3", "provider_message_id": "...", "subject": "...",
     "recipients": ["vendas@metalflex.com.br", ...], "sent_at": "<iso>|null",
     "attachments": [{"name": "itens.xlsx", "content_type": "..."}],
     "note": "Parece uma resposta, não um RFQ" | null, "preselected": true}

`id` is ours ("c<n>", per scan); the frontend only ever sends these ids back,
and the build job reads only provider messages listed in the scan, so a
client can never make Bido read an arbitrary message.

The model sees even less: the subject, how many recipients and their
domains, attachment names and the date (`classifier_input`).
"""

from collections.abc import Sequence
from datetime import datetime
from pathlib import PurePath
from typing import Final, TypedDict, cast

from bido_back.agents.catalog.contracts import (
    RfqCandidate,
    RfqCandidateClassifierInput,
    RfqCandidateVerdict,
)
from bido_back.mailbox.base import SentSummary

# The candidates list the user picks from; the scan stops adding beyond it.
MAX_CANDIDATES: Final = 60
_MONTHS_PT: Final = (
    "jan",
    "fev",
    "mar",
    "abr",
    "mai",
    "jun",
    "jul",
    "ago",
    "set",
    "out",
    "nov",
    "dez",
)


class StoredAttachment(TypedDict):
    name: str
    content_type: str


class StoredCandidate(TypedDict):
    id: str
    provider_message_id: str
    subject: str
    recipients: list[str]
    sent_at: str | None
    attachments: list[StoredAttachment]
    note: str | None
    preselected: bool


def classifier_input(
    ids: Sequence[str], summaries: Sequence[SentSummary]
) -> RfqCandidateClassifierInput:
    return RfqCandidateClassifierInput(
        candidates=[
            RfqCandidate(
                id=candidate_id,
                subject=summary.subject[:300],
                recipient_count=len(summary.recipients),
                recipient_domains=sorted(
                    {r.rsplit("@", 1)[-1].lower() for r in summary.recipients if "@" in r}
                )[:10],
                attachments=[a.name[:120] for a in summary.attachments][:10],
                sent_on=summary.sent_at.date() if summary.sent_at else None,
            )
            for candidate_id, summary in zip(ids, summaries, strict=True)
        ]
    )


def keep(
    candidate_id: str, summary: SentSummary, verdict: RfqCandidateVerdict | None
) -> StoredCandidate | None:
    """The stored candidate, or None when the model is sure it is not an RFQ.

    Kept: likely RFQs (pre-ticked when the model is confident) and doubtful
    ones (with the model's note, not pre-ticked). Without a verdict for the
    id (the model skipped it), the candidate is kept as doubtful.
    """
    if verdict is not None and not verdict.is_rfq and verdict.confident:
        return None
    confident_rfq = verdict is not None and verdict.is_rfq and verdict.confident
    note = None if confident_rfq else _note(verdict)
    return StoredCandidate(
        id=candidate_id,
        provider_message_id=summary.provider_message_id,
        subject=summary.subject,
        recipients=list(summary.recipients),
        sent_at=summary.sent_at.isoformat() if summary.sent_at else None,
        attachments=[
            StoredAttachment(name=a.name, content_type=a.content_type) for a in summary.attachments
        ],
        note=note,
        preselected=confident_rfq,
    )


def _note(verdict: RfqCandidateVerdict | None) -> str:
    if verdict is not None and verdict.note:
        return verdict.note.strip()[:160]
    return "Não temos certeza de que é um RFQ"


def stored(raw: object) -> list[StoredCandidate]:
    """The candidates of a scan row (rows are written only by the scan job)."""
    if not isinstance(raw, list):
        return []
    return [cast(StoredCandidate, c) for c in cast(list[object], raw) if isinstance(c, dict)]


def ordered(candidates: Sequence[StoredCandidate]) -> list[StoredCandidate]:
    """Newest first; capped at MAX_CANDIDATES."""
    return sorted(candidates, key=lambda c: c["sent_at"] or "", reverse=True)[:MAX_CANDIDATES]


# --- Display (pt-BR, as the onboarding screens show them) ----------------------------------


def display_date(sent_at: str | None) -> str:
    """ "12 set" (this year's format in the screens), or "" when unknown."""
    if not sent_at:
        return ""
    when = datetime.fromisoformat(sent_at)
    return f"{when.day} {_MONTHS_PT[when.month - 1]}"


def display_attachments(attachments: Sequence[StoredAttachment]) -> str:
    """ "sem anexo", "1 anexo · xlsx", "2 anexos · xlsx, pdf"."""
    if not attachments:
        return "sem anexo"
    kinds: list[str] = []
    for attachment in attachments:
        ext = PurePath(attachment["name"]).suffix.lstrip(".").lower()
        if ext and ext not in kinds:
            kinds.append(ext)
    count = len(attachments)
    label = f"{count} {'anexo' if count == 1 else 'anexos'}"
    return f"{label} · {', '.join(kinds)}" if kinds else label
