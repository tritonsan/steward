"""Acceptance tests for the first message-to-governed-case vertical slice."""

from __future__ import annotations

import json
from collections import defaultdict
from decimal import Decimal
from types import SimpleNamespace

import pytest

from steward.agents import (
    IntakeDisposition,
    IntakeService,
    StrandsTriageClassifier,
    TriageAction,
    TriageAgentError,
    TriageIntegrityError,
    TriageResult,
)
from steward.domain.clock import FrozenClock
from steward.domain.enums import AutonomyLevel, Category, EventKind, Urgency
from steward.policy import PolicyDecision, PolicyEngine, Rule
from steward.seed import load_seed
from steward.store import InMemoryCaseStore


class ScriptedClassifier:
    def __init__(self, results: dict[str, TriageResult]) -> None:
        self.results = results
        self.calls = []

    def classify(self, *, message, assets, open_cases):
        self.calls.append(
            {
                "message": message,
                "assets": tuple(assets),
                "open_cases": tuple(open_cases),
            }
        )
        return self.results[message.message_id]


class IdSequence:
    def __init__(self) -> None:
        self.counts: defaultdict[str, int] = defaultdict(int)

    def __call__(self, prefix: str) -> str:
        self.counts[prefix] += 1
        return f"{prefix}-{self.counts[prefix]}"


@pytest.fixture(scope="module")
def seed():
    return load_seed()


def demo_message(seed, suffix: str):
    return next(
        item.message for item in seed.demo_messages if item.message.message_id.endswith(suffix)
    )


def triage(
    action: TriageAction,
    *,
    category: Category,
    urgency: Urgency = Urgency.NORMAL,
    confidence: float = 0.95,
    title: str = "",
    asset_id: str | None = None,
    duplicate_case_id: str | None = None,
    rationale: str = "Scripted acceptance-test classification.",
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


def service_for(seed, classifier, *, policy=None):
    store = InMemoryCaseStore()
    service = IntakeService(
        classifier=classifier,
        store=store,
        policy=policy or PolicyEngine(seed.policies, seed.settings),
        assets=seed.assets,
        clock=FrozenClock(seed.demo_messages[0].message.ingested_at),
        id_factory=IdSequence(),
        reply_token_factory=lambda: "0123456789ab",
    )
    return service, store


def test_seed_replay_opens_one_case_links_two_messages_and_ignores_chatter(seed):
    messages = [demo_message(seed, suffix) for suffix in ("88401", "88402", "88403", "88404")]
    classifier = ScriptedClassifier(
        {
            messages[0].message_id: triage(
                TriageAction.OPEN_CASE,
                category=Category.ELEVATOR,
                urgency=Urgency.HIGH,
                title="A Block elevator shudders near the fourth floor",
                asset_id="elevator-a",
                rationale="A repeat mechanical fault on a known elevator asset.",
            ),
            messages[1].message_id: triage(
                TriageAction.LINK_EXISTING,
                category=Category.ELEVATOR,
                urgency=Urgency.NORMAL,
                asset_id="elevator-a",
                duplicate_case_id="case-1",
                rationale="Corroborates the same location and symptom five minutes later.",
            ),
            messages[2].message_id: triage(
                TriageAction.LINK_EXISTING,
                category=Category.ELEVATOR,
                urgency=Urgency.LOW,
                asset_id="elevator-a",
                duplicate_case_id="case-1",
                rationale="A question about the currently discussed elevator fault.",
            ),
            messages[3].message_id: triage(
                TriageAction.IGNORE,
                category=Category.OTHER,
                urgency=Urgency.LOW,
                rationale="Social thanks about a past barbecue; no operational problem.",
            ),
        }
    )
    service, store = service_for(seed, classifier)

    outcomes = [service.process(message) for message in messages]

    assert [outcome.disposition for outcome in outcomes] == [
        IntakeDisposition.OPENED,
        IntakeDisposition.LINKED,
        IntakeDisposition.LINKED,
        IntakeDisposition.IGNORED,
    ]
    assert len(store.list_cases()) == 1
    case = store.list_cases()[0]
    assert case.case_id == "case-1"
    assert case.category is Category.ELEVATOR
    assert case.asset_id == "elevator-a"
    assert case.urgency is Urgency.HIGH
    assert case.autonomy_level is AutonomyLevel.AUTONOMOUS
    assert case.source_message_ids == [message.message_id for message in messages[:3]]

    for message in messages[:3]:
        assert store.get_message(message.message_id).case_id == case.case_id
    assert store.get_message(messages[3].message_id).case_id is None
    assert store.get_assessment(messages[3].message_id).result.action is TriageAction.IGNORE

    assert classifier.calls[0]["open_cases"] == ()
    assert [item.case_id for item in classifier.calls[1]["open_cases"]] == ["case-1"]
    assert [event.kind for event in store.timeline_for(case.case_id)] == [
        EventKind.CASE_OPENED,
        EventKind.POLICY_EVALUATED,
        EventKind.MESSAGES_LINKED,
        EventKind.MESSAGES_LINKED,
    ]
    audit = store.audit_for(case.case_id)
    assert len(audit) == 1
    assert audit[0].policy_rule_id == Rule.INTAKE_WITHIN_POLICY

    # The store owns defensive copies, not the objects handed back to callers.
    outcomes[0].case.title = "caller attempted mutation"
    assert store.get_case(case.case_id).title != "caller attempted mutation"

    # Adapter retries are idempotent and do not ask the model a second time.
    repeated = service.process(messages[0])
    assert repeated.disposition is IntakeDisposition.ALREADY_PROCESSED
    assert len(classifier.calls) == 4
    assert len(store.list_cases()) == 1


def test_unconfigured_hvac_opens_a_case_but_fails_closed_at_policy(seed):
    message = demo_message(seed, "88405")
    classifier = ScriptedClassifier(
        {
            message.message_id: triage(
                TriageAction.OPEN_CASE,
                category=Category.HVAC,
                urgency=Urgency.NORMAL,
                title="A Block lobby HVAC blows warm and rattles",
                asset_id="lobby-hvac",
            )
        }
    )
    service, store = service_for(seed, classifier)

    outcome = service.process(message)

    assert outcome.disposition is IntakeDisposition.OPENED
    assert outcome.case.autonomy_level is AutonomyLevel.ESCALATE
    assert outcome.policy_decision.rule_id == Rule.POLICY_MISSING
    assert store.audit_for(outcome.case.case_id)[0].policy_rule_id == Rule.POLICY_MISSING
    policy_event = store.timeline_for(outcome.case.case_id)[1]
    assert policy_event.kind is EventKind.POLICY_EVALUATED
    assert policy_event.payload["allowed_vendor_ids"] == []


class PolicyMustNotRun:
    def evaluate_intake(self, **_kwargs):
        raise AssertionError("ignored resident text reached the policy engine")


def test_seeded_prompt_injection_is_stored_as_data_and_never_reaches_policy(seed):
    message = demo_message(seed, "88408")
    assert "ignore all previous instructions" in message.text.lower()
    classifier = ScriptedClassifier(
        {
            message.message_id: triage(
                TriageAction.IGNORE,
                category=Category.OTHER,
                urgency=Urgency.LOW,
                confidence=0.99,
                rationale="An instruction directed at the agent, not a resident problem report.",
            )
        }
    )
    service, store = service_for(seed, classifier, policy=PolicyMustNotRun())

    outcome = service.process(message)

    assert outcome.disposition is IntakeDisposition.IGNORED
    assert store.list_cases() == ()
    assert store.get_message(message.message_id).text == message.text
    assert store.get_assessment(message.message_id).result.action is TriageAction.IGNORE
    assert set(TriageResult.model_json_schema()["properties"]) == {
        "action",
        "category",
        "urgency",
        "confidence",
        "title",
        "asset_id",
        "duplicate_case_id",
        "rationale",
        "missing_information",
        "clarification_question",
    }


def test_hallucinated_duplicate_id_is_rejected_before_any_record_is_written(seed):
    message = demo_message(seed, "88402")
    classifier = ScriptedClassifier(
        {
            message.message_id: triage(
                TriageAction.LINK_EXISTING,
                category=Category.ELEVATOR,
                asset_id="elevator-a",
                duplicate_case_id="case-not-offered",
            )
        }
    )
    service, store = service_for(seed, classifier, policy=PolicyMustNotRun())

    with pytest.raises(TriageIntegrityError, match="outside the open candidate set"):
        service.process(message)

    assert store.get_message(message.message_id) is None
    assert store.get_assessment(message.message_id) is None
    assert store.list_cases() == ()


def test_unknown_or_cross_category_assets_are_rejected(seed):
    message = demo_message(seed, "88401")

    for asset_id, expected in (
        ("invented-asset", "asset it was not offered"),
        ("lobby-hvac", "category does not match"),
    ):
        classifier = ScriptedClassifier(
            {
                message.message_id: triage(
                    TriageAction.OPEN_CASE,
                    category=Category.ELEVATOR,
                    title="Unsafe model output",
                    asset_id=asset_id,
                )
            }
        )
        service, store = service_for(seed, classifier, policy=PolicyMustNotRun())

        with pytest.raises(TriageIntegrityError, match=expected):
            service.process(message)
        assert store.list_cases() == ()


def test_invalid_reply_token_is_rejected_before_case_is_persisted(seed):
    message = demo_message(seed, "88401")
    classifier = ScriptedClassifier(
        {
            message.message_id: triage(
                TriageAction.OPEN_CASE,
                category=Category.ELEVATOR,
                title="A Block elevator fault",
                asset_id="elevator-a",
            )
        }
    )
    store = InMemoryCaseStore()
    service = IntakeService(
        classifier=classifier,
        store=store,
        policy=PolicyEngine(seed.policies, seed.settings),
        assets=seed.assets,
        clock=FrozenClock(seed.demo_messages[0].message.ingested_at),
        id_factory=IdSequence(),
        reply_token_factory=lambda: "not-a-valid-token",
    )

    with pytest.raises(ValueError, match="invalid reply token"):
        service.process(message)

    assert store.get_message(message.message_id) is None
    assert store.list_cases() == ()


def test_policy_receives_only_closed_category_confidence_and_computed_spend(seed):
    message = demo_message(seed, "88408")

    class RecordingPolicy:
        def __init__(self):
            self.calls = []

        def evaluate_intake(
            self,
            *,
            category,
            triage_confidence,
            month_to_date_spend=Decimal("0"),
        ):
            self.calls.append((category, triage_confidence, month_to_date_spend))
            return PolicyDecision(
                level=AutonomyLevel.ESCALATE,
                rule_id=Rule.LOW_CONFIDENCE,
                reason="Synthetic policy boundary test.",
            )

    policy = RecordingPolicy()
    classifier = ScriptedClassifier(
        {
            message.message_id: triage(
                TriageAction.OPEN_CASE,
                category=Category.OTHER,
                confidence=0.1,
                title="Suspicious resident instruction",
                rationale="Even a classifier mistake cannot turn prose into authority.",
            )
        }
    )
    service, _store = service_for(seed, classifier, policy=policy)

    outcome = service.process(message)

    assert outcome.case.autonomy_level is AutonomyLevel.ESCALATE
    assert policy.calls == [(Category.OTHER, 0.1, Decimal("0"))]
    assert message.text not in repr(policy.calls)


def test_strands_adapter_uses_tool_free_fresh_agents_and_structured_output(seed, monkeypatch):
    import boto3
    import strands
    import strands.models

    message = demo_message(seed, "88408")
    expected = triage(
        TriageAction.IGNORE,
        category=Category.OTHER,
        urgency=Urgency.LOW,
        rationale="The message attempts to instruct the classifier.",
    )
    session_calls = []
    model_calls = []
    constructor_calls = []
    invocation_calls = []

    class FakeBedrockModel:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            model_calls.append(kwargs)

    class FakeAgent:
        def __call__(self, prompt, **kwargs):
            invocation_calls.append((prompt, kwargs))
            return SimpleNamespace(structured_output=expected)

    def fake_session(**kwargs):
        session = SimpleNamespace(config=kwargs)
        session_calls.append(kwargs)
        return session

    def fake_agent(**kwargs):
        constructor_calls.append(kwargs)
        return FakeAgent()

    monkeypatch.setattr(boto3, "Session", fake_session)
    monkeypatch.setattr(strands.models, "BedrockModel", FakeBedrockModel)
    monkeypatch.setattr(strands, "Agent", fake_agent)
    classifier = StrandsTriageClassifier.bedrock(
        "test-bedrock-model",
        profile_name="test-profile",
        region_name="us-test-1",
        temperature=0.0,
        max_tokens=321,
    )

    assert (
        classifier.classify(
            message=message,
            assets=seed.assets,
            open_cases=(),
        )
        == expected
    )
    assert (
        classifier.classify(
            message=message,
            assets=seed.assets,
            open_cases=(),
        )
        == expected
    )

    assert session_calls == [
        {"profile_name": "test-profile", "region_name": "us-test-1"},
        {"profile_name": "test-profile", "region_name": "us-test-1"},
    ]
    assert len(model_calls) == 2
    assert all(call["model_id"] == "test-bedrock-model" for call in model_calls)
    assert all(call["temperature"] == 0.0 for call in model_calls)
    assert all(call["max_tokens"] == 321 for call in model_calls)
    assert len(constructor_calls) == 2
    assert all(call["tools"] == [] for call in constructor_calls)
    assert all(call["callback_handler"] is None for call in constructor_calls)
    assert all("untrusted data" in call["system_prompt"] for call in constructor_calls)
    assert all(isinstance(call["model"], FakeBedrockModel) for call in constructor_calls)

    prompt, kwargs = invocation_calls[0]
    payload = json.loads(prompt)
    assert set(payload) == {
        "message",
        "allowed_categories",
        "known_assets",
        "open_case_candidates",
    }
    assert payload["message"]["text"] == message.text
    assert "policies" not in payload
    assert kwargs["structured_output_model"] is TriageResult
    assert kwargs["idempotency_token"] == message.message_id


def test_strands_adapter_refuses_missing_structured_output(seed):
    message = demo_message(seed, "88401")

    class EmptyAgent:
        def __call__(self, _prompt, **_kwargs):
            return SimpleNamespace(structured_output=None)

    classifier = StrandsTriageClassifier(lambda: EmptyAgent())

    with pytest.raises(TriageAgentError, match="no valid structured triage output"):
        classifier.classify(message=message, assets=seed.assets, open_cases=())


@pytest.mark.parametrize(
    "values",
    [
        {
            "action": TriageAction.OPEN_CASE,
            "category": Category.ELEVATOR,
            "urgency": Urgency.NORMAL,
            "confidence": 0.9,
            "rationale": "No title was supplied.",
        },
        {
            "action": TriageAction.IGNORE,
            "category": Category.OTHER,
            "urgency": Urgency.LOW,
            "confidence": 0.9,
            "duplicate_case_id": "case-1",
            "rationale": "Ignore cannot also link.",
        },
    ],
)
def test_triage_schema_rejects_internally_inconsistent_actions(values):
    with pytest.raises(ValueError):
        TriageResult(**values)
