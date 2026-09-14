"""Injectable time.

Steward's central claim is about what happens when nothing happens. A vendor
promises Tuesday, Tuesday passes, and the thing that normally occurs is that
everyone forgets. Demonstrating that Steward does not forget requires letting
two days pass, which is not a thing a five minute video can wait for.

So nothing in this system calls `datetime.now()` directly. Everything reads a
`Clock`, and in demo mode that clock is a `VirtualClock` whose offset can be
pushed forward on command. The deadlines, the sweeper, and the timestamps
written to memory all move together, because they are all reading the same
offset from the same shared store. Compressing time this way changes how long
the demo takes and changes nothing about what the system does.

The offset lives in a store rather than in a process, deliberately. The API
handler, the sweeper, and the agent runtime are separate processes; an offset
held in a module global would let them disagree about what time it is, and a
sweeper that thinks it is Monday while the console thinks it is Thursday is
worse than no time travel at all.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from typing import Protocol, runtime_checkable

__all__ = [
    "Clock",
    "ClockOffsetStore",
    "FrozenClock",
    "InMemoryOffsetStore",
    "SystemClock",
    "VirtualClock",
    "utc_now",
]


def utc_now() -> datetime:
    """Wall clock time, timezone-aware UTC.

    Prefer injecting a `Clock`. This exists for the few places that legitimately
    need real time regardless of demo state, such as stamping the moment an
    inbound email physically arrived.
    """
    return datetime.now(timezone.utc)


@runtime_checkable
class Clock(Protocol):
    """A source of the current time. Always returns timezone-aware UTC."""

    def now(self) -> datetime:  # pragma: no cover - protocol definition
        ...


@runtime_checkable
class ClockOffsetStore(Protocol):
    """Shared storage for how far demo time has been pushed ahead of real time.

    `advance` is a single method rather than a get/set pair so that backends
    able to increment atomically can do so. Two concurrent advances that each
    read, add, and write would silently lose one of the jumps.
    """

    def get_offset(self) -> timedelta:  # pragma: no cover - protocol definition
        ...

    def advance(self, delta: timedelta) -> timedelta:  # pragma: no cover
        """Add `delta` to the stored offset and return the new total."""
        ...

    def reset(self) -> None:  # pragma: no cover - protocol definition
        """Return demo time to real time."""
        ...


class SystemClock:
    """Real time. The only clock used in production mode."""

    __slots__ = ()

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def __repr__(self) -> str:
        return "SystemClock()"


class FrozenClock:
    """A clock that does not move unless told to. For tests.

    Deterministic in a way `VirtualClock` is not: it has no wall clock
    component at all, so a test asserting on exact timestamps cannot flake.
    """

    __slots__ = ("_at",)

    def __init__(self, at: datetime) -> None:
        self._at = _require_aware(at)

    def now(self) -> datetime:
        return self._at

    def set(self, at: datetime) -> None:
        self._at = _require_aware(at)

    def advance(self, delta: timedelta) -> datetime:
        self._at = self._at + delta
        return self._at

    def __repr__(self) -> str:
        return f"FrozenClock({self._at.isoformat()})"


class InMemoryOffsetStore:
    """Offset store backed by a process-local variable.

    Correct for tests and for a single-process local run. Not correct across
    Lambda invocations, which is what the DynamoDB-backed store in the
    persistence layer is for.
    """

    __slots__ = ("_offset", "_lock")

    def __init__(self, offset: timedelta = timedelta()) -> None:
        self._offset = offset
        self._lock = threading.Lock()

    def get_offset(self) -> timedelta:
        with self._lock:
            return self._offset

    def advance(self, delta: timedelta) -> timedelta:
        with self._lock:
            self._offset = self._offset + delta
            return self._offset

    def reset(self) -> None:
        with self._lock:
            self._offset = timedelta()

    def __repr__(self) -> str:
        return f"InMemoryOffsetStore(offset={self._offset!r})"


class VirtualClock:
    """Real time plus a shared, externally adjustable offset.

    Used in demo mode. Time still advances on its own at the normal rate; the
    offset only decides how far ahead of reality the whole system currently
    believes it is.
    """

    __slots__ = ("_store", "_base")

    def __init__(self, store: ClockOffsetStore, base: Clock | None = None) -> None:
        self._store = store
        self._base = base if base is not None else SystemClock()

    def now(self) -> datetime:
        return self._base.now() + self._store.get_offset()

    def advance(self, delta: timedelta) -> datetime:
        """Push demo time forward and return the new current time.

        Negative deltas are permitted so a demo can be rewound between takes.
        """
        self._store.advance(delta)
        return self.now()

    def reset(self) -> datetime:
        """Snap demo time back to real time."""
        self._store.reset()
        return self.now()

    @property
    def offset(self) -> timedelta:
        return self._store.get_offset()

    def __repr__(self) -> str:
        return f"VirtualClock(offset={self.offset!r})"


def _require_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Clock times must be timezone-aware")
    return value.astimezone(timezone.utc)


def parse_datetime(value: str) -> datetime:
    """Accept ISO timestamps emitted by JSON serializers on Python 3.10 too."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
