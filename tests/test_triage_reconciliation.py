"""Acceptance tests for model-first triage reconciliation."""

from __future__ import annotations

import json
from collections import defaultdict
from types import SimpleNamespace

import pytest

from steward.agents import (
    IntakeDisposition,
    IntakeService,
    StrandsTriageClassifier,
    TriageAction,
    TriageAssessment,
    TriageIntegrityError,
    TriageIssueCode,
    TriageReconciliationIssue,
    TriageResult,
)
from steward.domain.clock import FrozenClock
from steward.domain.enums import Category, EventKind, Urgency
from steward.policy import PolicyEngine
from steward.seed import load_seed
from steward.store import InMemoryCaseStore


class Ids:
    def __init__(self) -> None:
        self.counts: defaultdict[str, int] = defaultdict(int)

    def __call__(self, prefix: str) -> str:
        self.counts[prefix] += 1
        return f"{prefix}-reconcile-{self.counts[prefix]}"


class ReplyTokens:
    def __init__(self) -> None:
        self.value = 0

    def __call__(self) -> str:
        self.value += 1
        return f"{self.value:012x}"


class PolicyMustNotRun:
    def evaluate_intake(self, **_kwargs):
        raise AssertionError("invalid triage crossed into policy")


def triage(
    action: TriageAction,
    *,
    category: Category = Category.ELEVATOR,
    urgency: Urgency = Urgency.HIGH,
    confidence: float = 0.95,
    title: str = "",
    asset_id: str | None = None,
    duplicate_case_id: str | None = None,
    rationale: str = "Synthetic model classification.",
) -> TriageResult:
    return TriageResult(
        action=action,
        category=category,
        urgency=urgency,
        confidence=confidence,
        title=title,
        asset_id=asset_id,
        duplicate_case_id=duplicate_case_id,
        rationale=rationale,
    )


def service_for(classifier, *, policy=None, max_attempts=2):
    seed = load_seed()
    store = InMemoryCaseStore()
    service = IntakeService(
        classifier=classifier,
        store=store,
        policy=policy or PolicyEngine(seed.policies, seed.settings),
        assets=seed.assets,
        clock=FrozenClock(seed.demo_messages[0].message.ingested_at),
        max_reconciliation_attempts=max_attempts,
        id_factory=Ids(),
        reply_token_factory=ReplyTokens(),
    )
    return seed, service, store


class MissingAssetThenCorrected:
    def __init__(self) -> None:
        self.classify_calls = []
        self.reconcile_calls = []

    def classify(self, *, message, assets, open_cases):
        self.classify_calls.append((message, tuple(assets), tuple(open_cases)))
        return triage(
            TriageAction.OPEN_CASE,
            title="A Block elevator shudders near the fourth floor",
            rationale="A case should open, but the first model result omitted its asset.",
        )

    def reconcile(
        self,
        *,
        message,
        assets,
        open_cases,
        previous_result,
        issue,
        attempt,
    ):
        self.reconcile_calls.append(
            {
                "message": message,
                "assets": tuple(assets),
                "open_cases": tuple(open_cases),
                "previous_result": previous_result,
                "issue": issue,
                "attempt": attempt,
            }
        )
        return triage(
            TriageAction.OPEN_CASE,
            title="A Block elevator shudders near the fourth floor",
            asset_id="elevator-a",
            rationale="The fresh model matched the exact known A Block asset.",
        )


def test_missing_open_case_asset_is_reconciled_and_trace_is_idempotent():
    model = MissingAssetThenCorrected()
    seed, service, store = service_for(model)
    message = seed.demo_messages[0].message

    opened = service.process(message)

    assert opened.disposition is IntakeDisposition.OPENED
    assert opened.case is not None
    assert opened.case.asset_id == "elevator-a"
    assert opened.reconciliation_attempts == 1
    assert opened.reconciliation_issue_codes == (TriageIssueCode.ASSET_REQUIRED,)
    assert len(model.classify_calls) == 1
    assert len(model.reconcile_calls) == 1
    reconciliation = model.reconcile_calls[0]
    assert reconciliation["attempt"] == 1
    assert reconciliation["issue"].code is TriageIssueCode.ASSET_REQUIRED
    assert reconciliation["previous_result"].asset_id is None

    assessment = store.get_assessment(message.message_id)
    assert assessment is not None
    assert assessment.result.asset_id == "elevator-a"
    assert assessment.reconciliation_attempts == 1
    assert assessment.reconciliation_issue_codes == (TriageIssueCode.ASSET_REQUIRED,)
    opened_event = store.timeline_for(opened.case.case_id)[0]
    assert opened_event.kind is EventKind.CASE_OPENED
    assert opened_event.payload["triage_reconciliation_attempts"] == 1
    assert opened_event.payload["triage_reconciliation_issue_codes"] == [
        "asset_required"
    ]

    replayed = service.process(message)

    assert replayed.disposition is IntakeDisposition.ALREADY_PROCESSED
    assert replayed.reconciliation_attempts == 1
    assert replayed.reconciliation_issue_codes == (TriageIssueCode.ASSET_REQUIRED,)
    assert len(model.classify_calls) == 1
    assert len(model.reconcile_calls) == 1


class DuplicateConflictThenSeparateCase:
    def __init__(self, first_message_id: str, second_message_id: str) -> None:
        self.first_message_id = first_message_id
        self.second_message_id = second_message_id
        self.reconcile_calls = []

    def classify(self, *, message, assets, open_cases):
        del assets
        if message.message_id == self.first_message_id:
            assert open_cases == ()
            return triage(
                TriageAction.OPEN_CASE,
                title="A Block elevator shudders",
                asset_id="elevator-a",
            )
        assert message.message_id == self.second_message_id
        assert len(open_cases) == 1
        return triage(
            TriageAction.LINK_EXISTING,
            asset_id="elevator-b",
            duplicate_case_id=open_cases[0].case_id,
            rationale="The first result contradicted its own duplicate candidate.",
        )

    def reconcile(
        self,
        *,
        message,
        assets,
        open_cases,
        previous_result,
        issue,
        attempt,
    ):
        del assets
        self.reconcile_calls.append(
            (message, tuple(open_cases), previous_result, issue, attempt)
        )
        return triage(
            TriageAction.OPEN_CASE,
            title="B Block elevator doors stuck half-open",
            asset_id="elevator-b",
            rationale="The fresh model recognized a different physical elevator.",
        )


def test_cross_asset_duplicate_is_reclassified_by_model_not_deterministic_aliases():
    seed = load_seed()
    first = seed.demo_messages[0].message
    second = first.model_copy(
        update={
            "message_id": "tg:-1002481179934:reconcile-b",
            "sender_display": "Simon O.",
            "text": (
                "The B Block Elevator is a different physical lift. "
                "Its doors are stuck half-open at the ground floor."
            ),
        },
        deep=True,
    )
    model = DuplicateConflictThenSeparateCase(first.message_id, second.message_id)
    _seed, service, store = service_for(model)

    a_outcome = service.process(first)
    b_outcome = service.process(second)

    assert a_outcome.case is not None
    assert b_outcome.case is not None
    assert a_outcome.case.case_id != b_outcome.case.case_id
    assert a_outcome.case.asset_id == "elevator-a"
    assert b_outcome.case.asset_id == "elevator-b"
    assert b_outcome.disposition is IntakeDisposition.OPENED
    assert b_outcome.reconciliation_attempts == 1
    assert b_outcome.reconciliation_issue_codes == (
        TriageIssueCode.DUPLICATE_ASSET_MISMATCH,
    )
    assert len(store.list_cases()) == 2
    assert len(model.reconcile_calls) == 1
    _, candidates, rejected, issue, attempt = model.reconcile_calls[0]
    assert [case.asset_id for case in candidates] == ["elevator-a"]
    assert rejected.action is TriageAction.LINK_EXISTING
    assert issue.code is TriageIssueCode.DUPLICATE_ASSET_MISMATCH
    assert attempt == 1


class NeverRepairsMissingAsset:
    def __init__(self) -> None:
        self.classify_count = 0
        self.reconcile_issues = []

    def classify(self, *, message, assets, open_cases):
        del message, assets, open_cases
        self.classify_count += 1
        return self._invalid()

    def reconcile(
        self,
        *,
        message,
        assets,
        open_cases,
        previous_result,
        issue,
        attempt,
    ):
        del message, assets, open_cases, previous_result, attempt
        self.reconcile_issues.append(issue.code)
        return self._invalid()

    @staticmethod
    def _invalid():
        return triage(
            TriageAction.OPEN_CASE,
            title="Elevator fault without an asset",
            rationale="The model repeatedly failed to bind a known asset.",
        )


def test_reconciliation_exhaustion_fails_closed_without_persistence():
    model = NeverRepairsMissingAsset()
    seed, service, store = service_for(
        model,
        policy=PolicyMustNotRun(),
        max_attempts=2,
    )
    message = seed.demo_messages[0].message

    with pytest.raises(TriageIntegrityError, match="requires the model to select"):
        service.process(message)

    assert model.classify_count == 1
    assert model.reconcile_issues == [
        TriageIssueCode.ASSET_REQUIRED,
        TriageIssueCode.ASSET_REQUIRED,
    ]
    assert store.get_message(message.message_id) is None
    assert store.get_assessment(message.message_id) is None
    assert store.list_cases() == ()


def test_strands_reconciliation_uses_fresh_agent_and_controlled_context():
    initial = triage(
        TriageAction.OPEN_CASE,
        title="A Block elevator malfunction",
        rationale="Initial result omitted the asset.",
    )
    corrected = triage(
        TriageAction.OPEN_CASE,
        title="A Block elevator malfunction",
        asset_id="elevator-a",
        rationale="Replacement result uses the offered A Block asset.",
    )
    outputs = iter((initial, corrected))
    agents = []
    invocations = []

    class FakeAgent:
        def __init__(self, output):
            self.output = output

        def __call__(self, prompt, **kwargs):
            invocations.append((prompt, kwargs))
            return SimpleNamespace(structured_output=self.output)

    def factory():
        agent = FakeAgent(next(outputs))
        agents.append(agent)
        return agent

    seed = load_seed()
    message = seed.demo_messages[0].message
    classifier = StrandsTriageClassifier(factory)
    rejected = classifier.classify(message=message, assets=seed.assets, open_cases=())
    issue = TriageReconciliationIssue(
        code=TriageIssueCode.ASSET_REQUIRED,
        message="opening category 'elevator' requires the model to select a known asset",
    )

    replacement = classifier.reconcile(
        message=message,
        assets=seed.assets,
        open_cases=(),
        previous_result=rejected,
        issue=issue,
        attempt=1,
    )

    assert replacement == corrected
    assert len(agents) == 2
    assert agents[0] is not agents[1]
    initial_payload = json.loads(invocations[0][0])
    retry_payload = json.loads(invocations[1][0])
    assert "reconciliation_context" not in initial_payload
    assert retry_payload["reconciliation_context"] == {
        "attempt": 1,
        "issue": {
            "code": "asset_required",
            "message": (
                "opening category 'elevator' requires the model to select a known asset"
            ),
        },
        "rejected_result": initial.model_dump(mode="json"),
    }
    assert invocations[0][1]["idempotency_token"] == message.message_id
    assert invocations[1][1]["idempotency_token"] == (
        f"{message.message_id}:reconcile:1"
    )
    assert invocations[1][1]["structured_output_model"] is TriageResult


def test_reconciliation_trace_rejects_incomplete_persisted_evidence():
    seed = load_seed()
    message = seed.demo_messages[0].message

    with pytest.raises(ValueError, match="must retain its issue code"):
        TriageAssessment(
            message_id=message.message_id,
            assessed_at=message.ingested_at,
            result=triage(
                TriageAction.OPEN_CASE,
                title="A Block elevator malfunction",
                asset_id="elevator-a",
            ),
            reconciliation_attempts=1,
            reconciliation_issue_codes=(),
        )


def test_negative_reconciliation_limit_is_rejected():
    model = MissingAssetThenCorrected()

    with pytest.raises(ValueError, match="must not be negative"):
        service_for(model, max_attempts=-1)
