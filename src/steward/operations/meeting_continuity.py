"""Bounded meeting rounds and additive revisions of community decisions."""

from datetime import timedelta

from steward.domain.clock import parse_datetime
from steward.meetings import MeetingSchedulingPolicy
from steward.store import ConcurrencyConflict, WorkflowArtifact
from steward.store.workflow import stable_id


def active_schedule(store, case_id):
    rows = store.artifacts_for(kind="community.schedule.v1", case_id=case_id)
    return max(rows, key=lambda a: a.payload.get("round", 1)) if rows else None


class MeetingContinuity:
    def __init__(self, runtime):
        self.rt, self.store, self.clock = runtime, runtime.store, runtime._clock

    def deadline(self, job):
        from steward.operations.community import CommunityWorkflow

        schedule = active_schedule(self.store, job.case_id)
        if not schedule or schedule.artifact_id != job.source_id:
            return
        if self.store.artifacts_for(kind="meeting_packet.v1", case_id=job.case_id):
            return
        case = self.store.get_case(job.case_id)
        data = schedule.payload
        policy = MeetingSchedulingPolicy.model_validate(data["policy"])
        now = self.clock.now()
        options = data.get("retry_slots", [])
        used = {
            s["starts_at"]
            for a in self.store.artifacts_for(kind="community.schedule.v1", case_id=case.case_id)
            for s in a.payload["slots"]
        }
        available = [
            s
            for s in options
            if s["starts_at"] not in used
            and parse_datetime(s["starts_at"])
            > now + timedelta(hours=policy.minimum_notice_hours + 24)
        ]
        if data.get("round", 1) >= 3 or not available:
            CommunityWorkflow(self.rt).task(
                case.case_id,
                "meeting_reschedule",
                "Choose new meeting times",
                "No approved new times remain, or the two automatic retry rounds have been used.",
                suffix=schedule.artifact_id,
            )
            return
        new_id = stable_id("meeting-round", schedule.artifact_id)
        with self.store.atomic():
            if active_schedule(self.store, case.case_id).artifact_id != schedule.artifact_id:
                return
            new = WorkflowArtifact(
                artifact_id=new_id,
                kind="community.schedule.v1",
                case_id=case.case_id,
                created_at=now,
                source_ids=(schedule.artifact_id,),
                payload={
                    **data,
                    "round": data.get("round", 1) + 1,
                    "previous_id": schedule.artifact_id,
                    "slots": available[:2],
                    "response_deadline": (now + timedelta(hours=24)).isoformat(),
                },
            )
            self.store.save_transition(
                case=case.model_copy(update={"updated_at": now}),
                expected_version=self.store.case_version(case.case_id),
                idempotency_key=new_id,
                artifacts=(new,),
                workflow_jobs=(
                    self.rt._maintenance.job(
                        case,
                        "community.deadline",
                        due_at=now + timedelta(hours=24),
                        source_id=new_id,
                    ),
                ),
            )

    def linked_case(self, case, *, actor_id, notes, source, kind):
        """Explicit manager action creates a new case; prior decision remains immutable."""
        from steward.agents.triage import TriageAction, TriageAssessment, TriageResult
        from steward.domain.enums import Category, CategoryGroup
        from steward.domain.models import Case, ResidentMessage

        now = self.clock.now()
        if not notes.strip():
            raise ValueError("Explain why a new meeting is needed")
        if kind == "decision_revision" and not self.store.artifacts_for(
            kind="meeting_decision.v1", case_id=case.case_id
        ):
            raise ValueError("Only a confirmed decision can be revised")
        key = stable_id(kind, source)
        existing = self.store.artifact("community.revision.v1", source)
        if existing:
            return existing.payload["child_case_id"]
        child = Case(
            case_id=key,
            reply_token=stable_id("reply", key)[:24],
            title=("Revisit: " + case.title)[:160],
            category=Category.MEETING_ADMIN,
            group=CategoryGroup.COMMUNITY_ADMIN,
            urgency=case.urgency,
            opened_at=now,
            updated_at=now,
            source_message_ids=[source],
            related_case_ids=[case.case_id],
            currency=case.currency,
        )
        message = ResidentMessage(
            message_id=source,
            sender_display=actor_id,
            chat_id="management",
            source="web",
            sent_at=now,
            ingested_at=now,
            text=notes,
            case_id=key,
        )
        triage = TriageResult(
            action=TriageAction.OPEN_CASE,
            category=child.category,
            urgency=child.urgency,
            confidence=1,
            title=child.title,
            rationale="Management requested a linked meeting with this source evidence.",
        )
        self.store.record_intake(
            message=message,
            assessment=TriageAssessment(message_id=source, assessed_at=now, result=triage),
            case=child,
            new_case=True,
        )
        self.store.put_configuration(
            WorkflowArtifact(
                artifact_id=source,
                kind="community.revision.v1",
                case_id=case.case_id,
                created_at=now,
                source_ids=(case.case_id, actor_id),
                payload={
                    "child_case_id": key,
                    "kind": kind,
                    "reason": notes,
                    "status": "pending_confirmation",
                },
            )
        )
        if kind == "meeting_reschedule":
            self.store.put_configuration(
                WorkflowArtifact(
                    artifact_id=source,
                    kind="meeting.invalidated.v1",
                    case_id=case.case_id,
                    created_at=now,
                    payload={"replacement_case_id": key, "reason": notes},
                )
            )
        return key

    def reassign(self, case, data, actor_id, source):
        from steward.store.workflow import HumanTask

        decisions = self.store.artifacts_for(kind="meeting_decision.v1", case_id=case.case_id)
        item = next(
            (
                item
                for d in decisions
                for item in d.payload["action_items"]
                if item["action_id"] == data.get("action_id")
            ),
            None,
        )
        if not item or not data.get("notes", "").strip():
            raise ValueError("Select a confirmed action and explain the change")
        revisions = [
            record
            for record in self.store.artifacts_for(
                kind="meeting.assignment.v1", case_id=case.case_id
            )
            if record.payload["action_id"] == item["action_id"]
        ]
        previous = {**item, **revisions[-1].payload} if revisions else item
        version = revisions[-1].payload.get("version", len(revisions)) + 1 if revisions else 1
        owner = data.get("owner_participant_id", previous["owner_participant_id"])
        if owner not in {r.display_name for r in self.rt._bundle.residents}:
            raise ValueError("Choose a registered owner")
        due = parse_datetime(data.get("due_at", previous["due_at"]))
        if due.tzinfo is None or due <= self.clock.now():
            raise ValueError("A future date with timezone is required")
        if any(
            a.payload.get("action_id") == item["action_id"]
            for a in self.store.artifacts_for(
                kind="meeting_action_completion.v1", case_id=case.case_id
            )
        ):
            raise ConcurrencyConflict("A completed action cannot be reassigned")
        self.store.put_configuration(
            WorkflowArtifact(
                artifact_id=source,
                kind="meeting.assignment.v1",
                case_id=case.case_id,
                created_at=self.clock.now(),
                source_ids=(actor_id,),
                payload={
                    "action_id": item["action_id"],
                    "owner_participant_id": owner,
                    "due_at": due.isoformat(),
                    "reason": data["notes"],
                    "version": version,
                    "previous_assignment_id": revisions[-1].artifact_id if revisions else None,
                    "previous": {
                        key: previous[key]
                        for key in ("action_id", "owner_participant_id", "due_at")
                    },
                },
            )
        )
        task_id = stable_id("meeting_action", case.case_id, item["action_id"])
        # Update the current projection; immutable assignment history stays in artifacts.
        with self.store.atomic() as conn:
            row = conn.execute(
                "SELECT doc_json FROM human_tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if row:
                task = HumanTask.model_validate_json(row["doc_json"])
                revised = task.model_copy(
                    update={
                        "due_at": due,
                        "assigned_actor_ids": (owner,),
                        "reason": f"Owner: {owner}. {data['notes']}",
                    }
                )
                conn.execute(
                    "UPDATE human_tasks SET doc_json=? WHERE task_id=?",
                    (revised.model_dump_json(), task_id),
                )
            self.store.enqueue_job(
                self.rt._maintenance.job(
                    case, "community.assignment_due", source_id=source, due_at=due
                )
            )

    def assignment_due(self, job):
        from steward.operations.community import CommunityWorkflow

        revision = self.store.artifact("meeting.assignment.v1", job.source_id)
        if not revision:
            return
        action_id = revision.payload["action_id"]
        revisions = [
            record
            for record in self.store.artifacts_for(
                kind="meeting.assignment.v1", case_id=job.case_id
            )
            if record.payload["action_id"] == action_id
        ]
        if revisions[-1].artifact_id != revision.artifact_id or any(
            record.payload.get("action_id") == action_id
            for record in self.store.artifacts_for(
                kind="meeting_action_completion.v1", case_id=job.case_id
            )
        ):
            return
        CommunityWorkflow(self.rt).task(
            job.case_id,
            "meeting_assignment_overdue",
            "Follow up the revised action",
            f"The revised deadline for {revision.payload['owner_participant_id']} has passed. "
            "Completion evidence is still missing.",
            suffix=revision.artifact_id,
        )
