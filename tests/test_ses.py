from __future__ import annotations

from datetime import datetime, timezone

import boto3
import pytest
from botocore.stub import ANY, Stubber

from steward.mail import (
    MailDeliveryError,
    OutboundMessage,
    RecipientGuard,
    RecipientNotAllowed,
)
from steward.mail.ses import SesInboundReader, SesMailTransport

UTC = timezone.utc


def _ses_client():
    return boto3.client(
        "sesv2",
        region_name="us-east-1",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
        aws_session_token="testing",
    )


def _message(to: str = "vendor@vendors.example") -> OutboundMessage:
    return OutboundMessage(
        to=(to,),
        subject="Quote request",
        body_text="Please provide a quote. No work is authorized.",
        from_address="management@site.example",
        reply_to="case-abcdef123456@site.example",
        message_id="<steward-test-001@site.example>",
    )


def test_ses_transport_is_guarded_and_returns_provider_message_id():
    client = _ses_client()
    stubber = Stubber(client)
    stubber.add_response(
        "send_email",
        {"MessageId": "ses-provider-001"},
        {
            "FromEmailAddress": "management@site.example",
            "Destination": {"ToAddresses": ["vendor@vendors.example"]},
            "Content": {"Raw": {"Data": ANY}},
            "ReplyToAddresses": ["case-abcdef123456@site.example"],
            "ConfigurationSetName": "steward-events",
        },
    )
    transport = SesMailTransport(
        guard=RecipientGuard.of("vendors.example"),
        client=client,
        configuration_set_name="steward-events",
    )

    with stubber:
        assert transport.send(_message()) == "ses-provider-001"


def test_ses_transport_rejects_recipient_before_provider_call():
    client = _ses_client()
    transport = SesMailTransport(
        guard=RecipientGuard.of("vendors.example"),
        client=client,
    )

    with pytest.raises(RecipientNotAllowed):
        transport.send(_message("attacker@evil.example"))


def test_ses_throttling_is_classified_retryable_and_unambiguous():
    client = _ses_client()
    stubber = Stubber(client)
    stubber.add_client_error(
        "send_email",
        service_error_code="TooManyRequestsException",
        service_message="provider detail must not cross the transport boundary",
        http_status_code=429,
        expected_params={
            "FromEmailAddress": "management@site.example",
            "Destination": {"ToAddresses": ["vendor@vendors.example"]},
            "Content": {"Raw": {"Data": ANY}},
            "ReplyToAddresses": ["case-abcdef123456@site.example"],
        },
    )
    transport = SesMailTransport(
        guard=RecipientGuard.of("vendors.example"),
        client=client,
    )

    with stubber, pytest.raises(MailDeliveryError) as excinfo:
        transport.send(_message())

    assert excinfo.value.code == "ses_toomanyrequestsexception"
    assert excinfo.value.retryable is True
    assert excinfo.value.ambiguous is False
    assert "provider detail" not in str(excinfo.value)


class _Body:
    def __init__(self, raw: bytes) -> None:
        self.raw = raw
        self.read_sizes: list[int] = []
        self.closed = False

    def read(self, amount: int):
        self.read_sizes.append(amount)
        return self.raw[:amount]

    def close(self):
        self.closed = True


class _S3Client:
    def __init__(self, body: _Body, *, content_length: object) -> None:
        self.body = body
        self.content_length = content_length

    def get_object(self, **kwargs):
        assert kwargs == {"Bucket": "mail-bucket", "Key": "inbound/message.eml"}
        return {"Body": self.body, "ContentLength": self.content_length}


def _raw_inbound() -> bytes:
    return (
        b"From: vendor@vendors.example\r\n"
        b"To: case-abcdef123456@site.example\r\n"
        b"Subject: Quote\r\n\r\n900 USD all-in."
    )


def test_ses_inbound_reader_retains_only_s3_pointer_to_raw_message():
    received_at = datetime(2026, 9, 7, 10, tzinfo=UTC)
    body = _Body(_raw_inbound())
    inbound = SesInboundReader(
        bucket="mail-bucket",
        client=_S3Client(body, content_length=len(body.raw)),
    ).read(
        "inbound/message.eml",
        received_at=received_at,
    )

    assert inbound.from_address == "vendor@vendors.example"
    assert inbound.body_text == "900 USD all-in."
    assert inbound.raw_ref == "s3://mail-bucket/inbound/message.eml"
    assert body.read_sizes == [10 * 1024 * 1024 + 1]
    assert body.closed is True


def test_ses_inbound_reader_rejects_oversized_metadata_without_reading_body():
    body = _Body(_raw_inbound())
    reader = SesInboundReader(
        bucket="mail-bucket",
        client=_S3Client(body, content_length=33),
        max_message_bytes=32,
    )

    with pytest.raises(ValueError, match="exceeds the configured byte limit"):
        reader.read(
            "inbound/message.eml",
            received_at=datetime(2026, 9, 7, 10, tzinfo=UTC),
        )

    assert body.read_sizes == []
    assert body.closed is True


def test_ses_inbound_reader_bounded_read_rejects_underreported_object_size():
    body = _Body(b"x" * 33)
    reader = SesInboundReader(
        bucket="mail-bucket",
        client=_S3Client(body, content_length=32),
        max_message_bytes=32,
    )

    with pytest.raises(ValueError, match="exceeds the configured byte limit"):
        reader.read(
            "inbound/message.eml",
            received_at=datetime(2026, 9, 7, 10, tzinfo=UTC),
        )

    assert body.read_sizes == [33]
    assert body.closed is True
