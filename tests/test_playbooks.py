from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from steward.domain.clock import FrozenClock
from steward.domain.enums import ActorType, CaseStatus, Category, Urgency, group_of
from steward.domain.models import Case
from steward.operations import (
    CompletionClaim,
    CompletionVerificationService,
    VerificationIntegrityError,
    VerificationOutcome,
    VerificationResponse,
)
from steward.playbooks import CategoryPlaybook, PlaybookRisk, default_playbooks
from steward.policy import PolicyEngine
from steward.proactive import ProactiveMaintenanceEngine
from steward.seed import load_seed

UTC = timezone.utc


def fire_case_and_claim():
    opened = datetime(2026, 9, 1, 9, tzinfo=UTC)
    case = Case(
        case_id="case-fire-001",
        reply_token="abcdef123456",
        title="Annual fire panel inspection",
        category=Category.FIRE_SAFETY,
        group=group_of(Category.FIRE_SAFETY),
        urgency=Urgency.NORMAL,
        asset_id="fire-panel-a",
        opened_at=opened,
        updated_at=opened,
        accepted_quote_id="quote-fire-001",
        status=CaseStatus.SCHEDULED,
    )
    claim = CompletionClaim(
        claim_id="claim-fire-001",
        case_id=case.case_id,
        vendor_id="fire-vendor",
        quote_id=case.accepted_quote_id,
        source_id="fire-completion-report-001",
        onsite_at=opened + timedelta(days=1),
        resolved_at=opened + timedelta(days=1, hours=2),
        claimed_at=opened + timedelta(days=1, hours=3),
        actual_cost=Decimal("400.00"),
        currency="USD",
        work_performed="Inspected the panel and tested the alarm circuit.",
        simulated=True,
    )
    return case, claim


def test_default_catalog_covers_priority_categories_with_conservative_boundaries():
    catalog = default_playbooks()
    expected = {
        Category.ELEVATOR,
        Category.LANDSCAPING,
        Category.POOL,
        Category.HVAC,
        Category.FIRE_SAFETY,
        Category.COMMON_AREA_CLEANING,
        Category.WASTE,
    }

    assert expected.issubset(set(catalog.categories))
    for category in expected:
        playbook = catalog.for_category(category)
        assert playbook.third_party_contact_requires_policy is True
        assert playbook.proactive_vendor_contact_allowed is False
        assert playbook.automatic_closure_allowed is False
        assert playbook.completion_evidence
        assert playbook.allowed_verifiers
        assert all(rule.category is category for rule in playbook.proactive_rules)

    fire = catalog.for_category(Category.FIRE_SAFETY)
    assert fire.risk is PlaybookRisk.HIGH
    assert fire.allowed_verifiers == (ActorType.HUMAN,)
    assert fire.verification_timeout_hours == 24
    fallback = catalog.for_category(Category.PLUMBING)
    assert fallback.risk is PlaybookRisk.HIGH
    assert fallback.allowed_verifiers == (ActorType.HUMAN,)


def test_playbook_schema_cannot_enable_proactive_contact_or_automatic_closure():
    elevator = default_playbooks().for_category(Category.ELEVATOR)
    payload = elevator.model_dump()
    payload["proactive_vendor_contact_allowed"] = True
    with pytest.raises(ValidationError):
        CategoryPlaybook.model_validate(payload)
    payload = elevator.model_dump()
    payload["automatic_closure_allowed"] = True
    with pytest.raises(ValidationError):
        CategoryPlaybook.model_validate(payload)


def test_fire_playbook_rejects_resident_self_confirmation_but_accepts_manager():
    case, claim = fire_case_and_claim()
    clock = FrozenClock(claim.claimed_at)
    service = CompletionVerificationService(
        clock=clock,
        playbooks=default_playbooks(),
    )
    requested = service.request(case=case, claim=claim)
    assert requested.request.allowed_actors == (ActorType.HUMAN,)
    assert requested.request.due_at == claim.claimed_at + timedelta(hours=24)
    clock.advance(timedelta(hours=1))
    resident = VerificationResponse(
        response_id="resident-fire-response",
        request_id=requested.request.request_id,
        case_id=case.case_id,
        outcome=VerificationOutcome.CONFIRMED,
        actor=ActorType.RESIDENT,
        actor_label="Resident A.",
        source_id="resident-fire-source",
        responded_at=clock.now(),
    )
    with pytest.raises(VerificationIntegrityError, match="not allowed"):
        service.respond(
            case=requested.case,
            claim=claim,
            request=requested.request,
            response=resident,
        )
    manager = resident.model_copy(
        update={
            "response_id": "manager-fire-response",
            "actor": ActorType.HUMAN,
            "actor_label": "Building Manager",
            "source_id": "manager-fire-source",
        }
    )
    confirmed = service.respond(
        case=requested.case,
        claim=claim,
        request=requested.request,
        response=manager,
    )
    assert confirmed.resolution_evidence is not None
    assert confirmed.resolution_evidence.verification_source_ids == (
        "manager-fire-source",
    )


def test_catalog_rules_feed_proactive_engine_without_granting_contact():
    bundle = load_seed()
    catalog = default_playbooks()
    engine = ProactiveMaintenanceEngine(
        policy=PolicyEngine(bundle.policies, bundle.settings),
        rules=catalog.proactive_rules,
    )

    suggestions = engine.suggest(bundle.history, as_of=datetime(2026, 9, 1, tzinfo=UTC))
    by_rule = {item.rule_id: item for item in suggestions}

    assert "landscaping.autumn.beds" in by_rule
    assert "pool.season.close" in by_rule
    assert "fire.annual.review" in by_rule
    assert all(item.vendor_contact_allowed is False for item in suggestions)
    assert all(item.requires_human_review is True for item in suggestions)
