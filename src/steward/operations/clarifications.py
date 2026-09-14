"""Personal, source-bound clarification; inference runs outside write transactions."""

from datetime import timedelta

from steward.agents.triage import TriageAssessment
from steward.domain.enums import ActorType, EventKind
from steward.domain.models import TimelineEvent
from steward.store import ConcurrencyConflict, WorkflowArtifact
from steward.store.workflow import HumanTask, stable_id


def effective_assessment(store, case):
    revisions = store.artifacts_for(kind="intake.reassessment.v1", case_id=case.case_id)
    revisions = [a for a in revisions if a.payload.get("usable")]
    if revisions:
        latest = max(revisions, key=lambda a: a.payload["version"])
        return TriageAssessment.model_validate(latest.payload["assessment"])
    return store.get_assessment(case.source_message_ids[0])


class Clarifications:
    def __init__(self, runtime):
        self.rt, self.store, self.clock = runtime, runtime.store, runtime._clock

    def request(self, case):
        existing = self.store.artifacts_for(kind="intake.clarification.v1", case_id=case.case_id)
        if existing:
            return existing[0]
        message = self.store.get_message(case.source_message_ids[0])
        now = self.clock.now()
        key = stable_id("clarification", case.case_id)
        question = (
            f"About “{case.title}”: which block and exact location is affected, "
            "and what happens when you use it?"
        )
        assessment = effective_assessment(self.store, case)
        if assessment and assessment.result.clarification_question:
            question = assessment.result.clarification_question
        with self.store.atomic():
            artifact = WorkflowArtifact(
                artifact_id=key,
                kind="intake.clarification.v1",
                case_id=case.case_id,
                created_at=now,
                source_ids=(message.message_id,),
                payload={"actor_id": message.sender_display, "question": question},
            )
            self.store.put_configuration(artifact)
            self.store.add_human_task(
                HumanTask(
                    task_id=key,
                    case_id=case.case_id,
                    kind="clarification",
                    title=question,
                    reason="Please clarify the original report.",
                    created_at=now,
                    due_at=now + timedelta(hours=48),
                    expected_version=self.store.case_version(case.case_id),
                    allowed_roles=("resident", "manager"),
                    assigned_actor_ids=(message.sender_display,),
                )
            )
            for hours in (24, 48):
                self.store.enqueue_job(
                    self.rt._maintenance.job(
                        case,
                        "clarification.deadline",
                        due_at=now + timedelta(hours=hours),
                        source_id=key,
                        payload={"hours": hours},
                    ).model_copy(update={"job_id": stable_id(key, str(hours))})
                )
        return artifact

    def answer(self, case, *, actor_id, role, request_id, notes, source):
        request = self.store.artifact("intake.clarification.v1", request_id)
        if not request or request.case_id != case.case_id:
            raise ValueError("Unknown clarification request")
        if role != "manager" and request.payload["actor_id"] != actor_id:
            raise PermissionError("This question is assigned to another resident")
        if not notes.strip():
            raise ValueError("Please include the missing facts or a source reference")
        answered = self.store.artifacts_for(kind="intake.answer.v1", case_id=case.case_id)
        if answered:
            if (
                answered[0].payload["notes"] == notes
                and answered[0].payload["actor_id"] == actor_id
            ):
                return
            raise ConcurrencyConflict("This question has already been answered")
        self.store.put_configuration(
            WorkflowArtifact(
                artifact_id=source,
                kind="intake.answer.v1",
                case_id=case.case_id,
                created_at=self.clock.now(),
                source_ids=(request_id, actor_id),
                payload={"notes": notes, "actor_id": actor_id},
            )
        )
        self.store.resolve_human_task(
            request_id,
            actor_id=actor_id,
            role=role,
            expected_version=self.store.case_version(case.case_id),
            response={"notes": notes},
        )
        self.store.enqueue_job(
            self.rt._maintenance.job(
                case, "clarification.reassess", due_at=self.clock.now(), source_id=source
            )
        )

    def handle(self, job):
        case = self.store.get_case(job.case_id)
        if job.kind == "clarification.deadline":
            if self.store.artifacts_for(kind="intake.answer.v1", case_id=case.case_id):
                return
            if job.payload["hours"] == 48:
                self.rt._maintenance.task(
                    case,
                    "intake_review",
                    "Clarification is overdue",
                    "The reporting resident has not answered within 48 hours.",
                )
            else:
                key = stable_id("clarification-reminder", job.source_id)
                if not self.store.artifact("intake.reminder.v1", key):
                    self.store.put_configuration(
                        WorkflowArtifact(
                            artifact_id=key,
                            kind="intake.reminder.v1",
                            case_id=case.case_id,
                            created_at=self.clock.now(),
                            source_ids=(job.source_id,),
                            payload={"request_id": job.source_id},
                        )
                    )
            return
        key = stable_id("reassessment", job.source_id)
        if self.store.artifact("intake.reassessment.v1", key):
            return
        answer = self.store.artifact("intake.answer.v1", job.source_id)
        original = self.store.get_message(case.source_message_ids[0])
        version = self.store.case_version(case.case_id)
        message = original.model_copy(
            update={"text": original.text + "\nClarification: " + answer.payload["notes"]}
        )
        triage = self.rt._runner._intake._classifier.classify(
            message=message, assets=tuple(self.rt._assets.values()), open_cases=(case,)
        )
        asset = self.rt._assets.get(triage.asset_id)
        consistent = (
            asset is not None
            and asset.category == triage.category
            and triage.category == case.category
            and (case.asset_id is None or triage.asset_id == case.asset_id)
            and triage.confidence >= self.rt._policy.settings.min_triage_confidence
            and not triage.missing_information
            and triage.action.value != "ignore"
            and triage.duplicate_case_id in (None, case.case_id)
        )
        with self.store.atomic():
            if self.store.case_version(case.case_id) != version:
                raise ConcurrencyConflict("Case changed during clarification assessment")
            assessment = TriageAssessment(
                message_id=original.message_id, assessed_at=self.clock.now(), result=triage
            )
            artifact = WorkflowArtifact(
                artifact_id=key,
                kind="intake.reassessment.v1",
                case_id=case.case_id,
                created_at=self.clock.now(),
                source_ids=(original.message_id, answer.artifact_id),
                payload={
                    "version": version,
                    "assessment": assessment.model_dump(mode="json"),
                    "usable": consistent,
                },
            )
            updated = case.model_copy(
                update={
                    "asset_id": triage.asset_id if consistent else case.asset_id,
                    "title": (triage.title or case.title) if consistent else case.title,
                    "updated_at": self.clock.now(),
                }
            )
            self.store.save_transition(
                case=updated,
                expected_version=version,
                idempotency_key=key,
                artifacts=(artifact,),
                workflow_jobs=(
                    self.rt._maintenance.job(
                        updated, "case.opened", due_at=self.clock.now(), source_id=key
                    ),
                )
                if consistent
                else (),
                timeline_events=(
                    TimelineEvent(
                        event_id=key,
                        case_id=case.case_id,
                        at=self.clock.now(),
                        kind=EventKind.NOTE,
                        actor=ActorType.SYSTEM,
                        summary="Clarification reviewed; continuing the case."
                        if consistent
                        else "Clarification needs management review.",
                        refs=[answer.artifact_id],
                    ),
                ),
            )
            if not consistent:
                self.rt._maintenance.task(
                    updated,
                    "intake_review",
                    "Review conflicting or incomplete facts",
                    triage.rationale,
                )
