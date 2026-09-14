from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from steward.agents import IntakeService, TriageAction, TriageResult
from steward.channels import (
    TelegramAuthenticationError,
    TelegramChatNotAllowed,
    TelegramShadowAdapter,
    TelegramShadowDisposition,
    TelegramShadowWorker,
)
from steward.domain.clock import FrozenClock
from steward.domain.enums import Category, Urgency
from steward.policy import PolicyEngine
from steward.seed import load_seed
from steward.store import IdempotencyConflict, SqliteOperationalStore

UTC = timezone.utc
SECRET = "shadow-webhook-secret-2026"
CHAT_ID = "-1002481179934"


class Ids:
    def __init__(self) -> None:
        self.value = 0

    def __call__(self, prefix: str) -> str:
        self.value += 1
        return f"{prefix}-telegram-{self.value:03d}"


class ElevatorClassifier:
    def classify(self, *, message, assets, open_cases):
        del message, assets, open_cases
        return TriageResult(
            action=TriageAction.OPEN_CASE,
            category=Category.ELEVATOR,
            urgency=Urgency.HIGH,
            confidence=0.95,
            title="A Block elevator shudders near floor four",
            asset_id="elevator-a",
            rationale="Resident reports elevator shudder and grinding.",
        )


def telegram_update(*, update_id: int = 9001, chat_id: int = -1002481179934):
    sent = datetime(2026, 9, 10, 9, tzinfo=UTC)
    return {
        "update_id": update_id,
        "message": {
            "message_id": 77,
            "date": int(sent.timestamp()),
            "chat": {
                "id": chat_id,
                "type": "supergroup",
                "title": "Northgate Residents",
            },
            "from": {
                "id": 99887766,
                "is_bot": False,
                "first_name": "Daniel",
                "last_name": "Kaya",
                "username": "private_handle_should_not_persist",
                "language_code": "tr",
            },
            "text": "A Block lift is shuddering again near the fourth floor.",
        },
    }


def adapter(store, clock):
    return TelegramShadowAdapter(
        secret_token=SECRET,
        allowed_chat_ids={CHAT_ID},
        inbox=store,
        clock=clock,
    )


def test_shadow_adapter_authenticates_allowlists_and_stores_no_provider_identity(tmp_path):
    store = SqliteOperationalStore(tmp_path / "telegram.db")
    clock = FrozenClock(datetime(2026, 9, 10, 9, 1, tzinfo=UTC))
    shadow = adapter(store, clock)

    with pytest.raises(TelegramAuthenticationError):
        shadow.handle_update(telegram_update(), secret_header="wrong-secret-value")
    assert store.pending_inbound() == ()
    with pytest.raises(TelegramChatNotAllowed):
        shadow.handle_update(
            telegram_update(update_id=9002, chat_id=-999),
            secret_header=SECRET,
        )
    assert store.pending_inbound() == ()

    result = shadow.handle_update(telegram_update(), secret_header=SECRET)

    assert result.disposition is TelegramShadowDisposition.ACCEPTED
    assert result.outbound_enabled is False
    assert shadow.outbound_enabled is False
    assert result.message is not None
    assert result.message.message_id == "tg:-1002481179934:77"
    assert result.message.sender_display == "Daniel K."
    assert result.message.source == "telegram_shadow"
    normalized = result.message.model_dump()
    assert "username" not in normalized
    assert "user_id" not in normalized
    assert "phone" not in normalized
    assert "private_handle_should_not_persist" not in result.item.model_dump_json()
    assert "99887766" not in result.item.model_dump_json()
    store.close()


def test_shadow_update_is_idempotent_across_restart_and_changed_payload_conflicts(tmp_path):
    path = tmp_path / "restart.db"
    first_clock = FrozenClock(datetime(2026, 9, 10, 9, 1, tzinfo=UTC))
    first_store = SqliteOperationalStore(path)
    first = adapter(first_store, first_clock).handle_update(
        telegram_update(),
        secret_header=SECRET,
    )
    first_store.close()

    second_store = SqliteOperationalStore(path)
    second_clock = FrozenClock(first_clock.now() + timedelta(hours=2))
    duplicate = adapter(second_store, second_clock).handle_update(
        telegram_update(),
        secret_header=SECRET,
    )
    assert duplicate.disposition is TelegramShadowDisposition.DUPLICATE
    assert duplicate.item == first.item
    assert len(second_store.pending_inbound()) == 1

    changed = telegram_update()
    changed["message"]["text"] = "Changed text for the same update id"
    with pytest.raises(IdempotencyConflict):
        adapter(second_store, second_clock).handle_update(changed, secret_header=SECRET)
    assert second_store.inbox_item("telegram_shadow", "9001") == first.item
    second_store.close()


def test_non_text_and_bot_updates_are_recorded_as_ignored_without_messages(tmp_path):
    store = SqliteOperationalStore(tmp_path / "ignored.db")
    clock = FrozenClock(datetime(2026, 9, 10, 9, 1, tzinfo=UTC))
    shadow = adapter(store, clock)
    non_text = telegram_update(update_id=9100)
    non_text["message"].pop("text")
    bot = telegram_update(update_id=9101)
    bot["message"]["from"]["is_bot"] = True

    first = shadow.handle_update(non_text, secret_header=SECRET)
    second = shadow.handle_update(bot, secret_header=SECRET)

    assert first.disposition is TelegramShadowDisposition.IGNORED
    assert second.disposition is TelegramShadowDisposition.IGNORED
    assert first.message is None
    assert second.message is None
    assert store.pending_inbound() == ()
    store.close()


def test_shadow_worker_feeds_existing_intake_once_without_outbound(tmp_path):
    path = tmp_path / "worker.db"
    bundle = load_seed()
    clock = FrozenClock(datetime(2026, 9, 10, 9, 1, tzinfo=UTC))
    store = SqliteOperationalStore(path)
    shadow = adapter(store, clock)
    accepted = shadow.handle_update(telegram_update(update_id=9200), secret_header=SECRET)
    intake = IntakeService(
        classifier=ElevatorClassifier(),
        store=store,
        policy=PolicyEngine(bundle.policies, bundle.settings),
        assets=bundle.assets,
        clock=clock,
        id_factory=Ids(),
        reply_token_factory=lambda: "abcdef123456",
    )
    worker = TelegramShadowWorker(inbox=store, intake=intake)

    drained = worker.drain()

    assert drained.outbound_enabled is False
    assert drained.processed_external_ids == ("9200",)
    assert [outcome.disposition.value for outcome in drained.outcomes] == ["opened"]
    assert len(store.list_cases()) == 1
    assert store.get_message(accepted.message.message_id) is not None
    assert store.pending_inbound() == ()
    assert store.pending_outbox() == ()
    store.close()

    restarted = SqliteOperationalStore(path)
    replay = adapter(restarted, clock).handle_update(
        telegram_update(update_id=9200),
        secret_header=SECRET,
    )
    assert replay.disposition is TelegramShadowDisposition.DUPLICATE
    assert TelegramShadowWorker(
        inbox=restarted,
        intake=IntakeService(
            classifier=ElevatorClassifier(),
            store=restarted,
            policy=PolicyEngine(bundle.policies, bundle.settings),
            assets=bundle.assets,
            clock=clock,
            id_factory=Ids(),
        ),
    ).drain().outcomes == ()
    assert len(restarted.list_cases()) == 1
    restarted.close()
