"""Tests for injectable time."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from steward.domain.clock import (
    FrozenClock,
    InMemoryOffsetStore,
    SystemClock,
    VirtualClock,
)
from steward.domain.enums import Category, CategoryGroup, Urgency
from steward.domain.models import Case

AT = datetime(2026, 3, 14, 9, 0, tzinfo=timezone.utc)


def test_system_clock_returns_timezone_aware_utc():
    now = SystemClock().now()

    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)


def test_frozen_clock_does_not_move_on_its_own():
    clock = FrozenClock(AT)

    assert clock.now() == clock.now() == AT


def test_frozen_clock_rejects_naive_datetimes():
    with pytest.raises(ValueError, match="timezone-aware"):
        FrozenClock(datetime(2026, 3, 14, 9, 0))


def test_virtual_clock_reports_real_time_when_the_offset_is_zero():
    base = FrozenClock(AT)
    clock = VirtualClock(InMemoryOffsetStore(), base=base)

    assert clock.now() == AT
    assert clock.offset == timedelta()


def test_advancing_the_virtual_clock_moves_time_forward():
    clock = VirtualClock(InMemoryOffsetStore(), base=FrozenClock(AT))

    now = clock.advance(timedelta(days=2))

    assert now == AT + timedelta(days=2)
    assert clock.offset == timedelta(days=2)


def test_advances_accumulate():
    clock = VirtualClock(InMemoryOffsetStore(), base=FrozenClock(AT))

    clock.advance(timedelta(hours=30))
    clock.advance(timedelta(hours=18))

    assert clock.now() == AT + timedelta(hours=48)


def test_reset_snaps_demo_time_back_to_real_time():
    clock = VirtualClock(InMemoryOffsetStore(), base=FrozenClock(AT))
    clock.advance(timedelta(days=5))

    assert clock.reset() == AT
    assert clock.offset == timedelta()


def test_clocks_sharing_an_offset_store_agree_about_the_time():
    """The property that makes time travel safe across processes.

    The sweeper, the API handler, and the agent runtime are separate processes.
    If each held its own offset, one could believe it is Monday while another
    believes it is Thursday, and the follow-up logic would be nonsense.
    """
    store = InMemoryOffsetStore()
    base = FrozenClock(AT)
    sweeper_clock = VirtualClock(store, base=base)
    console_clock = VirtualClock(store, base=base)

    sweeper_clock.advance(timedelta(days=3))

    assert console_clock.now() == sweeper_clock.now() == AT + timedelta(days=3)


def test_offset_can_be_rewound_between_demo_takes():
    clock = VirtualClock(InMemoryOffsetStore(), base=FrozenClock(AT))
    clock.advance(timedelta(days=4))

    clock.advance(timedelta(days=-4))

    assert clock.now() == AT


def test_models_reject_naive_timestamps():
    """A single naive datetime in the store breaks the sweeper's comparison."""
    with pytest.raises(ValueError, match="naive datetime"):
        Case(
            case_id="c-1",
            reply_token="deadbeef",
            title="A Block elevator stuck between floors",
            category=Category.ELEVATOR,
            group=CategoryGroup.REPAIR_MAINTENANCE,
            urgency=Urgency.HIGH,
            opened_at=datetime(2026, 3, 14, 9, 0),
            updated_at=AT,
        )


def test_models_normalize_aware_timestamps_to_utc():
    tz = timezone(timedelta(hours=3))
    case = Case(
        case_id="c-1",
        reply_token="deadbeef",
        title="A Block elevator stuck between floors",
        category=Category.ELEVATOR,
        group=CategoryGroup.REPAIR_MAINTENANCE,
        urgency=Urgency.HIGH,
        opened_at=datetime(2026, 3, 14, 12, 0, tzinfo=tz),
        updated_at=AT,
    )

    assert case.opened_at == AT
    assert case.opened_at.utcoffset() == timedelta(0)


def test_urgency_ordering():
    assert Urgency.CRITICAL > Urgency.HIGH > Urgency.NORMAL > Urgency.LOW
    assert Urgency.HIGH >= Urgency.HIGH
