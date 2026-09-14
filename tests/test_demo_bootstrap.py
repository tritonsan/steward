from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from steward.agents import IntakeService, TriageAction, TriageResult
from steward.demo.bootstrap import bootstrap_demo_store, read_demo_snapshot
from steward.domain.clock import FrozenClock
from steward.domain.enums import CaseStatus, Category, Urgency
from steward.operations import OperationalRunner
from steward.playbooks import default_playbooks
from steward.policy import PolicyEngine
from steward.proactive import ProactiveMaintenanceEngine, SuggestionStatus
from steward.seed import load_seed
from steward.store import SqliteOperationalStore

UTC = timezone.utc
REFERENCE = datetime(2026, 9, 10, 9, tzinfo=UTC)


class Ids:
    def __init__(self) -> None:
        self.value = 0

    def __call__(self, prefix: str) -> str:
        self.value += 1
        return f"{prefix}-snapshot-runner-{self.value:03d}"


class LandscapingClassifier:
    def classify(self, *, message, assets, open_cases):
        del message, assets, open_cases
        return TriageResult(
            action=TriageAction.OPEN_CASE,
            category=Category.LANDSCAPING,
            urgency=Urgency.NORMAL,
            confidence=0.96,
            title="Courtyard branch brushing D Block balconies",
            asset_id="grounds-courtyard",
            rationale="Resident reports a recurring branch contact issue.",
        )


def test_bootstrap_creates_a_full_idempotent_operational_snapshot(tmp_path):
    path = tmp_path / "northgate-demo.db"

    first = bootstrap_demo_store(path, reset=True)

    assert first.history_records == 63
    assert first.active_cases == 10
    assert first.case_status_counts == {
        "awaiting_approval": 2,
        "awaiting_verification": 1,
        "detected": 1,
        "planning": 1,
        "resolved": 1,
        "scheduled": 2,
        "vendor_contacted": 1,
        "warranty_review": 1,
    }
    assert first.timeline_events == 30
    assert first.audit_entries == 13
    assert first.workflow_artifacts == 16
    assert first.pending_outbox == 5
    assert first.pending_inbound == 1
    assert first.spend_by_category == {
        "access_control": "430.00",
        "elevator": "520.00",
        "pool": "580.00",
    }
    assert first.suggestion_status_counts == {"suggested": 4}
    assert first.transitions_applied == 10
    assert first.memory_records_inserted == 63
    assert first.suggestions_inserted == 4
    assert first.outbound_enabled is False

    second = bootstrap_demo_store(path)

    assert second.transitions_applied == 0
    assert second.transitions_replayed == 10
    assert second.memory_records_inserted == 0
    assert second.suggestions_inserted == 0
    assert second.model_dump(
        exclude={
            "transitions_applied",
            "transitions_replayed",
            "memory_records_inserted",
            "suggestions_inserted",
        }
    ) == first.model_dump(
        exclude={
            "transitions_applied",
            "transitions_replayed",
            "memory_records_inserted",
            "suggestions_inserted",
        }
    )
    assert read_demo_snapshot(path).active_cases == 10


def test_snapshot_survives_restart_with_sources_and_no_outbound_delivery(tmp_path):
    path = tmp_path / "restart.db"
    bootstrap_demo_store(path, reset=True)
    store = SqliteOperationalStore(path)

    assert len(store.records()) == 63
    assert all(record.outcome_verified for record in store.records())
    assert all(record.is_simulated for record in store.records())
    assert all(record.verification_source_ids for record in store.records())

    warranty = store.get_case("case-demo-active-005")
    assert warranty is not None
    quote_artifact = store.artifact("quote_snapshot", warranty.accepted_quote_id)
    assert quote_artifact is not None
    assert quote_artifact.payload["vendor_id"] == "coastline-elevator"
    assert quote_artifact.payload["commitment_sent"] is False
    assert warranty.status is CaseStatus.WARRANTY_REVIEW
    assert store.get(warranty.case_id) is None
    warranty_artifact = store.artifacts_for(
        kind="warranty_claim",
        case_id=warranty.case_id,
    )
    assert len(warranty_artifact) == 1

    pending_outbox = store.pending_outbox(limit=100)
    assert len(pending_outbox) == 5
    assert all(item.delivered_at is None for item in pending_outbox)
    assert all(
        item.payload["outbound_channel_selected"] is False
        and item.payload["vendor_contact_allowed"] is False
        for item in pending_outbox
    )

    pending = store.pending_inbound(limit=100)
    assert len(pending) == 1
    serialized = pending[0].model_dump_json()
    assert "username" not in serialized
    assert "user_id" not in serialized
    assert "phone" not in serialized

    suggestions = store.list_suggestions(limit=100)
    assert len(suggestions) == 4
    assert all(item.status is SuggestionStatus.SUGGESTED for item in suggestions)
    assert all(item.vendor_contact_allowed is False for item in suggestions)
    assert all(item.requires_human_review is True for item in suggestions)
    assert store.month_to_date_spend(
        Category.ELEVATOR,
        as_of=REFERENCE,
        currency="USD",
    ) == Decimal("520.00")
    store.close()

    restarted = SqliteOperationalStore(path)
    assert len(restarted.list_cases()) == 10
    assert len(restarted.records()) == 63
    assert len(restarted.list_suggestions(limit=100)) == 4
    assert restarted.case_version("case-demo-active-010") == 1
    restarted.close()


def test_operational_runner_drains_bootstrap_inbox_once_without_sending(tmp_path):
    path = tmp_path / "runner.db"
    bootstrap_demo_store(path, reset=True)
    bundle = load_seed()
    clock = FrozenClock(REFERENCE)
    store = SqliteOperationalStore(path)
    policy = PolicyEngine(bundle.policies, bundle.settings)
    intake = IntakeService(
        classifier=LandscapingClassifier(),
        store=store,
        policy=policy,
        assets=bundle.assets,
        clock=clock,
        id_factory=Ids(),
        reply_token_factory=lambda: "123456abcdef",
    )
    runner = OperationalRunner(
        store=store,
        intake=intake,
        clock=clock,
        proactive=ProactiveMaintenanceEngine(
            policy=policy,
            rules=default_playbooks().proactive_rules,
        ),
        token_factory=lambda: "snapshot-runner-lease",
    )
    before_outbox = store.pending_outbox(limit=100)

    first = runner.tick()
    second = runner.tick()

    assert first.processed_external_ids == ("99501",)
    assert len(first.intake_outcomes) == 1
    assert first.intake_outcomes[0].case is not None
    assert first.intake_outcomes[0].case.category is Category.LANDSCAPING
    assert first.outbound_enabled is False
    assert second.processed_external_ids == ()
    assert second.intake_outcomes == ()
    assert second.outbound_enabled is False
    assert store.pending_inbound() == ()
    assert store.pending_outbox(limit=100) == before_outbox
    assert len(store.list_cases()) == 11
    store.close()

    restarted = SqliteOperationalStore(path)
    assert restarted.pending_inbound() == ()
    assert len(restarted.list_cases()) == 11
    assert len(restarted.pending_outbox(limit=100)) == 5
    restarted.close()
