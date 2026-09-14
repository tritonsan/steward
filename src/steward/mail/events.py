"""Deterministic parsing of S3 notifications produced by SES receipt rules."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import unquote_plus

__all__ = ["S3MailObject", "S3MailNotificationError", "parse_s3_mail_event"]


class S3MailNotificationError(ValueError):
    """An inbound S3 notification is malformed or outside this deployment."""


@dataclass(frozen=True, slots=True)
class S3MailObject:
    bucket: str
    key: str
    received_at: datetime
    source_id: str


def parse_s3_mail_event(
    event: Mapping[str, Any],
    *,
    expected_bucket: str,
) -> tuple[S3MailObject, ...]:
    records = event.get("Records")
    if not isinstance(records, list) or not records:
        raise S3MailNotificationError("S3 notification contains no records")
    expected = expected_bucket.strip()
    if not expected:
        raise ValueError("expected_bucket must not be blank")

    parsed: list[S3MailObject] = []
    for record in records:
        if not isinstance(record, Mapping):
            raise S3MailNotificationError("S3 notification record must be an object")
        if record.get("eventSource") != "aws:s3":
            raise S3MailNotificationError("notification record is not from S3")
        event_name = record.get("eventName")
        if not isinstance(event_name, str) or not event_name.startswith("ObjectCreated:"):
            raise S3MailNotificationError("S3 notification is not an object-created event")
        s3 = record.get("s3")
        if not isinstance(s3, Mapping):
            raise S3MailNotificationError("S3 notification has no s3 object")
        bucket_doc = s3.get("bucket")
        object_doc = s3.get("object")
        if not isinstance(bucket_doc, Mapping) or not isinstance(object_doc, Mapping):
            raise S3MailNotificationError("S3 notification bucket/object is malformed")
        bucket = bucket_doc.get("name")
        encoded_key = object_doc.get("key")
        if bucket != expected:
            raise S3MailNotificationError("S3 notification bucket is not allowlisted")
        if not isinstance(encoded_key, str) or not encoded_key:
            raise S3MailNotificationError("S3 notification object key is missing")
        key = unquote_plus(encoded_key)
        if not key or key.startswith("/") or "\x00" in key:
            raise S3MailNotificationError("S3 notification object key is invalid")

        event_time = record.get("eventTime")
        if not isinstance(event_time, str):
            raise S3MailNotificationError("S3 notification eventTime is missing")
        try:
            received_at = datetime.fromisoformat(event_time.replace("Z", "+00:00"))
        except ValueError as exc:
            raise S3MailNotificationError("S3 notification eventTime is invalid") from exc
        if received_at.tzinfo is None:
            raise S3MailNotificationError("S3 notification eventTime must be timezone-aware")
        received_at = received_at.astimezone(timezone.utc)

        sequencer = object_doc.get("sequencer")
        source_suffix = sequencer if isinstance(sequencer, str) and sequencer else event_name
        parsed.append(
            S3MailObject(
                bucket=bucket,
                key=key,
                received_at=received_at,
                source_id=f"s3:{bucket}:{key}:{source_suffix}",
            )
        )
    return tuple(parsed)
