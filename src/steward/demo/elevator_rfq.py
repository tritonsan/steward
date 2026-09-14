"""Recording-only elevator intake-to-RFQ demonstration.

Four synthetic Telegram updates exercise the real shadow adapter, durable SQLite
inbox, Nova-compatible intake port, institutional memory, deterministic policy,
and RFQ dispatcher.  ``RecordingMailTransport`` is the only mail transport and
no commitment or outbox item is ever created.
"""

from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from steward.agents import IntakeDisposition, IntakeService, StrandsTriageClassifier
from steward.agents.triage import TriageClassifier
from steward.channels import TelegramShadowAdapter
from steward.demo.quote_decision import DEFAULT_MODEL_ID, DEFAULT_REGION, SCHEME
from steward.domain.clock import FrozenClock
from steward.domain.enums import ActorType, CaseStatus, EventKind
from steward.domain.models import TimelineEvent
from steward.mail import RecipientGuard, RecordingMailTransport
from steward.memory import MemoryRecall
from steward.operations import OperationalRunner
from steward.policy import PolicyEngine
from steward.procurement import RecordingAuditSink, RfqDispatcher
from steward.seed import SeedBundle, load_seed
from steward.store import SqliteOperationalStore, WorkflowArtifact

CHAT_ID = "-1002481179934"
WEBHOOK_SECRET = "synthetic-elevator-demo-secret"
DEFAULT_PROFILE = "default"

SCENARIO: tuple[tuple[str, str], ...] = (
    (
        "Daniel K.",
        "The A Block Elevator is malfunctioning. The car shudders violently "
        "while approaching the fourth floor, and its doors reopen before closing.",
    ),
    (
        "Priya S.",
        "I just experienced the same malfunction in the A Block Elevator: it "
        "shuddered near the fourth floor and the doors would not close.",
    ),
    (
        "Simon O.",
        "This report concerns the B Block Elevator, a different physical lift "
        "from A Block. Its doors are physically stuck half-open at the ground floor.",
    ),
    (
        "Mei-Ling C.",
        "I confirm the same fault in the B Block Elevator: its doors remain stuck "
        "half-open at the ground floor and the car cannot be used.",
    ),
)


class _StableIds:
    def __init__(self) -> None:
        self._counts: defaultdict[str, int] = defaultdict(int)

    def __call__(self, prefix: str) -> str:
        self._counts[prefix] += 1
        return f"{prefix}-elevator-demo-{self._counts[prefix]:03d}"


class _StableReplyTokens:
    def __init__(self) -> None:
        self._value = 0

    def __call__(self) -> str:
        self._value += 1
        return f"{self._value:012x}"


def _simulation_start(bundle: SeedBundle) -> datetime:
    latest = max(record.closed_at for record in bundle.history)
    next_day = latest.astimezone(timezone.utc) + timedelta(days=7)
    return next_day.replace(hour=9, minute=0, second=0, microsecond=0)


def _telegram_update(
    *,
    index: int,
    sender: str,
    text: str,
    sent_at: datetime,
) -> dict[str, Any]:
    first_name, _, last_name = sender.partition(" ")
    return {
        "update_id": 930000 + index,
        "message": {
            "message_id": 71000 + index,
            "date": int(sent_at.timestamp()),
            "chat": {"id": int(CHAT_ID), "type": "supergroup"},
            "from": {
                "id": 880000 + index,
                "is_bot": False,
                "first_name": first_name,
                "last_name": last_name,
                "username": f"synthetic_resident_{index}",
            },
            "text": text,
        },
    }


def _memory_dict(recall: MemoryRecall | None, vendor_names: dict[str, str]) -> dict[str, Any]:
    if recall is None:
        return {"related_case_ids": [], "case_hits": [], "vendor_scorecards": []}
    return {
        "related_case_ids": list(recall.related_case_ids),
        "case_hits": [
            {
                "case_id": hit.case_id,
                "title": hit.record.title,
                "relation": hit.relation.value,
                "relevance_score": hit.relevance_score,
                "matched_fields": list(hit.matched_fields),
                "matched_terms": list(hit.matched_terms),
                "selected_vendor_id": hit.record.selected_vendor_id,
                "cost": str(hit.record.cost),
                "currency": hit.record.currency,
                "source_traced": bool(
                    hit.record.resolution_source_ids and hit.record.verification_source_ids
                ),
            }
            for hit in recall.case_hits
        ],
        "vendor_scorecards": [
            {
                "vendor_id": evidence.scorecard.vendor_id,
                "vendor_name": vendor_names.get(
                    evidence.scorecard.vendor_id,
                    evidence.scorecard.vendor_id,
                ),
                "jobs_completed": evidence.scorecard.jobs_completed,
                "avg_first_response_hours": evidence.scorecard.avg_first_response_hours,
                "avg_hours_to_onsite": evidence.scorecard.avg_hours_to_onsite,
                "avg_hours_to_resolution": evidence.scorecard.avg_hours_to_resolution,
                "avg_cost": (
                    str(evidence.scorecard.avg_cost)
                    if evidence.scorecard.avg_cost is not None
                    else None
                ),
                "currency": evidence.scorecard.currency,
                "repeat_failure_rate": evidence.scorecard.repeat_failure_rate,
                "source_case_ids": list(evidence.source_case_ids),
            }
            for evidence in recall.vendor_scorecards
        ],
    }


def _audit_dict(entry: Any) -> dict[str, Any]:
    return {
        "audit_id": entry.audit_id,
        "case_id": entry.case_id,
        "at": entry.at.isoformat(),
        "action": entry.action,
        "autonomy_level": entry.autonomy_level.value,
        "policy_rule_id": entry.policy_rule_id,
        "reason": entry.reason,
        "vendor_id": entry.vendor_id,
    }


def _remove_sqlite_files(path: Path) -> None:
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        candidate.unlink(missing_ok=True)


def run_elevator_rfq_demo(
    classifier: TriageClassifier,
    *,
    db_path: str | Path = ":memory:",
    seed: SeedBundle | None = None,
) -> dict[str, Any]:
    """Run the synthetic scenario and return source-derived evidence."""
    bundle = seed or load_seed()
    ids = _StableIds()
    reply_tokens = _StableReplyTokens()
    store = SqliteOperationalStore(db_path)
    try:
        inserted = sum(1 for record in bundle.history if store.write(record))
        if inserted != len(bundle.history):
            raise ValueError("the elevator RFQ demo requires a fresh SQLite database")

        policy = PolicyEngine(bundle.policies, bundle.settings)
        clock = FrozenClock(_simulation_start(bundle))
        intake = IntakeService(
            classifier=classifier,
            store=store,
            policy=policy,
            assets=bundle.assets,
            clock=clock,
            memory=store,
            id_factory=ids,
            reply_token_factory=reply_tokens,
        )
        runner = OperationalRunner(
            store=store,
            intake=intake,
            clock=clock,
            token_factory=lambda: "elevator-demo-inbound-lease",
        )
        telegram = TelegramShadowAdapter(
            secret_token=WEBHOOK_SECRET,
            allowed_chat_ids={CHAT_ID},
            inbox=store,
            clock=clock,
        )

        outcomes = []
        inbound_results = []
        for index, (sender, text) in enumerate(SCENARIO, start=1):
            observed_at = _simulation_start(bundle) + timedelta(minutes=(index - 1) * 5)
            clock.set(observed_at)
            update = _telegram_update(
                index=index,
                sender=sender,
                text=text,
                sent_at=observed_at - timedelta(minutes=1),
            )
            inbound = telegram.handle_update(update, secret_header=WEBHOOK_SECRET)
            inbound_results.append(inbound)
            tick = runner.tick()
            if len(tick.intake_outcomes) != 1:
                raise RuntimeError("each synthetic Telegram update must yield one intake outcome")
            outcomes.append(tick.intake_outcomes[0])

        vendor_names = {vendor.vendor_id: vendor.name for vendor in bundle.vendors}
        vendors_by_id = {vendor.vendor_id: vendor for vendor in bundle.vendors}
        assets_by_id = {asset.asset_id: asset for asset in bundle.assets}
        opened = [
            outcome for outcome in outcomes if outcome.disposition is IntakeDisposition.OPENED
        ]

        guard = RecipientGuard.of(SCHEME.vendor_domain)
        transport = RecordingMailTransport(guard=guard)
        audit_sink = RecordingAuditSink()
        rfq_at = clock.now() + timedelta(minutes=5)
        clock.set(rfq_at)
        dispatcher = RfqDispatcher(
            policy=policy,
            transport=transport,
            recipient_guard=guard,
            address_scheme=SCHEME,
            audit_sink=audit_sink,
            clock=clock,
            id_factory=ids,
        )

        batches = []
        prepared_emails = []
        for outcome in opened:
            if outcome.case is None or outcome.case.asset_id is None:
                raise RuntimeError("opened elevator intake has no case or asset")
            current = store.get_case(outcome.case.case_id)
            if current is None:
                raise RuntimeError("opened elevator case disappeared from SQLite")
            asset = assets_by_id[current.asset_id]
            transport_offset = len(transport.sent)
            batch = dispatcher.dispatch(
                case=current,
                triage_confidence=outcome.triage.confidence,
                asset=asset,
                vendors=bundle.vendors,
            )
            batches.append(batch)
            recorded_messages = tuple(transport.sent[transport_offset:])
            if len(recorded_messages) != len(batch.dispatches):
                raise RuntimeError("recording transport and RFQ batch disagree")

            artifacts = []
            for dispatch, message in zip(
                batch.dispatches,
                recorded_messages,
                strict=True,
            ):
                if message is not dispatch.message:
                    raise RuntimeError("RFQ dispatch did not retain the recorded message object")
                email = {
                    "case_id": current.case_id,
                    "vendor_id": dispatch.vendor_id,
                    "vendor_name": vendor_names[dispatch.vendor_id],
                    "provider_message_id": dispatch.provider_message_id,
                    "audit_id": dispatch.audit_id,
                    "to": list(message.to),
                    "from_address": message.from_address,
                    "from_display_name": message.from_display_name,
                    "reply_to": message.reply_to,
                    "subject": message.subject,
                    "body_text": message.body_text,
                    "delivered": False,
                }
                prepared_emails.append(email)
                artifact_id = ids("artifact-rfq-draft")
                artifacts.append(
                    WorkflowArtifact(
                        artifact_id=artifact_id,
                        case_id=current.case_id,
                        kind="rfq_draft",
                        created_at=rfq_at,
                        source_ids=(dispatch.audit_id, dispatch.provider_message_id),
                        payload=email,
                    )
                )

            updated = current.model_copy(deep=True)
            updated.status = CaseStatus.PLANNING
            updated.updated_at = rfq_at
            event = TimelineEvent(
                event_id=ids("event"),
                case_id=current.case_id,
                at=rfq_at,
                kind=EventKind.PLAN_DRAFTED,
                actor=ActorType.AGENT,
                summary=(
                    f"Prepared {len(artifacts)} recording-only RFQ drafts; "
                    "no message was delivered."
                ),
                refs=[artifact.artifact_id for artifact in artifacts],
                payload={
                    "transport": type(transport).__name__,
                    "delivered": False,
                    "allowed_vendor_ids": list(batch.policy_decision.allowed_vendor_ids),
                },
            )
            version = store.case_version(current.case_id)
            if version is None:
                raise RuntimeError("opened elevator case has no SQLite version")
            rfq_audits = tuple(
                entry for entry in audit_sink.entries if entry.case_id == current.case_id
            )
            store.save_transition(
                case=updated,
                expected_version=version,
                idempotency_key=f"elevator-rfq-drafts:{current.case_id}",
                timeline_events=(event,),
                audit_entries=rfq_audits,
                artifacts=tuple(artifacts),
            )

        case_memory = {
            outcome.case.case_id: _memory_dict(outcome.memory_recall, vendor_names)
            for outcome in opened
            if outcome.case is not None
        }
        message_steps = []
        for inbound, outcome in zip(inbound_results, outcomes, strict=True):
            message = inbound.item.message
            if message is None:
                raise RuntimeError("accepted synthetic update lost its normalized message")
            decision = outcome.policy_decision
            message_steps.append(
                {
                    "message_id": message.message_id,
                    "sender": message.sender_display,
                    "text": message.text,
                    "telegram_shadow_disposition": inbound.disposition.value,
                    "triage": {
                        "action": outcome.triage.action.value,
                        "category": outcome.triage.category.value,
                        "asset_id": outcome.triage.asset_id,
                        "urgency": outcome.triage.urgency.value,
                        "confidence": outcome.triage.confidence,
                        "title": outcome.triage.title,
                        "duplicate_case_id": outcome.triage.duplicate_case_id,
                        "rationale": outcome.triage.rationale,
                        "reconciliation_attempts": outcome.reconciliation_attempts,
                        "reconciliation_issue_codes": [
                            code.value for code in outcome.reconciliation_issue_codes
                        ],
                    },
                    "intake_disposition": outcome.disposition.value,
                    "case_id": outcome.case.case_id if outcome.case else None,
                    "policy": (
                        {
                            "evaluated": True,
                            "rule_id": decision.rule_id,
                            "autonomy_level": decision.level.value,
                            "reason": decision.reason,
                            "spend_cap": (
                                str(decision.spend_cap) if decision.spend_cap is not None else None
                            ),
                            "currency": decision.currency,
                            "allowed_vendor_ids": list(decision.allowed_vendor_ids),
                        }
                        if decision is not None
                        else {
                            "evaluated": False,
                            "reason": "Linked to an already governed open case.",
                        }
                    ),
                }
            )

        case_reports = []
        for outcome in opened:
            if outcome.case is None:
                continue
            case = store.get_case(outcome.case.case_id)
            if case is None:
                raise RuntimeError("case disappeared while creating the report")
            batch = next(item for item in batches if item.case_id == case.case_id)
            case_reports.append(
                {
                    "case_id": case.case_id,
                    "title": case.title,
                    "category": case.category.value,
                    "asset_id": case.asset_id,
                    "status": case.status.value,
                    "source_message_ids": list(case.source_message_ids),
                    "related_case_ids": list(case.related_case_ids),
                    "institutional_memory": case_memory[case.case_id],
                    "policy": {
                        "rule_id": batch.policy_decision.rule_id,
                        "autonomy_level": batch.policy_decision.level.value,
                        "reason": batch.policy_decision.reason,
                        "spend_cap": (
                            str(batch.policy_decision.spend_cap)
                            if batch.policy_decision.spend_cap is not None
                            else None
                        ),
                        "currency": batch.policy_decision.currency,
                        "allowed_vendors": [
                            {
                                "vendor_id": vendor_id,
                                "vendor_name": vendor_names.get(vendor_id, vendor_id),
                                "email": vendors_by_id[vendor_id].email,
                            }
                            for vendor_id in batch.policy_decision.allowed_vendor_ids
                            if vendor_id in vendors_by_id
                        ],
                    },
                    "prepared_email_count": len(batch.dispatches),
                    "timeline": [
                        {
                            "at": event.at.isoformat(),
                            "kind": event.kind.value,
                            "summary": event.summary,
                            "refs": list(event.refs),
                            "payload": event.payload,
                        }
                        for event in store.timeline_for(case.case_id)
                    ],
                    "audit": [_audit_dict(entry) for entry in store.audit_for(case.case_id)],
                }
            )

        expected_dispositions = ["opened", "linked", "opened", "linked"]
        actual_dispositions = [outcome.disposition.value for outcome in outcomes]
        case_ids = [outcome.case.case_id if outcome.case else None for outcome in outcomes]
        expected_email_count = sum(
            len(batch.policy_decision.allowed_vendor_ids) for batch in batches
        )
        all_emails_guarded = all(
            guard.is_allowed(address)
            for message in transport.sent
            for address in message.all_recipients
        )
        commitment_events = [
            event
            for case in store.list_cases()
            for event in store.timeline_for(case.case_id)
            if event.kind in {EventKind.COMMITMENT_AUTHORIZED, EventKind.COMMITMENT_SENT}
        ]
        errors = []
        if actual_dispositions != expected_dispositions:
            errors.append(f"expected intake {expected_dispositions}, got {actual_dispositions}")
        if not (case_ids[0] == case_ids[1] and case_ids[2] == case_ids[3]):
            errors.append("same-asset reports did not link to the same case")
        if case_ids[0] == case_ids[2]:
            errors.append("A Block and B Block were incorrectly merged")
        if [outcome.triage.asset_id for outcome in opened] != ["elevator-a", "elevator-b"]:
            errors.append("opened cases were not bound to elevator-a and elevator-b")
        if len(transport.sent) != expected_email_count:
            errors.append("recorded RFQ count does not match policy vendor allowlists")
        if not all_emails_guarded:
            errors.append("an RFQ recipient escaped the vendor-domain guard")
        if store.pending_outbox():
            errors.append("the demo unexpectedly created a sendable outbox item")
        if commitment_events:
            errors.append("the demo unexpectedly authorized or sent a commitment")
        if any("No work is authorized" not in message.body_text for message in transport.sent):
            errors.append("an RFQ draft omitted the no-authorization statement")

        return {
            "passed": not errors,
            "simulation": {
                "synthetic_data": True,
                "history_records_loaded": len(store.records()),
                "input_message_count": len(SCENARIO),
                "opened_case_count": len(opened),
                "recorded_email_count": len(transport.sent),
            },
            "safety": {
                "telegram_adapter": type(telegram).__name__,
                "telegram_outbound_enabled": telegram.outbound_enabled,
                "operational_runner_outbound_enabled": runner.outbound_enabled,
                "mail_transport": type(transport).__name__,
                "prepared_email_source": "RecordingMailTransport.sent",
                "recipient_guard_domains": sorted(guard.allowed_domains),
                "all_recipients_guarded": all_emails_guarded,
                "real_email_delivery": False,
                "pending_outbox_count": len(store.pending_outbox()),
                "commitment_event_count": len(commitment_events),
                "commitment_sent": False,
            },
            "messages": message_steps,
            "cases": case_reports,
            "prepared_emails": prepared_emails,
            "recorded_transport_messages": len(transport.sent),
            "persisted_rfq_draft_artifacts": len(store.artifacts_for(kind="rfq_draft")),
            "errors": errors,
        }
    finally:
        store.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run four synthetic elevator reports through Nova and record RFQ drafts."
    )
    parser.add_argument(
        "--model-id",
        default=os.environ.get("STEWARD_BEDROCK_MODEL_ID", DEFAULT_MODEL_ID),
    )
    parser.add_argument("--profile", default=DEFAULT_PROFILE)
    parser.add_argument(
        "--region",
        default=os.environ.get("AWS_REGION", DEFAULT_REGION),
    )
    parser.add_argument("--triage-max-tokens", type=int, default=512)
    parser.add_argument("--db", default=":memory:")
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    db_path: str | Path = args.db
    if args.db != ":memory:":
        db_path = Path(args.db).expanduser().resolve()
        if args.reset:
            _remove_sqlite_files(db_path)
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    classifier = StrandsTriageClassifier.bedrock(
        model_id=args.model_id,
        profile_name=args.profile,
        region_name=args.region,
        temperature=0.0,
        max_tokens=args.triage_max_tokens,
    )
    report = run_elevator_rfq_demo(classifier, db_path=db_path)
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output is not None:
        args.output.expanduser().resolve().write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
