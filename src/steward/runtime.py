"""Production composition root for one restart-safe Steward operational tick.

The default mode is DRY_RUN and therefore constructs only a recording mail
transport. LIVE modes require validated settings and a guarded SES adapter.
No import or construction step sends a message; delivery occurs only when
``tick`` claims a previously persisted typed outbox intent.
"""

from __future__ import annotations

import json
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import uuid4

from steward.agents import (
    IntakeService,
    MeetingAgendaPlanner,
    MeetingMinutesExtractor,
    QuoteExtractor,
    QuoteRecommender,
    ResolutionPlanner,
    StrandsMeetingAgendaPlanner,
    StrandsMeetingMinutesExtractor,
    StrandsQuoteExtractor,
    StrandsQuoteRecommender,
    StrandsTriageClassifier,
    TriageClassifier,
)
from steward.channels import TelegramShadowAdapter, TelegramShadowResult
from steward.config import RuntimeExecutionMode, StewardSettings
from steward.domain.clock import Clock, SystemClock
from steward.mail import (
    AddressScheme,
    InboundMessage,
    MailTransport,
    RecipientGuard,
    RecordingMailTransport,
    parse_s3_mail_event,
)
from steward.mail.ses import SesInboundReader, SesMailTransport
from steward.meetings import (
    MeetingActionCompletionResult,
    MeetingAvailabilityResponse,
    MeetingCandidateSlot,
    MeetingDecisionConfirmation,
    MeetingDecisionResult,
    MeetingDueResult,
    MeetingGovernancePolicy,
    MeetingLifecycleService,
    MeetingMinutesResult,
    MeetingPreparationResult,
    MeetingPreparationService,
    MeetingSchedulingPolicy,
    MeetingVerificationOutcome,
    MeetingVerificationResult,
)
from steward.operations import (
    OperationalRunner,
    OperationalTickReport,
    OutboxDispatcher,
    OutboxDispatchReport,
)
from steward.orchestration import ResolutionPlanningResult, ResolutionPlanningService
from steward.playbooks import default_playbooks
from steward.policy import PolicyEngine
from steward.proactive import ProactiveMaintenanceEngine
from steward.procurement import (
    QueuedRfqBatch,
    QuoteDecisionResult,
    QuoteDecisionService,
    RfqOutboxCoordinator,
    VendorReplyResult,
    VendorReplyService,
)
from steward.seed import SeedBundle, load_seed
from steward.store import SqliteOperationalStore
from steward.store.workflow import HumanTask, stable_id

__all__ = [
    "StewardRuntime",
    "StewardRuntimeReport",
    "build_runtime",
    "main",
]


class _InboundMailReader(Protocol):
    def read(self, key: str, *, received_at: datetime) -> InboundMessage: ...


@dataclass(frozen=True, slots=True)
class StewardRuntimeReport:
    inbound: OperationalTickReport
    queued_rfqs: tuple[QueuedRfqBatch, ...]
    outbound: OutboxDispatchReport
    meeting_due_results: tuple[MeetingDueResult, ...] = ()
    workflow_failures: tuple[str, ...] = ()


class StewardRuntime:
    """Own the process-level components for one safe vertical slice."""

    __slots__ = (
        "_assets",
        "_clock",
        "_policy",
        "_maintenance",
        "_order_reply_extractor",
        "_bundle",
        "_inbound_bucket",
        "_inbound_reader",
        "_meeting_lifecycle",
        "_meeting_preparation",
        "_outbox",
        "_quote_decisions",
        "_resolution_planning",
        "_rfq",
        "_runner",
        "_settings",
        "_telegram",
        "_vendor_replies",
        "_vendors",
        "store",
        "transport",
    )

    def __init__(
        self,
        *,
        settings: StewardSettings,
        store: SqliteOperationalStore,
        runner: OperationalRunner,
        outbox: OutboxDispatcher,
        rfq: RfqOutboxCoordinator,
        assets: dict[str, Any],
        vendors: tuple[Any, ...],
        transport: MailTransport,
        telegram: TelegramShadowAdapter | None,
        vendor_replies: VendorReplyService,
        quote_decisions: QuoteDecisionService,
        resolution_planning: ResolutionPlanningService,
        meeting_preparation: MeetingPreparationService,
        meeting_lifecycle: MeetingLifecycleService,
        inbound_reader: _InboundMailReader | None,
        inbound_bucket: str | None,
        clock: Clock,
        policy: PolicyEngine,
        bundle: SeedBundle,
    ) -> None:
        self._settings = settings
        self._clock = clock
        self._policy = policy
        self._bundle = bundle
        self.store = store
        self._runner = runner
        self._outbox = outbox
        self._rfq = rfq
        self._assets = assets
        self._vendors = vendors
        self.transport = transport
        self._telegram = telegram
        self._vendor_replies = vendor_replies
        self._quote_decisions = quote_decisions
        self._resolution_planning = resolution_planning
        self._meeting_preparation = meeting_preparation
        self._meeting_lifecycle = meeting_lifecycle
        self._inbound_reader = inbound_reader
        self._inbound_bucket = inbound_bucket
        from steward.operations.maintenance import MaintenanceWorkflow

        self._maintenance = MaintenanceWorkflow(self)
        from steward.agents.order_reply import StrandsOrderReplyExtractor

        self._order_reply_extractor = StrandsOrderReplyExtractor(settings)
        self._outbox._authority_validator = self._validate_delivery

    @property
    def execution_mode(self) -> RuntimeExecutionMode:
        return self._settings.execution_mode

    @property
    def telegram_enabled(self) -> bool:
        return self._telegram is not None

    @property
    def inbound_mail_enabled(self) -> bool:
        return self._inbound_reader is not None and self._inbound_bucket is not None

    def handle_telegram_update(
        self,
        update: dict[str, Any],
        *,
        secret_header: str,
    ) -> TelegramShadowResult:
        if self._telegram is None:
            raise RuntimeError("Telegram adapter is disabled by runtime settings")
        from steward.channels.telegram_groups import TelegramGroups, current_group

        group_result = TelegramGroups(self).handle(update, secret_header)
        if group_result is not None:
            return group_result
        if update.get("message", {}).get("chat", {}).get("type") == "private":
            from steward.channels.private_telegram import PrivateTelegram

            return PrivateTelegram(self).handle(update, secret_header)
        bindings = {
            a.payload["chat_id"]: a.payload["actor_id"]
            for a in self.store.artifacts_for(kind="telegram.binding.v1")
        }
        if "message" in update and (
            self._settings.telegram_member_names
            or bindings
            or current_group(self.store)
            or self.execution_mode.value != "dry_run"
        ):
            import copy

            sender_id = str(update["message"].get("from", {}).get("id", ""))
            name = bindings.get(sender_id) or self._settings.telegram_member_names.get(sender_id)
            from steward.channels.private_telegram import member_role

            if not member_role(self, name):
                raise PermissionError("Telegram sender is not a registered community member")
            update = copy.deepcopy(update)
            update["message"]["from"]["first_name"] = name
            update["message"]["from"].pop("last_name", None)
        with self.store.atomic():
            result = self._telegram.handle_update(update, secret_header=secret_header)
            external_id = str(update["update_id"])
            item = self.store.inbox_item("telegram_shadow", external_id)
            if item and item.message:
                from steward.domain.clock import utc_now
                from steward.store import WorkflowArtifact

                key = stable_id("telegram-receipt", external_id)
                if not self.store.artifact("telegram.group-receipt.v1", key):
                    self.store.put_configuration(
                        WorkflowArtifact(
                            artifact_id=key,
                            kind="telegram.group-receipt.v1",
                            created_at=utc_now(),
                            payload={"external_id": external_id, "chat_id": item.message.chat_id},
                        )
                    )
            return result

    def handle_vendor_reply(
        self,
        inbound: InboundMessage,
        *,
        source_id: str,
    ) -> VendorReplyResult:
        from steward.operations.appointments import Appointments

        route = self._vendor_replies._route(inbound)
        if route.case.accepted_quote_id:
            result = Appointments(self).ingest(inbound, source_id=source_id)
            if result is not None:
                return result
        return self._vendor_replies.ingest(inbound, source_id=source_id)

    def decide_quotes(
        self,
        case_id: str,
        *,
        idempotency_key: str | None = None,
    ) -> QuoteDecisionResult:
        safe_case_id = case_id.strip()
        if idempotency_key is None:
            from steward.procurement.replies import QUOTE_ARTIFACT_KIND

            evidence = (
                *self.store.artifacts_for(kind=QUOTE_ARTIFACT_KIND, case_id=safe_case_id),
                *self.store.artifacts_for(kind="quote.rejection.v1", case_id=safe_case_id),
                *self.store.artifacts_for(kind="quote.supersession.v1", case_id=safe_case_id),
            )
            self.refresh_policy()
            case = self.store.get_case(safe_case_id)
            current_policy = self._policy.policy_for(case.category) if case else None
            idempotency_key = "quote-evidence:" + stable_id(
                *(a.artifact_id for a in evidence),
                current_policy.model_dump_json() if current_policy else "no-policy",
                self._policy.settings.model_dump_json(),
            )
        return self._quote_decisions.decide(
            case_id=safe_case_id,
            idempotency_key=(
                idempotency_key
                if idempotency_key is not None
                else f"runtime:quote-decision:v1:{safe_case_id}"
            ),
        )

    def plan_resolution(
        self,
        case_id: str,
        *,
        idempotency_key: str | None = None,
        revise: bool = False,
    ) -> ResolutionPlanningResult:
        safe_case_id = case_id.strip()
        return self._resolution_planning.plan(
            case_id=safe_case_id,
            revise=revise,
            idempotency_key=(
                idempotency_key
                if idempotency_key is not None
                else f"runtime:resolution-plan:v1:{safe_case_id}"
            ),
        )

    def prepare_meeting(
        self,
        case_id: str,
        *,
        policy: MeetingSchedulingPolicy,
        candidate_slots: tuple[MeetingCandidateSlot, ...],
        availability: tuple[MeetingAvailabilityResponse, ...],
        idempotency_key: str | None = None,
    ) -> MeetingPreparationResult:
        """Build an immutable meeting packet without creating any outbound intent."""
        safe_case_id = case_id.strip()
        return self._meeting_preparation.prepare(
            case_id=safe_case_id,
            policy=policy,
            candidate_slots=candidate_slots,
            availability=availability,
            idempotency_key=(
                idempotency_key
                if idempotency_key is not None
                else f"runtime:meeting-packet:v1:{safe_case_id}"
            ),
        )

    def extract_meeting_minutes(
        self,
        case_id: str,
        *,
        held_at: datetime,
        body_text: str,
        source_id: str,
        participant_count: int,
        governance: MeetingGovernancePolicy,
        simulated: bool = False,
        idempotency_key: str | None = None,
    ) -> MeetingMinutesResult:
        """Freeze minutes before invoking the authority-free extraction model."""
        safe_case_id = case_id.strip()
        return self._meeting_lifecycle.extract_minutes(
            case_id=safe_case_id,
            held_at=held_at,
            body_text=body_text,
            source_id=source_id,
            participant_count=participant_count,
            governance=governance,
            simulated=simulated,
            idempotency_key=(
                idempotency_key
                if idempotency_key is not None
                else f"runtime:meeting-minutes:v1:{safe_case_id}"
            ),
        )

    def confirm_meeting_decisions(
        self,
        case_id: str,
        *,
        confirmation: MeetingDecisionConfirmation,
    ) -> MeetingDecisionResult:
        """Confirm exact candidate IDs as a configured human actor."""
        return self._meeting_lifecycle.confirm_decision(
            case_id=case_id.strip(),
            confirmation=confirmation,
        )

    def record_meeting_action_completion(
        self,
        case_id: str,
        *,
        action_id: str,
        actor_id: str,
        actor_label: str,
        source_id: str,
        notes: str,
        completed_at: datetime,
    ) -> MeetingActionCompletionResult:
        """Record source-backed completion for one confirmed action."""
        return self._meeting_lifecycle.complete_action(
            case_id=case_id.strip(),
            action_id=action_id,
            actor_id=actor_id,
            actor_label=actor_label,
            source_id=source_id,
            notes=notes,
            completed_at=completed_at,
        )

    def verify_meeting_outcome(
        self,
        case_id: str,
        *,
        outcome: MeetingVerificationOutcome,
        actor_id: str,
        actor_label: str,
        source_id: str,
        notes: str,
        responded_at: datetime,
    ) -> MeetingVerificationResult:
        """Apply a configured human's final verification and atomic closure."""
        return self._meeting_lifecycle.verify_outcome(
            case_id=case_id.strip(),
            outcome=outcome,
            actor_id=actor_id,
            actor_label=actor_label,
            source_id=source_id,
            notes=notes,
            responded_at=responded_at,
        )

    def handle_s3_mail_event(
        self,
        event: dict[str, Any],
    ) -> tuple[VendorReplyResult, ...]:
        if self._inbound_reader is None or self._inbound_bucket is None:
            raise RuntimeError("SES/S3 inbound mail is disabled by runtime settings")
        objects = parse_s3_mail_event(event, expected_bucket=self._inbound_bucket)
        results: list[VendorReplyResult] = []
        for item in objects:
            inbound = self._inbound_reader.read(
                item.key,
                received_at=item.received_at,
            )
            results.append(self._vendor_replies.ingest(inbound, source_id=item.source_id))
        return tuple(results)

    def tick(self, *, due_limit: int = 100) -> StewardRuntimeReport:
        self.refresh_policy()
        inbound = self._runner.tick(due_limit=due_limit)
        from steward.operations.community import CommunityWorkflow

        CommunityWorkflow(self).reconcile_drafts()
        meeting_due_results = self._meeting_lifecycle.sweep_due(case_ids=inbound.due_case_ids)
        queued: list[QueuedRfqBatch] = []
        failures = []
        token = uuid4().hex
        from steward.operations.lease import JobHeartbeat
        from steward.store import StaleLeaseToken

        # Claim just-in-time; a large claimed batch must not expire while earlier jobs run.
        for _ in range(due_limit):
            claimed = self.store.claim_jobs(
                now=self._clock.now(),
                token=token,
                lease_seconds=self._settings.lease_seconds,
                limit=1,
            )
            if not claimed:
                break
            job = claimed[0]
            try:
                with JobHeartbeat(
                    self.store, self._clock, job.job_id, token, self._settings.lease_seconds
                ) as heartbeat:
                    batch, successors = self._maintenance.handle(job)
                    if heartbeat.failure:
                        raise heartbeat.failure
                if batch is not None:
                    queued.append(batch)
                self.store.complete_job(
                    job.job_id, token=token, now=self._clock.now(), successors=successors
                )
            except Exception as exc:
                with suppress(StaleLeaseToken):
                    self.store.fail_job(
                        job.job_id,
                        token=token,
                        now=self._clock.now(),
                        error_code=type(exc).__name__,
                    )
                failures.append(job.job_id)
        outbound = self._outbox.dispatch()
        self._maintenance.reconcile_rfqs()
        self._maintenance.reconcile_orders()
        from steward.operations.appointments import Appointments

        Appointments(self).reconcile()
        return StewardRuntimeReport(
            inbound=inbound,
            queued_rfqs=tuple(queued),
            outbound=outbound,
            meeting_due_results=meeting_due_results,
            workflow_failures=tuple(failures),
        )

    def refresh_policy(self):
        from steward.domain.enums import Category
        from steward.domain.models import ApprovalPolicy, GlobalSettings

        records = self.store.artifacts_for(kind="configuration.v1")
        if records:
            config = max(records, key=lambda a: a.payload["expected_version"]).payload
            self._policy._settings = GlobalSettings.model_validate(config["settings"])
            self._policy._policies = {
                Category(key): ApprovalPolicy.model_validate(value)
                for key, value in config["policies"].items()
            }

    def _validate_delivery(self, item, payload):
        from decimal import Decimal

        from steward.mail import MailDeliveryError
        from steward.operations.outbox import OutboundPurpose

        self.refresh_policy()
        if self._policy.settings.kill_switch:
            raise MailDeliveryError("global_kill_switch", retryable=True)
        case = self.store.get_case(item.case_id)
        if case is None:
            raise MailDeliveryError("missing_case", retryable=False)
        from steward.domain.clock import parse_datetime
        from steward.operations.clarifications import effective_assessment

        assessment = effective_assessment(self.store, case)
        if assessment is None:
            raise MailDeliveryError("missing_assessment", retryable=False)
        vendor = next((v for v in self._vendors if v.vendor_id == payload.vendor_id), None)
        if vendor is None or not vendor.allowlisted or tuple(payload.message.to) != (vendor.email,):
            raise MailDeliveryError("vendor_target_mismatch", retryable=False)
        decision = self._policy.evaluate_intake(
            category=case.category, triage_confidence=assessment.result.confidence
        )
        if (
            not decision.may_contact_third_parties
            or vendor.vendor_id not in decision.allowed_vendor_ids
        ):
            raise MailDeliveryError("contact_no_longer_authorized", retryable=False)
        if payload.purpose == OutboundPurpose.FOLLOW_UP:
            proposals = self.store.artifacts_for(
                kind="appointment.proposal.v1", case_id=case.case_id
            )
            proposal = next(
                (
                    a
                    for a in proposals
                    if stable_id("appointment-accept", a.artifact_id) == item.outbox_id
                ),
                None,
            )
            if proposal:
                configs = self.store.artifacts_for(kind="appointment.rules.v1")
                overrides = [
                    a
                    for a in self.store.artifacts_for(
                        kind="appointment.override.v1", case_id=case.case_id
                    )
                    if a.payload["appointment_id"] == proposal.artifact_id
                ]
                rule_id = (
                    overrides[-1].payload["rules_id"] if overrides else proposal.payload["rules_id"]
                )
                if proposal != proposals[-1] or not configs or configs[-1].artifact_id != rule_id:
                    raise MailDeliveryError("appointment_changed_before_send", retryable=False)
                from steward.agents.order_reply import OrderReply

                if (
                    OrderReply.model_validate(proposal.payload["facts"]).starts_at
                    <= self._clock.now()
                ):
                    raise MailDeliveryError("appointment_time_expired", retryable=False)
        if payload.purpose == OutboundPurpose.COMMITMENT:
            decisions = self.store.artifacts_for(kind="quote_decision.v1", case_id=case.case_id)
            quote = next(
                (
                    d.payload["selected_quote"]
                    for d in decisions
                    if d.payload["selected_quote"]["quote_id"] == case.accepted_quote_id
                ),
                None,
            )
            if not quote:
                raise MailDeliveryError("missing_quote_evidence", retryable=False)
            from steward.domain.models import Quote

            current_quote = Quote.model_validate(quote)
            if current_quote.valid_until and current_quote.valid_until <= self._clock.now():
                raise MailDeliveryError("quote_expired_before_send", retryable=False)
            reservations = self.store.reservations(case.case_id)
            own = next(
                (
                    r
                    for r in reservations
                    if r.quote_id == case.accepted_quote_id and r.status == "reserved"
                ),
                None,
            )
            if own is None:
                raise MailDeliveryError("missing_budget_reservation", retryable=False)
            selected_decision = next(
                (a for a in decisions if stable_id("reserve", a.artifact_id) == own.reservation_id),
                None,
            )
            if selected_decision is None or payload.authority_audit_id != stable_id(
                "commit-audit", selected_decision.artifact_id
            ):
                raise MailDeliveryError("superseded_commitment", retryable=False)
            portfolio = self.store.artifact(
                "quote_portfolio.v1", selected_decision.payload["portfolio_id"]
            )
            superseded = {
                a.payload["previous_quote_id"]
                for a in self.store.artifacts_for(
                    kind="quote.supersession.v1", case_id=case.case_id
                )
            }
            from steward.operations.quote_completeness import missing_fields

            superseded.update(
                a.payload["quote_id"]
                for a in self.store.artifacts_for(kind="quote.rejection.v1", case_id=case.case_id)
            )
            selected_artifact = self.store.artifact("vendor_quote.v1", current_quote.quote_id)
            if (
                not selected_artifact
                or missing_fields(selected_artifact.payload)
                or current_quote.quote_id in superseded
            ):
                raise MailDeliveryError("quote_incomplete_or_rejected", retryable=False)
            actual = {
                a.artifact_id
                for a in self.store.artifacts_for(kind="vendor_quote.v1", case_id=case.case_id)
                if a.artifact_id not in superseded
                and not missing_fields(a.payload)
                and (
                    not a.payload["quote"].get("valid_until")
                    or parse_datetime(a.payload["quote"]["valid_until"]) >= self._clock.now()
                )
            }
            if actual != {q["quote_artifact_id"] for q in portfolio.payload["quotes"]}:
                raise MailDeliveryError("new_quote_requires_review", retryable=False)
            pending = sum(
                (
                    r.amount
                    for r in self.store.reservations()
                    if r.status == "reserved"
                    and r.reservation_id != own.reservation_id
                    and r.category == own.category
                    and r.currency == own.currency
                ),
                Decimal(0),
            )
            spend = self.store.month_to_date_spend(
                case.category, as_of=self._clock.now(), currency=case.currency
            )
            verdict = self.commitment_policy(case).authorize_commitment(
                category=case.category,
                triage_confidence=assessment.result.confidence,
                vendor_id=vendor.vendor_id,
                vendor_allowlisted=vendor.allowlisted,
                amount=own.amount,
                currency=own.currency,
                month_to_date_spend=spend + pending,
            )
            if not verdict.may_commit_spend:
                raise MailDeliveryError("commitment_no_longer_authorized", retryable=False)

    def commitment_policy(self, case, quote_id=None):
        """A reviewed order may use prepare-only authority; caps and kill switch still apply."""
        from steward.domain.enums import ApprovalMode

        policy = self._policy.policy_for(case.category)
        if policy and policy.mode == ApprovalMode.PREPARE_ONLY:
            approvals = self.store.artifacts_for(
                kind="maintenance.approval.v1", case_id=case.case_id
            )
            quote_ids = {quote_id or case.accepted_quote_id}
            if any(
                a.payload["actor_id"] in self._bundle.property_profile.management_committee
                and a.payload["quote_id"] in quote_ids
                and a.payload["policy_version"] == stable_id(policy.model_dump_json())
                for a in approvals
            ):
                policies = dict(self._policy._policies)
                policies[case.category] = policy.model_copy(
                    update={"mode": ApprovalMode.ALWAYS_APPROVE}
                )
                return PolicyEngine(policies, self._policy.settings)
        return self._policy

    def _continue_opened_case(self, case_id: str) -> QueuedRfqBatch | None:
        from steward.domain.enums import TERMINAL_STATUSES

        current = self.store.get_case(case_id)
        version = self.store.case_version(case_id)
        if current is None or version is None:
            raise RuntimeError("workflow lost its case")
        if current.status in TERMINAL_STATUSES:
            return None
        recent = [
            r
            for r in self.store.records()
            if r.outcome_verified
            and r.asset_id == current.asset_id
            and current.asset_id is not None
            and r.category == current.category
            and r.case_id != case_id
            and 0
            <= (current.opened_at - r.closed_at).total_seconds()
            <= self._policy.settings.recurrence_window_days * 86400
        ]
        reviewed = self.store.artifacts_for(kind="warranty.check.v1", case_id=case_id)
        if recent and not reviewed:
            self.store.add_human_task(
                HumanTask(
                    task_id=stable_id("warranty-check", case_id),
                    case_id=case_id,
                    kind="warranty_check",
                    title="Check the previous repair first",
                    reason="This asset has a recent verified repair. Check the original warranty "
                    "before authorizing another paid job. Prior records: "
                    + ", ".join(r.case_id for r in recent),
                    created_at=self._clock.now(),
                    due_at=self._clock.now(),
                    expected_version=version,
                )
            )
            return None
        from steward.operations.clarifications import Clarifications, effective_assessment

        assessment = effective_assessment(self.store, current)
        if (
            (assessment and assessment.result.missing_information)
            or current.asset_id is None
            or (
                assessment
                and assessment.result.confidence < self._policy.settings.min_triage_confidence
            )
        ):
            Clarifications(self).request(current)
            return None
        if assessment is None:
            raise RuntimeError("workflow lost its triage assessment")
        spend = self.store.month_to_date_spend(
            current.category, as_of=self._clock.now(), currency=current.currency
        )
        decision = self._policy.evaluate_intake(
            category=current.category,
            triage_confidence=assessment.result.confidence,
            month_to_date_spend=spend,
        )
        if (
            not decision.may_contact_third_parties
            or current.category not in self._settings.auto_queue_rfq_categories
            or current.asset_id is None
        ):
            self.store.add_human_task(
                HumanTask(
                    task_id=stable_id("intake-review", case_id),
                    case_id=case_id,
                    kind="intake_review",
                    title="Choose the next step",
                    reason=decision.reason,
                    created_at=self._clock.now(),
                    due_at=self._clock.now(),
                    expected_version=version,
                )
            )
            return None
        return self._rfq.queue(
            case=current,
            expected_version=version,
            idempotency_key=f"runtime:rfq:v1:{case_id}",
            triage_confidence=assessment.result.confidence,
            asset=self._assets[current.asset_id],
            vendors=self._vendors,
            month_to_date_spend=spend,
        )

    def close(self) -> None:
        self.store.close()

    def __enter__(self) -> StewardRuntime:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def build_runtime(
    settings: StewardSettings | None = None,
    *,
    classifier: TriageClassifier | None = None,
    quote_extractor: QuoteExtractor | None = None,
    decision_recommender: QuoteRecommender | None = None,
    resolution_planner: ResolutionPlanner | None = None,
    meeting_agenda_planner: MeetingAgendaPlanner | None = None,
    meeting_minutes_extractor: MeetingMinutesExtractor | None = None,
    clock: Clock | None = None,
    transport: MailTransport | None = None,
    inbound_reader: _InboundMailReader | None = None,
    seed: SeedBundle | None = None,
) -> StewardRuntime:
    """Build the runtime without performing network or outbound side effects."""
    configured = settings or StewardSettings()
    if configured.channel_configuration:
        channels = json.loads(configured.channel_configuration.get_secret_value())
        allowed = {
            "telegram_enabled",
            "telegram_delivery_mode",
            "telegram_bot_token",
            "telegram_bot_username",
            "telegram_webhook_secret",
            "telegram_allowed_chat_ids",
            "telegram_member_names",
        }
        if not set(channels) <= allowed:
            raise ValueError("unsupported channel configuration key")
        configured = StewardSettings.model_validate(
            {**configured.model_dump(), **channels, "channel_configuration": None}
        )
    if configured.database_secret and not configured.database_url:
        from psycopg.conninfo import make_conninfo
        from pydantic import SecretStr

        secret = json.loads(configured.database_secret.get_secret_value())
        configured = configured.model_copy(
            update={
                "database_url": SecretStr(
                    make_conninfo(
                        host=secret["host"],
                        port=secret.get("port", 5432),
                        dbname=secret.get("dbname", "steward"),
                        user=secret["username"],
                        password=secret["password"],
                        sslmode="verify-full",
                        sslrootcert="/app/rds-ca.pem",
                    )
                )
            }
        )
    runtime_clock = clock or SystemClock()
    bundle = seed or load_seed(configured.seed_dir)
    if configured.database_url:
        from steward.store.postgres import PostgresOperationalStore

        store = PostgresOperationalStore(configured.database_url.get_secret_value())
        if configured.semantic_memory_enabled:
            from steward.memory.semantic import SemanticIndex

            store.semantic_index = SemanticIndex(
                store, region=configured.aws_region, profile=configured.aws_profile
            )
    else:
        store = SqliteOperationalStore(configured.database_path)
    if clock is None and configured.simulation_clock_start:
        from steward.simulation import PersistentClock

        start = datetime.fromisoformat(configured.simulation_clock_start.replace("Z", "+00:00"))
        if start.tzinfo is None:
            raise ValueError("simulation clock start requires a timezone")
        runtime_clock = PersistentClock(store, start)
    policy = PolicyEngine(bundle.policies, bundle.settings)
    triage = classifier or StrandsTriageClassifier.bedrock(
        model_id=configured.bedrock_model_id,
        profile_name=configured.aws_profile,
        region_name=configured.aws_region,
        temperature=0.0,
        max_tokens=configured.bedrock_max_tokens,
    )
    intake = IntakeService(
        classifier=triage,
        reconciler=triage if isinstance(triage, StrandsTriageClassifier) else None,
        store=store,
        policy=policy,
        assets=bundle.assets,
        clock=runtime_clock,
        memory=store,
        max_reconciliation_attempts=2,
        spend_lookup=lambda category, at: store.month_to_date_spend(
            category,
            as_of=at,
            currency=bundle.settings.default_currency,
        ),
    )
    proactive = ProactiveMaintenanceEngine(
        policy=policy,
        rules=default_playbooks().proactive_rules,
    )
    runner = OperationalRunner(
        store=store,
        intake=intake,
        clock=runtime_clock,
        proactive=proactive,
        lease_seconds=configured.lease_seconds,
        batch_limit=configured.inbound_batch_limit,
        max_attempts=configured.inbound_max_attempts,
    )

    scheme = AddressScheme(
        management_domain=configured.management_domain,
        vendor_domain=configured.vendor_domains[0],
        management_local=configured.management_local_part,
        management_display_name=configured.management_display_name,
    )
    guard = RecipientGuard.of(*configured.vendor_domains)
    outbound_transport = transport
    if outbound_transport is None:
        if configured.execution_mode is RuntimeExecutionMode.DRY_RUN:
            outbound_transport = RecordingMailTransport(guard=guard)
        else:
            outbound_transport = SesMailTransport(
                guard=guard,
                region_name=configured.aws_region,
                configuration_set_name=configured.ses_configuration_set_name,
            )
    dispatcher = OutboxDispatcher(
        store=store,
        transport=outbound_transport,
        recipient_guard=guard,
        clock=runtime_clock,
        execution_mode=configured.execution_mode,
        lease_seconds=configured.lease_seconds,
        batch_limit=configured.outbox_batch_limit,
        max_attempts=configured.outbox_max_attempts,
    )
    rfq = RfqOutboxCoordinator(
        store=store,
        policy=policy,
        recipient_guard=guard,
        address_scheme=scheme,
        clock=runtime_clock,
    )
    quote_model = quote_extractor or StrandsQuoteExtractor.bedrock(
        configured.bedrock_model_id,
        profile_name=configured.aws_profile,
        region_name=configured.aws_region,
        temperature=0.0,
        max_tokens=configured.quote_max_tokens,
    )

    def quote_continuations(case, artifact):
        from datetime import timedelta

        from steward.store.workflow import WorkflowJob

        current_policy = policy.policy_for(case.category)
        versions = store.artifacts_for(kind="configuration.v1")
        if versions:
            from steward.domain.models import ApprovalPolicy

            latest = max(versions, key=lambda a: a.payload["expected_version"])
            configured_policy = latest.payload["policies"].get(case.category.value)
            if configured_policy:
                current_policy = ApprovalPolicy.model_validate(configured_policy)
        from steward.operations.quote_completeness import missing_fields

        missing = missing_fields(artifact.payload) if artifact.kind == "vendor_quote.v1" else []
        is_quote = artifact.kind == "vendor_quote.v1" and not missing
        due = (
            max(
                runtime_clock.now(),
                case.opened_at
                + timedelta(hours=current_policy.quote_wait_hours if current_policy else 24),
            )
            if is_quote
            else runtime_clock.now()
        )
        return (
            WorkflowJob(
                job_id=stable_id("reply-continuation", artifact.artifact_id),
                case_id=case.case_id,
                kind="procurement.review" if is_quote else "vendor.clarify",
                source_id=artifact.artifact_id,
                created_at=runtime_clock.now(),
                due_at=due,
                expected_version=store.case_version(case.case_id),
                payload={}
                if is_quote
                else {
                    "vendor_id": artifact.payload.get("vendor_id")
                    or artifact.payload["quote"]["vendor_id"],
                    "reason": "Missing: " + ", ".join(missing)
                    if missing
                    else artifact.payload["reason"],
                },
            ),
        )

    vendor_replies = VendorReplyService(
        store=store,
        extractor=quote_model,
        address_scheme=scheme,
        vendors=tuple(bundle.vendors),
        continuations=quote_continuations,
    )
    recommendation_model = decision_recommender or StrandsQuoteRecommender.bedrock(
        configured.bedrock_model_id,
        profile_name=configured.aws_profile,
        region_name=configured.aws_region,
        temperature=0.0,
        max_tokens=configured.decision_max_tokens,
    )
    quote_decisions = QuoteDecisionService(
        store=store,
        policy=policy,
        recommender=recommendation_model,
        vendors=tuple(bundle.vendors),
        clock=runtime_clock,
    )
    from steward.agents.coordinator import StrandsCoordinator

    resolution_model = resolution_planner or StrandsCoordinator(
        settings=configured, store=store, clock=runtime_clock
    )
    if resolution_planner is None and configured.agentcore_runtime_arn:
        from steward.agents.remote import AgentCoreResolutionPlanner

        resolution_model = AgentCoreResolutionPlanner(
            settings=configured, store=store, clock=runtime_clock
        )
    resolution_planning = ResolutionPlanningService(
        store=store,
        planner=resolution_model,
        vendors=tuple(bundle.vendors),
        clock=runtime_clock,
        policy=policy,
    )
    agenda_model = meeting_agenda_planner or StrandsMeetingAgendaPlanner.bedrock(
        configured.bedrock_model_id,
        profile_name=configured.aws_profile,
        region_name=configured.aws_region,
        temperature=0.0,
        max_tokens=configured.meeting_max_tokens,
    )
    meeting_preparation = MeetingPreparationService(
        store=store,
        planning=resolution_planning,
        agenda_planner=agenda_model,
        vendors=tuple(bundle.vendors),
        clock=runtime_clock,
    )
    minutes_model = meeting_minutes_extractor or StrandsMeetingMinutesExtractor.bedrock(
        configured.bedrock_model_id,
        profile_name=configured.aws_profile,
        region_name=configured.aws_region,
        temperature=0.0,
        max_tokens=configured.minutes_max_tokens,
    )
    meeting_lifecycle = MeetingLifecycleService(
        store=store,
        meeting=meeting_preparation,
        policy=policy,
        extractor=minutes_model,
        clock=runtime_clock,
        inference_lease_seconds=configured.inference_lease_seconds,
        inference_max_attempts=configured.inference_max_attempts,
    )
    configured_reader = inbound_reader
    if configured.ses_inbound_enabled and configured_reader is None:
        bucket = configured.ses_inbound_bucket
        if bucket is None:  # guarded by settings validation
            raise RuntimeError("validated SES inbound bucket disappeared")
        configured_reader = SesInboundReader(
            bucket=bucket,
            region_name=configured.aws_region,
        )

    telegram = None
    if configured.telegram_enabled:
        from steward.channels.telegram_groups import group_ids

        secret = configured.telegram_webhook_secret
        if secret is None:  # guarded by settings validation; keeps type check explicit
            raise RuntimeError("validated Telegram secret disappeared")
        telegram = TelegramShadowAdapter(
            secret_token=secret.get_secret_value(),
            allowed_chat_ids=configured.telegram_allowed_chat_ids,
            inbox=store,
            clock=runtime_clock,
            chat_ids_provider=lambda: group_ids(store, configured),
            validation_clock=SystemClock(),
        )

    return StewardRuntime(
        settings=configured,
        store=store,
        runner=runner,
        outbox=dispatcher,
        rfq=rfq,
        assets={asset.asset_id: asset for asset in bundle.assets},
        vendors=tuple(bundle.vendors),
        transport=outbound_transport,
        telegram=telegram,
        vendor_replies=vendor_replies,
        quote_decisions=quote_decisions,
        resolution_planning=resolution_planning,
        meeting_preparation=meeting_preparation,
        meeting_lifecycle=meeting_lifecycle,
        inbound_reader=configured_reader,
        inbound_bucket=(configured.ses_inbound_bucket if configured.ses_inbound_enabled else None),
        clock=runtime_clock,
        policy=policy,
        bundle=bundle,
    )


def main() -> int:
    """Run one scheduler-friendly tick and emit a non-sensitive JSON summary."""
    with build_runtime() as runtime:
        report = runtime.tick()
        print(
            json.dumps(
                {
                    "execution_mode": runtime.execution_mode.value,
                    "processed_inbound": len(report.inbound.processed_external_ids),
                    "inbound_failures": len(report.inbound.failures),
                    "queued_rfq_cases": [batch.case.case_id for batch in report.queued_rfqs],
                    "outbox_deliveries": len(report.outbound.deliveries),
                    "outbox_failures": len(report.outbound.failures),
                    "real_delivery_enabled": report.outbound.real_delivery_enabled,
                },
                sort_keys=True,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
