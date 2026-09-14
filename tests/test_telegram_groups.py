from datetime import timedelta
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from test_quote_decisions import SECRET, _ElevatorClassifier, _SelectingRecommender, _update

from steward.api import create_app
from steward.channels import TelegramChatNotAllowed
from steward.channels.telegram_groups import TelegramGroups, current_group, group_ids
from steward.config import StewardSettings
from steward.domain.clock import FrozenClock, utc_now
from steward.runtime import build_runtime


@pytest.fixture
def setup(tmp_path):
    clock = FrozenClock(utc_now())
    cfg = StewardSettings(
        database_path=tmp_path / "group.db",
        telegram_enabled=True,
        telegram_webhook_secret=SECRET,
        telegram_bot_token="123:test-only",
        telegram_bot_username="steward_test_bot",
    )
    rt = build_runtime(
        cfg,
        clock=clock,
        classifier=_ElevatorClassifier(),
        decision_recommender=_SelectingRecommender(),
    )
    client = TestClient(
        create_app(
            rt,
            simulation=True,
            tokens={
                "manager": {"actor_id": "Simon O.", "role": "manager"},
                "resident": {"actor_id": "James D.", "role": "resident"},
            },
        )
    )
    client.headers["Authorization"] = "Bearer manager"
    yield rt, clock, client
    client.close()
    rt.close()


def link_account(rt, client):
    reply = client.post("/api/telegram/link")
    assert reply.status_code == 200, reply.text
    token = parse_qs(urlparse(reply.json()["url"]).query)["start"][0]
    rt.handle_telegram_update(
        {
            "update_id": 1,
            "message": {
                "chat": {"id": 700, "type": "private"},
                "from": {"id": 700},
                "text": "/start " + token,
            },
        },
        secret_header=SECRET,
    )


def test_personal_link_expires_with_wall_clock_when_simulation_is_frozen(setup):
    rt, clock, client = setup
    now = utc_now()
    with patch("steward.channels.private_telegram.utc_now", return_value=now):
        response = client.post("/api/telegram/link")
    token = parse_qs(urlparse(response.json()["url"]).query)["start"][0]
    with (
        patch(
            "steward.channels.private_telegram.utc_now", return_value=now + timedelta(minutes=11)
        ),
        pytest.raises(ValueError, match="expired"),
    ):
        rt.handle_telegram_update(
            {
                "update_id": 999,
                "message": {
                    "chat": {"id": 700, "type": "private"},
                    "from": {"id": 700},
                    "text": "/start " + token,
                },
            },
            secret_header=SECRET,
        )
    assert not rt.store.artifacts_for(kind="telegram.binding.v1")


def group_link(rt, client):
    link_account(rt, client)
    response = client.post("/api/telegram/group", json={"action": "connect", "expected_version": 0})
    assert response.status_code == 200, response.text
    token = parse_qs(urlparse(response.json()["url"]).query)["startgroup"][0]
    return {
        "update_id": 2,
        "message": {
            "from": {"id": 700},
            "chat": {"id": -9876, "type": "supergroup", "title": "My test group"},
            "text": "/start@steward_test_bot " + token,
        },
    }


def provider(self, method, **params):
    if method == "getMe":
        return {"id": 123, "username": "steward_test_bot"}
    return {"status": "administrator"}


def test_manager_connects_group_and_message_reaches_worker(setup):
    rt, clock, client = setup
    update = group_link(rt, client)
    with patch.object(TelegramGroups, "call", provider):
        assert rt.handle_telegram_update(update, secret_header=SECRET)["accepted"]
        assert rt.handle_telegram_update(update, secret_header=SECRET)["accepted"]
    assert group_ids(rt.store, rt._settings) == frozenset({"-9876"})
    message = _update(clock.now())
    message["message"]["chat"]["id"] = -9876
    message["message"]["from"]["id"] = 700
    rt.handle_telegram_update(message, secret_header=SECRET)
    rt.handle_telegram_update(message, secret_header=SECRET)
    status = client.get("/api/telegram/group").json()
    assert status["recent_messages"][0]["status"] == "pending"
    assert not rt.tick().workflow_failures
    status = client.get("/api/telegram/group").json()
    assert len(status["recent_messages"]) == 1
    assert status["recent_messages"][0]["case_ids"]
    assert status["recent_messages"][0]["status"] == "processed"
    assert rt.store.list_cases()[0].source_message_ids


def test_real_message_clock_is_independent_of_frozen_workflow_clock(setup):
    rt, clock, client = setup
    with patch.object(TelegramGroups, "call", provider):
        rt.handle_telegram_update(group_link(rt, client), secret_header=SECRET)
    rt._telegram._clock = FrozenClock(utc_now() - timedelta(hours=3))
    message = _update(utc_now())
    message["message"]["chat"]["id"] = -9876
    message["message"]["from"]["id"] = 700
    received = rt.handle_telegram_update(message, secret_header=SECRET)
    assert received.message.sent_at > received.message.ingested_at
    message["update_id"] += 1
    message["message"]["date"] += 3600
    with pytest.raises(ValueError, match="future"):
        rt.handle_telegram_update(message, secret_header=SECRET)


def test_webhook_records_terminal_rejection_without_blocking_group_connection(setup):
    rt, clock, client = setup
    link_account(rt, client)
    message = _update(clock.now())
    message["message"]["from"]["id"] = 700
    headers = {"X-Telegram-Bot-Api-Secret-Token": SECRET}
    for _ in range(2):
        response = client.post("/api/channels/telegram", json=message, headers=headers)
        assert response.status_code == 200
        assert response.json()["recorded"] and not response.json()["accepted"]
    rows = rt.store.artifacts_for(kind="telegram.webhook-rejected.v1")
    assert len(rows) == 1 and rows[0].payload["update"] == message
    assert not rt.store.list_cases()
    response = client.post("/api/channels/telegram", json=message)
    assert response.status_code == 403
    assert len(rt.store.artifacts_for(kind="telegram.webhook-rejected.v1")) == 1


def test_webhook_does_not_acknowledge_rejection_if_storage_fails(setup):
    rt, clock, client = setup
    link_account(rt, client)
    message = _update(clock.now())
    message["message"]["from"]["id"] = 700
    with (
        patch.object(type(rt.store), "put_configuration", side_effect=RuntimeError("storage down")),
        pytest.raises(RuntimeError, match="storage down"),
    ):
        client.post(
            "/api/channels/telegram",
            json=message,
            headers={"X-Telegram-Bot-Api-Secret-Token": SECRET},
        )


def test_webhook_preserves_retryable_store_conflict(setup):
    from steward.store import ConcurrencyConflict

    rt, clock, client = setup
    message = _update(clock.now())
    with patch.object(type(rt), "handle_telegram_update", side_effect=ConcurrencyConflict("retry")):
        response = client.post(
            "/api/channels/telegram",
            json=message,
            headers={"X-Telegram-Bot-Api-Secret-Token": SECRET},
        )
    assert response.status_code == 409
    assert not rt.store.artifacts_for(kind="telegram.webhook-rejected.v1")


@pytest.mark.parametrize("failure", ["conflict", "invariant", "storage"])
def test_poller_preserves_cursor_until_retryable_failure_is_recovered(setup, failure):
    from unittest.mock import Mock

    import httpx

    from steward.channels.private_telegram import TestTelegramPoller
    from steward.store import ConcurrencyConflict
    from steward.store.memory import StoreInvariantError

    rt, clock, _ = setup
    rt._settings = rt._settings.model_copy(update={"telegram_test_polling": True})
    message = _update(clock.now())
    client = Mock()
    client.get.return_value = httpx.Response(
        200, json={"ok": True, "result": [message]}, request=httpx.Request("GET", "https://test")
    )
    error = {
        "conflict": ConcurrencyConflict,
        "invariant": StoreInvariantError,
        "storage": RuntimeError,
    }[failure]
    poller = TestTelegramPoller(rt, client)
    with (
        patch.object(type(rt), "handle_telegram_update", side_effect=error("retry")),
        pytest.raises(error, match="retry"),
    ):
        poller.poll()
    assert not rt.store.artifacts_for(kind="telegram.poll-cursor.v1")
    assert not rt.store.artifacts_for(kind="telegram.rejected.v1")
    with patch.object(type(rt), "handle_telegram_update", return_value={"accepted": True}):
        assert poller.poll() == 1
    assert rt.store.artifacts_for(kind="telegram.poll-cursor.v1")[0].payload["offset"] == (
        message["update_id"] + 1
    )


def test_poller_acknowledges_durable_terminal_rejection(setup):
    from unittest.mock import Mock

    import httpx

    from steward.channels.private_telegram import TestTelegramPoller

    rt, clock, _ = setup
    rt._settings = rt._settings.model_copy(update={"telegram_test_polling": True})
    message = _update(clock.now())
    client = Mock()
    client.get.return_value = httpx.Response(
        200, json={"ok": True, "result": [message]}, request=httpx.Request("GET", "https://test")
    )
    with patch.object(type(rt), "handle_telegram_update", side_effect=PermissionError("unlinked")):
        assert TestTelegramPoller(rt, client).poll() == 1
    assert len(rt.store.artifacts_for(kind="telegram.rejected.v1")) == 1
    assert rt.store.artifacts_for(kind="telegram.poll-cursor.v1")[0].payload["offset"] == (
        message["update_id"] + 1
    )


@pytest.mark.parametrize(
    "fault", ["other_sender", "not_admin", "bot_not_admin", "expired", "bad_secret"]
)
def test_group_connection_requires_identity_permissions_and_fresh_token(setup, fault):
    rt, _, client = setup
    update = group_link(rt, client)
    if fault == "other_sender":
        update["message"]["from"]["id"] = 701

    def answer(self, method, **params):
        if method == "getChatMember" and (
            (fault == "not_admin" and params["user_id"] == 700)
            or (fault == "bot_not_admin" and params["user_id"] == 123)
        ):
            return {"status": "member"}
        return provider(self, method, **params)

    with (
        patch.object(TelegramGroups, "call", answer),
        patch(
            "steward.channels.telegram_groups.utc_now",
            return_value=utc_now() + timedelta(minutes=11 if fault == "expired" else 0),
        ),
    ):
        if fault in ("other_sender", "expired", "bad_secret"):
            with pytest.raises((PermissionError, ValueError)):
                rt.handle_telegram_update(
                    update, secret_header="bad" if fault == "bad_secret" else SECRET
                )
        else:
            assert not rt.handle_telegram_update(update, secret_header=SECRET)["accepted"]
            assert client.get("/api/telegram/group").json()["last_issue"]
    assert current_group(rt.store) is None


def test_disconnect_and_permission_removal_block_messages_and_group_notices(setup):
    from steward.channels.notifications import TelegramNotices
    from steward.store import OutboxItem, OutboxStatus

    rt, clock, client = setup
    update = group_link(rt, client)
    with patch.object(TelegramGroups, "call", provider):
        rt.handle_telegram_update(update, secret_header=SECRET)
    notice = OutboxItem(
        outbox_id="test",
        dedup_key="test",
        kind="telegram.notice.v1",
        status=OutboxStatus.PENDING,
        created_at=clock.now(),
        payload={"audience": "group", "chat_id": "-9876"},
    )
    assert TelegramNotices(rt).authorized(notice)
    response = client.post(
        "/api/telegram/group", json={"action": "disconnect", "expected_version": 1}
    )
    assert response.status_code == 200
    assert not TelegramNotices(rt).authorized(notice)
    message = _update(clock.now())
    message["message"]["chat"]["id"] = -9876
    message["message"]["from"]["id"] = 700
    with pytest.raises(TelegramChatNotAllowed):
        rt.handle_telegram_update(message, secret_header=SECRET)
    assert (
        client.post(
            "/api/telegram/group", json={"action": "connect", "expected_version": 1}
        ).status_code
        == 409
    )
    # A used connection token cannot reconnect a disconnected group.
    with patch.object(TelegramGroups, "call", provider):
        rt.handle_telegram_update(update, secret_header=SECRET)
    assert not group_ids(rt.store, rt._settings)


def test_resident_cannot_configure_group_and_unlinked_manager_gets_next_step(setup):
    _, _, client = setup
    response = client.post("/api/telegram/group", json={"action": "connect", "expected_version": 0})
    assert response.status_code == 422
    assert "personal" in response.text
    assert (
        client.get("/api/telegram/group", headers={"Authorization": "Bearer resident"}).status_code
        == 403
    )
    assert (
        client.post(
            "/api/telegram/group",
            headers={"Authorization": "Bearer resident"},
            json={"action": "connect", "expected_version": 0},
        ).status_code
        == 403
    )


def test_bot_permission_removal_disables_group_without_restart(setup):
    rt, _, client = setup
    update = group_link(rt, client)
    with patch.object(TelegramGroups, "call", provider):
        rt.handle_telegram_update(update, secret_header=SECRET)
    rt.handle_telegram_update(
        {
            "update_id": 22,
            "my_chat_member": {"chat": {"id": -9876}, "new_chat_member": {"status": "left"}},
        },
        secret_header=SECRET,
    )
    assert not group_ids(rt.store, rt._settings)
    assert client.get("/api/telegram/group").json()["connection"]["status"] == "disconnected"


def test_connection_persists_for_a_second_runtime(setup):
    rt, _, client = setup
    update = group_link(rt, client)
    with patch.object(TelegramGroups, "call", provider):
        rt.handle_telegram_update(update, secret_header=SECRET)
    with build_runtime(rt._settings, classifier=_ElevatorClassifier()) as second:
        assert group_ids(second.store, second._settings) == frozenset({"-9876"})
        assert TelegramGroups(second).status("Simon O.")["personal_linked"]


def test_connection_and_used_token_rollback_together(setup):
    rt, _, client = setup
    update = group_link(rt, client)
    original = type(rt.store).put_configuration

    def fail_used(store, artifact):
        if artifact.kind == "telegram.group-used.v1":
            raise RuntimeError("interrupted connection")
        return original(store, artifact)

    with (
        patch.object(TelegramGroups, "call", provider),
        patch.object(type(rt.store), "put_configuration", fail_used),
        pytest.raises(RuntimeError),
    ):
        rt.handle_telegram_update(update, secret_header=SECRET)
    assert current_group(rt.store) is None
    with patch.object(TelegramGroups, "call", provider):
        assert rt.handle_telegram_update(update, secret_header=SECRET)["accepted"]
