"""Request and response models of `/v1/onboarding` (the frontend's onboarding screens)."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

Provider = Literal["google", "microsoft"]
TemplateSource = Literal["search", "upload", "none"]
TemplateStatus = Literal["building", "ready", "failed"]
ScanStatus = Literal["running", "done", "failed"]


class MailboxSummary(BaseModel):
    provider: Provider
    email_address: str


class TemplateView(BaseModel):
    id: UUID
    source: TemplateSource
    status: TemplateStatus
    language: str | None
    greeting: str | None
    columns: list[str]
    footer: str | None
    fields: list[str]
    # How many emails or files it was learned from (0 for the standard template).
    source_count: int
    is_default: bool
    # A code when `failed`: nothing_readable, no_examples, agent_failed, provider_error, ...
    error: str | None
    created_at: datetime


class OnboardingState(BaseModel):
    onboarded_at: datetime | None
    mailbox: MailboxSummary | None
    default_template: TemplateView | None


class CandidateView(BaseModel):
    """One sent email that may be an RFQ (the frontend's `RfqCandidate`)."""

    id: str
    subject: str
    # First recipient, and how many more ("+2").
    to: str
    more: int
    # "12 set" (pt-BR) and the timestamp behind it.
    date: str
    sent_at: datetime | None
    # "1 anexo · xlsx" (pt-BR) and the count behind it.
    attachments: str
    attachment_count: int
    note: str | None
    preselected: bool


class ScanView(BaseModel):
    id: UUID
    status: ScanStatus
    # Sent emails checked so far, out of at most `limit`.
    checked: int
    limit: int
    found: int
    candidates: list[CandidateView]
    # A code when `failed`: mailbox_revoked, mailbox_not_connected, provider_error, agent_failed.
    error: str | None
    created_at: datetime


class UploadView(BaseModel):
    id: UUID
    filename: str
    size_bytes: int
    ext: str


class CreateTemplateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: TemplateSource
    # source=search: the scan and the candidate ids the user kept ticked.
    scan_id: UUID | None = None
    candidate_ids: list[str] = Field(default_factory=list[str], max_length=10)
    # source=upload: ids from POST /v1/onboarding/uploads.
    upload_ids: list[UUID] = Field(default_factory=list[UUID], max_length=10)
