"""Durable maintenance continuation shared by worker, API and simulation."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from steward.domain.clock import parse_datetime
from steward.domain.enums import TERMINAL_STATUSES, ActorType, CaseStatus, EventKind
from steward.domain.models import AuditEntry, TimelineEvent
from steward.mail import AddressScheme, OutboundMessage
from steward.memory.records import CaseRecord, QuoteRecord
from steward.operations.outbox import OutboundPurpose, mail_outbox_item
from steward.procurement.portfolio import (
    QUOTE_DECISION_ARTIFACT_KIND,
    DurableQuoteDecision,
)
from steward.store import ConcurrencyConflict, OutboxStatus, WorkflowArtifact
from steward.store.workflow import BudgetReservation, HumanTask, WorkflowJob, stable_id


class MaintenanceWorkflow:
    def __init__(self, runtime):
        self.runtime = runtime
        self.store = runtime.store
        self.clock = runtime._clock

    def job(self, case, kind, *, due_at, source_id, payload=None):
        return WorkflowJob(
            job_id=stable_id(kind, case.case_id, source_id),
            case_id=case.case_id,
            kind=kind,
            source_id=source_id,
            created_at=self.clock.now(),
            due_at=due_at,
            expected_version=self.store.case_version(case.case_id),
            payload=payload or {},
        )

    def task(self, case, kind, title, reason, suffix=""):
        task = HumanTask(
            task_id=stable_id(kind, case.case_id, suffix),
            case_id=case.case_id,
            kind=kind,
            title=title,
            reason=reason,
            allowed_roles=("manager", "resident") if kind == "verify_completion" else ("manager",),
            assigned_actor_ids=tuple(
                dict.fromkeys(
                    self.store.get_message(mid).sender_display
                    for mid in case.source_message_ids
                    if self.store.get_message(mid)
                )
            )
            if kind == "verify_completion"
            else (),
            created_at=self.clock.now(),
            due_at=self.clock.now(),
            expected_version=self.store.case_version(case.case_id),
        )
        self.store.add_human_task(task)
        return task

    def handle(self, job):
        case = self.store.get_case(job.case_id)
        if case is None:
            raise ValueError("unknown workflow case")
        if job.kind == "mail.process":
            from steward.mail import InboundMessage

            if self.store.artifact("ses.processed.v1", job.source_id):
                return None, ()
            raw = self.store.artifact("ses.raw.v1", job.source_id)
            data = dict(raw.payload["inbound"])
            data["received_at"] = parse_datetime(data["received_at"])
            for name in ("to_addresses", "cc_addresses", "references", "unreadable_attachments"):
                data[name] = tuple(data.get(name, []))
            self.runtime.handle_vendor_reply(InboundMessage(**data), source_id=job.source_id)
            self.store.put_configuration(
                WorkflowArtifact(
                    artifact_id=job.source_id,
                    kind="ses.processed.v1",
                    case_id=job.case_id,
                    created_at=self.clock.now(),
                    payload={"status": "processed"},
                )
            )
            return None, ()
        if job.kind == "memory.index":
            index = getattr(self.store, "semantic_index", None)
            if index:
                index.rebuild()
            return None, ()
        if case.status in TERMINAL_STATUSES:
            return None, ()
        from steward.operations.community import CommunityWorkflow

        if job.kind.startswith("community."):
            CommunityWorkflow(self.runtime).handle(job)
            return None, ()
        if job.kind.startswith("appointment."):
            from steward.operations.appointments import Appointments

            Appointments(self.runtime).handle(job)
            return None, ()
        if job.kind.startswith("clarification."):
            from steward.operations.clarifications import Clarifications

            Clarifications(self.runtime).handle(job)
            return None, ()
        if job.kind == "case.opened":
            from steward.domain.enums import Category

            for message_id in case.source_message_ids:
                request = self.store.artifact("community.procurement.request.v1", message_id)
                if request:
                    link_id = stable_id(
                        "community-maintenance-link", request.artifact_id, case.case_id
                    )
                    if self.store.artifact("community.procurement.link.v1", link_id) is None:
                        with self.store.atomic():
                            self.store.put_configuration(
                                WorkflowArtifact(
                                    artifact_id=link_id,
                                    kind="community.procurement.link.v1",
                                    case_id=request.case_id,
                                    created_at=self.clock.now(),
                                    source_ids=(request.artifact_id,),
                                    payload={
                                        "child_case_id": case.case_id,
                                        "action_id": request.payload["action_id"],
                                    },
                                )
                            )
                            current = self.store.get_case(case.case_id)
                            self.store.save_transition(
                                case=current.model_copy(
                                    update={
                                        "related_case_ids": list(
                                            dict.fromkeys(
                                                [*current.related_case_ids, request.case_id]
                                            )
                                        )
                                    }
                                ),
                                expected_version=self.store.case_version(case.case_id),
                                idempotency_key=link_id,
                            )
                        case = self.store.get_case(case.case_id)

            if case.accepted_quote_id:
                self.task(
                    case,
                    "order_review",
                    "Review the existing service order",
                    "An existing commitment was recovered. Check delivery before continuing.",
                )
                return None, ()
            if case.category in (Category.MEETING_ADMIN, Category.OTHER):
                if case.status not in (CaseStatus.DETECTED, CaseStatus.PLANNING):
                    self.task(
                        case,
                        "meeting_review",
                        "Review the recovered meeting",
                        "Continue from the existing meeting records.",
                    )
                    return None, ()
                CommunityWorkflow(self.runtime).opened(case)
                return None, ()
            batch = (
                None
                if case.contacted_vendor_ids
                else self.runtime._continue_opened_case(case.case_id)
            )
            if batch is None and not case.contacted_vendor_ids:
                return None, ()
            policy = self.runtime._policy.policy_for(case.category)
            return batch, (
                self.job(
                    case,
                    "procurement.review",
                    due_at=case.opened_at
                    + timedelta(hours=policy.quote_wait_hours if policy else 24),
                    source_id=job.job_id,
                ),
            )
        if job.kind == "procurement.review":
            if case.accepted_quote_id:
                active = next(
                    (
                        r
                        for r in self.store.reservations(case.case_id)
                        if r.quote_id == case.accepted_quote_id
                        and r.status in ("reserved", "committed")
                    ),
                    None,
                )
                current_decision = next(
                    (
                        a
                        for a in self.store.artifacts_for(
                            kind=QUOTE_DECISION_ARTIFACT_KIND, case_id=case.case_id
                        )
                        if active and stable_id("reserve", a.artifact_id) == active.reservation_id
                    ),
                    None,
                )
                portfolio = (
                    self.store.artifact(
                        "quote_portfolio.v1", current_decision.payload["portfolio_id"]
                    )
                    if current_decision
                    else None
                )
                included = (
                    {q["quote_artifact_id"] for q in portfolio.payload["quotes"]}
                    if portfolio
                    else set()
                )
                policy_change = self.store.artifact("configuration.v1", job.source_id)
                if job.source_id in included or not (
                    self.store.artifact("vendor_quote.v1", job.source_id) or policy_change
                ):
                    return None, ()
                orders = [
                    o
                    for o in self.store.outbox_for_case(case.case_id)
                    if o.payload.get("purpose") == "commitment"
                    and o.last_error_code != "superseded_before_send"
                ]
                if (
                    len(orders) == 1
                    and orders[0].delivery_started_at is None
                    and orders[0].status in (OutboxStatus.PENDING, OutboxStatus.DEAD_LETTER)
                ):
                    self.withdraw_unsent_order(case, orders[0], job.source_id)
                    case = self.store.get_case(case.case_id)
                else:
                    if policy_change:
                        return None, ()  # A new policy cannot rewrite an existing external order.
                    self.task(
                        case,
                        "late_quote",
                        "Review a change received after ordering",
                        "New quote evidence is available. "
                        "The existing service order has not been changed.",
                        suffix=job.source_id,
                    )
                    return None, ()
            from steward.operations.quote_completeness import review_due
            from steward.procurement.portfolio import QuoteDecisionNotReadyError

            due = review_due(self.runtime, case)
            if due is None or due > self.clock.now():
                return None, (
                    self.job(
                        case,
                        "procurement.review",
                        due_at=due or self.clock.now() + timedelta(hours=1),
                        source_id=job.job_id,
                    ),
                )
            if case.status != CaseStatus.QUOTES_RECEIVED:
                return None, self._remind(case, job, "Please send your quotation and availability.")
            try:
                result = self.runtime.decide_quotes(case.case_id)
            except QuoteDecisionNotReadyError as exc:
                self.task(
                    case,
                    "quote_review",
                    "Review the available quotations",
                    str(exc),
                    suffix=job.source_id,
                )
                return None, ()
            self.commit(result.decision)
            return None, ()
        if job.kind == "vendor.clarify":
            from steward.operations.quote_completeness import missing_fields

            if any(
                a.payload["quote"]["vendor_id"] == job.payload.get("vendor_id")
                and a.artifact_id != job.source_id
                and not missing_fields(a.payload)
                and a.created_at >= job.created_at
                for a in self.store.artifacts_for(kind="vendor_quote.v1", case_id=case.case_id)
            ):
                return None, ()
            if case.accepted_quote_id:
                self.task(
                    case,
                    "late_vendor_reply",
                    "Review a vendor reply after ordering",
                    job.payload.get("reason", "A vendor reply needs review"),
                    suffix=job.source_id,
                )
                return None, ()
            return None, self._remind(
                case,
                job,
                "Please clarify your quotation: "
                + job.payload.get("reason", "price, currency, scope and availability are required")
                + ". Include total, currency, scope, exclusions, availability and expiry.",
            )
        if job.kind == "attendance.check":
            if case.status == CaseStatus.SCHEDULED:
                return None, ()
            if not any(
                r.reservation_id
                == stable_id("reserve", job.payload.get("order_decision_id", job.source_id))
                and r.status in ("reserved", "committed")
                for r in self.store.reservations(case.case_id)
            ):
                return None, ()
            if case.status in (CaseStatus.AWAITING_VERIFICATION, CaseStatus.WARRANTY_REVIEW):
                return None, ()
            return None, self._remind(
                case,
                job,
                "Please confirm acceptance of the order and propose an appointment "
                "with start, end and access arrangements.",
            )
        if job.kind == "warranty.followup":
            if case.status != CaseStatus.WARRANTY_REVIEW:
                return None, ()
            return None, self._remind(
                case,
                job,
                "The resident did not verify the repair as successful. Please investigate warranty "
                "or rework under the original order. No additional paid work is authorized.",
            )
        raise ValueError("unsupported workflow job kind")

    def _message(self, case, vendor, text, subject):
        cfg = self.runtime._settings
        scheme = AddressScheme(
            management_domain=cfg.management_domain, vendor_domain=cfg.vendor_domains[0]
        )
        return OutboundMessage(
            to=(vendor.email,),
            subject=subject,
            body_text=text,
            from_address=scheme.management_address,
            from_display_name=cfg.management_display_name,
            reply_to=scheme.case_reply_address(case.reply_token),
        )

    def withdraw_unsent_order(self, case, order, source_id):
        """Release only an order for which no provider call has started."""
        now = self.clock.now()
        with self.store.atomic() as conn:
            current = self.store.get_case(case.case_id)
            item = self.store.outbox_item(order.outbox_id)
            if (
                current.accepted_quote_id != case.accepted_quote_id
                or item.delivery_started_at is not None
                or item.status not in (OutboxStatus.PENDING, OutboxStatus.DEAD_LETTER)
            ):
                raise ConcurrencyConflict("Service order dispatch has already started")
            reservation = next(
                r
                for r in self.store.reservations(case.case_id)
                if r.quote_id == case.accepted_quote_id and r.status == "reserved"
            )
            withdrawn = item.model_copy(
                update={
                    "status": OutboxStatus.DEAD_LETTER,
                    "last_error_code": "superseded_before_send",
                    "failed_at": now,
                }
            )
            conn.execute("DELETE FROM claim_outbox WHERE outbox_id=?", (item.outbox_id,))
            conn.execute(
                "UPDATE outbox SET status=?,last_error_code=?,failed_at=?,doc_json=? "
                "WHERE outbox_id=?",
                (
                    OutboxStatus.DEAD_LETTER.value,
                    "superseded_before_send",
                    now.isoformat(),
                    withdrawn.model_dump_json(),
                    item.outbox_id,
                ),
            )
            released = reservation.model_copy(update={"status": "released"})
            conn.execute(
                "UPDATE budget_reservations SET status='released',doc_json=? "
                "WHERE reservation_id=?",
                (released.model_dump_json(), reservation.reservation_id),
            )
            self.store.save_transition(
                case=current.model_copy(
                    update={
                        "accepted_quote_id": None,
                        "status": CaseStatus.QUOTES_RECEIVED,
                        "updated_at": now,
                        "next_action_due_at": now,
                    }
                ),
                expected_version=self.store.case_version(case.case_id),
                idempotency_key=f"withdraw-unsent:{item.outbox_id}",
                artifacts=(
                    WorkflowArtifact(
                        artifact_id=stable_id("withdrawn", item.outbox_id),
                        case_id=case.case_id,
                        kind="order.withdrawal.v1",
                        created_at=now,
                        source_ids=(item.outbox_id, source_id),
                        payload={
                            "reason": "Decision inputs changed before provider dispatch",
                            "released_reservation": reservation.reservation_id,
                        },
                    ),
                ),
            )

    def _remind(self, case, job, text):
        count = int(job.payload.get("reminders", 0))
        configured = self.runtime._policy.policy_for(case.category)
        if count >= (configured.max_reminders if configured else 2):
            self.task(
                case,
                "vendor_overdue",
                "A vendor response is overdue",
                "Two reminders have not resolved this case. Review or contact the vendor.",
            )
            return ()
        now = self.clock.now()
        from steward.operations.clarifications import effective_assessment
        assessment = effective_assessment(self.store, case)
        allowed = self.runtime._policy.evaluate_intake(
            category=case.category, triage_confidence=assessment.result.confidence
        )
        if not allowed.may_contact_third_parties:
            self.task(case, "contact_blocked", "Review contact permissions", allowed.reason)
            return ()
        items, audits = [], []
        selected_vendor = None
        if case.accepted_quote_id:
            for artifact in self.store.artifacts_for(
                kind=QUOTE_DECISION_ARTIFACT_KIND, case_id=case.case_id
            ):
                if artifact.payload["selected_quote"]["quote_id"] == case.accepted_quote_id:
                    selected_vendor = artifact.payload["selected_quote"]["vendor_id"]
        for vendor in self.runtime._vendors:
            if job.payload.get("vendor_id") and vendor.vendor_id != job.payload["vendor_id"]:
                continue
            if case.accepted_quote_id and vendor.vendor_id != selected_vendor:
                continue
            if vendor.vendor_id not in case.contacted_vendor_ids or not vendor.allowlisted:
                continue
            if vendor.vendor_id not in allowed.allowed_vendor_ids:
                continue
            aid = stable_id("reminder-authority", job.job_id, vendor.vendor_id)
            audits.append(
                AuditEntry(
                    audit_id=aid,
                    case_id=case.case_id,
                    at=now,
                    action="send_follow_up",
                    autonomy_level=allowed.level,
                    policy_rule_id=allowed.rule_id,
                    reason=allowed.reason,
                    vendor_id=vendor.vendor_id,
                )
            )
            items.append(
                mail_outbox_item(
                    outbox_id=stable_id("reminder", aid),
                    case_id=case.case_id,
                    dedup_key=aid,
                    purpose=OutboundPurpose.FOLLOW_UP,
                    authority_audit_id=aid,
                    vendor_id=vendor.vendor_id,
                    created_at=now,
                    message=self._message(case, vendor, text, f"Update requested: {case.title}"),
                )
            )
        successor = self.job(
            case,
            job.kind,
            due_at=now + timedelta(hours=configured.reminder_interval_hours if configured else 24),
            source_id=job.job_id,
            payload={**job.payload, "reminders": count + 1},
        )
        updated = case.model_copy(
            update={
                "updated_at": now,
                "next_action_due_at": successor.due_at,
                "follow_up_count": case.follow_up_count + 1,
            }
        )
        # Detect replay by persisted job identity before rebuilding timestamped intent.
        if any(j.job_id == successor.job_id for j in self.store.workflow_jobs(case.case_id)):
            return ()
        self.store.save_transition(
            case=updated,
            expected_version=self.store.case_version(case.case_id),
            idempotency_key=f"follow-up:{job.job_id}",
            outbox_items=tuple(items),
            audit_entries=tuple(audits),
            workflow_jobs=(successor,),
        )
        return ()

    def commit(self, decision: DurableQuoteDecision, *, actor_id=None, source_id=None):
        now = self.clock.now()
        quote = decision.selected_quote
        with self.store.atomic():
            case = self.store.get_case(decision.case_id)
            if case.accepted_quote_id:
                if case.accepted_quote_id != quote.quote_id:
                    raise ConcurrencyConflict("an existing order cannot be silently replaced")
                return
            if case.status != CaseStatus.QUOTES_RECEIVED:
                raise ValueError("commitment requires current quotes")
            self.runtime.refresh_policy()
            configured = self.runtime._policy.policy_for(case.category)
            if actor_id is not None:
                if actor_id not in self.runtime._bundle.property_profile.management_committee:
                    raise PermissionError("only a registered manager can approve an order")
                self.store.put_configuration(
                    WorkflowArtifact(
                        artifact_id=source_id,
                        kind="maintenance.approval.v1",
                        case_id=case.case_id,
                        created_at=now,
                        payload={
                            "actor_id": actor_id,
                            "quote_id": quote.quote_id,
                            "policy_version": stable_id(configured.model_dump_json()),
                        },
                    )
                )
            from steward.operations.quote_completeness import missing_fields

            quote_artifact = self.store.artifact("vendor_quote.v1", quote.quote_id)
            missing = (
                missing_fields(quote_artifact.payload) if quote_artifact else ["source evidence"]
            )
            rejected = any(
                a.payload["quote_id"] == quote.quote_id
                for a in self.store.artifacts_for(kind="quote.rejection.v1", case_id=case.case_id)
            )
            if missing or rejected:
                self.task(
                    case,
                    "quote_review",
                    "Complete the selected quotation",
                    "Rejected quote" if rejected else "Missing: " + ", ".join(missing),
                    suffix=quote.quote_id,
                )
                return
            if quote.valid_until and quote.valid_until < now:
                self.task(
                    case, "quote_expired", "Request an updated quote", "The selected quote expired."
                )
                return
            from steward.procurement.portfolio import QUOTE_PORTFOLIO_ARTIFACT_KIND

            portfolios = self.store.artifacts_for(
                kind=QUOTE_PORTFOLIO_ARTIFACT_KIND, case_id=case.case_id
            )
            latest = max(portfolios, key=lambda a: a.payload.get("revision", 1))
            if latest.artifact_id != decision.portfolio_id:
                raise ConcurrencyConflict("a newer decision must be reviewed first")
            vendor = next(v for v in self.runtime._vendors if v.vendor_id == quote.vendor_id)
            spend = self.store.month_to_date_spend(case.category, as_of=now, currency=case.currency)
            policy = self.runtime.commitment_policy(case, quote.quote_id).authorize_commitment(
                category=case.category,
                triage_confidence=decision.triage_confidence,
                vendor_id=vendor.vendor_id,
                vendor_allowlisted=vendor.allowlisted,
                amount=quote.amount,
                currency=quote.currency,
                month_to_date_spend=spend,
            )
            if not policy.may_commit_spend:
                self.task(
                    case, "quote_approval", "Review the proposed service order", policy.reason
                )
                return
            configured = self.runtime._policy.policy_for(case.category)
            if configured.per_incident_cap is None:
                self.task(
                    case,
                    "budget_review",
                    "Configure an incident budget",
                    "A finite incident cap is required before placing a service order.",
                )
                return
            reservation = BudgetReservation(
                reservation_id=stable_id("reserve", decision.decision_id),
                case_id=case.case_id,
                quote_id=quote.quote_id,
                category=case.category.value,
                currency=quote.currency,
                amount=quote.amount,
                created_at=now,
                policy_version=stable_id(configured.model_dump_json()),
            )
            version = self.store.case_version(case.case_id)
            try:
                self.store.reserve_budget(
                    reservation,
                    expected_version=version,
                    incident_cap=configured.per_incident_cap,
                    monthly_cap=configured.monthly_cap
                    if configured.monthly_cap is not None
                    else Decimal("Infinity"),
                )
            except ValueError:
                self.task(
                    case,
                    "budget_review",
                    "Review the available budget",
                    "Other pending orders have reserved the remaining budget.",
                )
                return
            aid = stable_id("commit-audit", decision.decision_id)
            audit = AuditEntry(
                audit_id=aid,
                case_id=case.case_id,
                at=now,
                action="commit_quote",
                autonomy_level=policy.level,
                policy_rule_id=policy.rule_id,
                reason=policy.reason,
                amount=quote.amount,
                vendor_id=quote.vendor_id,
            )
            item = mail_outbox_item(
                outbox_id=stable_id("order", decision.decision_id),
                case_id=case.case_id,
                dedup_key=f"order:{case.case_id}:{decision.decision_id}",
                purpose=OutboundPurpose.COMMITMENT,
                authority_audit_id=aid,
                vendor_id=vendor.vendor_id,
                created_at=now,
                message=self._message(
                    case,
                    vendor,
                    f"We accept quote {quote.quote_id} for {quote.amount} {quote.currency}.\n"
                    f"Scope: {quote.scope}\nPlease confirm the appointment. "
                    "No additional work is authorized.",
                    f"Service order: {case.title}",
                ),
            )
            due = now + timedelta(hours=24)
            updated = case.model_copy(
                update={
                    "accepted_quote_id": quote.quote_id,
                    "status": CaseStatus.COMMITTED,
                    "updated_at": now,
                    "next_action_due_at": due,
                }
            )
            monitor = self.job(
                case,
                "attendance.check",
                due_at=due,
                source_id=decision.decision_id,
                payload={"order_decision_id": decision.decision_id},
            )
            self.store.save_transition(
                case=updated,
                expected_version=version,
                idempotency_key=f"commit:{case.case_id}:{decision.decision_id}",
                audit_entries=(audit,),
                outbox_items=(item,),
                workflow_jobs=(monitor,),
                timeline_events=(
                    TimelineEvent(
                        event_id=stable_id("order-event", case.case_id, decision.decision_id),
                        case_id=case.case_id,
                        at=now,
                        kind=EventKind.COMMITMENT_AUTHORIZED,
                        actor=ActorType.AGENT,
                        summary=f"Service order queued for {quote.amount} {quote.currency}.",
                        refs=[decision.decision_id, aid],
                    ),
                ),
            )

    def claim_completion(
        self, case_id: str, *, actor_id: str, notes: str, expected_version: int, source_id: str
    ):
        if not notes.strip():
            raise ValueError("completion evidence is required")
        now = self.clock.now()
        if actor_id not in self.runtime._bundle.property_profile.management_committee:
            raise PermissionError("completion must be recorded by an authenticated manager")
        with self.store.atomic():
            case = self.store.get_case(case_id)
            if case.status not in (
                CaseStatus.COMMITTED,
                CaseStatus.SCHEDULED,
                CaseStatus.WARRANTY_REVIEW,
            ):
                raise ValueError("case is not awaiting work")
            if case.status not in (CaseStatus.SCHEDULED, CaseStatus.WARRANTY_REVIEW):
                raise ValueError("service order delivery has not been confirmed")
            updated = case.model_copy(
                update={
                    "status": CaseStatus.AWAITING_VERIFICATION,
                    "updated_at": now,
                    "next_action_due_at": now + timedelta(hours=48),
                }
            )
            artifact = WorkflowArtifact(
                artifact_id=stable_id("completion", source_id),
                case_id=case_id,
                kind="maintenance.completion.v1",
                created_at=now,
                source_ids=(source_id,),
                payload={"actor_id": actor_id, "notes": notes, "case_version": expected_version},
            )
            self.store.save_transition(
                case=updated,
                expected_version=expected_version,
                idempotency_key=f"completion:{source_id}",
                artifacts=(artifact,),
            )
            self.task(
                updated,
                "verify_completion",
                "Did the work solve the problem?",
                notes,
                suffix=artifact.artifact_id,
            )

    def verify(
        self,
        case_id: str,
        *,
        actor_id: str,
        accepted: bool,
        notes: str,
        expected_version: int,
        source_id: str,
    ):
        if not notes.strip():
            raise ValueError("verification notes are required")
        now = self.clock.now()
        current = self.store.get_case(case_id)
        authors = (
            {
                self.store.get_message(mid).sender_display
                for mid in current.source_message_ids
                if self.store.get_message(mid)
            }
            if current
            else set()
        )
        residents = {r.display_name for r in self.runtime._bundle.residents}
        if actor_id not in set(self.runtime._bundle.property_profile.management_committee) | (
            authors & residents
        ):
            raise PermissionError("verification requires a manager or the reporting resident")
        with self.store.atomic():
            case = self.store.get_case(case_id)
            if case.status != CaseStatus.AWAITING_VERIFICATION:
                raise ValueError("case is not awaiting verification")
            claims = self.store.artifacts_for(kind="maintenance.completion.v1", case_id=case_id)
            if not claims:
                raise ValueError("missing completion evidence")
            claims = sorted(claims, key=lambda a: a.payload.get("case_version", 0))
            decisions = self.store.artifacts_for(kind=QUOTE_DECISION_ARTIFACT_KIND, case_id=case_id)
            decision = next(
                DurableQuoteDecision.model_validate(a.payload)
                for a in reversed(decisions)
                if a.payload["selected_quote"]["quote_id"] == case.accepted_quote_id
            )
            quote = decision.selected_quote
            updated = case.model_copy(
                update={
                    "status": CaseStatus.CLOSED if accepted else CaseStatus.WARRANTY_REVIEW,
                    "updated_at": now,
                    "resolved_at": now if accepted else None,
                    "closed_at": now if accepted else None,
                    "total_cost": quote.amount if accepted else None,
                    "resolution_notes": notes,
                    "next_action_due_at": None if accepted else now,
                }
            )
            record = CaseRecord(
                case_id=case_id,
                title=case.title,
                category=case.category,
                asset_id=case.asset_id,
                urgency=case.urgency,
                opened_at=case.opened_at,
                closed_at=now,
                is_simulated=self.runtime.execution_mode.value == "dry_run",
                outcome_verified=True,
                selected_vendor_id=quote.vendor_id,
                cost=quote.amount,
                currency=quote.currency,
                work_performed=claims[-1].payload["notes"],
                resolution_notes=notes,
                resolved_at=now,
                vendors_contacted=case.contacted_vendor_ids,
                selection_source_ids=list(decision.source_ids),
                selection_rationale=decision.recommendation.rationale,
                resolution_source_ids=[claims[-1].artifact_id],
                verification_source_ids=[source_id],
                quotes=[
                    QuoteRecord(
                        quote_id=quote.quote_id,
                        vendor_id=quote.vendor_id,
                        amount=quote.amount,
                        scope=quote.scope,
                        first_response_at=quote.received_at,
                    )
                ],
            )
            self.store.save_transition(
                case=updated,
                expected_version=expected_version,
                idempotency_key=f"verify:{source_id}",
                memory_records=(record,) if accepted else (),
                workflow_jobs=(self.job(case, "memory.index", due_at=now, source_id=source_id),)
                if accepted
                else (self.job(case, "warranty.followup", due_at=now, source_id=source_id),),
                timeline_events=(
                    TimelineEvent(
                        event_id=stable_id("verification", source_id),
                        case_id=case_id,
                        at=now,
                        kind=EventKind.CLOSED if accepted else EventKind.WARRANTY_OPENED,
                        actor=ActorType.HUMAN,
                        actor_label=actor_id,
                        summary=notes,
                        refs=[source_id, claims[-1].artifact_id],
                    ),
                ),
            )
            if not accepted:
                self.task(updated, "warranty_review", "Review a repair that did not hold", notes)

    def reconcile_rfqs(self):
        for case in self.store.list_open_cases():
            delivered = [
                o
                for o in self.store.outbox_for_case(case.case_id)
                if o.payload.get("purpose") == "rfq" and o.delivered_at
            ]
            missing = [
                o for o in delivered if o.payload.get("vendor_id") not in case.contacted_vendor_ids
            ]
            if not missing:
                continue
            with self.store.atomic():
                current = self.store.get_case(case.case_id)
                self.store.save_transition(
                    case=current.model_copy(
                        update={
                            "contacted_vendor_ids": list(
                                dict.fromkeys(
                                    [
                                        *current.contacted_vendor_ids,
                                        *[o.payload["vendor_id"] for o in missing],
                                    ]
                                )
                            ),
                            "status": CaseStatus.VENDOR_CONTACTED
                            if current.status in (CaseStatus.DETECTED, CaseStatus.PLANNING)
                            else current.status,
                            "updated_at": self.clock.now(),
                        }
                    ),
                    expected_version=self.store.case_version(case.case_id),
                    idempotency_key=stable_id("rfq-delivered", *[o.outbox_id for o in missing]),
                    timeline_events=tuple(
                        TimelineEvent(
                            event_id=stable_id("rfq-delivered-event", o.outbox_id),
                            case_id=case.case_id,
                            at=o.delivered_at,
                            kind=EventKind.RFQ_SENT,
                            actor=ActorType.SYSTEM,
                            summary="Quotation request delivered.",
                            refs=[o.outbox_id],
                        )
                        for o in missing
                    ),
                )

    def reconcile_orders(self):
        """Resume accounting after delivery, including a crash after provider acknowledgement."""
        for case in self.store.list_open_cases():
            if case.status != CaseStatus.COMMITTED:
                continue
            for item in self.store.outbox_for_case(case.case_id):
                if item.payload.get("purpose") != "commitment":
                    continue
                if item.last_error_code == "superseded_before_send":
                    continue
                if item.status in (OutboxStatus.AMBIGUOUS, OutboxStatus.DEAD_LETTER):
                    self.task(
                        case,
                        "delivery_review",
                        "Check service order delivery",
                        "The service order needs delivery reconciliation before any retry.",
                    )
                if item.status != OutboxStatus.DELIVERED:
                    continue
                with self.store.atomic():
                    current = self.store.get_case(case.case_id)
                    if current.status != CaseStatus.COMMITTED:
                        continue
                    reservation = next(
                        r
                        for r in self.store.reservations(case.case_id)
                        if r.quote_id == current.accepted_quote_id
                    )
                    self.store.settle_reservation(
                        reservation.reservation_id,
                        now=item.delivered_at,
                        audit_id=item.payload["authority_audit_id"],
                    )
                    updated = current.model_copy(
                        update={
                            "status": CaseStatus.AWAITING_APPOINTMENT,
                            "updated_at": self.clock.now(),
                        }
                    )
                    self.store.save_transition(
                        case=updated,
                        expected_version=self.store.case_version(case.case_id),
                        idempotency_key=f"order-delivered:{item.outbox_id}",
                    )
