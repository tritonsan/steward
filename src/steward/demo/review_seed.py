"""Prepare two synthetic, actionable jury scenarios in the isolated review store.

This is an explicit operator command, never an application startup hook. It
uses the ordinary message, vendor-reply, policy and worker APIs. Model ports
are deterministic fixtures for this preparation only; newly entered messages
in the deployed review application use its configured live model adapters.
There is intentionally no reset, database override, or live-store option.
"""

from __future__ import annotations

import argparse
import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from secrets import token_urlsafe

from fastapi.testclient import TestClient

from steward.agents import (
    MeetingAgendaItem,
    MeetingAgendaItemKind,
    MeetingAgendaRecommendation,
    QuoteExtraction,
    QuoteRecommendation,
    ResolutionPlanRecommendation,
    TriageAction,
    TriageResult,
)
from steward.agents.decision import QuoteAlternative
from steward.agents.planning import MeetingOpenQuestion, MeetingQuoteNeed, MeetingSolutionOption
from steward.api import create_app
from steward.config import RuntimeExecutionMode, TelegramDeliveryMode
from steward.domain.enums import Category, ResolutionPath, Urgency
from steward.mail import RecordingMailTransport
from steward.store import WorkflowArtifact

SEED_ID = "jury-preparation-v1"
MARKER_KIND = "review.preparation.v1"
PARKING = (
    "The visitor parking spaces at Northgate are often occupied overnight, leaving no room "
    "for guests. Some residents prefer a 24-hour parking limit, while others want 72 hours. "
    "Could management arrange a residents' meeting to discuss these options, agree on a "
    "shared rule, and decide how it should be communicated?"
)
ELEVATOR = (
    "The A Block elevator shudders near the fourth floor again. The guide shoes were "
    "replaced before but the vibration returned. Please inspect the guide shoes and rail "
    "alignment rather than replacing the same part without finding the cause."
)
QUOTES = {
    "meridian-lift": (
        "Quote is 705 USD including labour, travel, guide shoes, and rail alignment "
        "correction. We can attend in two days. No exclusions. Valid for seven days."
    ),
    "coastline-elevator": (
        "Quote is 540 USD including labour, travel, and replacement guide shoes only. "
        "We can attend in four days. Rail alignment excluded. Valid for seven days."
    ),
}


class PreparedIntake:
    """Closed fixtures: these ports cannot silently process arbitrary reviewer input."""

    def classify(self, *, message, assets, open_cases):
        del assets, open_cases
        if message.text == PARKING:
            return TriageResult(
                action=TriageAction.OPEN_CASE,
                category=Category.MEETING_ADMIN,
                urgency=Urgency.NORMAL,
                confidence=0.97,
                title="Visitor parking: choose a 24-hour or 72-hour limit",
                rationale="Prepared fixture: residents disagree about a shared parking rule.",
            )
        if message.text == ELEVATOR:
            return TriageResult(
                action=TriageAction.OPEN_CASE,
                category=Category.ELEVATOR,
                urgency=Urgency.HIGH,
                confidence=0.97,
                title="A Block elevator: recurring shudder near floor four",
                asset_id="elevator-a",
                rationale="Prepared fixture: a recurring fault needs a complete repair scope.",
            )
        raise ValueError("Preparation fixtures cannot process other messages")


class PreparedResolution:
    def plan(self, *, context_id, context):
        del context_id
        return ResolutionPlanRecommendation(
            path=ResolutionPath.MEETING_RESOLUTION,
            rationale=(
                "Prepared fixture: residents must choose the shared parking rule. "
                "No spending or community decision is authorized by this preparation."
            ),
            source_ids=(context["allowed_source_ids"][0],),
        )


class PreparedAgenda:
    def prepare(self, *, brief_id, context):
        del brief_id
        sources = (context["messages"][0]["message_id"],)
        return MeetingAgendaRecommendation(
            summary=(
                "Prepared discussion draft: agree how long visitors can park, how exceptions "
                "will work, and who will publish the rule. No option has been adopted."
            ),
            summary_source_ids=sources,
            agenda_items=(
                MeetingAgendaItem(
                    kind=MeetingAgendaItemKind.CONTEXT,
                    title="Understand overnight occupancy",
                    detail="Review the reported lack of guest spaces and the existing rule.",
                    source_ids=sources,
                ),
                MeetingAgendaItem(
                    kind=MeetingAgendaItemKind.DECISION,
                    title="Choose a time limit and exception process",
                    detail="Compare the proposed 24-hour and 72-hour limits before voting.",
                    source_ids=sources,
                ),
                MeetingAgendaItem(
                    kind=MeetingAgendaItemKind.DECISION,
                    title="Assign communication and follow-up",
                    detail="Name an owner and review date for the written rule.",
                    source_ids=sources,
                ),
            ),
            solution_options=tuple(
                MeetingSolutionOption(
                    kind=MeetingAgendaItemKind.DISCUSSION,
                    title=title,
                    detail=detail,
                    tradeoffs=tradeoffs,
                    source_ids=sources,
                )
                for title, detail, tradeoffs in (
                    (
                        "24-hour visitor parking limit",
                        "Keep visitor spaces available for short visits.",
                        "More frequent turnover; overnight guests may need an exception.",
                    ),
                    (
                        "72-hour visitor parking limit",
                        "Allow multi-day visits within one shared maximum stay.",
                        "More flexible for guests; fewer spaces may be available each day.",
                    ),
                )
            ),
            open_questions=(
                MeetingOpenQuestion(
                    question="What rule is currently published, and who can grant exceptions?",
                    source_ids=sources,
                ),
            ),
            quote_needs=(
                MeetingQuoteNeed(
                    question="Will the agreed rule need new signs?",
                    scope="Supply and install visitor parking signs after wording is approved.",
                    reason="A physical sign may help communicate the eventual decision.",
                    source_ids=sources,
                ),
            ),
        )


class PreparedQuotes:
    def extract(self, **kwargs):
        body, received = kwargs["body_text"], kwargs["received_at"]
        vendor = next((key for key, text in QUOTES.items() if text == body), None)
        if vendor is None:
            raise ValueError("Preparation fixtures cannot extract other mail")
        meridian = vendor == "meridian-lift"
        scope = body.split(" We can attend", 1)[0]
        return QuoteExtraction(
            has_quote=True,
            amount=Decimal("705.00" if meridian else "540.00"),
            currency="USD",
            amount_evidence="Quote is 705 USD" if meridian else "Quote is 540 USD",
            scope_evidence=scope,
            inclusions_evidence=scope,
            exclusions_evidence="No exclusions." if meridian else "Rail alignment excluded.",
            earliest_onsite_at=received + timedelta(days=2 if meridian else 4),
            onsite_evidence="in two days" if meridian else "in four days",
            valid_until=received + timedelta(days=7),
            validity_evidence="Valid for seven days.",
        )


class PreparedRecommendation:
    def recommend(self, *, portfolio_id, context):
        del portfolio_id
        quotes = [item["quote"] for item in context["quotes"]]
        selected = next(q for q in quotes if q["vendor_id"] == "meridian-lift")
        alternative = next(q for q in quotes if q["vendor_id"] == "coastline-elevator")
        return QuoteRecommendation(
            recommended_quote_id=selected["quote_id"],
            rationale=(
                "Prepared fixture: Meridian includes both guide shoes and rail alignment, "
                "matching the resident's recurring-fault report. The lower price excludes "
                "alignment. Historical jobs have small samples and unequal scopes; the "
                "offered scope is the decisive evidence here."
            ),
            source_ids=(selected["quote_id"], alternative["quote_id"]),
            alternatives=(
                QuoteAlternative(
                    quote_id=alternative["quote_id"],
                    reason_not_selected=(
                        "Lower price, but rail alignment is excluded and arrival is later."
                    ),
                ),
            ),
            decision_changes_when=(
                "A revised competing quote includes alignment at a lower comparable total.",
                "Inspection evidence shows alignment correction is unnecessary.",
            ),
        )


def assert_isolated(runtime):
    """Fail before importing history, entering events, or changing permissions."""
    settings = runtime._settings
    if not getattr(settings, "review_isolated", False):
        raise ValueError("Jury preparation requires build_review_runtime()")
    if settings.database_url or settings.database_secret:
        if getattr(settings, "database_schema", None) != "steward_review":
            raise ValueError("Jury preparation requires the steward_review PostgreSQL schema")
    elif Path(settings.database_path).name != "steward-review.db":
        raise ValueError("Jury preparation requires the isolated steward-review.db")
    if (
        settings.execution_mode != RuntimeExecutionMode.DRY_RUN
        or settings.telegram_delivery_mode != TelegramDeliveryMode.DRY_RUN
        or settings.telegram_enabled
        or settings.channel_configuration
        or settings.telegram_bot_token
        or settings.ses_inbound_enabled
        or settings.ses_queue_url
        or settings.allow_live_commitments
        or not isinstance(runtime.transport, RecordingMailTransport)
    ):
        raise ValueError("All external channels must be disabled for jury preparation")


def prepare(runtime):
    """Idempotent bootstrap. Existing completed or reviewer-modified cases are preserved."""
    assert_isolated(runtime)
    store = runtime.store
    done = store.artifact(MARKER_KIND, SEED_ID + ":complete")
    if done:
        return {**done.payload, "already_prepared": True}
    started = store.artifact(MARKER_KIND, SEED_ID + ":started")
    if not started and (store.list_cases() or store.pending_inbound()):
        raise ValueError("Refusing to seed an already-used review workspace")
    if not started:
        started = WorkflowArtifact(
            artifact_id=SEED_ID + ":started",
            kind=MARKER_KIND,
            created_at=runtime._clock.now(),
            payload={
                "seed": SEED_ID,
                "is_simulated": True,
                "prepared_model_outputs": "deterministic fixtures",
                "live_model_calls": False,
                "external_delivery": False,
            },
        )
        store.put_configuration(started)
    # A partial bootstrap can resume, but must never consume new reviewer work.
    from steward.store.workflow import stable_id

    expected_messages = {
        "sim:" + stable_id(SEED_ID + ":parking"),
        "sim:" + stable_id(SEED_ID + ":elevator"),
    }
    if any(not set(c.source_message_ids) <= expected_messages for c in store.list_cases()):
        raise ValueError("Refusing to resume preparation after reviewers added case evidence")
    if any(i.external_id not in expected_messages for i in store.pending_inbound()):
        raise ValueError("Refusing to consume unrelated pending review input")

    for record in runtime._bundle.history:
        if not record.is_simulated:
            raise ValueError("Only synthetic seed history is accepted")
        store.write(record)

    token = token_urlsafe(32)
    app = create_app(
        runtime,
        simulation=True,
        tokens={token: {"actor_id": "Simon O.", "role": "manager"}},
    )
    headers = {"Authorization": "Bearer " + token}
    with TestClient(app) as client:

        def request(method, path, body=None, *, key=None):
            response = client.request(
                method,
                "/api" + path,
                json=body,
                headers={**headers, "Idempotency-Key": key or SEED_ID + path},
            )
            if response.status_code != 200:
                raise RuntimeError(f"Preparation {path} failed ({response.status_code})")
            result = response.json()
            if path == "/simulation/tick" and result.get("workflow_failures"):
                raise RuntimeError("Prepared scenario worker reported a workflow failure")
            return result

        for name, text in (("parking", PARKING), ("elevator", ELEVATOR)):
            if not store.get_message("sim:" + stable_id(SEED_ID + ":" + name)):
                request(
                    "POST",
                    "/simulation/messages",
                    {"sender": "Daniel K.", "text": text},
                    key=SEED_ID + ":" + name,
                )
        request("POST", "/simulation/tick")
        cases = store.list_cases()
        parking = next(c for c in cases if c.category == Category.MEETING_ADMIN)
        elevator = next(c for c in cases if c.category == Category.ELEVATOR)
        config = request("GET", "/settings")
        if config["policies"]["elevator"]["mode"] != "prepare_only":
            config["policies"]["elevator"]["mode"] = "prepare_only"
            request(
                "PUT",
                "/settings",
                {
                    "expected_version": config["version"],
                    "settings": config["settings"],
                    "policies": config["policies"],
                },
                key=SEED_ID + ":prepare-only-policy",
            )
        for vendor, text in QUOTES.items():
            if not any(
                a.payload["quote"]["vendor_id"] == vendor
                for a in store.artifacts_for(kind="vendor_quote.v1", case_id=elevator.case_id)
            ):
                request(
                    "POST",
                    "/simulation/vendor-replies",
                    {"case_id": elevator.case_id, "vendor_id": vendor, "text": text},
                    key=SEED_ID + ":" + vendor,
                )
        # Move only to the initial comparison deadline, never by another day on retry.
        comparison_due = started.created_at + timedelta(hours=24)
        if runtime._clock.now() < comparison_due:
            runtime._clock.advance(comparison_due - runtime._clock.now())
        request("POST", "/simulation/tick")

        # A real resident response is still needed; preparation never fabricates quorum.
        if not store.artifacts_for(kind="community.schedule.v1", case_id=parking.case_id):
            first_slot = comparison_due + timedelta(days=2, hours=6)
            request(
                "POST",
                f"/cases/{parking.case_id}/commands",
                {
                    "action": "meeting_schedule",
                    "expected_version": store.case_version(parking.case_id),
                    "notes": "Prepared review scenario: manager offers two discussion times.",
                    "data": {
                        "policy": {
                            "timezone": "Europe/Istanbul",
                            "quorum": 2,
                            "eligible_participant_ids": ["Simon O.", "Daniel K."],
                            "required_participant_ids": ["Simon O."],
                        },
                        "slots": [
                            {"slot_id": "first-evening", "starts_at": first_slot.isoformat()},
                            {
                                "slot_id": "second-evening",
                                "starts_at": (first_slot + timedelta(days=1)).isoformat(),
                            },
                        ],
                    },
                },
                key=SEED_ID + ":meeting-times",
            )
        if not store.artifacts_for(kind="community.availability.v1", case_id=parking.case_id):
            request(
                "POST",
                f"/cases/{parking.case_id}/availability",
                {
                    "expected_version": store.case_version(parking.case_id),
                    "available_slot_ids": ["first-evening", "second-evening"],
                },
                key=SEED_ID + ":manager-availability",
            )
        request("POST", "/simulation/tick")

    if not store.artifacts_for(kind="meeting_preparation_draft.v1", case_id=parking.case_id):
        raise RuntimeError("Parking preparation did not produce an agenda")
    if not any(
        t.kind == "meeting_availability" and t.status == "open"
        for t in store.human_tasks(parking.case_id)
    ):
        raise RuntimeError("Parking preparation did not reach resident availability")
    if not any(
        t.kind == "quote_approval" and t.status == "open"
        for t in store.human_tasks(elevator.case_id)
    ):
        raise RuntimeError("Elevator preparation did not reach human approval")
    if store.get_case(elevator.case_id).accepted_quote_id:
        raise RuntimeError("Preparation unexpectedly committed a quote")
    payload = {
        **started.payload,
        "history_records": len(runtime._bundle.history),
        "case_ids": {"parking": parking.case_id, "elevator": elevator.case_id},
        "prepared_states": {"parking": "meeting_availability", "elevator": "quote_approval"},
        "resident_actor": "Daniel K.",
        "simulated_clock": runtime._clock.now().isoformat(),
        "already_prepared": False,
    }
    store.put_configuration(
        WorkflowArtifact(
            artifact_id=SEED_ID + ":complete",
            kind=MARKER_KIND,
            created_at=runtime._clock.now(),
            payload=payload,
        )
    )
    return payload


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    from steward.review import build_review_runtime

    with build_review_runtime(
        classifier=PreparedIntake(),
        quote_extractor=PreparedQuotes(),
        decision_recommender=PreparedRecommendation(),
        resolution_planner=PreparedResolution(),
        meeting_agenda_planner=PreparedAgenda(),
    ) as runtime:
        print(json.dumps(prepare(runtime), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
