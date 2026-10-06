"""Email: outbound transports (SMTP for Mailpit, SES), reply addresses, inbound parsing."""

from bido_back.email.types import EmailSender
from bido_back.settings import Settings


def build_email_sender(settings: Settings) -> EmailSender:
    """The transport selected by `settings.email_transport`."""
    if settings.email_transport == "ses":
        from bido_back.email.ses import SesEmailSender

        return SesEmailSender(region=settings.aws_region)

    from bido_back.email.smtp import SmtpEmailSender

    return SmtpEmailSender(settings.smtp_host, settings.smtp_port)


__all__ = ["build_email_sender"]
