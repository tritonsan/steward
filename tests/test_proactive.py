from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from steward.domain.enums import AutonomyLevel, Category
from steward.memory import CaseRecord
from steward.policy import PolicyEngine
from steward.proactive import (
    MaintenanceSuggestion,
    ProactiveMaintenanceEngine,
    ProactiveRule,
    ProactiveRuleKind,
)
from steward.seed import load_seed

UTC = timezone.utc


def record(case_id: str, opened: datetime, *, verified: bool = True) -> CaseRecord:
    return CaseRecord(
        case_id=case_id,
        title="A Block elevator shudders near floor four",
        category=Category.ELEVATOR,
        asset_id="elevator-a",
        opened_at=opened,
        resolved_at=opened,
        closed_at=opened,
        outcome_verified=verified,
        problem="Recurring shudder and grinding near floor four.",
        work_performed="Corrected rail bracket alignment.",
        verification_source_ids=([f"verification-{case_id}"] if verified else []),
    )


def test_recurrence_suggestion_uses_only_verified_cases_and_is_deterministic():
    bundle = load_seed()
    rules = (
        ProactiveRule(
            rule_id="elevator.recurrence.review",
            category=Category.ELEVATOR,
            kind=ProactiveRuleKind.RECURRENCE,
            title="Review recurring elevator fault before the next interval",
            asset_id="elevator-a",
            lead_days=30,
            horizon_days=120,
            min_verified_cases=2,
        ),
    )
    engine = ProactiveMaintenanceEngine(
        policy=PolicyEngine(bundle.policies, bundle.settings),
        rules=rules,
    )
    records = (
        record("verified-001", datetime(2026, 1, 1, tzinfo=UTC)),
        record("verified-002", datetime(2026, 4, 1, tzinfo=UTC)),
        record("unverified-003", datetime(2026, 5, 1, tzinfo=UTC), verified=False),
    )
    as_of = datetime(2026, 6, 15, tzinfo=UTC)

    first = engine.suggest(records, as_of=as_of)
    second = engine.suggest(reversed(records), as_of=as_of)

    assert first == second
    assert len(first) == 1
    suggestion = first[0]
    assert suggestion.source_case_ids == ("verified-001", "verified-002")
    assert "unverified-003" not in suggestion.source_ids
    assert suggestion.due_at.date().isoformat() == "2026-06-30"
    assert suggestion.policy_level is AutonomyLevel.AUTONOMOUS
    assert suggestion.vendor_contact_allowed is False
    assert suggestion.requires_human_review is True
    assert suggestion.rule_id in suggestion.source_ids


def test_calendar_rules_are_source_traced_and_conservative_for_fire_safety():
    bundle = load_seed()
    rules = (
        ProactiveRule(
            rule_id="landscaping.autumn.beds",
            category=Category.LANDSCAPING,
            kind=ProactiveRuleKind.CALENDAR,
            title="Prepare autumn planting",
            calendar_month=9,
            calendar_day=20,
            lead_days=30,
            horizon_days=60,
        ),
        ProactiveRule(
            rule_id="fire.annual.review",
            category=Category.FIRE_SAFETY,
            kind=ProactiveRuleKind.CALENDAR,
            title="Prepare annual fire-safety review",
            calendar_month=9,
            calendar_day=25,
            lead_days=30,
            horizon_days=60,
        ),
    )
    engine = ProactiveMaintenanceEngine(
        policy=PolicyEngine(bundle.policies, bundle.settings),
        rules=rules,
    )

    suggestions = engine.suggest((), as_of=datetime(2026, 9, 1, tzinfo=UTC))

    assert [item.rule_id for item in suggestions] == [
        "landscaping.autumn.beds",
        "fire.annual.review",
    ]
    fire = suggestions[1]
    assert fire.policy_level is AutonomyLevel.PREPARE_ONLY
    assert fire.policy_level is not AutonomyLevel.AUTONOMOUS
    assert fire.vendor_contact_allowed is False
    assert fire.source_ids == ("fire.annual.review",)


def test_suggestion_schema_cannot_be_coerced_to_vendor_contact():
    bundle = load_seed()
    rule = ProactiveRule(
        rule_id="pool.season.close",
        category=Category.POOL,
        kind=ProactiveRuleKind.CALENDAR,
        title="Prepare pool season close",
        calendar_month=9,
        calendar_day=21,
    )
    suggestion = ProactiveMaintenanceEngine(
        policy=PolicyEngine(bundle.policies, bundle.settings),
        rules=(rule,),
    ).suggest((), as_of=datetime(2026, 9, 1, tzinfo=UTC))[0]

    payload = suggestion.model_dump()
    payload["vendor_contact_allowed"] = True
    with pytest.raises(ValidationError):
        MaintenanceSuggestion.model_validate(payload)


def test_invalid_proactive_rules_fail_closed():
    with pytest.raises(ValidationError, match="month and day"):
        ProactiveRule(
            rule_id="bad-calendar",
            category=Category.POOL,
            kind=ProactiveRuleKind.CALENDAR,
            title="Bad calendar",
        )
    with pytest.raises(ValueError, match="unique"):
        rule = ProactiveRule(
            rule_id="duplicate",
            category=Category.POOL,
            kind=ProactiveRuleKind.CALENDAR,
            title="Pool review",
            calendar_month=9,
            calendar_day=20,
        )
        ProactiveMaintenanceEngine(
            policy=PolicyEngine({}, load_seed().settings),
            rules=(rule, rule),
        )


def test_bootstrap_enriches_one_suggestion_and_preserves_legacy_decisions(tmp_path):
    from steward.proactive import SuggestionStatus
    from steward.store import SqliteOperationalStore

    bundle = load_seed()
    engine = ProactiveMaintenanceEngine(
        policy=PolicyEngine(bundle.policies, bundle.settings),
        rules=(
            ProactiveRule(
                rule_id="elevator.annual.review",
                category=Category.ELEVATOR,
                kind=ProactiveRuleKind.CALENDAR,
                title="Annual inspection",
                asset_id="elevator-a",
                calendar_month=9,
                calendar_day=20,
            ),
        ),
    )
    now = datetime(2026, 9, 10, tzinfo=UTC)
    empty = engine.suggest((), as_of=now)[0]
    enriched = engine.suggest((record("verified-1", datetime(2026, 8, 1, tzinfo=UTC)),), as_of=now)[
        0
    ]
    assert empty.suggestion_id == enriched.suggestion_id
    with closing(SqliteOperationalStore(tmp_path / "enrichment.db")) as store:
        assert store.upsert_suggestion(empty)
        assert store.upsert_suggestion(enriched)
        assert len(store.list_suggestions(status=SuggestionStatus.SUGGESTED)) == 1
        assert store.suggestion(empty.suggestion_id).source_case_ids == ("verified-1",)
        store.decide_suggestion(
            empty.suggestion_id,
            status=SuggestionStatus.DISMISSED,
            decided_by="Simon O.",
            decided_at=now,
        )
        assert not store.upsert_suggestion(enriched)
        assert not store.list_suggestions(status=SuggestionStatus.SUGGESTED)

    # Model a pre-fix identity with the original evidence retained as history.
    legacy = empty.model_copy(update={"suggestion_id": "legacy-empty-evidence"})
    with closing(SqliteOperationalStore(tmp_path / "legacy.db")) as store:
        store.upsert_suggestion(legacy)
        assert store.upsert_suggestion(enriched)
        assert store.suggestion(legacy.suggestion_id).status is SuggestionStatus.EXPIRED
        assert store.list_suggestions(status=SuggestionStatus.SUGGESTED) == (enriched,)
    with closing(SqliteOperationalStore(tmp_path / "legacy-decision.db")) as store:
        store.upsert_suggestion(legacy)
        decided = store.decide_suggestion(
            legacy.suggestion_id,
            status=SuggestionStatus.ACCEPTED,
            decided_by="Simon O.",
            decided_at=now,
        )
        assert not store.upsert_suggestion(enriched)
        assert store.suggestion(legacy.suggestion_id) == decided
        assert not store.list_suggestions(status=SuggestionStatus.SUGGESTED)


@pytest.mark.parametrize("legacy_status", ["accepted", "dismissed"])
def test_acted_on_legacy_review_expires_coexisting_pending_reviews(tmp_path, legacy_status):
    from steward.proactive import SuggestionStatus
    from steward.store import SqliteOperationalStore

    bundle = load_seed()
    now = datetime(2026, 9, 10, tzinfo=UTC)
    rule = ProactiveRule(
        rule_id="elevator.annual.review",
        category=Category.ELEVATOR,
        kind=ProactiveRuleKind.CALENDAR,
        title="Annual inspection",
        asset_id="elevator-a",
        calendar_month=9,
        calendar_day=20,
    )
    canonical = ProactiveMaintenanceEngine(
        policy=PolicyEngine(bundle.policies, bundle.settings), rules=(rule,)
    ).suggest((record("verified-1", datetime(2026, 8, 1, tzinfo=UTC)),), as_of=now)[0]
    legacy = MaintenanceSuggestion.model_validate(
        {
            **canonical.model_dump(),
            "suggestion_id": "legacy-acted-on-review",
            "status": legacy_status,
            "source_ids": (rule.rule_id,),
            "source_case_ids": (),
            "decided_by": "Simon O.",
            "decided_at": now,
        }
    )
    old_pending = canonical.model_copy(update={"suggestion_id": "legacy-pending-review"})
    path = tmp_path / "coexisting-legacy.db"
    with closing(SqliteOperationalStore(path)) as store:
        assert store.upsert_suggestion(canonical)
        # A mixed-version rollout or imported database can already contain both
        # identities. Insert that historical fixture without today's dedup logic.
        with store.atomic() as conn:
            for historical in (legacy, old_pending):
                conn.execute(
                    "INSERT INTO proactive_suggestions(suggestion_id, rule_id, category, "
                    "status, due_at, suggested_at, doc_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        historical.suggestion_id,
                        historical.rule_id,
                        historical.category.value,
                        historical.status.value,
                        historical.due_at.isoformat(),
                        historical.suggested_at.isoformat(),
                        historical.model_dump_json(),
                    ),
                )
        assert len(store.list_suggestions(status=SuggestionStatus.SUGGESTED)) == 2

        assert not store.upsert_suggestion(canonical)

        assert not store.list_suggestions(status=SuggestionStatus.SUGGESTED)
        assert store.suggestion(legacy.suggestion_id) == legacy
        assert store.suggestion(canonical.suggestion_id) == canonical.model_copy(
            update={"status": SuggestionStatus.EXPIRED}
        )
        assert store.suggestion(old_pending.suggestion_id) == old_pending.model_copy(
            update={"status": SuggestionStatus.EXPIRED}
        )
    with closing(SqliteOperationalStore(path)) as store:
        assert not store.upsert_suggestion(canonical)
        assert not store.list_suggestions(status=SuggestionStatus.SUGGESTED)
        assert store.suggestion(legacy.suggestion_id) == legacy
