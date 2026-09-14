from datetime import datetime, timedelta, timezone

import pytest

from steward.store import (
    FailureDisposition,
    IdempotencyConflict,
    InferenceClaimDisposition,
    InferenceJobStatus,
    SqliteOperationalStore,
    StaleLeaseToken,
    StoreInvariantError,
)

UTC = timezone.utc


def test_inference_claim_is_fingerprint_bound_and_reuses_completed_result(tmp_path):
    path = tmp_path / "inference-jobs.db"
    first_store = SqliteOperationalStore(path)
    second_store = SqliteOperationalStore(path)
    now = datetime(2026, 9, 8, 8, tzinfo=UTC)
    common = {
        "job_id": "inference-job-001",
        "kind": "meeting_minutes_extraction.v1",
        "case_id": "case-meeting-001",
        "input_sha256": "a" * 64,
    }

    claimed = first_store.claim_inference_job(
        **common,
        token="worker-a",
        now=now,
        lease_seconds=120,
        max_attempts=3,
    )
    assert claimed.disposition is InferenceClaimDisposition.CLAIMED
    assert claimed.lease is not None
    assert claimed.lease.scope == "inference"
    assert claimed.job.status is InferenceJobStatus.PENDING
    assert claimed.job.attempts == 1

    same_owner = first_store.claim_inference_job(
        **common,
        token="worker-a",
        now=now + timedelta(seconds=5),
        lease_seconds=120,
        max_attempts=3,
    )
    assert same_owner.disposition is InferenceClaimDisposition.CLAIMED
    assert same_owner.job.attempts == 1

    busy = second_store.claim_inference_job(
        **common,
        token="worker-b",
        now=now + timedelta(seconds=5),
        lease_seconds=120,
        max_attempts=3,
    )
    assert busy.disposition is InferenceClaimDisposition.BUSY
    assert busy.lease is None

    with pytest.raises(IdempotencyConflict, match="different immutable input"):
        second_store.claim_inference_job(
            **{**common, "input_sha256": "b" * 64},
            token="worker-b",
            now=now + timedelta(seconds=5),
        )

    renewed = first_store.renew_inference_claim(
        job_id=common["job_id"],
        token="worker-a",
        now=now + timedelta(seconds=30),
        lease_seconds=180,
    )
    assert renewed.lease_expires_at == now + timedelta(seconds=210)

    completed = first_store.complete_inference_claim(
        job_id=common["job_id"],
        token="worker-a",
        completed_at=now + timedelta(seconds=40),
        result_payload={"summary": "Durable structured result."},
    )
    assert completed.status is InferenceJobStatus.COMPLETED
    assert completed.result_payload == {"summary": "Durable structured result."}

    recovered = second_store.claim_inference_job(
        **common,
        token="worker-b",
        now=now + timedelta(seconds=41),
    )
    assert recovered.disposition is InferenceClaimDisposition.COMPLETED
    assert recovered.lease is None
    assert recovered.job == second_store.inference_job(common["job_id"])

    with pytest.raises(StaleLeaseToken):
        first_store.complete_inference_claim(
            job_id=common["job_id"],
            token="worker-a",
            completed_at=now + timedelta(seconds=42),
            result_payload={"summary": "A conflicting replay."},
        )

    first_store.close()
    second_store.close()


def test_expired_inference_lease_is_reclaimed_and_stale_worker_cannot_complete(
    tmp_path,
):
    path = tmp_path / "inference-expiry.db"
    first_store = SqliteOperationalStore(path)
    second_store = SqliteOperationalStore(path)
    now = datetime(2026, 9, 8, 9, tzinfo=UTC)
    common = {
        "job_id": "inference-job-expiry",
        "kind": "meeting_minutes_extraction.v1",
        "case_id": "case-meeting-expiry",
        "input_sha256": "c" * 64,
    }

    first = first_store.claim_inference_job(
        **common,
        token="crashed-worker",
        now=now,
        lease_seconds=60,
        max_attempts=3,
    )
    assert first.disposition is InferenceClaimDisposition.CLAIMED

    reclaimed = second_store.claim_inference_job(
        **common,
        token="recovery-worker",
        now=now + timedelta(seconds=61),
        lease_seconds=60,
        max_attempts=3,
    )
    assert reclaimed.disposition is InferenceClaimDisposition.CLAIMED
    assert reclaimed.job.attempts == 2

    with pytest.raises(StaleLeaseToken):
        first_store.complete_inference_claim(
            job_id=common["job_id"],
            token="crashed-worker",
            completed_at=now + timedelta(seconds=62),
            result_payload={"worker": "stale"},
        )

    completed = second_store.complete_inference_claim(
        job_id=common["job_id"],
        token="recovery-worker",
        completed_at=now + timedelta(seconds=62),
        result_payload={"worker": "recovered"},
    )
    assert completed.status is InferenceJobStatus.COMPLETED
    assert completed.attempts == 2
    assert completed.result_payload == {"worker": "recovered"}

    first_store.close()
    second_store.close()


def test_retryable_inference_failure_becomes_terminal_at_attempt_limit(tmp_path):
    store = SqliteOperationalStore(tmp_path / "inference-failure.db")
    now = datetime(2026, 9, 8, 10, tzinfo=UTC)
    common = {
        "job_id": "inference-job-failure",
        "kind": "meeting_minutes_extraction.v1",
        "case_id": "case-meeting-failure",
        "input_sha256": "d" * 64,
    }

    first = store.claim_inference_job(
        **common,
        token="worker-one",
        now=now,
        max_attempts=2,
    )
    assert first.disposition is InferenceClaimDisposition.CLAIMED
    assert (
        store.fail_inference_claim(
            job_id=common["job_id"],
            token="worker-one",
            failed_at=now + timedelta(seconds=1),
            error_code="temporary_model_error",
            retryable=True,
            max_attempts=2,
        )
        is FailureDisposition.RETRY_PENDING
    )
    pending = store.inference_job(common["job_id"])
    assert pending is not None
    assert pending.status is InferenceJobStatus.PENDING
    assert pending.last_error_code == "temporary_model_error"

    second = store.claim_inference_job(
        **common,
        token="worker-two",
        now=now + timedelta(seconds=2),
        max_attempts=2,
    )
    assert second.disposition is InferenceClaimDisposition.CLAIMED
    assert second.job.attempts == 2
    assert (
        store.fail_inference_claim(
            job_id=common["job_id"],
            token="worker-two",
            failed_at=now + timedelta(seconds=3),
            error_code="temporary_model_error",
            retryable=True,
            max_attempts=2,
        )
        is FailureDisposition.DEAD_LETTERED
    )

    terminal = store.claim_inference_job(
        **common,
        token="worker-three",
        now=now + timedelta(seconds=4),
        max_attempts=2,
    )
    assert terminal.disposition is InferenceClaimDisposition.FAILED
    assert terminal.job.status is InferenceJobStatus.FAILED
    assert terminal.job.failed_at == now + timedelta(seconds=3)
    assert terminal.job.result_payload is None
    store.close()



def test_inference_state_updates_reject_clock_rollback(tmp_path):
    store = SqliteOperationalStore(tmp_path / "inference-clock-rollback.db")
    now = datetime(2026, 9, 8, 11, tzinfo=UTC)
    earlier = now - timedelta(seconds=1)
    common = {
        "job_id": "inference-job-clock-rollback",
        "kind": "meeting_minutes_extraction.v1",
        "case_id": "case-meeting-clock-rollback",
        "input_sha256": "e" * 64,
    }
    claim = store.claim_inference_job(
        **common,
        token="worker-one",
        now=now,
        lease_seconds=60,
        max_attempts=3,
    )
    assert claim.disposition is InferenceClaimDisposition.CLAIMED

    with pytest.raises(StoreInvariantError, match="clock moved backwards"):
        store.claim_inference_job(
            **common,
            token="worker-one",
            now=earlier,
            lease_seconds=60,
            max_attempts=3,
        )
    with pytest.raises(StoreInvariantError, match="clock moved backwards"):
        store.renew_inference_claim(
            job_id=common["job_id"],
            token="worker-one",
            now=earlier,
            lease_seconds=60,
        )
    with pytest.raises(StoreInvariantError, match="clock moved backwards"):
        store.complete_inference_claim(
            job_id=common["job_id"],
            token="worker-one",
            completed_at=earlier,
            result_payload={"summary": "Must not persist."},
        )
    with pytest.raises(StoreInvariantError, match="clock moved backwards"):
        store.fail_inference_claim(
            job_id=common["job_id"],
            token="worker-one",
            failed_at=earlier,
            error_code="temporary_model_error",
            retryable=True,
            max_attempts=3,
        )

    unchanged = store.inference_job(common["job_id"])
    assert unchanged is not None
    assert unchanged.status is InferenceJobStatus.PENDING
    assert unchanged.attempts == 1
    assert unchanged.updated_at == now
    assert unchanged.last_error_code is None
    store.close()
