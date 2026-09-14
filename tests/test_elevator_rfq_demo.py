"""Acceptance coverage for the recording-only elevator RFQ demo."""

from __future__ import annotations

from steward.agents import TriageAction, TriageResult
from steward.demo.elevator_rfq import run_elevator_rfq_demo
from steward.domain.enums import Category, Urgency


class DeterministicElevatorClassifier:
    def classify(self, *, message, assets, open_cases):
        del assets
        suffix = message.message_id.rpartition(":")[2]
        if suffix in {"71001", "71003"}:
            is_a_block = suffix == "71001"
            return TriageResult(
                action=TriageAction.OPEN_CASE,
                category=Category.ELEVATOR,
                urgency=Urgency.HIGH,
                confidence=0.98,
                title=(
                    "A Block elevator shudders and its doors reopen"
                    if is_a_block
                    else "B Block elevator stops below the landing"
                ),
                asset_id="elevator-a" if is_a_block else "elevator-b",
                rationale="A distinct elevator fault is clearly reported.",
            )

        asset_id = "elevator-a" if suffix == "71002" else "elevator-b"
        target = next(case for case in open_cases if case.asset_id == asset_id)
        return TriageResult(
            action=TriageAction.LINK_EXISTING,
            category=Category.ELEVATOR,
            urgency=Urgency.HIGH,
            confidence=0.99,
            title=target.title,
            asset_id=asset_id,
            duplicate_case_id=target.case_id,
            rationale="The resident explicitly confirms the same asset and fault.",
        )


def test_elevator_demo_records_governed_rfq_drafts_without_delivery(tmp_path):
    report = run_elevator_rfq_demo(
        DeterministicElevatorClassifier(),
        db_path=tmp_path / "elevator-rfq.db",
    )

    assert report["passed"] is True
    assert [step["intake_disposition"] for step in report["messages"]] == [
        "opened",
        "linked",
        "opened",
        "linked",
    ]
    assert all(
        "reconciliation_attempts" in step["triage"]
        and "reconciliation_issue_codes" in step["triage"]
        for step in report["messages"]
    )
    assert report["simulation"] == {
        "synthetic_data": True,
        "history_records_loaded": 63,
        "input_message_count": 4,
        "opened_case_count": 2,
        "recorded_email_count": 6,
    }
    assert report["persisted_rfq_draft_artifacts"] == 6
    assert report["recorded_transport_messages"] == 6

    safety = report["safety"]
    assert safety["prepared_email_source"] == "RecordingMailTransport.sent"

    assert safety["mail_transport"] == "RecordingMailTransport"
    assert safety["real_email_delivery"] is False
    assert safety["pending_outbox_count"] == 0
    assert safety["commitment_event_count"] == 0
    assert safety["commitment_sent"] is False
    assert safety["all_recipients_guarded"] is True

    first_case, second_case = report["cases"]
    assert first_case["asset_id"] == "elevator-a"
    assert second_case["asset_id"] == "elevator-b"
    assert len(first_case["source_message_ids"]) == 2
    assert len(second_case["source_message_ids"]) == 2
    assert first_case["policy"]["rule_id"] == "INTAKE.WITHIN_POLICY"
    assert second_case["policy"]["rule_id"] == "INTAKE.WITHIN_POLICY"

    assert all(
        email["to"][0].endswith("@vendors.narrativenode-labs.cloud")
        for email in report["prepared_emails"]
    )
    assert all(email["delivered"] is False for email in report["prepared_emails"])
    assert all(
        "No work is authorized by this email." in email["body_text"]
        for email in report["prepared_emails"]
    )
