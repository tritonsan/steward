from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from steward.agents import TriageAction, TriageAssessment, TriageResult
from steward.domain.clock import FrozenClock
from steward.domain.enums import AutonomyLevel, Category, Urgency, group_of
from steward.domain.models import Case, ResidentMessage
from steward.mail import AddressScheme, MailDeliveryError, RecipientGuard, RecordingMailTransport
from steward.operations import (
    EmailOutboxPayload,
    OutboundExecutionMode,
    OutboxDispatcher,
)
from steward.policy import PolicyEngine
from steward.procurement import RfqOutboxCoordinator
from steward.seed import load_seed
from steward.store import OutboxItem, OutboxStatus, SqliteOperationalStore

UTC = timezone.utc
SCHEME = AddressScheme(
    management_domain="site.narrativenode-labs.cloud",
    vendor_domain="vendors.narrativenode-labs.cloud",
)


def _open_case(store: SqliteOperationalStore, at: datetime) -> Case:
    message = ResidentMessage(
        message_id="tg:test:rfq-1",
        source="telegram_shadow",
        chat_id="test",
        sender_display="Daniel K.",
        text="The A Block elevator is shuddering near the fourth floor.",
        sent_at=at,
        ingested_at=at,
        case_id="case-outbox-rfq-001",
    )
    triage = TriageResult(
        action=TriageAction.OPEN_CASE,
        category=Category.ELEVATOR,
        urgency=Urgency.HIGH,
        confidence=0.96,
        title="A Block elevator shudders near floor four",
        asset_id="elevator-a",
        rationale="A resident reported an operational elevator fault.",
    )
    assessment = TriageAssessment(
        message_id=message.message_id,
        assessed_at=at,
        result=triage,
    )
    case = Case(
        case_id="case-outbox-rfq-001",
        reply_token="abcdef123456",
        title=triage.title,
        category=triage.category,
        group=group_of(triage.category),
        urgency=triage.urgency,
        asset_id=triage.asset_id,
        opened_at=at,
        updated_at=at,
        source_message_ids=[message.message_id],
        autonomy_level=AutonomyLevel.AUTONOMOUS,
        spend_cap=Decimal("1500.00"),
    )
    assert store.record_intake(
        message=message,
        assessment=assessment,
        case=case,
        new_case=True,
    )
    return case


def _queue(store: SqliteOperationalStore, clock: FrozenClock):
    bundle = load_seed()
    guard = RecipientGuard.of(SCHEME.vendor_domain)
    coordinator = RfqOutboxCoordinator(
        store=store,
        policy=PolicyEngine(bundle.policies, bundle.settings),
        recipient_guard=guard,
        address_scheme=SCHEME,
        clock=clock,
    )
    case = _open_case(store, clock.now())
    queued = coordinator.queue(
        case=case,
        expected_version=1,
        idempotency_key="test:durable-rfq:v1",
        triage_confidence=0.96,
        asset=bundle.asset("elevator-a"),
        vendors=bundle.vendors,
    )
    return bundle, guard, coordinator, case, queued


def test_durable_rfq_is_audited_then_recorded_from_typed_outbox(tmp_path):
    at = datetime(2026, 9, 7, 10, tzinfo=UTC)
    clock = FrozenClock(at)
    store = SqliteOperationalStore(tmp_path / "durable-rfq.db")
    _bundle, guard, coordinator, case, queued = _queue(store, clock)

    assert queued.transition.applied is True
    assert queued.queued_vendor_ids == (
        "meridian-lift",
        "coastline-elevator",
        "pinnacle-vertical",
    )
    assert len(store.pending_outbox()) == 3
    assert all(entry.action == "send_rfq" for entry in store.audit_for(case.case_id))
    payloads = [EmailOutboxPayload.model_validate(item.payload) for item in queued.outbox_items]
    assert all(payload.message.message_id.startswith("<steward-") for payload in payloads)
    assert len({payload.message.message_id for payload in payloads}) == 3

    # A generic workflow intent has no selected physical channel and must not be
    # consumed or failed by the mail dispatcher.
    current = store.get_case(case.case_id)
    version = store.case_version(case.case_id)
    assert current is not None and version is not None
    generic = OutboxItem(
        outbox_id="outbox-generic-verification",
        case_id=case.case_id,
        dedup_key="generic:verification:1",
        kind="request_completion_verification",
        payload={"outbound_channel_selected": False},
        created_at=at,
    )
    store.save_transition(
        case=current,
        expected_version=version,
        idempotency_key="test:add-generic-intent",
        outbox_items=(generic,),
    )

    transport = RecordingMailTransport(guard=guard)
    dispatcher = OutboxDispatcher(
        store=store,
        transport=transport,
        recipient_guard=guard,
        clock=clock,
        execution_mode=OutboundExecutionMode.DRY_RUN,
    )
    report = dispatcher.dispatch()

    assert report.real_delivery_enabled is False
    assert len(report.deliveries) == 3
    assert report.failures == ()
    assert len(transport.sent) == 3
    assert store.pending_outbox() == (generic,)
    assert store.outbox_item(generic.outbox_id) == generic
    for item in queued.outbox_items:
        delivered = store.outbox_item(item.outbox_id)
        assert delivered is not None
        assert delivered.status is OutboxStatus.DELIVERED
        assert delivered.provider_message_id is not None
        assert delivered.attempts == 1

    repeated = coordinator.queue(
        case=case,
        expected_version=1,
        idempotency_key="test:durable-rfq:v1",
        triage_confidence=0.10,
        asset=_bundle.asset("elevator-a"),
        vendors=tuple(
            vendor
            for vendor in _bundle.vendors
            if vendor.vendor_id != "coastline-elevator"
        ),
    )
    assert repeated.transition.applied is False
    assert repeated.outbox_items == tuple(
        store.outbox_item(item.outbox_id) for item in queued.outbox_items
    )
    store.close()


class _FlakyTransport:
    def __init__(self, *, ambiguous: bool = False) -> None:
        self.calls = 0
        self.ambiguous = ambiguous

    def send(self, message):
        del message
        self.calls += 1
        if self.calls == 1:
            raise MailDeliveryError(
                "test_delivery_failure",
                retryable=not self.ambiguous,
                ambiguous=self.ambiguous,
            )
        return "provider-success-001"


def test_retryable_transport_failure_retries_with_persisted_attempt_count(tmp_path):
    at = datetime(2026, 9, 7, 10, tzinfo=UTC)
    clock = FrozenClock(at)
    store = SqliteOperationalStore(tmp_path / "retry-rfq.db")
    _bundle, guard, _coordinator, _case, _queued = _queue(store, clock)
    transport = _FlakyTransport()
    dispatcher = OutboxDispatcher(
        store=store,
        transport=transport,
        recipient_guard=guard,
        clock=clock,
        execution_mode=OutboundExecutionMode.LIVE_RFQ,
        batch_limit=1,
        max_attempts=2,
    )

    first = dispatcher.dispatch()
    assert first.failures[0].disposition.value == "retry_pending"
    failed_id = first.failures[0].outbox_id
    pending = store.outbox_item(failed_id)
    assert pending is not None
    assert pending.status is OutboxStatus.PENDING
    assert pending.attempts == 1
    assert pending.last_error_code == "test_delivery_failure"

    second = dispatcher.dispatch()
    assert second.deliveries[0].attempts == 2
    delivered = store.outbox_item(failed_id)
    assert delivered is not None
    assert delivered.status is OutboxStatus.DELIVERED
    assert delivered.attempts == 2
    assert delivered.provider_message_id == "provider-success-001"
    store.close()


def test_ambiguous_transport_failure_is_never_blindly_retried(tmp_path):
    at = datetime(2026, 9, 7, 10, tzinfo=UTC)
    clock = FrozenClock(at)
    store = SqliteOperationalStore(tmp_path / "ambiguous-rfq.db")
    _bundle, guard, _coordinator, _case, _queued = _queue(store, clock)
    dispatcher = OutboxDispatcher(
        store=store,
        transport=_FlakyTransport(ambiguous=True),
        recipient_guard=guard,
        clock=clock,
        execution_mode=OutboundExecutionMode.LIVE_RFQ,
        batch_limit=1,
    )

    report = dispatcher.dispatch()

    assert report.failures[0].disposition.value == "ambiguous"
    item = store.outbox_item(report.failures[0].outbox_id)
    assert item is not None
    assert item.status is OutboxStatus.AMBIGUOUS
    assert item.last_error_code == "test_delivery_failure"
    second = dispatcher.dispatch()
    assert all(delivery.outbox_id != item.outbox_id for delivery in second.deliveries)
    unchanged = store.outbox_item(item.outbox_id)
    assert unchanged is not None
    assert unchanged.status is OutboxStatus.AMBIGUOUS
    assert unchanged.attempts == 1
    store.close()
