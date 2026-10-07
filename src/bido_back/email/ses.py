"""Sending through Amazon SES (API v2, raw MIME)."""

import asyncio
from collections.abc import Mapping
from typing import Protocol, cast

from bido_back.email.mime import build_mime
from bido_back.email.types import OutboundEmail, SentEmail


class _SesV2Api(Protocol):
    """The slice of the boto3 sesv2 client used here (boto3 ships no type stubs)."""

    def send_email(self, **kwargs: object) -> Mapping[str, object]: ...


class SesEmailSender:
    """`EmailSender` over SES `SendEmail` with `Content.Raw`.

    SES replaces the Message-ID header we set with its own,
    `<SES-MessageId@<region>.amazonses.com>` (`email.amazonses.com` in
    us-east-1). The returned `SentEmail.message_id` is that SES value, because
    it is what the supplier's mail client will put in In-Reply-To; store it as
    the thread's Message-ID. (Matching by reply token does not depend on it.)
    """

    def __init__(
        self,
        *,
        region: str,
        configuration_set: str | None = None,
        ses: _SesV2Api | None = None,
    ) -> None:
        self._region = region
        self._configuration_set = configuration_set
        self._ses = ses or _boto3_sesv2(region)

    async def send(self, email: OutboundEmail) -> SentEmail:
        msg = build_mime(email)
        raw = msg.as_bytes()
        kwargs: dict[str, object] = {
            "FromEmailAddress": email.from_address,
            # Envelope recipients: To and Cc.
            "Destination": {"ToAddresses": list(email.to), "CcAddresses": list(email.cc)},
            "Content": {"Raw": {"Data": raw}},
        }
        if self._configuration_set:
            kwargs["ConfigurationSetName"] = self._configuration_set
        response = await asyncio.to_thread(self._ses.send_email, **kwargs)
        ses_id = response.get("MessageId")
        message_id = ses_message_id(str(ses_id), self._region) if ses_id else str(msg["Message-ID"])
        return SentEmail(message_id=message_id, raw=raw)


def ses_message_id(ses_id: str, region: str) -> str:
    host = "email.amazonses.com" if region == "us-east-1" else f"{region}.amazonses.com"
    return f"<{ses_id}@{host}>"


def _boto3_sesv2(region: str) -> _SesV2Api:
    import boto3  # pyright: ignore[reportMissingTypeStubs]

    return cast(_SesV2Api, boto3.client("sesv2", region_name=region))  # pyright: ignore[reportUnknownMemberType]
