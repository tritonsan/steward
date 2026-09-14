"""Telegram inbound-only shadow adapter with no network or outbound capability."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Literal, Protocol

from steward.agents import IntakeOutcome, IntakeService
from steward.domain.clock import Clock
from steward.domain.models import ResidentMessage
from steward.store import InboxItem, InboxStatus

__all__ = [
    "TelegramAuthenticationError",
    "TelegramChatNotAllowed",
    "TelegramShadowAdapter",
    "TelegramShadowDisposition",
    "TelegramShadowError",
    "TelegramShadowResult",
    "TelegramShadowWorker",
    "TelegramUpdateError",
]

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class TelegramShadowError(ValueError):
    """Base class for a refused shadow update."""


class TelegramAuthenticationError(TelegramShadowError):
    """Webhook secret did not match."""


class TelegramChatNotAllowed(TelegramShadowError):
    """Update came from a chat outside the explicit allowlist."""


class TelegramUpdateError(TelegramShadowError):
    """Telegram payload did not match the accepted text-message shape."""


class TelegramShadowDisposition(str, Enum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    IGNORED = "ignored"


class ShadowInbox(Protocol):
    def record_inbound(self, item: InboxItem) -> bool: ...

    def inbox_item(self, source: str, external_id: str) -> InboxItem | None: ...

    def pending_inbound(self, *, limit: int = 100) -> tuple[InboxItem, ...]: ...

    def mark_inbound_processed(self, source: str, external_id: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class TelegramShadowResult:
    disposition: TelegramShadowDisposition
    item: InboxItem
    reason: str
    outbound_enabled: Literal[False] = False

    @property
    def message(self) -> ResidentMessage | None:
        return self.item.message


@dataclass(frozen=True, slots=True)
class TelegramDrainResult:
    outcomes: tuple[IntakeOutcome, ...]
    processed_external_ids: tuple[str, ...]
    outbound_enabled: Literal[False] = False


class TelegramShadowAdapter:
    """Normalize a Telegram webhook update and persist it for later intake.

    The adapter has deliberately no HTTP client, bot token, or send method.
    A future Lambda/API route can pass the decoded JSON and secret header into
    ``handle_update`` without changing the privacy or idempotency boundary.
    """

    __slots__ = (
        "_allowed_chat_ids",
        "_chat_ids_provider",
        "_clock",
        "_inbox",
        "_max_clock_skew",
        "_max_text_chars",
        "_secret",
        "_validation_clock",
    )

    def __init__(
        self,
        *,
        secret_token: str,
        allowed_chat_ids: frozenset[str] | set[str],
        inbox: ShadowInbox,
        clock: Clock,
        max_text_chars: int = 4096,
        max_future_clock_skew_seconds: int = 300,
        chat_ids_provider=None,
        validation_clock: Clock | None = None,
    ) -> None:
        if len(secret_token) < 16:
            raise ValueError("Telegram webhook secret must contain at least 16 characters")
        cleaned_chats = frozenset(
            str(value).strip() for value in allowed_chat_ids if str(value).strip()
        )
        if not cleaned_chats and chat_ids_provider is None:
            raise ValueError("Telegram shadow mode needs at least one allowed chat id")
        if max_text_chars <= 0:
            raise ValueError("Telegram max text length must be positive")
        if max_future_clock_skew_seconds < 0:
            raise ValueError("Telegram future clock skew cannot be negative")
        self._secret = secret_token
        self._allowed_chat_ids = cleaned_chats
        self._chat_ids_provider = chat_ids_provider
        self._inbox = inbox
        self._clock = clock
        self._validation_clock = validation_clock or clock
        self._max_text_chars = max_text_chars
        self._max_clock_skew = timedelta(seconds=max_future_clock_skew_seconds)

    @property
    def outbound_enabled(self) -> Literal[False]:
        return False

    def handle_update(
        self,
        update: Mapping[str, Any],
        *,
        secret_header: str,
    ) -> TelegramShadowResult:
        if not hmac.compare_digest(secret_header or "", self._secret):
            raise TelegramAuthenticationError("Telegram webhook secret was rejected")
        if not isinstance(update, Mapping):
            raise TelegramUpdateError("Telegram update must be a JSON object")
        payload_hash = _payload_hash(update)
        update_id = _required_int(update.get("update_id"), "update_id")
        if update_id < 0:
            raise TelegramUpdateError("Telegram update_id must not be negative")
        external_id = str(update_id)

        body = update.get("message")
        if not isinstance(body, Mapping):
            return self._record_ignored(
                external_id=external_id,
                payload_hash=payload_hash,
                reason="unsupported Telegram update type",
            )
        chat = body.get("chat")
        if not isinstance(chat, Mapping):
            raise TelegramUpdateError("Telegram message has no chat object")
        chat_id = str(_required_int(chat.get("id"), "chat.id"))
        allowed = self._chat_ids_provider() if self._chat_ids_provider else self._allowed_chat_ids
        if chat_id not in allowed:
            raise TelegramChatNotAllowed("Telegram chat is outside the shadow allowlist")

        sender = body.get("from")
        if isinstance(sender, Mapping) and sender.get("is_bot") is True:
            return self._record_ignored(
                external_id=external_id,
                payload_hash=payload_hash,
                reason="bot-authored Telegram messages are ignored",
            )
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            return self._record_ignored(
                external_id=external_id,
                payload_hash=payload_hash,
                reason="Telegram update contains no text message",
            )
        cleaned_text = _clean_text(text)
        if len(cleaned_text) > self._max_text_chars:
            raise TelegramUpdateError("Telegram text exceeds the configured maximum")

        message_id = _required_int(body.get("message_id"), "message.message_id")
        unix_time = _required_int(body.get("date"), "message.date")
        try:
            sent_at = datetime.fromtimestamp(unix_time, tz=timezone.utc)
        except (OSError, OverflowError, ValueError) as exc:
            raise TelegramUpdateError("Telegram message date is invalid") from exc
        ingested_at = self._clock.now()
        if sent_at > self._validation_clock.now() + self._max_clock_skew:
            raise TelegramUpdateError("Telegram message date is implausibly far in the future")
        display_name = _display_name(sender)
        message = ResidentMessage(
            message_id=f"tg:{chat_id}:{message_id}",
            source="telegram_shadow",
            chat_id=chat_id,
            sender_display=display_name,
            text=cleaned_text,
            sent_at=sent_at,
            ingested_at=ingested_at,
        )
        item = InboxItem(
            source="telegram_shadow",
            external_id=external_id,
            payload_hash=payload_hash,
            received_at=ingested_at,
            status=InboxStatus.PENDING,
            message=message,
        )
        return self._record(item, accepted_reason="normalized text stored for shadow intake")

    def _record_ignored(
        self,
        *,
        external_id: str,
        payload_hash: str,
        reason: str,
    ) -> TelegramShadowResult:
        item = InboxItem(
            source="telegram_shadow",
            external_id=external_id,
            payload_hash=payload_hash,
            received_at=self._clock.now(),
            status=InboxStatus.IGNORED,
            message=None,
        )
        return self._record(item, accepted_reason=reason)

    def _record(self, item: InboxItem, *, accepted_reason: str) -> TelegramShadowResult:
        recorded = self._inbox.record_inbound(item)
        if recorded:
            disposition = (
                TelegramShadowDisposition.ACCEPTED
                if item.status is InboxStatus.PENDING
                else TelegramShadowDisposition.IGNORED
            )
            return TelegramShadowResult(
                disposition=disposition,
                item=item,
                reason=accepted_reason,
            )
        existing = self._inbox.inbox_item(item.source, item.external_id)
        if existing is None:
            raise RuntimeError("duplicate Telegram update disappeared from the inbox")
        return TelegramShadowResult(
            disposition=TelegramShadowDisposition.DUPLICATE,
            item=existing,
            reason="Telegram update was already recorded",
        )


class TelegramShadowWorker:
    """Feed durable shadow messages into IntakeService without outbound I/O."""

    __slots__ = ("_inbox", "_intake")

    def __init__(self, *, inbox: ShadowInbox, intake: IntakeService) -> None:
        self._inbox = inbox
        self._intake = intake

    @property
    def outbound_enabled(self) -> Literal[False]:
        return False

    def drain(self, *, limit: int = 100) -> TelegramDrainResult:
        outcomes: list[IntakeOutcome] = []
        processed: list[str] = []
        for item in self._inbox.pending_inbound(limit=limit):
            if item.message is None:
                raise RuntimeError("pending Telegram inbox item has no normalized message")
            outcome = self._intake.process(item.message)
            self._inbox.mark_inbound_processed(item.source, item.external_id)
            outcomes.append(outcome)
            processed.append(item.external_id)
        return TelegramDrainResult(
            outcomes=tuple(outcomes),
            processed_external_ids=tuple(processed),
        )


def _required_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TelegramUpdateError(f"Telegram {field} must be an integer")
    return value


def _payload_hash(update: Mapping[str, Any]) -> str:
    try:
        canonical = json.dumps(
            update,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise TelegramUpdateError("Telegram update is not valid JSON data") from exc
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _display_name(sender: Any) -> str:
    if not isinstance(sender, Mapping):
        return "Telegram resident"
    first = _clean_display_part(sender.get("first_name")) or "Telegram resident"
    last = _clean_display_part(sender.get("last_name"))
    if not last:
        return first[:120]
    return f"{first} {last[0]}."[:120]


def _clean_display_part(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(_CONTROL_CHARS.sub("", value).split())


def _clean_text(value: str) -> str:
    return _CONTROL_CHARS.sub("", value).strip()
