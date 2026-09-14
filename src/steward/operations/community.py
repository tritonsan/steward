"""Product commands and durable continuations for shared community decisions."""

from datetime import timedelta

from steward.domain.clock import parse_datetime
from steward.domain.enums import CaseStatus, ResolutionPath
from steward.meetings import (
    MeetingAvailabilityResponse,
    MeetingCandidateSlot,
    MeetingDecisionConfirmation,
    MeetingGovernancePolicy,
    MeetingSchedulingPolicy,
    MeetingVerificationOutcome,
)
from steward.store import ConcurrencyConflict, WorkflowArtifact
from steward.store.workflow import HumanTask, stable_id


class CommunityWorkflow:
    def __init__(self, runtime):
        self.runtime, self.store, self.clock = runtime, runtime.store, runtime._clock

    def task(self, case_id, kind, title, reason, *, due=None, suffix=""):
        self.store.add_human_task(
            HumanTask(
                task_id=stable_id(kind, case_id, suffix),
                case_id=case_id,
                kind=kind,
                title=title,
                reason=reason,
                created_at=self.clock.now(),
                due_at=due or self.clock.now(),
                expected_version=self.store.case_version(case_id),
            )
        )

    def opened(self, case, source_id=None):
        result = self.runtime.plan_resolution(
            case.case_id,
            idempotency_key=f"community-plan:{case.case_id}:{source_id}" if source_id else None,
            revise=bool(source_id),
        )
        if result.plan.recommendation.path == ResolutionPath.MEETING_RESOLUTION:
            self.enqueue_draft(case, result.plan.plan_id)
            self.task(
                case.case_id,
                "meeting_schedule",
                "Offer meeting times",
                "Choose candidate times and a quorum; participants confirm their own availability.",
            )
        else:
            self.task(
                case.case_id,
                "intake_review",
                "Review this community concern",
                result.plan.recommendation.rationale,
            )

    def enqueue_draft(self, case, plan_id):
        self.store.enqueue_job(
            self.runtime._maintenance.job(
                case, "community.draft", due_at=self.clock.now(), source_id=plan_id
            )
        )

    def reconcile_drafts(self):
        """Recover earlier meeting plans that never had a preparation job."""
        from steward.meetings.dossier import draft_for_plan
        from steward.orchestration.planning import ResolutionPlanningIntegrityError

        for case in self.store.list_open_cases():
            if case.status not in (CaseStatus.DETECTED, CaseStatus.PLANNING):
                continue
            if self.store.artifacts_for(kind="meeting_packet.v1", case_id=case.case_id):
                continue
            try:
                plan = self.runtime._resolution_planning.load(case.case_id)
            except ResolutionPlanningIntegrityError:
                # A resolution job can be between freezing its input and committing output.
                # Its existing retry/review path owns recovery; do not block other jobs.
                continue
            if (
                plan
                and plan.plan.recommendation.path == ResolutionPath.MEETING_RESOLUTION
                and not draft_for_plan(self.store, case.case_id, plan.plan.plan_id)
            ):
                self.enqueue_draft(case, plan.plan.plan_id)

    def handle(self, job):
        if job.kind == "community.draft":
            self.runtime._meeting_preparation.prepare_draft(job.case_id, job.source_id)
            return
        if job.kind == "community.assignment_due":
            from steward.operations.meeting_continuity import MeetingContinuity

            MeetingContinuity(self.runtime).assignment_due(job)
            return
        if job.kind == "community.deadline":
            from steward.operations.meeting_continuity import MeetingContinuity

            MeetingContinuity(self.runtime).deadline(job)
            return
        if job.kind == "community.route":
            self.opened(self.store.get_case(job.case_id), source_id=job.source_id)
            if any(
                t.kind == "meeting_schedule" and t.status == "open"
                for t in self.store.human_tasks(job.case_id)
            ):
                self.resolve(job.case_id, {"intake_review"}, "Resolution prepared")
            return
        if job.kind == "community.prepare":
            artifact = self.store.artifact("community.schedule.v1", job.source_id)
            from steward.operations.meeting_continuity import active_schedule

            if (
                not artifact
                or active_schedule(self.store, job.case_id).artifact_id != artifact.artifact_id
            ):
                return
            if self.store.artifacts_for(kind="meeting.invalidated.v1", case_id=job.case_id):
                return
            config = artifact.payload
            responses = self.store.artifacts_for(
                kind="community.availability.v1", case_id=job.case_id
            )
            latest = {}
            for a in sorted(responses, key=lambda a: a.payload["version"]):
                if (
                    a.payload.get(
                        "schedule_id", artifact.artifact_id if config.get("round", 1) == 1 else None
                    )
                    != artifact.artifact_id
                ):
                    continue
                latest[a.payload["participant_id"]] = MeetingAvailabilityResponse(
                    participant_id=a.payload["participant_id"],
                    source_id=a.artifact_id,
                    available_slot_ids=tuple(a.payload["available_slot_ids"]),
                )
            policy = MeetingSchedulingPolicy.model_validate(config["policy"])
            slots = tuple(MeetingCandidateSlot.model_validate(s) for s in config["slots"])
            eligible = [
                s
                for s in slots
                if s.starts_at >= self.clock.now() + timedelta(hours=policy.minimum_notice_hours)
            ]
            quorum = any(
                sum(s.slot_id in r.available_slot_ids for r in latest.values()) >= policy.quorum
                and all(
                    p in latest and s.slot_id in latest[p].available_slot_ids
                    for p in policy.required_participant_ids
                )
                for s in eligible
            )
            if not quorum:
                self.task(
                    job.case_id,
                    "meeting_availability",
                    "Waiting for participant availability",
                    "A meeting is scheduled only when the configured quorum is available.",
                )
                return
            self.runtime.prepare_meeting(
                job.case_id,
                policy=policy,
                candidate_slots=slots,
                availability=tuple(latest.values()),
            )
            self.resolve(
                job.case_id, {"meeting_schedule", "meeting_availability"}, "Meeting packet prepared"
            )
            self.task(
                job.case_id,
                "meeting_minutes",
                "Record the meeting minutes",
                "Upload written minutes after the meeting. "
                "Extracted decisions require confirmation.",
            )
        elif job.kind == "community.minutes":
            data = job.payload
            schedule = self.store.artifacts_for(kind="community.schedule.v1", case_id=job.case_id)[
                -1
            ]
            managers = tuple(self.runtime._bundle.property_profile.management_committee)

            result = self.runtime.extract_meeting_minutes(
                job.case_id,
                held_at=parse_datetime(data["held_at"]),
                body_text=data["notes"],
                source_id=job.source_id,
                participant_count=data["participant_count"],
                governance=MeetingGovernancePolicy(
                    action_owner_ids=tuple(schedule.payload["policy"]["eligible_participant_ids"]),
                    authorized_decider_ids=managers,
                    authorized_verifier_ids=managers,
                ),
                simulated=self.runtime.execution_mode.value == "dry_run",
            )
            self.resolve(job.case_id, {"meeting_minutes"}, "Minutes extracted")
            self.task(
                job.case_id,
                "meeting_confirm",
                "Confirm the extracted decisions",
                result.candidates.extraction.summary,
            )
        else:
            raise ValueError("unknown community job")

    def resolve(self, case_id, kinds, notes, actor_id="Steward", role="manager"):
        for task in self.store.human_tasks(case_id):
            if task.kind in kinds and task.status == "open":
                self.store.resolve_human_task(
                    task.task_id,
                    actor_id=actor_id,
                    role=role,
                    expected_version=self.store.case_version(case_id),
                    response={"notes": notes},
                )

    def command(self, case, action, data, actor_id, source):
        now = self.clock.now()
        if action in ("meeting_revise_decision", "meeting_reschedule"):
            from steward.operations.meeting_continuity import MeetingContinuity

            MeetingContinuity(self.runtime).linked_case(
                case,
                actor_id=actor_id,
                notes=data["notes"],
                source=source,
                kind="decision_revision"
                if action == "meeting_revise_decision"
                else "meeting_reschedule",
            )
            return
        if action == "meeting_reassign":
            from steward.operations.meeting_continuity import MeetingContinuity

            MeetingContinuity(self.runtime).reassign(case, data, actor_id, source)
            return
        if action == "meeting_schedule":
            residents = {r.display_name for r in self.runtime._bundle.residents}
            policy = MeetingSchedulingPolicy.model_validate(data["policy"])
            if not set(policy.eligible_participant_ids) <= residents:
                raise ValueError("meeting contains an unregistered participant")
            slots = tuple(MeetingCandidateSlot.model_validate(s) for s in data["slots"])
            if not slots or len({s.slot_id for s in slots}) != len(slots):
                raise ValueError("unique candidate slots are required")
            if any(
                s.starts_at <= now + timedelta(hours=policy.minimum_notice_hours) for s in slots
            ):
                raise ValueError("candidate times require the configured minimum notice")
            if self.store.artifacts_for(kind="community.schedule.v1", case_id=case.case_id):
                raise ConcurrencyConflict("meeting times already offered")

            deadline = (
                parse_datetime(data["response_deadline"])
                if data.get("response_deadline")
                else now + timedelta(hours=24)
            )
            if (
                deadline.tzinfo is None
                or deadline <= now
                or deadline >= min(s.starts_at for s in slots)
            ):
                raise ValueError("Response deadline must precede candidate times")
            retry_slots = tuple(
                MeetingCandidateSlot.model_validate(s) for s in data.get("retry_slots", [])
            )
            if len(retry_slots) > 12 or len({s.starts_at for s in retry_slots}) != len(retry_slots):
                raise ValueError("At most 12 distinct approved retry slots are allowed")
            artifact = WorkflowArtifact(
                artifact_id=source,
                kind="community.schedule.v1",
                case_id=case.case_id,
                created_at=now,
                source_ids=(actor_id,),
                payload={
                    "round": 1,
                    "response_deadline": deadline.isoformat(),
                    "retry_slots": [s.model_dump(mode="json") for s in retry_slots],
                    "policy": policy.model_dump(mode="json"),
                    "slots": [s.model_dump(mode="json") for s in slots],
                },
            )
            self.store.put_configuration(artifact)
            self.store.enqueue_job(
                self.runtime._maintenance.job(
                    case, "community.deadline", due_at=deadline, source_id=source
                )
            )
            self.task(
                case.case_id,
                "meeting_availability",
                "Waiting for participant availability",
                "Each participant can submit available slots using their own account.",
            )
            self.resolve(case.case_id, {"meeting_schedule"}, "Times offered", actor_id)
        elif action == "meeting_minutes":
            if case.status != CaseStatus.MEETING_READY:
                raise ValueError("a meeting packet must be prepared first")
            self.store.enqueue_job(
                self.runtime._maintenance.job(
                    case, "community.minutes", due_at=now, source_id=source, payload=data
                )
            )
        elif action == "meeting_confirm":
            result = self.runtime.confirm_meeting_decisions(
                case.case_id,
                confirmation=MeetingDecisionConfirmation(
                    actor_id=actor_id,
                    actor_label=actor_id,
                    source_id=source,
                    confirmed_at=now,
                    confirmed_decision_ids=tuple(data["decision_ids"]),
                    confirmed_action_ids=tuple(data.get("action_ids", [])),
                ),
            )
            self.resolve(case.case_id, {"meeting_confirm"}, "Confirmed by management", actor_id)
            for link in self.store.artifacts_for(kind="community.revision.v1"):
                if (
                    link.payload["child_case_id"] == case.case_id
                    and link.payload["kind"] == "decision_revision"
                ):
                    self.store.put_configuration(
                        WorkflowArtifact(
                            artifact_id=source,
                            kind="community.revision.confirmed.v1",
                            case_id=link.case_id,
                            created_at=now,
                            source_ids=(link.artifact_id, result.decision.meeting_decision_id),
                            payload={
                                "child_case_id": case.case_id,
                                "decision_id": result.decision.meeting_decision_id,
                            },
                        )
                    )
            for item in result.decision.action_items:
                self.task(
                    case.case_id,
                    "meeting_action",
                    item.description,
                    f"Owner: {item.owner_participant_id}. Completion evidence is required.",
                    due=item.due_at,
                    suffix=item.action_id,
                )
            if result.verification_request:
                self.task(
                    case.case_id,
                    "meeting_verify",
                    "Verify the community outcome",
                    "Confirm whether the agreed decision worked in practice.",
                )
        elif action == "meeting_procurement":
            from hashlib import sha256

            from steward.domain.models import ResidentMessage
            from steward.store import InboxItem, InboxStatus

            decisions = self.store.artifacts_for(kind="meeting_decision.v1", case_id=case.case_id)
            chosen = next(
                (
                    item
                    for decision in decisions
                    for item in decision.payload["action_items"]
                    if item["action_id"] == data.get("action_id")
                ),
                None,
            )
            asset = next(
                (a for a in self.runtime._bundle.assets if a.asset_id == data.get("asset_id")), None
            )
            if chosen is None or asset is None or not data["notes"].strip():
                raise ValueError(
                    "Choose a confirmed action, a known asset and the authorized scope"
                )
            if any(
                a.payload["action_id"] == chosen["action_id"]
                for a in self.store.artifacts_for(
                    kind="community.procurement.request.v1", case_id=case.case_id
                )
            ):
                raise ConcurrencyConflict("Maintenance has already been requested for this action")
            message = ResidentMessage(
                message_id=source,
                source="community_procurement",
                chat_id="northgate-management",
                sender_display=actor_id,
                sent_at=now,
                ingested_at=now,
                text=f"Maintenance work is required on {asset.label}. Scope: {data['notes']}. "
                "Please assess the maintenance need and obtain a quotation "
                "under the existing category policy.",
            )
            self.store.record_inbound(
                InboxItem(
                    source=message.source,
                    external_id=source,
                    status=InboxStatus.PENDING,
                    received_at=now,
                    payload_hash=sha256(message.text.encode()).hexdigest(),
                    message=message,
                )
            )
            self.store.put_configuration(
                WorkflowArtifact(
                    artifact_id=source,
                    kind="community.procurement.request.v1",
                    case_id=case.case_id,
                    created_at=now,
                    source_ids=(chosen["action_id"],),
                    payload={
                        "action_id": chosen["action_id"],
                        "asset_id": asset.asset_id,
                        "actor_id": actor_id,
                        "scope": data["notes"],
                    },
                )
            )
        elif action == "meeting_action":
            requests = [
                a
                for a in self.store.artifacts_for(
                    kind="community.procurement.request.v1", case_id=case.case_id
                )
                if a.payload["action_id"] == data["action_id"]
            ]
            for request in requests:
                links = [
                    a
                    for a in self.store.artifacts_for(
                        kind="community.procurement.link.v1", case_id=case.case_id
                    )
                    if request.artifact_id in a.source_ids
                ]
                if not links or not all(
                    self.store.get(a.payload["child_case_id"])
                    and self.store.get(a.payload["child_case_id"]).outcome_verified
                    for a in links
                ):
                    raise ValueError(
                        "The linked maintenance outcome must be verified "
                        "before completing this action"
                    )
            result = self.runtime.record_meeting_action_completion(
                case.case_id,
                action_id=data["action_id"],
                actor_id=actor_id,
                actor_label=actor_id,
                source_id=source,
                notes=data["notes"],
                completed_at=now,
            )
            task_id = stable_id("meeting_action", case.case_id, data["action_id"])
            self.store.resolve_human_task(
                task_id,
                actor_id=actor_id,
                role="manager",
                expected_version=self.store.case_version(case.case_id),
                response={"notes": data["notes"]},
            )
            if result.verification_request:
                self.task(
                    case.case_id,
                    "meeting_verify",
                    "Verify the community outcome",
                    "All actions have completion evidence. Did the decision solve the concern?",
                )
        elif action == "meeting_verify":
            self.runtime.verify_meeting_outcome(
                case.case_id,
                outcome=MeetingVerificationOutcome(data["outcome"]),
                actor_id=actor_id,
                actor_label=actor_id,
                source_id=source,
                notes=data["notes"],
                responded_at=now,
            )
            self.resolve(case.case_id, {"meeting_verify"}, data["notes"], actor_id)
            if self.store.get_case(case.case_id).status == CaseStatus.CLOSED:
                self.store.enqueue_job(
                    self.runtime._maintenance.job(
                        case, "memory.index", due_at=now, source_id=source
                    )
                )
        else:
            raise ValueError("unsupported community action")
