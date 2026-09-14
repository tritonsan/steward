"""Versioned vendor appointments; delivery and actual scheduling are separate facts."""

from datetime import timedelta
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator

from steward.agents.order_reply import OrderReply
from steward.domain.enums import ActorType, CaseStatus, EventKind
from steward.domain.models import TimelineEvent
from steward.store import ConcurrencyConflict, WorkflowArtifact
from steward.store.workflow import stable_id


class AppointmentRules(BaseModel):
    model_config = ConfigDict(extra="forbid")
    weekdays: list[int] = Field(min_length=1, max_length=7)
    start_hour: int = Field(ge=0, le=23)
    end_hour: int = Field(ge=1, le=24)
    minimum_notice_hours: int = Field(ge=1, le=168)
    access_instructions: str = Field(min_length=1, max_length=2000)
    access_actor_id: str | None = None

    @model_validator(mode="after")
    def valid_window(self):
        if self.start_hour >= self.end_hour or any(d not in range(7) for d in self.weekdays):
            raise ValueError("Invalid working hours or weekdays")
        return self


class Appointments:
    def __init__(self, runtime):
        self.rt, self.store, self.clock = runtime, runtime.store, runtime._clock

    def ingest(self, inbound, *, source_id):
        """Acknowledge only after raw input and inference continuation are durable."""
        from dataclasses import asdict

        from steward.domain.enums import TERMINAL_STATUSES

        route = self.rt._vendor_replies._route(inbound)
        case = self.store.get_case(route.case.case_id)
        quote = self.store.artifact("vendor_quote.v1", case.accepted_quote_id or "")
        if not quote or quote.payload["quote"]["vendor_id"] != route.vendor.vendor_id:
            return None
        if case.status in TERMINAL_STATUSES:
            raise ValueError("A closed case cannot accept a new appointment")
        key = stable_id("order-inbound", case.case_id, inbound.message_id or source_id)
        with self.store.atomic():
            prior = self.store.artifact("order.inbound.v1", key)
            if prior:
                if prior.payload["inbound"]["body_text"] != inbound.body_text:
                    raise ConcurrencyConflict("Vendor message identity reused")
                return prior
            data = asdict(inbound)
            data["received_at"] = inbound.received_at.isoformat()
            artifact = WorkflowArtifact(
                artifact_id=key,
                kind="order.inbound.v1",
                case_id=case.case_id,
                created_at=self.clock.now(),
                source_ids=(source_id,),
                payload={"inbound": data, "source_id": source_id},
            )
            self.store.put_configuration(artifact)
            self.store.enqueue_job(
                self.rt._maintenance.job(
                    case, "appointment.extract", due_at=self.clock.now(), source_id=key
                )
            )
            return artifact

    def extract(self, inbound, *, source_id):
        route = self.rt._vendor_replies._route(inbound)
        case = self.store.get_case(route.case.case_id)
        quote = self.store.artifact("vendor_quote.v1", case.accepted_quote_id or "")
        if not quote or quote.payload["quote"]["vendor_id"] != route.vendor.vendor_id:
            return None
        key = stable_id("order-reply", case.case_id, inbound.message_id or source_id)
        previous = self.store.artifact("order.reply.v1", key)
        if previous:
            if previous.payload["body_text"] != inbound.body_text:
                raise ConcurrencyConflict("Vendor message identity reused")
            return previous
        version = self.store.case_version(case.case_id)
        model = self.rt._order_reply_extractor
        facts = OrderReply.model_validate(
            model.extract(
                body_text=inbound.body_text,
                received_at=inbound.received_at,
                timezone=self.rt._bundle.property_profile.timezone,
                order=quote.payload["quote"],
            )
        )
        for field in ("evidence", "time_evidence", "unchanged_terms_evidence", "access_evidence"):
            value = getattr(facts, field)
            if value and value not in inbound.body_text:
                raise ValueError("Order reply evidence must be a verbatim excerpt")
        if facts.kind == "quote_change":
            return self.rt._vendor_replies.ingest(inbound, source_id=source_id)
        with self.store.atomic():
            if self.store.case_version(case.case_id) != version:
                raise ConcurrencyConflict("Case changed during order reply extraction")
            artifact = WorkflowArtifact(
                artifact_id=key,
                kind="order.reply.v1",
                case_id=case.case_id,
                created_at=self.clock.now(),
                source_ids=(source_id, inbound.message_id, quote.artifact_id),
                payload={
                    "facts": facts.model_dump(mode="json"),
                    "body_text": inbound.body_text,
                    "vendor_id": route.vendor.vendor_id,
                },
            )
            self.store.save_transition(
                case=case.model_copy(update={"updated_at": self.clock.now()}),
                expected_version=version,
                idempotency_key=key,
                artifacts=(artifact,),
                workflow_jobs=(
                    self.rt._maintenance.job(
                        case, "appointment.reply", due_at=self.clock.now(), source_id=key
                    ),
                ),
            )
            return artifact

    def handle(self, job):
        case = self.store.get_case(job.case_id)
        if job.kind == "appointment.extract":
            from steward.domain.clock import parse_datetime
            from steward.mail import InboundMessage

            raw = self.store.artifact("order.inbound.v1", job.source_id)
            data = dict(raw.payload["inbound"])
            data["received_at"] = parse_datetime(data["received_at"])
            for field in ("to_addresses", "cc_addresses", "unreadable_attachments", "references"):
                data[field] = tuple(data.get(field, []))
            self.extract(InboundMessage(**data), source_id=raw.payload["source_id"])
            return
        if job.kind == "appointment.check":
            confirmed = self.store.artifacts_for(
                kind="appointment.confirmed.v1", case_id=case.case_id
            )
            if not confirmed or confirmed[-1].payload["appointment_id"] != job.payload.get(
                "appointment_id", job.source_id
            ):
                return
            if case.status != CaseStatus.SCHEDULED:
                return
            self.rt._maintenance._remind(
                case,
                job,
                "The appointment has ended. Please confirm attendance and work status. "
                "A missing reply is not treated as a confirmed no-show.",
            )
            return
        source = self.store.artifact("order.reply.v1", job.source_id)
        facts = OrderReply.model_validate(source.payload["facts"])
        if self.store.artifact("appointment.processed.v1", job.source_id):
            return
        with self.store.atomic():
            if facts.kind in ("appointment", "postponement"):
                self.propose(case, facts, source)
            elif facts.kind == "completion":
                if case.status == CaseStatus.SCHEDULED:
                    now = self.clock.now()
                    self.store.save_transition(
                        case=case.model_copy(
                            update={"status": CaseStatus.AWAITING_VERIFICATION, "updated_at": now}
                        ),
                        expected_version=self.store.case_version(case.case_id),
                        idempotency_key="vendor-completion:" + job.source_id,
                        artifacts=(
                            WorkflowArtifact(
                                artifact_id=stable_id("completion", job.source_id),
                                kind="maintenance.completion.v1",
                                case_id=case.case_id,
                                created_at=now,
                                source_ids=(job.source_id,),
                                payload={
                                    "actor_id": source.payload["vendor_id"],
                                    "notes": facts.evidence,
                                },
                            ),
                        ),
                    )
                    self.rt._maintenance.task(
                        case,
                        "verify_completion",
                        "Did the work solve the problem?",
                        facts.evidence,
                        suffix=job.source_id,
                    )
                else:
                    self.review(case, source, "Completion arrived without a confirmed appointment")
            elif facts.kind != "accepted":
                self.review(case, source, "Vendor reply requires review: " + facts.kind)
            self.store.put_configuration(
                WorkflowArtifact(
                    artifact_id=job.source_id,
                    kind="appointment.processed.v1",
                    case_id=case.case_id,
                    created_at=self.clock.now(),
                    payload={"kind": facts.kind},
                )
            )

    def review(self, case, source, reason):
        self.rt._maintenance.task(
            case,
            "appointment_review",
            "Review the vendor appointment",
            reason,
            suffix=source.artifact_id,
        )

    def propose(self, case, facts, source):
        now = self.clock.now()
        previous = self.store.artifacts_for(kind="appointment.proposal.v1", case_id=case.case_id)
        configs = self.store.artifacts_for(kind="appointment.rules.v1")
        config = configs[-1] if configs else None
        rules = AppointmentRules.model_validate(config.payload["rules"]) if config else None
        reason = None
        if not any(
            o.payload.get("purpose") == "commitment" and o.delivered_at
            for o in self.store.outbox_for_case(case.case_id)
        ):
            reason = "Service order delivery is not confirmed"
        elif not rules:
            reason = "Working hours and access rules have not been configured"
        elif len(previous) >= 3:
            reason = "Two automatic rescheduling attempts have been used"
        elif (
            not facts.starts_at
            or not facts.ends_at
            or facts.ends_at <= facts.starts_at
            or not facts.time_evidence
        ):
            reason = "Explicit start and end times are required"
        elif not facts.unchanged_terms_evidence or not facts.access_evidence:
            reason = "Confirm unchanged price/scope and the access arrangements"
        else:
            zone = ZoneInfo(self.rt._bundle.property_profile.timezone)
            start, end = facts.starts_at.astimezone(zone), facts.ends_at.astimezone(zone)
            if (
                start.weekday() not in rules.weekdays
                or start.date() != end.date()
                or start.hour < rules.start_hour
                or end.hour + end.minute / 60 > rules.end_hour
                or facts.starts_at < now + timedelta(hours=rules.minimum_notice_hours)
            ):
                reason = "Proposed time is outside the approved hours or notice period"
            for other in self.store.list_open_cases():
                if other.case_id == case.case_id:
                    continue
                confirmed = self.store.artifacts_for(
                    kind="appointment.confirmed.v1", case_id=other.case_id
                )
                if confirmed:
                    proposal = self.store.artifact(
                        "appointment.proposal.v1", confirmed[-1].payload["appointment_id"]
                    )
                    old = OrderReply.model_validate(proposal.payload["facts"])
                    if old.starts_at < facts.ends_at and facts.starts_at < old.ends_at:
                        reason = "Another community appointment overlaps this time"
        artifact = WorkflowArtifact(
            artifact_id=source.artifact_id,
            kind="appointment.proposal.v1",
            case_id=case.case_id,
            created_at=now,
            source_ids=(source.artifact_id,),
            payload={
                "revision": len(previous) + 1,
                "previous_id": previous[-1].artifact_id if previous else None,
                "facts": facts.model_dump(mode="json"),
                "rules_id": config.artifact_id if config else None,
                "reason": reason,
                "status": "review" if reason else "pending_confirmation",
            },
        )
        self.store.put_configuration(artifact)
        if reason:
            self.review(case, source, reason)
        elif rules.access_actor_id:
            from steward.store.workflow import HumanTask

            self.store.add_human_task(
                HumanTask(
                    task_id=stable_id("appointment-access", source.artifact_id),
                    case_id=case.case_id,
                    kind="appointment_access",
                    title="Confirm access for the repair visit",
                    reason=f"{facts.starts_at.isoformat()} — {rules.access_instructions}",
                    created_at=now,
                    due_at=facts.starts_at,
                    expected_version=self.store.case_version(case.case_id),
                    allowed_roles=("manager", "resident"),
                    assigned_actor_ids=(rules.access_actor_id,),
                )
            )
        else:
            self.accept(case, artifact)

    def accept(self, case, proposal):
        """Queue the acceptance through the same guarded mail outbox as follow-ups."""
        from steward.domain.models import AuditEntry
        from steward.operations.outbox import OutboundPurpose, mail_outbox_item

        if any(
            o.kind == "mail.send.v1"
            and o.status.value in ("ambiguous", "dispatching")
            and any(
                stable_id("appointment-accept", a.artifact_id) == o.outbox_id
                for a in self.store.artifacts_for(
                    kind="appointment.proposal.v1", case_id=case.case_id
                )
            )
            for o in self.store.outbox_for_case(case.case_id)
        ):
            raise ConcurrencyConflict(
                "Reconcile the previous uncertain appointment acceptance first"
            )
        source = self.store.artifact("order.reply.v1", proposal.artifact_id)
        vendor = next(v for v in self.rt._vendors if v.vendor_id == source.payload["vendor_id"])
        key = stable_id("appointment-accept", proposal.artifact_id)
        if self.store.outbox_item(key):
            return
        audit = AuditEntry(
            audit_id=key,
            case_id=case.case_id,
            at=self.clock.now(),
            action="confirm_appointment",
            autonomy_level=case.autonomy_level,
            policy_rule_id=proposal.payload["rules_id"],
            reason="Within approved hours and access rules.",
            vendor_id=vendor.vendor_id,
        )
        item = mail_outbox_item(
            outbox_id=key,
            case_id=case.case_id,
            dedup_key=key,
            purpose=OutboundPurpose.FOLLOW_UP,
            authority_audit_id=key,
            vendor_id=vendor.vendor_id,
            created_at=self.clock.now(),
            message=self.rt._maintenance._message(
                case,
                vendor,
                f"Appointment accepted: {proposal.payload['facts']['starts_at']} "
                f"to {proposal.payload['facts']['ends_at']}. "
                "Existing price and scope only. No additional work is authorized.",
                "Appointment: " + case.title,
            ),
        )
        self.store.save_transition(
            case=self.store.get_case(case.case_id),
            expected_version=self.store.case_version(case.case_id),
            idempotency_key=key,
            audit_entries=(audit,),
            outbox_items=(item,),
        )

    def reconcile(self):
        for case in self.store.list_open_cases():
            if case.status not in (
                CaseStatus.COMMITTED,
                CaseStatus.AWAITING_APPOINTMENT,
                CaseStatus.SCHEDULED,
            ):
                continue
            for proposal in self.store.artifacts_for(
                kind="appointment.proposal.v1", case_id=case.case_id
            ):
                key = stable_id("appointment-accept", proposal.artifact_id)
                item = self.store.outbox_item(key)
                if item and item.status.value in ("ambiguous", "dead_letter"):
                    self.rt._maintenance.task(
                        case,
                        "appointment_delivery",
                        "Check appointment acceptance delivery",
                        "The provider did not confirm delivery. "
                        "Reconcile the provider receipt before another acceptance.",
                        suffix=proposal.artifact_id,
                    )
                if (
                    not item
                    or not item.delivered_at
                    or self.store.artifact("appointment.confirmed.v1", key)
                ):
                    continue
                with self.store.atomic():
                    confirmed = self.store.artifacts_for(
                        kind="appointment.confirmed.v1", case_id=case.case_id
                    )
                    if any(
                        record.payload["revision"] > proposal.payload["revision"]
                        for record in confirmed
                    ):
                        self.rt._maintenance.task(
                            case,
                            "appointment_delivery",
                            "Review an older appointment acceptance receipt",
                            "A newer appointment is already confirmed. This older receipt "
                            "does not change the current appointment; check with the vendor.",
                            suffix=proposal.artifact_id,
                        )
                        continue
                    facts = OrderReply.model_validate(proposal.payload["facts"])
                    current = self.store.get_case(case.case_id)
                    self.store.save_transition(
                        case=current.model_copy(
                            update={
                                "status": CaseStatus.SCHEDULED,
                                "scheduled_for": facts.starts_at,
                                "updated_at": self.clock.now(),
                                "next_action_due_at": facts.ends_at + timedelta(hours=1),
                            }
                        ),
                        expected_version=self.store.case_version(case.case_id),
                        idempotency_key="confirmed:" + key,
                        artifacts=(
                            WorkflowArtifact(
                                artifact_id=key,
                                kind="appointment.confirmed.v1",
                                case_id=case.case_id,
                                created_at=self.clock.now(),
                                source_ids=(proposal.artifact_id, key),
                                payload={
                                    "appointment_id": proposal.artifact_id,
                                    "revision": proposal.payload["revision"],
                                },
                            ),
                        ),
                        workflow_jobs=(
                            self.rt._maintenance.job(
                                current,
                                "appointment.check",
                                source_id=proposal.artifact_id,
                                due_at=facts.ends_at + timedelta(hours=1),
                                payload={"appointment_id": proposal.artifact_id},
                            ),
                        ),
                        timeline_events=(
                            TimelineEvent(
                                event_id=key,
                                case_id=case.case_id,
                                at=self.clock.now(),
                                kind=EventKind.SCHEDULED,
                                actor=ActorType.SYSTEM,
                                summary="Vendor appointment confirmed after acceptance delivery.",
                                refs=[proposal.artifact_id],
                            ),
                        ),
                    )
