"""Judging credentials, storage and dispatch cannot cross the owner boundary."""

import json
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from test_runtime import _ElevatorClassifier

from steward.api import create_app
from steward.config import RuntimeExecutionMode, StewardSettings
from steward.mail import RecordingMailTransport
from steward.review import (
    PREPARATION_ID,
    PREPARATION_KIND,
    ReviewAccess,
    ReviewSessionRequest,
    assert_review_boundary,
    build_review_runtime,
    review_ready,
    tick_review,
)
from steward.runtime import build_runtime
from steward.store import WorkflowArtifact
from steward.store.postgres import PostgresOperationalStore

CODE = "test-only-judging-code-with-at-least-32-characters"


def base_settings(tmp_path):
    return StewardSettings(
        database_path=tmp_path / "owner.db", database_url=None, database_secret=None
    )


def mark_review_ready(runtime):
    runtime.store.put_configuration(
        WorkflowArtifact(
            artifact_id=PREPARATION_ID,
            kind=PREPARATION_KIND,
            created_at=runtime._clock.now(),
            payload={"fixture": True},
        )
    )


def test_review_credentials_cannot_access_owner_and_owner_cannot_access_review(
    tmp_path, monkeypatch
):
    settings = base_settings(tmp_path)
    with build_runtime(settings) as owner, build_review_runtime(settings) as review:
        mark_review_ready(review)
        # An environment token/registry still authenticates only the owner application.
        monkeypatch.setenv(
            "STEWARD_API_TOKENS",
            json.dumps({"owner-token": {"actor_id": "Simon O.", "role": "manager"}}),
        )
        monkeypatch.setenv("STEWARD_MEMBERS", json.dumps({"__review_access_code": CODE}))
        monkeypatch.setenv("STEWARD_COGNITO_DOMAIN", "owner.example")
        with TestClient(
            create_app(owner, review_runtime=review, review_access_code=CODE, review_enabled=True)
        ) as client:
            assert client.get("/api/review/config").json() == {"enabled": True}
            response = client.post("/api/review/session", json={"access_code": CODE})
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            manager = response.json()
            headers = {"Authorization": "Bearer " + manager["token"]}
            assert manager["role"] == "manager"
            assert client.get("/review/api/me", headers=headers).json()["actor_id"] == "Simon O."
            assert client.get("/api/me", headers=headers).status_code == 401
            owner_headers = {"Authorization": "Bearer owner-token"}
            assert client.get("/api/me", headers=owner_headers).status_code == 200
            assert client.get("/review/api/me", headers=owner_headers).status_code == 401
            assert client.get("/review/api/auth/config").json() == {
                "domain": None,
                "client_id": None,
            }
            assert client.get("/review/api/cases").status_code == 401
            resident = client.post(
                "/api/review/session", json={"access_code": CODE, "role": "resident"}
            ).json()
            resident_headers = {"Authorization": "Bearer " + resident["token"]}
            assert (
                client.get("/review/api/resident/overview", headers=resident_headers).status_code
                == 200
            )
            assert client.get("/review/api/settings", headers=resident_headers).status_code == 403
            assert client.get("/api/me", headers=resident_headers).status_code == 401
            for action in ("connect", "disconnect", "test_delivery"):
                result = client.post(
                    "/review/api/telegram/group",
                    headers=headers,
                    json={"action": action, "expected_version": 0},
                )
                assert result.status_code == 403
            assert client.post("/review/api/telegram/link", headers=headers).status_code == 403


def test_review_queue_clock_and_restart_are_separate_from_owner(tmp_path):
    settings = base_settings(tmp_path)
    with (
        build_runtime(settings) as owner,
        build_review_runtime(settings, classifier=_ElevatorClassifier()) as review,
    ):
        assert review.store.list_cases() == ()  # No implicit demo bootstrap.
        mark_review_ready(review)
        owner.store.put_configuration(
            WorkflowArtifact(
                artifact_id="owner-private",
                kind="test.private",
                created_at=owner._clock.now(),
                payload={"private": "owner only"},
            )
        )
        with TestClient(
            create_app(owner, review_runtime=review, review_access_code=CODE, review_enabled=True)
        ) as client:
            token = client.post("/api/review/session", json={"access_code": CODE}).json()["token"]
            headers = {"Authorization": "Bearer " + token, "Idempotency-Key": "review-message-1"}
            result = client.post(
                "/review/api/simulation/messages",
                headers=headers,
                json={
                    "sender": "Daniel K.",
                    "text": "The A Block elevator shudders near the fourth floor. "
                    "Please inspect it.",
                },
            )
            assert result.status_code == 200
            tick = tick_review(review)
            assert tick.inbound.failures == ()
            assert len(review.store.list_cases()) == 1
            assert owner.store.list_cases() == ()
            assert review.store.artifacts_for(kind="test.private") == ()
            assert len(review.transport.sent) == 3
            assert not tick.outbound.real_delivery_enabled
            case_id = review.store.list_cases()[0].case_id
            assert client.get(f"/api/cases/{case_id}", headers=headers).status_code == 401
            review._clock.advance(timedelta(hours=2))
            advanced = review._clock.now()
    with build_review_runtime(settings, classifier=_ElevatorClassifier()) as resumed:
        assert resumed.store.get_case(case_id).case_id == case_id
        assert resumed._clock.now() == advanced
        assert resumed.store.artifacts_for(kind="test.private") == ()
        assert tick_review(resumed).outbound.deliveries == ()


def test_live_owner_configuration_cannot_enable_review_transports(tmp_path, monkeypatch):
    channels = {
        "telegram_enabled": True,
        "telegram_delivery_mode": "live",
        "telegram_bot_token": "not-a-real-token",
        "telegram_bot_username": "test_bot",
        "telegram_webhook_secret": "owner-webhook",
        "telegram_allowed_chat_ids": "-1234",
    }
    settings = base_settings(tmp_path).model_copy(
        update={
            "execution_mode": RuntimeExecutionMode.LIVE_COMMITMENT,
            "allow_live_commitments": True,
            "channel_configuration": SecretStr(json.dumps(channels)),
            "ses_inbound_enabled": True,
            "ses_inbound_bucket": "owner-mail-bucket",
            "ses_queue_url": "https://example.invalid/owner-mail-queue",
            "ses_configuration_set_name": "owner-events",
        }
    )

    def unexpected(*args, **kwargs):
        pytest.fail("review attempted to construct a real mail transport or Telegram request")

    monkeypatch.setattr("steward.runtime.SesMailTransport", unexpected)
    monkeypatch.setattr("steward.runtime.SesInboundReader", unexpected)
    monkeypatch.setattr("httpx.post", unexpected)
    with build_review_runtime(settings, classifier=_ElevatorClassifier()) as review:
        assert_review_boundary(review)
        assert isinstance(review.transport, RecordingMailTransport)
        assert not review.telegram_enabled
        assert not review.inbound_mail_enabled
        assert review._settings.channel_configuration is None
        assert review._settings.telegram_bot_token is None
        assert not review._settings.ses_queue_url
        assert review._settings.telegram_delivery_mode.value == "dry_run"
        assert review.execution_mode.value == "dry_run"
        assert review.store.records() == ()
        # A later unsafe settings replacement also fails before any worker tick.
        review._settings = review._settings.model_copy(update={"telegram_enabled": True})
        with pytest.raises(ValidationError, match="review runtime"):
            tick_review(review)


def test_review_login_rejects_bad_code_and_role_and_limits_failed_attempts(tmp_path):
    access = ReviewAccess(CODE)
    with pytest.raises(ValueError, match="24 characters"):
        ReviewAccess("weak")
    for _ in range(12):
        with pytest.raises(HTTPException) as error:
            access.session(ReviewSessionRequest(access_code="wrong"), "test-client")
        assert error.value.status_code == 401
    with pytest.raises(HTTPException) as error:
        access.session(ReviewSessionRequest(access_code="wrong"), "test-client")
    assert error.value.status_code == 429
    assert (
        access.session(ReviewSessionRequest(access_code=CODE), "test-client")["role"] == "manager"
    )
    assert (
        access.session(ReviewSessionRequest(access_code=CODE, role="resident"), "other")["role"]
        == "resident"
    )
    with pytest.raises(ValidationError):
        ReviewSessionRequest(access_code=CODE, role="owner")
    with build_runtime(base_settings(tmp_path)) as runtime:
        client = TestClient(create_app(runtime, review_enabled=False))
        assert client.get("/api/review/config").json() == {"enabled": False}
        assert client.post("/api/review/session", json={"access_code": CODE}).status_code == 404


@pytest.mark.parametrize(
    "schema", ["public", "pg_catalog", "pg_review", "bad; DROP SCHEMA public", "x.y"]
)
def test_schema_configuration_rejects_unsafe_names(schema):
    with pytest.raises(ValidationError, match="non-system SQL identifier"):
        StewardSettings(database_schema=schema)


def test_review_preparation_blocks_login_and_ignores_owner_seed_override(tmp_path, monkeypatch):
    settings = base_settings(tmp_path)
    monkeypatch.setenv("STEWARD_SEED_DIR", str(tmp_path / "not-the-bundled-seed"))
    # Review ignores both settings and process seed overrides.
    with build_review_runtime(
        settings.model_copy(update={"seed_dir": tmp_path / "missing"})
    ) as review:
        assert len(review._bundle.history) == 63
        assert not review_ready(review)
        # Owner is constructed with the actual bundled path for this standalone test.
        with (
            build_runtime(
                settings.model_copy(update={"seed_dir": review._settings.seed_dir})
            ) as owner,
            TestClient(
                create_app(
                    owner, review_runtime=review, review_access_code=CODE, review_enabled=True
                )
            ) as client,
        ):
            result = client.post("/api/review/session", json={"access_code": CODE})
            assert result.status_code == 503
            assert result.headers["cache-control"] == "no-store"
            mark_review_ready(review)
            assert review_ready(review)
            assert client.post("/api/review/session", json={"access_code": CODE}).status_code == 200


def test_postgres_review_namespace_persists_and_never_falls_back_to_owner():
    dsn = os.getenv("STEWARD_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("STEWARD_TEST_POSTGRES_DSN required")
    import psycopg
    from psycopg import sql

    schema = "test_review_" + uuid4().hex
    try:
        with closing(PostgresOperationalStore(dsn, schema=schema)) as review:
            artifact = WorkflowArtifact(
                artifact_id="isolated",
                kind="review.test",
                created_at=datetime.now(timezone.utc),
                payload={},
            )
            review.put_configuration(artifact)
            row = review._one("SELECT current_schema() AS name")
            assert row["name"] == schema
        with closing(PostgresOperationalStore(dsn, schema=schema)) as resumed:
            assert resumed.artifact("review.test", "isolated") is not None
            assert "public" not in resumed._one("SHOW search_path")["search_path"]
    finally:
        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
            )


def test_postgres_simultaneous_review_start_and_owner_id_collision():
    dsn = os.getenv("STEWARD_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("STEWARD_TEST_POSTGRES_DSN required")
    import psycopg
    from psycopg import sql

    review_schema, owner_schema = "test_review_" + uuid4().hex, "test_owner_" + uuid4().hex
    stores = []
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            # Mirrors simultaneous API/worker first startup against a nonexistent schema.
            stores = list(
                pool.map(lambda _: PostgresOperationalStore(dsn, schema=review_schema), range(2))
            )
        owner = PostgresOperationalStore(dsn, schema=owner_schema)
        stores.append(owner)
        for store, value in ((owner, "owner"), (stores[0], "review")):
            store.put_configuration(
                WorkflowArtifact(
                    artifact_id="same-logical-id",
                    kind="namespace.test",
                    created_at=datetime.now(timezone.utc),
                    payload={"workspace": value},
                )
            )
        assert owner.artifact("namespace.test", "same-logical-id").payload == {"workspace": "owner"}
        assert stores[1].artifact("namespace.test", "same-logical-id").payload == {
            "workspace": "review"
        }
    finally:
        for store in stores:
            store.close()
        with psycopg.connect(dsn, autocommit=True) as connection:
            for schema in (review_schema, owner_schema):
                connection.execute(
                    sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
                )
