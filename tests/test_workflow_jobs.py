from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest
from test_runtime import CHAT_ID, SECRET, _ElevatorClassifier, _update

from steward.config import StewardSettings
from steward.domain.clock import FrozenClock
from steward.procurement import RfqOutboxCoordinator
from steward.runtime import build_runtime
from steward.store import SqliteOperationalStore, StaleLeaseToken
from steward.store.workflow import BudgetReservation, JobStatus


def opened(tmp_path):
    clock = FrozenClock(datetime(2026, 9, 7, 10, tzinfo=timezone.utc))
    cfg = StewardSettings(
        database_path=tmp_path / "workflow.db",
        telegram_enabled=True,
        telegram_webhook_secret=SECRET,
        telegram_allowed_chat_ids=frozenset({CHAT_ID}),
    )
    runtime = build_runtime(cfg, classifier=_ElevatorClassifier(), clock=clock)
    runtime.handle_telegram_update(_update(clock.now()), secret_header=SECRET)
    return runtime, cfg, clock


def test_rfq_failure_after_intake_recovers_after_restart(tmp_path):
    runtime, cfg, clock = opened(tmp_path)
    with patch.object(RfqOutboxCoordinator, "queue", side_effect=RuntimeError("cut")):
        first = runtime.tick()
    assert len(first.workflow_failures) == 1
    assert runtime.store.pending_inbound() == ()
    assert len(runtime.store.list_open_cases()) == 1
    runtime.close()
    clock.advance(timedelta(seconds=21))
    with build_runtime(cfg, classifier=_ElevatorClassifier(), clock=clock) as restarted:
        report = restarted.tick()
        assert len(report.queued_rfqs) == 1
        assert len(report.outbound.deliveries) == 3
        assert restarted.tick().queued_rfqs == ()
        assert restarted.store.workflow_jobs()[0].status == JobStatus.COMPLETED


def test_restart_after_rfq_persistence_does_not_repeat_mail(tmp_path):
    runtime, cfg, clock = opened(tmp_path)
    with patch.object(SqliteOperationalStore, "complete_job", side_effect=RuntimeError("cut")):
        runtime.tick()
    assert len(runtime.transport.sent) == 3
    runtime.close()
    clock.advance(timedelta(seconds=21))
    with build_runtime(cfg, classifier=_ElevatorClassifier(), clock=clock) as restarted:
        report = restarted.tick()
        assert not report.outbound.deliveries
        assert (
            len(restarted.store.outbox_for_case(restarted.store.list_open_cases()[0].case_id)) == 3
        )


def test_claim_fencing_and_exhaustion_creates_human_task(tmp_path):
    runtime, cfg, clock = opened(tmp_path)
    runtime._runner.tick()
    a = runtime.store.claim_jobs(now=clock.now(), token="a", lease_seconds=5)[0]
    other = SqliteOperationalStore(cfg.database_path)
    assert not other.claim_jobs(now=clock.now(), token="b")
    clock.advance(timedelta(seconds=6))
    b = other.claim_jobs(now=clock.now(), token="b", lease_seconds=5)[0]
    with pytest.raises(StaleLeaseToken):
        runtime.store.complete_job(a.job_id, token="a", now=clock.now())
    other.fail_job(b.job_id, token="b", now=clock.now(), error_code="error", max_attempts=2)
    assert other.workflow_jobs()[0].status == JobStatus.FAILED
    assert other.human_tasks()[0].kind == "workflow_failure"
    other.close()
    runtime.close()


def test_concurrent_reservations_cannot_overspend(tmp_path):
    runtime, cfg, clock = opened(tmp_path)
    runtime.tick()
    update = _update(clock.now())
    update["update_id"] += 1
    update["message"]["message_id"] += 1
    runtime.handle_telegram_update(update, secret_header=SECRET)
    runtime.tick()
    cases = runtime.store.list_open_cases()
    assert len(cases) == 2
    versions = {c.case_id: runtime.store.case_version(c.case_id) for c in cases}

    def reserve(case):
        store = SqliteOperationalStore(cfg.database_path)
        try:
            return store.reserve_budget(
                BudgetReservation(
                    reservation_id=case.case_id,
                    case_id=case.case_id,
                    quote_id="quote",
                    category="elevator",
                    currency="USD",
                    amount=Decimal("700"),
                    created_at=clock.now(),
                    policy_version="v1",
                ),
                expected_version=versions[case.case_id],
                monthly_cap=Decimal("1000"),
                incident_cap=Decimal("800"),
            )
        except ValueError:
            return False
        finally:
            store.close()

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(reserve, cases))
    assert sorted(results) == [False, True]
    runtime.close()
