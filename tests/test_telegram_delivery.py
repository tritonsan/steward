"""Outbound Telegram uses durable worker delivery, independent of live procurement."""

from unittest.mock import patch

import httpx
import pytest
from pydantic import ValidationError
from test_quote_decisions import SECRET
from test_telegram_groups import group_link, provider
from test_telegram_groups import setup as group_setup

from steward.channels.notifications import TelegramNotices
from steward.channels.telegram_groups import TelegramGroups
from steward.config import RuntimeExecutionMode, StewardSettings, TelegramDeliveryMode
from steward.domain.clock import utc_now
from steward.runtime import build_runtime

setup = group_setup


def connect(rt, client):
    with patch.object(TelegramGroups, "call", provider):
        rt.handle_telegram_update(group_link(rt, client), secret_header=SECRET)


def live(rt):
    rt._settings = rt._settings.model_copy(
        update={"telegram_delivery_mode": TelegramDeliveryMode.LIVE}
    )


def queue(client, version=1, headers=None):
    return client.post(
        "/api/telegram/group",
        json={"action": "test_delivery", "expected_version": version},
        headers=headers,
    )


def http_provider(request):
    assert request.url.path.endswith("/sendMessage")
    return httpx.Response(200, json={"ok": True, "result": {"message_id": 4321}})


def test_live_group_test_uses_worker_once_and_leaves_mail_dry(setup):
    rt, _, client = setup
    connect(rt, client)
    live(rt)
    before = rt.store.list_cases()
    response = queue(client)
    assert response.status_code == 200, response.text
    assert queue(client).json() == response.json()
    assert rt.store.outbox_item(response.json()["outbox_id"]).status.value == "pending"
    calls = []

    def deliver(request):
        calls.append(request)
        assert b"Steward connection test" in request.content
        return http_provider(request)

    with httpx.Client(transport=httpx.MockTransport(deliver)) as http:
        assert TelegramNotices(rt, http).dispatch() == 1
        assert TelegramNotices(rt, http).dispatch() == 0
    assert len(calls) == 1
    result = client.get("/api/telegram/group").json()
    assert result["delivery_mode"] == "live"
    assert result["test_delivery"]["status"] == "delivered"
    assert result["test_delivery"]["provider_message_id"] == "4321"
    assert rt.execution_mode is RuntimeExecutionMode.DRY_RUN
    assert rt.store.list_cases() == before
    assert not rt._settings.allow_live_commitments


def test_test_command_requires_manager_current_connection_and_live_mode(setup):
    rt, _, client = setup
    assert queue(client, headers={"Authorization": "Bearer resident"}).status_code == 403
    connect(rt, client)
    assert queue(client).status_code == 422
    live(rt)
    assert queue(client, version=0).status_code == 409
    assert not rt.store.pending_outbox()


def test_simulation_intents_are_not_replayed_when_enabling_telegram(setup):
    rt, _, client = setup
    connect(rt, client)
    notice = TelegramNotices(rt)
    identity = notice.queue(None, "old-demo-event", "-9876", "Simulated update", audience="group")
    live(rt)
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: pytest.fail("must not send"))
    ) as http:
        assert TelegramNotices(rt, http).dispatch() == 1
    assert rt.store.outbox_item(identity).provider_message_id.startswith("dry-run:")


def test_unknown_delivery_is_not_automatically_retried(setup):
    rt, _, client = setup
    connect(rt, client)
    live(rt)
    identity = queue(client).json()["outbox_id"]
    calls = []

    def uncertain(request):
        calls.append(1)
        raise httpx.ReadTimeout("No acknowledgement", request=request)

    with httpx.Client(transport=httpx.MockTransport(uncertain)) as http:
        assert TelegramNotices(rt, http).dispatch() == 0
        assert TelegramNotices(rt, http).dispatch() == 0
    assert len(calls) == 1
    assert rt.store.outbox_item(identity).status.value == "ambiguous"
    assert client.get("/api/telegram/group").json()["test_delivery"]["status"] == "ambiguous"


def test_disconnected_group_suppresses_queued_message(setup):
    rt, _, client = setup
    connect(rt, client)
    live(rt)
    identity = queue(client).json()["outbox_id"]
    client.post("/api/telegram/group", json={"action": "disconnect", "expected_version": 1})
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: pytest.fail("must not send"))
    ) as http:
        TelegramNotices(rt, http).dispatch()
    assert rt.store.outbox_item(identity).status.value == "dead_letter"


def test_emergency_stop_blocks_dispatch_after_intent_is_queued(setup):
    rt, _, client = setup
    connect(rt, client)
    live(rt)
    identity = queue(client).json()["outbox_id"]
    policy = client.get("/api/settings").json()
    response = client.put(
        "/api/settings",
        headers={"Idempotency-Key": "stop-telegram"},
        json={
            "expected_version": policy["version"],
            "policies": policy["policies"],
            "settings": {**policy["settings"], "kill_switch": True},
        },
    )
    assert response.status_code == 200, response.text
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: pytest.fail("must not send"))
    ) as http:
        assert TelegramNotices(rt, http).dispatch() == 0
        assert queue(client).status_code == 422
    assert rt.store.outbox_item(identity).status.value == "pending"


def test_independent_default_and_secret_hydration(tmp_path):
    assert StewardSettings().telegram_delivery_mode is TelegramDeliveryMode.DRY_RUN
    cfg = StewardSettings(
        database_path=tmp_path / "secret.db",
        telegram_delivery_mode="live",
        simulation_clock_start="2026-09-10T10:00:00Z",
        channel_configuration='{"telegram_enabled":true,"telegram_bot_token":"123:test",'
        '"telegram_bot_username":"test_bot","telegram_webhook_secret":"test-webhook-secret"}',
    )
    with build_runtime(cfg) as rt:
        assert rt._settings.telegram_delivery_mode is TelegramDeliveryMode.LIVE
        assert rt.execution_mode is RuntimeExecutionMode.DRY_RUN
        assert rt._settings.channel_configuration is None


def test_live_requires_bot_even_after_secret_hydration(tmp_path):
    with pytest.raises(ValidationError, match="live Telegram delivery"):
        StewardSettings(telegram_delivery_mode="live")
    with pytest.raises(ValidationError, match="live Telegram delivery"):
        build_runtime(
            StewardSettings(
                database_path=tmp_path / "invalid.db",
                telegram_delivery_mode="live",
                channel_configuration="{}",
            )
        )


def test_live_receipt_uses_actual_time_despite_frozen_demo_clock(setup):
    rt, clock, client = setup
    connect(rt, client)
    live(rt)
    from steward.domain.clock import FrozenClock, parse_datetime

    rt._clock = FrozenClock(parse_datetime("2026-09-10T10:00:00Z"))
    identity = queue(client).json()["outbox_id"]
    with httpx.Client(transport=httpx.MockTransport(http_provider)) as http:
        TelegramNotices(rt, http).dispatch()
    receipt = rt.store.outbox_item(identity)
    assert abs((receipt.delivered_at - utc_now()).total_seconds()) < 10


def test_pending_test_survives_runtime_restart_without_duplicate(setup):
    rt, _, client = setup
    connect(rt, client)
    live(rt)
    identity = queue(client).json()["outbox_id"]
    with (
        build_runtime(rt._settings) as restarted,
        httpx.Client(transport=httpx.MockTransport(http_provider)) as http,
    ):
        assert TelegramNotices(restarted, http).dispatch() == 1
        assert restarted.store.outbox_item(identity).provider_message_id == "4321"
        assert TelegramNotices(rt, http).dispatch() == 0


def test_live_mail_does_not_implicitly_enable_telegram(setup):
    rt, _, client = setup
    connect(rt, client)
    rt._settings = rt._settings.model_copy(update={"execution_mode": RuntimeExecutionMode.LIVE_RFQ})
    identity = TelegramNotices(rt).queue(None, "status", "-9876", "Update", audience="group")
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: pytest.fail("must not send"))
    ) as http:
        assert TelegramNotices(rt, http).dispatch() == 1
    assert rt.store.outbox_item(identity).provider_message_id.startswith("dry-run:")
