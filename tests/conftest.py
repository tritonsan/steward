"""Shared fixtures and builders for the test suite."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from steward.domain.enums import ApprovalMode, Category
from steward.domain.models import ApprovalPolicy, GlobalSettings

FIXED_NOW = datetime(2026, 3, 14, 9, 0, tzinfo=timezone.utc)


def make_policy(
    category: Category = Category.ELEVATOR,
    mode: ApprovalMode = ApprovalMode.ALWAYS_APPROVE,
    per_incident_cap: Decimal | None = Decimal("1500.00"),
    monthly_cap: Decimal | None = Decimal("4000.00"),
    currency: str = "USD",
    allowed_vendor_ids: list[str] | None = None,
) -> ApprovalPolicy:
    """Build an approval policy that is permissive unless a test narrows it."""
    return ApprovalPolicy(
        category=category,
        mode=mode,
        per_incident_cap=per_incident_cap,
        monthly_cap=monthly_cap,
        currency=currency,
        allowed_vendor_ids=(
            ["acme-elevator"] if allowed_vendor_ids is None else allowed_vendor_ids
        ),
        updated_at=FIXED_NOW,
        updated_by="selim",
    )


@pytest.fixture
def settings() -> GlobalSettings:
    return GlobalSettings(min_triage_confidence=0.7)


@pytest.fixture
def elevator_policy() -> ApprovalPolicy:
    return make_policy()
