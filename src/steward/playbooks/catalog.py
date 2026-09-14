"""Data-driven category playbooks; authority remains in PolicyEngine."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from steward.domain.enums import ActorType, Category
from steward.proactive import ProactiveRule, ProactiveRuleKind

__all__ = [
    "CategoryPlaybook",
    "EvidenceRequirement",
    "PlaybookCatalog",
    "PlaybookRisk",
    "default_playbooks",
]


class PlaybookRisk(str, Enum):
    ROUTINE = "routine"
    CONTROLLED = "controlled"
    HIGH = "high"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=False)


class EvidenceRequirement(_Model):
    code: str = Field(min_length=1)
    label: str = Field(min_length=1)
    required_fact: str = Field(min_length=1)
    source_required: bool = True


class CategoryPlaybook(_Model):
    playbook_id: str = Field(min_length=1)
    category: Category
    label: str = Field(min_length=1)
    risk: PlaybookRisk
    required_intake_facts: tuple[str, ...]
    completion_evidence: tuple[EvidenceRequirement, ...]
    allowed_verifiers: tuple[ActorType, ...]
    verification_timeout_hours: int = Field(default=48, gt=0, le=720)
    proactive_rules: tuple[ProactiveRule, ...] = ()
    safety_notes: tuple[str, ...] = ()
    third_party_contact_requires_policy: Literal[True] = True
    proactive_vendor_contact_allowed: Literal[False] = False
    automatic_closure_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _internally_consistent(self) -> CategoryPlaybook:
        if not self.required_intake_facts:
            raise ValueError("playbook needs required intake facts")
        if not self.completion_evidence:
            raise ValueError("playbook needs completion evidence")
        if not self.allowed_verifiers:
            raise ValueError("playbook needs at least one verifier")
        if any(rule.category is not self.category for rule in self.proactive_rules):
            raise ValueError("playbook proactive rule category mismatch")
        codes = [item.code for item in self.completion_evidence]
        if len(codes) != len(set(codes)):
            raise ValueError("playbook evidence codes must be unique")
        return self

    def validate_completion_facts(self, facts: Mapping[str, Any]) -> None:
        missing = [
            requirement.required_fact
            for requirement in self.completion_evidence
            if facts.get(requirement.required_fact) in (None, "", (), [])
        ]
        if missing:
            raise ValueError(
                f"playbook {self.playbook_id} is missing completion facts: "
                + ", ".join(sorted(set(missing)))
            )


class PlaybookCatalog:
    __slots__ = ("_by_category", "_fallback")

    def __init__(
        self,
        playbooks: Iterable[CategoryPlaybook],
        *,
        fallback: CategoryPlaybook,
    ) -> None:
        items = tuple(playbooks)
        categories = [item.category for item in items]
        ids = [item.playbook_id for item in items]
        if len(categories) != len(set(categories)):
            raise ValueError("playbook categories must be unique")
        if len(ids) != len(set(ids)):
            raise ValueError("playbook ids must be unique")
        all_rules = [rule.rule_id for item in (*items, fallback) for rule in item.proactive_rules]
        if len(all_rules) != len(set(all_rules)):
            raise ValueError("playbook proactive rule ids must be globally unique")
        self._by_category = {item.category: item for item in items}
        self._fallback = fallback

    def for_category(self, category: Category) -> CategoryPlaybook:
        return self._by_category.get(category, self._fallback)

    @property
    def proactive_rules(self) -> tuple[ProactiveRule, ...]:
        return tuple(
            rule for playbook in self._by_category.values() for rule in playbook.proactive_rules
        )

    @property
    def categories(self) -> tuple[Category, ...]:
        return tuple(sorted(self._by_category, key=lambda item: item.value))


def default_playbooks() -> PlaybookCatalog:
    common_claim = (
        EvidenceRequirement(
            code="work_performed",
            label="What work was actually performed",
            required_fact="work_performed",
        ),
        EvidenceRequirement(
            code="actual_cost",
            label="Exact final cost",
            required_fact="actual_cost",
        ),
        EvidenceRequirement(
            code="onsite_at",
            label="When attendance occurred",
            required_fact="onsite_at",
        ),
        EvidenceRequirement(
            code="source_id",
            label="Source message or completion report",
            required_fact="source_id",
        ),
    )
    resident_or_manager = (ActorType.RESIDENT, ActorType.HUMAN)
    manager_only = (ActorType.HUMAN,)
    playbooks = (
        CategoryPlaybook(
            playbook_id="playbook.elevator.v1",
            category=Category.ELEVATOR,
            label="Elevator fault and recurrence",
            risk=PlaybookRisk.CONTROLLED,
            required_intake_facts=("asset_id", "symptom", "location", "travel_direction"),
            completion_evidence=common_claim,
            allowed_verifiers=resident_or_manager,
            proactive_rules=(
                ProactiveRule(
                    rule_id="elevator.recurrence.review",
                    category=Category.ELEVATOR,
                    kind=ProactiveRuleKind.RECURRENCE,
                    title="Review a recurring elevator fault before its next interval",
                    lead_days=30,
                    horizon_days=120,
                    min_verified_cases=2,
                ),
            ),
            safety_notes=("Never treat urgency prose as spend authority.",),
        ),
        CategoryPlaybook(
            playbook_id="playbook.landscaping.v1",
            category=Category.LANDSCAPING,
            label="Grounds and seasonal landscaping",
            risk=PlaybookRisk.ROUTINE,
            required_intake_facts=("location", "requested_work", "season"),
            completion_evidence=common_claim,
            allowed_verifiers=resident_or_manager,
            proactive_rules=(
                ProactiveRule(
                    rule_id="landscaping.autumn.beds",
                    category=Category.LANDSCAPING,
                    kind=ProactiveRuleKind.CALENDAR,
                    title="Prepare autumn planting",
                    calendar_month=9,
                    calendar_day=20,
                    lead_days=45,
                    horizon_days=90,
                ),
            ),
        ),
        CategoryPlaybook(
            playbook_id="playbook.pool.v1",
            category=Category.POOL,
            label="Pool seasonal operation",
            risk=PlaybookRisk.CONTROLLED,
            required_intake_facts=("asset_id", "season_action", "target_date"),
            completion_evidence=common_claim,
            allowed_verifiers=manager_only,
            proactive_rules=(
                ProactiveRule(
                    rule_id="pool.season.close",
                    category=Category.POOL,
                    kind=ProactiveRuleKind.CALENDAR,
                    title="Prepare pool season close",
                    calendar_month=9,
                    calendar_day=21,
                    lead_days=45,
                    horizon_days=90,
                ),
            ),
        ),
        CategoryPlaybook(
            playbook_id="playbook.hvac.v1",
            category=Category.HVAC,
            label="HVAC fault and seasonal inspection",
            risk=PlaybookRisk.CONTROLLED,
            required_intake_facts=("asset_id", "temperature_behavior", "noise", "duration"),
            completion_evidence=common_claim,
            allowed_verifiers=resident_or_manager,
            proactive_rules=(
                ProactiveRule(
                    rule_id="hvac.preseason.review",
                    category=Category.HVAC,
                    kind=ProactiveRuleKind.CALENDAR,
                    title="Prepare pre-season HVAC review",
                    calendar_month=5,
                    calendar_day=1,
                    lead_days=45,
                    horizon_days=90,
                ),
            ),
        ),
        CategoryPlaybook(
            playbook_id="playbook.fire_safety.v1",
            category=Category.FIRE_SAFETY,
            label="Fire-safety inspection and evidence",
            risk=PlaybookRisk.HIGH,
            required_intake_facts=("location", "device_or_system", "observed_condition"),
            completion_evidence=common_claim,
            allowed_verifiers=manager_only,
            verification_timeout_hours=24,
            proactive_rules=(
                ProactiveRule(
                    rule_id="fire.annual.review",
                    category=Category.FIRE_SAFETY,
                    kind=ProactiveRuleKind.CALENDAR,
                    title="Prepare annual fire-safety review",
                    calendar_month=9,
                    calendar_day=25,
                    lead_days=60,
                    horizon_days=120,
                ),
            ),
            safety_notes=(
                "A playbook never grants autonomous fire-safety authority.",
                "Completion requires management evidence.",
            ),
        ),
        CategoryPlaybook(
            playbook_id="playbook.cleaning.v1",
            category=Category.COMMON_AREA_CLEANING,
            label="Common-area cleaning service",
            risk=PlaybookRisk.ROUTINE,
            required_intake_facts=("location", "service_gap", "observed_at"),
            completion_evidence=common_claim,
            allowed_verifiers=resident_or_manager,
        ),
        CategoryPlaybook(
            playbook_id="playbook.waste.v1",
            category=Category.WASTE,
            label="Waste collection service",
            risk=PlaybookRisk.ROUTINE,
            required_intake_facts=("location", "service_gap", "observed_at"),
            completion_evidence=common_claim,
            allowed_verifiers=resident_or_manager,
        ),
    )
    fallback = CategoryPlaybook(
        playbook_id="playbook.conservative_fallback.v1",
        category=Category.OTHER,
        label="Conservative human-reviewed fallback",
        risk=PlaybookRisk.HIGH,
        required_intake_facts=("problem", "location"),
        completion_evidence=common_claim,
        allowed_verifiers=manager_only,
        verification_timeout_hours=24,
        safety_notes=("Unknown categories never gain authority from this playbook.",),
    )
    return PlaybookCatalog(playbooks, fallback=fallback)
