"""Prepare an evidence-backed discussion draft independently of scheduling.

Drafts are keyed by the immutable resolution plan. A restart resumes the same
input; a newer plan keeps its predecessor without reusing its conclusions.
No invitation, RFQ, commitment, or availability is created here.
"""

from types import SimpleNamespace

from steward.domain.enums import ActorType, EventKind, ResolutionPath
from steward.domain.models import TimelineEvent
from steward.store import ConcurrencyConflict, WorkflowArtifact
from steward.store.workflow import stable_id

DRAFT_KIND = "meeting_preparation_draft.v1"
INPUT_KIND = "meeting_preparation_input.v1"


def draft_for_plan(store, case_id, plan_id):
    return store.artifact(DRAFT_KIND, stable_id("meeting-draft", case_id, plan_id))


def prepare_draft(service, case_id, plan_id):
    store = service._store
    existing = draft_for_plan(store, case_id, plan_id)
    if existing:
        return existing
    planning = service._planning.load(case_id)
    if not planning or planning.plan.plan_id != plan_id:
        return None  # Superseded jobs cannot replace the current preparation.
    if planning.plan.recommendation.path != ResolutionPath.MEETING_RESOLUTION:
        return None
    current = service._require_meeting_case(case_id)
    context = planning.context
    if set(current.source_message_ids) != {message.message_id for message in context.messages}:
        raise ConcurrencyConflict("Resolution planning must include the latest meeting evidence")
    draft_id = stable_id("meeting-draft", case_id, plan_id)
    input_id = stable_id("meeting-draft-input", case_id, plan_id)
    frozen = store.artifact(INPUT_KIND, input_id)
    if frozen is None:
        contacts = service._vendor_contacts(context=context, plan=planning.plan)
        source_ids = tuple(
            dict.fromkeys(
                (
                    plan_id,
                    context.context_id,
                    *context.source_ids,
                    *(v.vendor_id for v in contacts),
                )
            )
        )
        frozen = WorkflowArtifact(
            artifact_id=input_id,
            case_id=case_id,
            kind=INPUT_KIND,
            created_at=service._clock.now(),
            source_ids=source_ids,
            payload={
                "plan_id": plan_id,
                "context_id": context.context_id,
                "source_message_ids": current.source_message_ids,
                "vendor_contacts": [v.model_dump(mode="json") for v in contacts],
                "context": {
                    "brief_id": input_id,
                    "case": {"title": context.case_title, "category": context.category.value},
                    "selected_meeting_slot": None,
                    "required_fact_codes": list(planning.plan.recommendation.required_fact_codes),
                    "allowed_source_ids": list(source_ids),
                    "messages": [m.model_dump(mode="json") for m in context.messages],
                    "history": list(context.history),
                    "management_notes": list(context.management_notes),
                    "vendor_options": [
                        {
                            "vendor_id": v.vendor_id,
                            "name": v.name,
                            "categories": [c.value for c in v.categories],
                            "allowlisted": v.allowlisted,
                            "simulated": v.simulated,
                        }
                        for v in contacts
                    ],
                },
            },
        )
        with store.atomic():
            previous = store.artifact(INPUT_KIND, input_id)
            if previous:
                frozen = previous
            else:
                store.put_configuration(frozen)

    # Worker owns the renewable lease. The network call is outside any DB transaction.
    agenda = service._agenda_planner.prepare(brief_id=input_id, context=frozen.payload["context"])
    from steward.meetings.workflow import MeetingVendorContact
    from steward.orchestration import ResolutionContextMessage

    brief = SimpleNamespace(
        source_ids=frozen.source_ids,
        messages=tuple(
            ResolutionContextMessage.model_validate(m)
            for m in frozen.payload["context"]["messages"]
        ),
        vendor_contacts=tuple(
            MeetingVendorContact.model_validate(v) for v in frozen.payload["vendor_contacts"]
        ),
    )
    service._validate_agenda(brief, agenda)
    now = service._clock.now()
    draft = WorkflowArtifact(
        artifact_id=draft_id,
        kind=DRAFT_KIND,
        case_id=case_id,
        created_at=now,
        source_ids=frozen.source_ids,
        payload={
            "plan_id": plan_id,
            "input_id": input_id,
            "revision": context.revision,
            "title": frozen.payload["context"]["case"]["title"],
            "agenda": agenda.model_dump(mode="json"),
            "history": frozen.payload["context"]["history"],
            "vendor_options": frozen.payload["context"]["vendor_options"],
            "preparation_status": "draft",
            "scheduled": False,
            "outbound_enabled": False,
            "model_output_is_authority": False,
        },
    )
    with store.atomic():
        existing = draft_for_plan(store, case_id, plan_id)
        if existing:
            return existing
        latest_plan = service._planning.load(case_id)
        current = service._require_meeting_case(case_id)
        if not latest_plan or latest_plan.plan.plan_id != plan_id:
            return None
        if current.source_message_ids != frozen.payload["source_message_ids"]:
            raise ConcurrencyConflict("Meeting evidence changed during preparation")
        store.save_transition(
            case=current.model_copy(update={"updated_at": max(current.updated_at, now)}),
            expected_version=store.case_version(case_id),
            idempotency_key=draft_id,
            artifacts=(draft,),
            timeline_events=(
                TimelineEvent(
                    event_id=stable_id("meeting-draft-event", draft_id),
                    case_id=case_id,
                    at=now,
                    kind=EventKind.PLAN_DRAFTED,
                    actor=ActorType.AGENT,
                    summary=(
                        "Prepared the meeting topic, agenda, discussion options and quote needs. "
                        "Meeting time and community decisions remain unconfirmed."
                    ),
                    refs=list(draft.source_ids),
                    payload={"draft_id": draft_id, "plan_id": plan_id, "outbound_enabled": False},
                ),
            ),
        )
    return draft
