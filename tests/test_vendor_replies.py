from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from steward.agents import QuoteExtraction, TriageAction, TriageResult
from steward.config import StewardSettings
from steward.domain.clock import FrozenClock
from steward.domain.enums import CaseStatus, Category, EventKind, Urgency
from steward.domain.models import Vendor
from steward.mail import (
    AddressScheme,
    InboundMessage,
    RecordingMailTransport,
    S3MailNotificationError,
)
from steward.procurement import (
    NO_QUOTE_ARTIFACT_KIND,
    QUOTE_ARTIFACT_KIND,
    VENDOR_EMAIL_ARTIFACT_KIND,
    StoredVendorEmail,
    VendorReplyAuthorizationError,
    VendorReplyConflictError,
    VendorReplyDisposition,
    VendorReplyRouteError,
    VendorReplyService,
)
from steward.runtime import build_runtime
from steward.seed import load_seed

UTC = timezone.utc
SECRET = "vendor-reply-webhook-secret-2026"
CHAT_ID = "-1002481179934"
SCHEME = AddressScheme(
    management_domain="site.narrativenode-labs.cloud",
    vendor_domain="vendors.narrativenode-labs.cloud",
    management_display_name="Steward Property Management",
)
BODY = (
    "Quote is 705 including labour, travel and shoes, and rail alignment "
    "correction if we find it. We can attend tomorrow afternoon."
)


class _ElevatorClassifier:
    def classify(self, *, message, assets, open_cases):
        del message, assets, open_cases
        return TriageResult(
            action=TriageAction.OPEN_CASE,
            category=Category.ELEVATOR,
            urgency=Urgency.HIGH,
            confidence=0.97,
            title="A Block elevator shudders near floor four",
            asset_id="elevator-a",
            rationale="The resident reported an operational elevator fault.",
        )


class _StaticExtractor:
    def __init__(self, result: QuoteExtraction | None = None) -> None:
        self.result = result
        self.calls: list[dict] = []

    def extract(self, **kwargs):
        self.calls.append(kwargs)
        if self.result is None:
            received_at = kwargs["received_at"]
            return QuoteExtraction(
                has_quote=True,
                amount=Decimal("705.00"),
                currency="USD",
                currency_inferred_from_rfq=True,
                amount_evidence="Quote is 705",
                scope_evidence=(
                    "Quote is 705 including labour, travel and shoes, and rail alignment "
                    "correction if we find it."
                ),
                earliest_onsite_at=received_at + timedelta(days=1),
                onsite_evidence="tomorrow afternoon",
            )
        return self.result


class _FailOnceExtractor(_StaticExtractor):
    def extract(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) == 1:
            raise RuntimeError("temporary model failure; raw detail must not be persisted")
        received_at = kwargs["received_at"]
        return QuoteExtraction(
            has_quote=True,
            amount=Decimal("705.00"),
            currency="USD",
            currency_inferred_from_rfq=True,
            amount_evidence="Quote is 705",
            scope_evidence=(
                "Quote is 705 including labour, travel and shoes, and rail alignment "
                "correction if we find it."
            ),
            earliest_onsite_at=received_at + timedelta(days=1),
            onsite_evidence="tomorrow afternoon",
        )


def _update(at: datetime):
    return {
        "update_id": 99001,
        "message": {
            "message_id": 77001,
            "date": int(at.timestamp()),
            "chat": {"id": int(CHAT_ID), "type": "supergroup"},
            "from": {"id": 12345, "is_bot": False, "first_name": "Daniel"},
            "text": "The A Block lift is shuddering near the fourth floor.",
        },
    }


def _prepare_runtime(tmp_path, extractor, *, inbound_reader=None, inbound_enabled=False):
    at = datetime(2026, 9, 7, 10, tzinfo=UTC)
    clock = FrozenClock(at)
    settings = StewardSettings(
        database_path=tmp_path / "vendor-replies.db",
        telegram_enabled=True,
        telegram_webhook_secret=SECRET,
        telegram_allowed_chat_ids=frozenset({CHAT_ID}),
        ses_inbound_enabled=inbound_enabled,
        ses_inbound_bucket="steward-inbound-mail" if inbound_enabled else None,
    )
    runtime = build_runtime(
        settings,
        classifier=_ElevatorClassifier(),
        quote_extractor=extractor,
        clock=clock,
        inbound_reader=inbound_reader,
    )
    runtime.handle_telegram_update(_update(at), secret_header=SECRET)
    report = runtime.tick()
    assert len(report.queued_rfqs) == 1
    assert len(report.outbound.deliveries) == 3
    case = report.queued_rfqs[0].case
    clock.advance(timedelta(hours=4))
    return runtime, clock, case


def _reply(runtime, clock, case, *, body=BODY, message_id="<reply-001@vendor.example>"):
    assert isinstance(runtime.transport, RecordingMailTransport)
    sent = next(
        message
        for message in runtime.transport.sent
        if message.to == ("meridian@vendors.narrativenode-labs.cloud",)
    )
    assert sent.message_id is not None
    return InboundMessage(
        message_id=message_id,
        from_address=sent.to[0],
        from_display_name="Meridian Lift Services",
        to_addresses=(SCHEME.case_reply_address(case.reply_token),),
        cc_addresses=(),
        delivered_to=SCHEME.case_reply_address(case.reply_token),
        subject=f"Re: {sent.subject}",
        body_text=body,
        body_full_text=body + "\n\n> Previous unrelated amount: 9999 USD",
        received_at=clock.now(),
        in_reply_to=sent.message_id,
        references=(sent.message_id,),
        raw_ref="s3://steward-inbound-mail/inbound/reply-001.eml",
    )


def test_vendor_reply_round_trip_persists_email_before_source_backed_quote(tmp_path):
    extractor = _StaticExtractor()
    runtime, clock, case = _prepare_runtime(tmp_path, extractor)
    inbound = _reply(runtime, clock, case)

    result = runtime.handle_vendor_reply(
        inbound,
        source_id="s3:steward-inbound-mail:inbound/reply-001.eml:0001",
    )

    assert result.disposition is VendorReplyDisposition.QUOTE_RECORDED
    assert result.vendor_id == "meridian-lift"
    assert result.quote is not None
    assert result.quote.amount == Decimal("705.00")
    assert result.quote.source_email_message_id == inbound.message_id
    assert result.extraction is not None
    assert len(extractor.calls) == 1

    email_artifact = runtime.store.artifact(
        VENDOR_EMAIL_ARTIFACT_KIND,
        result.email_artifact_id,
    )
    assert email_artifact is not None
    stored = StoredVendorEmail.model_validate(email_artifact.payload)
    assert stored.body_text == BODY
    assert "9999" not in stored.body_text
    assert stored.raw_ref == inbound.raw_ref
    assert stored.rfq_outbox_id
    assert inbound.raw_ref in email_artifact.source_ids

    quote_artifact = runtime.store.artifact(
        QUOTE_ARTIFACT_KIND,
        result.quote_artifact_id,
    )
    assert quote_artifact is not None
    assert quote_artifact.payload["quote"]["amount"] == "705.00"
    assert quote_artifact.payload["extraction"]["amount_evidence"] == "Quote is 705"
    persisted_case = runtime.store.get_case(case.case_id)
    assert persisted_case is not None
    assert persisted_case.status is CaseStatus.QUOTES_RECEIVED
    assert set(persisted_case.contacted_vendor_ids) == {
        "meridian-lift",
        "coastline-elevator",
        "pinnacle-vertical",
    }
    kinds = [event.kind for event in runtime.store.timeline_for(case.case_id)]
    assert kinds.count(EventKind.VENDOR_REPLIED) == 1
    assert kinds.count(EventKind.QUOTE_RECORDED) == 1

    duplicate = runtime.handle_vendor_reply(
        inbound,
        source_id="s3:steward-inbound-mail:inbound/reply-001.eml:duplicate-event",
    )
    assert duplicate.disposition is VendorReplyDisposition.DUPLICATE
    assert duplicate.quote == result.quote
    assert len(extractor.calls) == 1
    with pytest.raises(VendorReplyConflictError):
        runtime.handle_vendor_reply(
            replace(inbound, body_text=BODY + " Changed after delivery."),
            source_id="s3:steward-inbound-mail:inbound/reply-001.eml:tampered",
        )
    assert len(extractor.calls) == 1
    runtime.close()


def test_model_failure_keeps_email_durable_and_retry_records_quote_once(tmp_path):
    extractor = _FailOnceExtractor()
    runtime, clock, case = _prepare_runtime(tmp_path, extractor)
    inbound = _reply(runtime, clock, case)
    source_id = "s3:steward-inbound-mail:inbound/retry.eml:0001"

    with pytest.raises(RuntimeError, match="temporary model failure"):
        runtime.handle_vendor_reply(inbound, source_id=source_id)

    email_artifacts = runtime.store.artifacts_for(
        kind=VENDOR_EMAIL_ARTIFACT_KIND,
        case_id=case.case_id,
    )
    assert len(email_artifacts) == 1
    assert runtime.store.artifacts_for(kind=QUOTE_ARTIFACT_KIND, case_id=case.case_id) == ()
    assert runtime.store.get_case(case.case_id).status is CaseStatus.VENDOR_CONTACTED

    recovered = runtime.handle_vendor_reply(inbound, source_id=source_id)
    assert recovered.disposition is VendorReplyDisposition.QUOTE_RECORDED
    assert len(extractor.calls) == 2
    assert (
        len(runtime.store.artifacts_for(kind=VENDOR_EMAIL_ARTIFACT_KIND, case_id=case.case_id)) == 1
    )
    assert len(runtime.store.artifacts_for(kind=QUOTE_ARTIFACT_KIND, case_id=case.case_id)) == 1
    kinds = [event.kind for event in runtime.store.timeline_for(case.case_id)]
    assert kinds.count(EventKind.VENDOR_REPLIED) == 1
    assert kinds.count(EventKind.QUOTE_RECORDED) == 1
    runtime.close()


@pytest.mark.parametrize(
    ("mutate", "error"),
    [
        (
            lambda inbound, case: replace(
                inbound,
                from_address="attacker@evil.example",
            ),
            VendorReplyAuthorizationError,
        ),
        (
            lambda inbound, case: replace(
                inbound,
                to_addresses=(SCHEME.case_reply_address("11111111"),),
                delivered_to=None,
            ),
            VendorReplyRouteError,
        ),
        (
            lambda inbound, case: replace(
                inbound,
                to_addresses=(
                    SCHEME.case_reply_address(case.reply_token),
                    SCHEME.case_reply_address("11111111"),
                ),
            ),
            VendorReplyRouteError,
        ),
        (
            lambda inbound, case: replace(
                inbound,
                in_reply_to="<unrelated-rfq@site.example>",
                references=(),
            ),
            VendorReplyAuthorizationError,
        ),
    ],
)
def test_foreign_sender_token_conflict_and_wrong_thread_fail_before_model(
    tmp_path,
    mutate,
    error,
):
    extractor = _StaticExtractor()
    runtime, clock, case = _prepare_runtime(tmp_path, extractor)
    inbound = mutate(_reply(runtime, clock, case), case)

    with pytest.raises(error):
        runtime.handle_vendor_reply(inbound, source_id="s3:rejected:1")

    assert extractor.calls == []
    assert (
        runtime.store.artifacts_for(
            kind=VENDOR_EMAIL_ARTIFACT_KIND,
            case_id=case.case_id,
        )
        == ()
    )
    runtime.close()


def test_registered_but_unsent_vendor_cannot_inject_a_quote(tmp_path):
    extractor = _StaticExtractor()
    runtime, clock, case = _prepare_runtime(tmp_path, extractor)
    bundle = load_seed()
    uninvited = Vendor(
        vendor_id="uninvited-lift",
        name="Uninvited Lift Services",
        email="uninvited@vendors.narrativenode-labs.cloud",
        categories=[Category.ELEVATOR],
        allowlisted=True,
        simulated=True,
    )
    service = VendorReplyService(
        store=runtime.store,
        extractor=extractor,
        address_scheme=SCHEME,
        vendors=(*bundle.vendors, uninvited),
    )
    inbound = replace(
        _reply(runtime, clock, case),
        from_address=uninvited.email,
        in_reply_to=None,
        references=(),
    )

    with pytest.raises(VendorReplyAuthorizationError, match="no delivered RFQ"):
        service.ingest(inbound, source_id="s3:uninvited:1")

    assert extractor.calls == []
    runtime.close()


def test_no_quote_reply_is_persisted_without_repeated_model_calls(tmp_path):
    extractor = _StaticExtractor(
        QuoteExtraction(has_quote=False, no_quote_reason="Cannot service this asset.")
    )
    runtime, clock, case = _prepare_runtime(tmp_path, extractor)
    inbound = _reply(
        runtime,
        clock,
        case,
        body="We cannot provide a quote for this work.",
        message_id="<no-quote-001@vendor.example>",
    )

    first = runtime.handle_vendor_reply(inbound, source_id="s3:no-quote:1")
    second = runtime.handle_vendor_reply(inbound, source_id="s3:no-quote:duplicate")

    assert first.disposition is VendorReplyDisposition.NO_QUOTE_RECORDED
    assert second.disposition is VendorReplyDisposition.DUPLICATE
    assert len(extractor.calls) == 1
    assert len(runtime.store.artifacts_for(kind=NO_QUOTE_ARTIFACT_KIND, case_id=case.case_id)) == 1
    assert runtime.store.get_case(case.case_id).status is CaseStatus.VENDOR_CONTACTED
    runtime.close()


class _FakeInboundReader:
    def __init__(self) -> None:
        self.inbound: InboundMessage | None = None
        self.calls: list[tuple[str, datetime]] = []

    def read(self, key: str, *, received_at: datetime) -> InboundMessage:
        self.calls.append((key, received_at))
        if self.inbound is None:
            raise RuntimeError("test inbound message was not configured")
        return replace(self.inbound, received_at=received_at)


def test_runtime_handles_url_encoded_s3_notification_end_to_end(tmp_path):
    extractor = _StaticExtractor()
    reader = _FakeInboundReader()
    runtime, clock, case = _prepare_runtime(
        tmp_path,
        extractor,
        inbound_reader=reader,
        inbound_enabled=True,
    )
    reader.inbound = _reply(runtime, clock, case)
    event_time = clock.now().isoformat().replace("+00:00", "Z")
    event = {
        "Records": [
            {
                "eventSource": "aws:s3",
                "eventName": "ObjectCreated:Put",
                "eventTime": event_time,
                "s3": {
                    "bucket": {"name": "steward-inbound-mail"},
                    "object": {
                        "key": "inbound%2Freply+001.eml",
                        "sequencer": "0000000000000001",
                    },
                },
            }
        ]
    }

    results = runtime.handle_s3_mail_event(event)

    assert len(results) == 1
    assert results[0].disposition is VendorReplyDisposition.QUOTE_RECORDED
    assert reader.calls == [("inbound/reply 001.eml", clock.now())]
    event["Records"][0]["s3"]["bucket"]["name"] = "attacker-mail-bucket"
    with pytest.raises(S3MailNotificationError, match="not allowlisted"):
        runtime.handle_s3_mail_event(event)
    assert len(reader.calls) == 1
    runtime.close()
