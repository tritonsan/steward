from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from steward.agents import TriageAction, TriageResult
from steward.config import RuntimeExecutionMode, StewardSettings
from steward.domain.clock import FrozenClock
from steward.domain.enums import Category, Urgency
from steward.mail import RecordingMailTransport
from steward.runtime import build_runtime
from steward.store import OutboxStatus

UTC = timezone.utc
SECRET = "runtime-webhook-secret-2026"
CHAT_ID = "-1002481179934"


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
            rationale="The resident reported a specific operational elevator fault.",
        )


def _update(at: datetime):
    return {
        "update_id": 88001,
        "message": {
            "message_id": 71001,
            "date": int(at.timestamp()),
            "chat": {"id": int(CHAT_ID), "type": "supergroup"},
            "from": {"id": 123456, "is_bot": False, "first_name": "Daniel"},
            "text": "The A Block lift is shuddering again near the fourth floor.",
        },
    }


def test_runtime_dry_run_closes_inbound_to_guarded_recorded_rfq_slice(tmp_path):
    at = datetime(2026, 9, 7, 10, tzinfo=UTC)
    clock = FrozenClock(at)
    settings = StewardSettings(
        database_path=tmp_path / "runtime.db",
        execution_mode=RuntimeExecutionMode.DRY_RUN,
        telegram_enabled=True,
        telegram_webhook_secret=SECRET,
        telegram_allowed_chat_ids=frozenset({CHAT_ID}),
    )

    with build_runtime(settings, classifier=_ElevatorClassifier(), clock=clock) as runtime:
        accepted = runtime.handle_telegram_update(
            _update(at),
            secret_header=SECRET,
        )
        assert accepted.message is not None

        report = runtime.tick()

        assert report.inbound.processed_external_ids == ("88001",)
        assert report.inbound.failures == ()
        assert len(report.queued_rfqs) == 1
        assert report.queued_rfqs[0].case.asset_id == "elevator-a"
        assert len(report.queued_rfqs[0].outbox_items) == 3
        assert len(report.outbound.deliveries) == 3
        assert report.outbound.failures == ()
        assert report.outbound.real_delivery_enabled is False
        assert isinstance(runtime.transport, RecordingMailTransport)
        assert len(runtime.transport.sent) == 3
        assert all(
            message.to[0].endswith("@vendors.narrativenode-labs.cloud")
            for message in runtime.transport.sent
        )
        assert all(
            "No work is authorized by this email." in message.body_text
            for message in runtime.transport.sent
        )
        assert all(
            runtime.store.outbox_item(item.outbox_id).status is OutboxStatus.DELIVERED
            for item in report.queued_rfqs[0].outbox_items
        )

        second = runtime.tick()
        assert second.inbound.processed_external_ids == ()
        assert second.queued_rfqs == ()
        assert second.outbound.deliveries == ()
        assert len(runtime.transport.sent) == 3


def test_live_modes_fail_fast_without_required_ses_and_commitment_ack():
    with pytest.raises(ValidationError, match="SES configuration set"):
        StewardSettings(execution_mode=RuntimeExecutionMode.LIVE_RFQ)

    with pytest.raises(ValidationError, match="allow_live_commitments"):
        StewardSettings(
            execution_mode=RuntimeExecutionMode.LIVE_COMMITMENT,
            ses_configuration_set_name="steward-events",
        )

    configured = StewardSettings(
        execution_mode=RuntimeExecutionMode.LIVE_COMMITMENT,
        ses_configuration_set_name="steward-events",
        allow_live_commitments=True,
    )
    assert configured.execution_mode is RuntimeExecutionMode.LIVE_COMMITMENT


def test_environment_style_lists_are_parsed_and_normalized():
    settings = StewardSettings(
        vendor_domains="VENDORS.EXAMPLE, backup.vendors.example",
        auto_queue_rfq_categories="elevator,plumbing",
        telegram_allowed_chat_ids="-1001, -1002",
    )

    assert settings.vendor_domains == (
        "vendors.example",
        "backup.vendors.example",
    )
    assert settings.auto_queue_rfq_categories == frozenset(
        {Category.ELEVATOR, Category.PLUMBING}
    )
    assert settings.telegram_allowed_chat_ids == frozenset({"-1001", "-1002"})
