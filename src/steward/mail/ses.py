"""Amazon SES implementation of `MailTransport`.

Sending goes out as raw MIME rather than through the simple-content API so that
threading headers survive. Receiving is not implemented as a poll: SES receipt
rules drop the raw message into S3 and invoke a handler, so inbound mail is
push-driven and a vendor's reply reaches the agent in seconds. The only inbound
code needed here is the part that fetches those bytes and parses them.
"""

from __future__ import annotations

from datetime import datetime

import boto3
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ConnectionClosedError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)

from steward.mail.transport import (
    InboundMessage,
    MailDeliveryError,
    OutboundMessage,
    RecipientGuard,
    build_mime,
    parse_inbound,
)

__all__ = ["SES_MAX_MESSAGE_BYTES", "SesInboundReader", "SesMailTransport"]

SES_MAX_MESSAGE_BYTES = 10 * 1024 * 1024
"""SES rejects raw messages above roughly 10 MB. Fail locally with a clear
message rather than shipping bytes we know will be refused."""


class SesMailTransport:
    """Sends mail through SES v2.

    A `RecipientGuard` is mandatory. Even if every reasoning layer above this
    adapter is subverted, bytes cannot leave for a domain a human did not
    explicitly allow.
    """

    def __init__(
        self,
        *,
        guard: RecipientGuard,
        client=None,
        region_name: str | None = None,
        configuration_set_name: str | None = None,
    ) -> None:
        self._client = (
            client if client is not None else boto3.client("sesv2", region_name=region_name)
        )
        self._guard = guard
        self._configuration_set_name = configuration_set_name

    def send(self, message: OutboundMessage) -> str:
        self._guard.check(message.all_recipients)

        raw = build_mime(message)
        if len(raw) > SES_MAX_MESSAGE_BYTES:
            raise ValueError(
                f"message is {len(raw)} bytes, above the SES limit of {SES_MAX_MESSAGE_BYTES}"
            )

        destination: dict[str, list[str]] = {"ToAddresses": list(message.to)}
        if message.cc:
            destination["CcAddresses"] = list(message.cc)

        request: dict[str, object] = {
            "FromEmailAddress": (
                f"{message.from_display_name} <{message.from_address}>"
                if message.from_display_name
                else message.from_address
            ),
            "Destination": destination,
            "Content": {"Raw": {"Data": raw}},
        }
        if message.reply_to:
            request["ReplyToAddresses"] = [message.reply_to]
        if self._configuration_set_name:
            request["ConfigurationSetName"] = self._configuration_set_name

        try:
            response = self._client.send_email(**request)
        except ClientError as exc:
            provider_code = str(exc.response.get("Error", {}).get("Code", "client_error"))
            code = f"ses_{_safe_provider_code(provider_code)}"
            retryable = provider_code.lower() in {
                "throttlingexception",
                "toomanyrequestsexception",
                "serviceunavailableexception",
                "internalfailure",
            }
            raise MailDeliveryError(
                code,
                retryable=retryable,
                ambiguous=False,
            ) from exc
        except (EndpointConnectionError, ConnectTimeoutError) as exc:
            raise MailDeliveryError(
                "ses_endpoint_unavailable",
                retryable=True,
                ambiguous=False,
            ) from exc
        except (ReadTimeoutError, ConnectionClosedError) as exc:
            raise MailDeliveryError(
                "ses_delivery_outcome_unknown",
                retryable=False,
                ambiguous=True,
            ) from exc
        except BotoCoreError as exc:
            raise MailDeliveryError(
                "ses_transport_outcome_unknown",
                retryable=False,
                ambiguous=True,
            ) from exc
        message_id = response.get("MessageId")
        if not isinstance(message_id, str) or not message_id.strip():
            raise MailDeliveryError(
                "ses_missing_message_id",
                retryable=False,
                ambiguous=True,
            )
        return message_id.strip()


class SesInboundReader:
    """Fetches and boundedly parses raw inbound messages archived to S3."""

    def __init__(
        self,
        *,
        bucket: str,
        client=None,
        region_name: str | None = None,
        max_message_bytes: int = SES_MAX_MESSAGE_BYTES,
    ) -> None:
        if max_message_bytes <= 0:
            raise ValueError("max_message_bytes must be positive")
        self._bucket = bucket
        self._client = client if client is not None else boto3.client("s3", region_name=region_name)
        self._max_message_bytes = max_message_bytes

    def read(self, key: str, *, received_at: datetime) -> InboundMessage:
        """Load one archived message and parse it within the SES size bound.

        `received_at` is passed in rather than read from the message's own `Date`
        header, which is written by the sender and therefore not something to
        base a service-level deadline on.
        """
        response = self._client.get_object(Bucket=self._bucket, Key=key)
        body = response.get("Body")
        if body is None or not callable(getattr(body, "read", None)):
            raise ValueError("S3 response did not contain a readable message body")

        try:
            content_length = response.get("ContentLength")
            if content_length is not None and (
                isinstance(content_length, bool)
                or not isinstance(content_length, int)
                or content_length < 0
            ):
                raise ValueError("S3 response contained an invalid content length")
            if content_length is not None and content_length > self._max_message_bytes:
                raise ValueError("inbound message exceeds the configured byte limit")

            raw = body.read(self._max_message_bytes + 1)
            if not isinstance(raw, (bytes, bytearray)):
                raise ValueError("S3 message body did not return bytes")
            if len(raw) > self._max_message_bytes:
                raise ValueError("inbound message exceeds the configured byte limit")
            if content_length is not None and len(raw) != content_length:
                raise ValueError("S3 message body length did not match metadata")
        finally:
            close = getattr(body, "close", None)
            if callable(close):
                close()

        return parse_inbound(
            bytes(raw),
            received_at=received_at,
            raw_ref=f"s3://{self._bucket}/{key}",
        )


def _safe_provider_code(value: str) -> str:
    cleaned = "".join(character.lower() if character.isalnum() else "_" for character in value)
    return cleaned.strip("_")[:60] or "client_error"
