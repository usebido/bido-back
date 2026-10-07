"""How the RFQ flow picks a sender: EMAIL_TRANSPORT, and mailbox errors as HTTP. No database."""

from typing import cast
from uuid import uuid4

import pytest
from fastapi import status
from sqlalchemy.ext.asyncio import AsyncSession

from bido_back import mailbox
from bido_back.api.rfqs import rfq_http_error
from bido_back.email.smtp import SmtpEmailSender
from bido_back.rfqs.errors import MailboxNotConfigured, MailboxNotConnected
from bido_back.rfqs.mailbox import default_mailbox_sender, mailbox_senders_for
from bido_back.settings import Settings

UNUSED_SESSION = cast(AsyncSession, object())


def settings(**overrides: object) -> Settings:
    base: dict[str, object] = {"database_url": "postgresql://app@localhost:5432/unused"}
    return Settings.model_validate({**base, **overrides})


async def test_smtp_transport_sends_every_rfq_email_through_mailpit_locally() -> None:
    factory = mailbox_senders_for(settings(environment="local", email_transport="smtp"))

    sender = await factory(UNUSED_SESSION, uuid4(), uuid4())

    assert isinstance(sender, SmtpEmailSender)


@pytest.mark.parametrize("environment", ["dev", "prd"])
@pytest.mark.parametrize("transport", ["smtp", "ses"])
def test_non_mailbox_transports_are_refused_outside_local_and_test(
    environment: str, transport: str
) -> None:
    with pytest.raises(ValueError, match="local and test runs only"):
        mailbox_senders_for(settings(environment=environment, email_transport=transport))


def test_mailbox_is_the_default_transport() -> None:
    assert mailbox_senders_for(settings(environment="prd")) is default_mailbox_sender


@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        (mailbox.MailboxNotConnected("x"), MailboxNotConnected),
        (mailbox.MailboxNotConfigured("x"), MailboxNotConfigured),
    ],
)
async def test_mailbox_errors_become_rfq_errors(
    monkeypatch: pytest.MonkeyPatch, raised: Exception, expected: type[Exception]
) -> None:
    async def failing(*_: object, **__: object) -> object:
        raise raised

    monkeypatch.setattr(mailbox, "get_mailbox_sender", failing)

    with pytest.raises(expected):
        await default_mailbox_sender(UNUSED_SESSION, uuid4(), uuid4())


def test_mailbox_not_configured_is_a_conflict_not_a_server_error() -> None:
    error = rfq_http_error(MailboxNotConfigured("mailbox sending is not configured"))

    assert error.status_code == status.HTTP_409_CONFLICT
    assert "not set up" in str(error.detail)
