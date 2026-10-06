"""Email: outbound transports (SMTP for Mailpit, SES), reply addresses, inbound parsing."""

from bido_back.email.types import EmailSender
from bido_back.settings import Settings


def build_email_sender(settings: Settings) -> EmailSender:
    """The SMTP or SES transport selected by `settings.email_transport`.

    The "mailbox" transport is per buyer, not process-wide: see
    `bido_back.rfqs.mailbox.mailbox_senders_for`.
    """
    if settings.email_transport == "ses":
        from bido_back.email.ses import SesEmailSender

        return SesEmailSender(region=settings.aws_region)
    if settings.email_transport == "smtp":
        from bido_back.email.smtp import SmtpEmailSender

        return SmtpEmailSender(settings.smtp_host, settings.smtp_port)
    raise ValueError("EMAIL_TRANSPORT=mailbox has no process-wide sender")


__all__ = ["build_email_sender"]
