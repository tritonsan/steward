"""Build and inspect a deterministic, fully populated Northgate demo database.

The bootstrap is intentionally offline.  It writes synthetic history, active
case state, timeline/audit records, channel-neutral outbox intents, workflow
artifacts, spend entries, normalized inbox examples, and proactive suggestions.
It never constructs a Telegram or mail client and never dispatches an outbox
item.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from steward.domain.enums import (
    ActorType,
    CaseStatus,
    Category,
    EventKind,
    Urgency,
    group_of,
)
from steward.domain.models import AuditEntry, Case, ResidentMessage, TimelineEvent, UtcDatetime
from steward.playbooks import default_playbooks
from steward.policy import PolicyEngine
from steward.proactive import ProactiveMaintenanceEngine
from steward.seed import default_seed_dir, load_manifest, load_seed
from steward.store import (
    InboxItem,
    InboxStatus,
    OutboxItem,
    SpendEntry,
    SqliteOperationalStore,
    WorkflowArtifact,
)

__all__ = [
    "DemoBootstrapReport",
    "bootstrap_demo_store",
    "read_demo_snapshot",
]


class _ActiveCaseSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    category: Category
    asset_id: str | None = None
    urgency: Urgency
    status: CaseStatus
    opened_at: UtcDatetime
    updated_at: UtcDatetime
    next_action_due_at: UtcDatetime | None = None
    scheduled_for: UtcDatetime | None = None
    resolved_at: UtcDatetime | None = None
    escalated_at: UtcDatetime | None = None
    contacted_vendor_ids: tuple[str, ...] = ()
    accepted_quote_id: str | None = None
    follow_up_count: int = Field(default=0, ge=0)
    event_kind: EventKind
    event_summary: str = Field(min_length=1)
    outbox_kind: str | None = None
    artifact_kind: str | None = None
    spend_amount: Decimal | None = Field(default=None, gt=0)
    spend_vendor_id: str | None = None
    resolution_notes: str = ""

    @model_validator(mode="after")
    def _consistent(self) -> _ActiveCaseSpec:
        if self.updated_at < self.opened_at:
            raise ValueError("active case update cannot precede opening")
        if self.resolved_at is not None and self.resolved_at < self.opened_at:
            raise ValueError("active case resolution cannot precede opening")
        if (self.spend_amount is None) != (self.spend_vendor_id is None):
            raise ValueError("demo spend amount and vendor must be supplied together")
        if self.spend_vendor_id and self.spend_vendor_id not in self.contacted_vendor_ids:
            raise ValueError("demo spend vendor must have been contacted")
        return self


class DemoBootstrapReport(BaseModel):
    """Stable, JSON-ready summary of the operational demo database."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    database_path: str
    reference_at: UtcDatetime
    history_records: int
    active_cases: int
    case_status_counts: dict[str, int]
    timeline_events: int
    audit_entries: int
    workflow_artifacts: int
    pending_outbox: int
    pending_inbound: int
    due_case_ids: tuple[str, ...]
    spend_by_category: dict[str, str]
    suggestion_status_counts: dict[str, int]
    transitions_applied: int = 0
    transitions_replayed: int = 0
    memory_records_inserted: int = 0
    suggestions_inserted: int = 0
    outbound_enabled: Literal[False] = False


class _Ids:
    __slots__ = ()

    @staticmethod
    def reply_token(case_id: str) -> str:
        return hashlib.sha256(case_id.encode()).hexdigest()[:16]

    @staticmethod
    def event(case_id: str, suffix: str) -> str:
        return f"event-bootstrap-{case_id}-{suffix}"

    @staticmethod
    def audit(case_id: str, suffix: str) -> str:
        return f"audit-bootstrap-{case_id}-{suffix}"


_ARTIFACT_KINDS = {
    "demo_case_state",
    "quote_snapshot",
    "verification_request",
    "warranty_claim",
    "verification_response",
}


def bootstrap_demo_store(
    path: str | Path,
    *,
    reset: bool = False,
    seed_dir: str | Path | None = None,
) -> DemoBootstrapReport:
    """Create or idempotently refresh the offline Northgate demo database."""
    database = Path(path).expanduser().resolve()
    database.parent.mkdir(parents=True, exist_ok=True)
    if reset:
        _remove_sqlite_files(database)

    root = Path(seed_dir).expanduser().resolve() if seed_dir else default_seed_dir()
    bundle = load_seed(root)
    manifest = load_manifest(root)
    reference_at = _parse_reference(bundle.reference_date)
    specs = tuple(_ActiveCaseSpec.model_validate(item) for item in manifest["active_cases"])
    _validate_specs(specs, bundle=bundle)

    policy = PolicyEngine(bundle.policies, bundle.settings)
    store = SqliteOperationalStore(
        database,
        recurrence_window_days=bundle.settings.recurrence_window_days,
    )
    memory_inserted = 0
    applied = 0
    replayed = 0
    suggestions_inserted = 0
    try:
        for record in bundle.history:
            memory_inserted += int(store.write(record))

        for spec in specs:
            result = _write_active_case(store=store, spec=spec, policy=policy)
            if result:
                applied += 1
            else:
                replayed += 1

        _write_inbox_examples(store=store, reference_at=reference_at)

        engine = ProactiveMaintenanceEngine(
            policy=policy,
            rules=default_playbooks().proactive_rules,
        )
        for suggestion in engine.suggest(store.records(), as_of=reference_at):
            suggestions_inserted += int(store.upsert_suggestion(suggestion))
        store.expire_suggestions(now=reference_at)

        snapshot = _snapshot(
            store=store,
            database=database,
            reference_at=reference_at,
        )
        return snapshot.model_copy(
            update={
                "transitions_applied": applied,
                "transitions_replayed": replayed,
                "memory_records_inserted": memory_inserted,
                "suggestions_inserted": suggestions_inserted,
            }
        )
    finally:
        store.close()


def read_demo_snapshot(
    path: str | Path,
    *,
    reference_at: datetime | None = None,
) -> DemoBootstrapReport:
    """Read a demo database without changing it."""
    database = Path(path).expanduser().resolve()
    bundle = load_seed()
    at = reference_at or _parse_reference(bundle.reference_date)
    store = SqliteOperationalStore(database)
    try:
        return _snapshot(store=store, database=database, reference_at=at)
    finally:
        store.close()


def _write_active_case(
    *,
    store: SqliteOperationalStore,
    spec: _ActiveCaseSpec,
    policy: PolicyEngine,
) -> bool:
    decision = policy.evaluate_intake(category=spec.category, triage_confidence=0.95)
    source_id = f"demo-source:{spec.case_id}"
    case = Case(
        case_id=spec.case_id,
        reply_token=_Ids.reply_token(spec.case_id),
        title=spec.title,
        category=spec.category,
        group=group_of(spec.category),
        urgency=spec.urgency,
        status=spec.status,
        asset_id=spec.asset_id,
        opened_at=spec.opened_at,
        updated_at=spec.updated_at,
        source_message_ids=[source_id],
        autonomy_level=decision.level,
        spend_cap=decision.spend_cap,
        currency=decision.currency,
        contacted_vendor_ids=list(spec.contacted_vendor_ids),
        accepted_quote_id=spec.accepted_quote_id,
        next_action_due_at=spec.next_action_due_at,
        follow_up_count=spec.follow_up_count,
        escalated_at=spec.escalated_at,
        scheduled_for=spec.scheduled_for,
        resolved_at=spec.resolved_at,
        total_cost=(spec.spend_amount if spec.status is CaseStatus.RESOLVED else None),
        resolution_notes=spec.resolution_notes,
    )
    policy_at = min(spec.opened_at + timedelta(minutes=1), spec.updated_at)
    timeline = (
        TimelineEvent(
            event_id=_Ids.event(spec.case_id, "opened"),
            case_id=spec.case_id,
            at=spec.opened_at,
            kind=EventKind.CASE_OPENED,
            actor=ActorType.AGENT,
            summary="Loaded a synthetic active case into the Northgate demo snapshot.",
            refs=[source_id],
            payload={"synthetic": True, "bootstrap": True},
        ),
        TimelineEvent(
            event_id=_Ids.event(spec.case_id, "policy"),
            case_id=spec.case_id,
            at=policy_at,
            kind=EventKind.POLICY_EVALUATED,
            actor=ActorType.SYSTEM,
            summary=decision.reason,
            refs=[decision.rule_id],
            payload={
                "policy_rule_id": decision.rule_id,
                "autonomy_level": decision.level.value,
            },
        ),
        TimelineEvent(
            event_id=_Ids.event(spec.case_id, "state"),
            case_id=spec.case_id,
            at=spec.updated_at,
            kind=spec.event_kind,
            actor=ActorType.SYSTEM,
            summary=spec.event_summary,
            refs=[source_id, *([spec.accepted_quote_id] if spec.accepted_quote_id else [])],
            payload={
                "status": spec.status.value,
                "synthetic": True,
                "outbound_delivered": False,
            },
        ),
    )
    policy_audit = AuditEntry(
        audit_id=_Ids.audit(spec.case_id, "policy"),
        case_id=spec.case_id,
        at=policy_at,
        action="evaluate_intake",
        autonomy_level=decision.level,
        policy_rule_id=decision.rule_id,
        reason=decision.reason,
    )
    audits: list[AuditEntry] = [policy_audit]
    spend: tuple[SpendEntry, ...] = ()
    if spec.spend_amount is not None and spec.spend_vendor_id is not None:
        commit_audit = AuditEntry(
            audit_id=_Ids.audit(spec.case_id, "commit"),
            case_id=spec.case_id,
            at=spec.updated_at,
            action="commit_quote_demo_only",
            autonomy_level=decision.level,
            policy_rule_id="DEMO.AUTHORIZED_NOT_SENT",
            reason="Synthetic snapshot commitment; no message or obligation was sent.",
            amount=spec.spend_amount,
            vendor_id=spec.spend_vendor_id,
        )
        audits.append(commit_audit)
        spend = (
            SpendEntry(
                entry_id=f"spend-bootstrap-{spec.case_id}",
                case_id=spec.case_id,
                category=spec.category,
                amount=spec.spend_amount,
                currency="USD",
                committed_at=spec.updated_at,
                source_audit_id=commit_audit.audit_id,
            ),
        )

    outbox: tuple[OutboxItem, ...] = ()
    if spec.outbox_kind:
        outbox = (
            OutboxItem(
                outbox_id=f"outbox-bootstrap-{spec.case_id}",
                case_id=spec.case_id,
                dedup_key=f"bootstrap:{spec.case_id}:{spec.outbox_kind}",
                kind=spec.outbox_kind,
                payload={
                    "case_id": spec.case_id,
                    "outbound_channel_selected": False,
                    "vendor_contact_allowed": False,
                    "synthetic": True,
                },
                created_at=spec.updated_at,
            ),
        )

    artifact_kind = spec.artifact_kind or "demo_case_state"
    artifact_sources = (
        source_id,
        *([spec.accepted_quote_id] if spec.accepted_quote_id else []),
    )
    artifacts = [
        WorkflowArtifact(
            artifact_id=f"artifact-bootstrap-{spec.case_id}",
            case_id=spec.case_id,
            kind=artifact_kind,
            created_at=spec.updated_at,
            source_ids=artifact_sources,
            payload={
                "case_id": spec.case_id,
                "status": spec.status.value,
                "synthetic": True,
            },
        )
    ]
    if spec.accepted_quote_id:
        vendor_id = spec.spend_vendor_id or (
            spec.contacted_vendor_ids[0] if spec.contacted_vendor_ids else None
        )
        artifacts.append(
            WorkflowArtifact(
                artifact_id=spec.accepted_quote_id,
                case_id=spec.case_id,
                kind="quote_snapshot",
                created_at=spec.updated_at,
                source_ids=(f"<{spec.accepted_quote_id}@vendors.narrativenode-labs.cloud>",),
                payload={
                    "quote_id": spec.accepted_quote_id,
                    "vendor_id": vendor_id,
                    "amount": (
                        format(spec.spend_amount, ".2f") if spec.spend_amount is not None else None
                    ),
                    "currency": "USD",
                    "synthetic": True,
                    "commitment_sent": False,
                },
            )
        )
    result = store.save_transition(
        case=case,
        expected_version=0,
        idempotency_key=f"bootstrap:v1:{spec.case_id}",
        new_case=True,
        timeline_events=timeline,
        audit_entries=tuple(audits),
        outbox_items=outbox,
        artifacts=tuple(artifacts),
        spend_entries=spend,
    )
    return result.applied


def _write_inbox_examples(*, store: SqliteOperationalStore, reference_at: datetime) -> None:
    pending_message = ResidentMessage(
        message_id="tg:-1002481179934:99501",
        source="telegram_shadow",
        chat_id="-1002481179934",
        sender_display="Mei-Ling C.",
        text="The courtyard branch is brushing the D Block balconies again.",
        sent_at=reference_at - timedelta(minutes=12),
        ingested_at=reference_at - timedelta(minutes=11),
    )
    processed_message = ResidentMessage(
        message_id="tg:-1002481179934:99500",
        source="telegram_shadow",
        chat_id="-1002481179934",
        sender_display="Kwame A.",
        text="The pool close date works for us.",
        sent_at=reference_at - timedelta(hours=2),
        ingested_at=reference_at - timedelta(hours=2) + timedelta(minutes=1),
        case_id="case-demo-active-003",
    )
    items = (
        _inbox_item("99501", pending_message, InboxStatus.PENDING),
        _inbox_item("99500", processed_message, InboxStatus.PROCESSED),
        InboxItem(
            source="telegram_shadow",
            external_id="99499",
            payload_hash=hashlib.sha256(b"synthetic-non-text-update").hexdigest(),
            received_at=reference_at - timedelta(hours=3),
            status=InboxStatus.IGNORED,
            message=None,
        ),
    )
    for item in items:
        store.record_inbound(item)


def _inbox_item(
    external_id: str,
    message: ResidentMessage,
    status: InboxStatus,
) -> InboxItem:
    digest = hashlib.sha256(message.model_dump_json().encode()).hexdigest()
    return InboxItem(
        source="telegram_shadow",
        external_id=external_id,
        payload_hash=digest,
        received_at=message.ingested_at,
        status=status,
        message=message,
    )


def _snapshot(
    *,
    store: SqliteOperationalStore,
    database: Path,
    reference_at: datetime,
) -> DemoBootstrapReport:
    cases = store.list_cases()
    status_counts = Counter(case.status.value for case in cases)
    artifact_count = sum(len(store.artifacts_for(kind=kind)) for kind in _ARTIFACT_KINDS)
    spend = {
        category.value: format(
            store.month_to_date_spend(category, as_of=reference_at, currency="USD"),
            ".2f",
        )
        for category in Category
    }
    spend = {category: amount for category, amount in spend.items() if amount != "0.00"}
    suggestions = store.list_suggestions(limit=500)
    suggestion_counts = Counter(item.status.value for item in suggestions)
    due_ids = tuple(case.case_id for case in store.due_cases(now=reference_at, limit=500))
    return DemoBootstrapReport(
        database_path=str(database),
        reference_at=reference_at,
        history_records=len(store.records()),
        active_cases=len(cases),
        case_status_counts=dict(sorted(status_counts.items())),
        timeline_events=sum(len(store.timeline_for(case.case_id)) for case in cases),
        audit_entries=sum(len(store.audit_for(case.case_id)) for case in cases),
        workflow_artifacts=artifact_count,
        pending_outbox=len(store.pending_outbox(limit=500)),
        pending_inbound=len(store.pending_inbound(limit=500)),
        due_case_ids=due_ids,
        spend_by_category=dict(sorted(spend.items())),
        suggestion_status_counts=dict(sorted(suggestion_counts.items())),
    )


def _validate_specs(specs: tuple[_ActiveCaseSpec, ...], *, bundle: Any) -> None:
    case_ids = [spec.case_id for spec in specs]
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("active demo case ids must be unique")
    assets = {asset.asset_id for asset in bundle.assets}
    vendors = {vendor.vendor_id for vendor in bundle.vendors}
    for spec in specs:
        if spec.asset_id and spec.asset_id not in assets:
            raise ValueError(f"active case {spec.case_id} references unknown asset")
        unknown = set(spec.contacted_vendor_ids) - vendors
        if unknown:
            raise ValueError(f"active case {spec.case_id} references unknown vendors: {unknown}")


def _parse_reference(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("seed reference_date must be an ISO timestamp string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("seed reference_date must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _remove_sqlite_files(database: Path) -> None:
    for candidate in (database, Path(f"{database}-wal"), Path(f"{database}-shm")):
        if candidate.exists():
            candidate.unlink()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        default="data/demo/steward-demo.db",
        help="SQLite path to create or inspect",
    )
    parser.add_argument("--reset", action="store_true", help="replace the existing demo DB")
    parser.add_argument("--inspect", action="store_true", help="read without bootstrapping")
    parser.add_argument("--json", action="store_true", help="emit one JSON object")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = (
        read_demo_snapshot(args.db)
        if args.inspect
        else bootstrap_demo_store(args.db, reset=args.reset)
    )
    if args.json:
        print(report.model_dump_json())
    else:
        print(json.dumps(report.model_dump(mode="json"), indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through CLI tests
    raise SystemExit(main())
