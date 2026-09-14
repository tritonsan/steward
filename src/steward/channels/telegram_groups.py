"""Manager-authorized Telegram group onboarding for one community workspace."""

import hashlib
import hmac
import secrets
from datetime import timedelta

import httpx

from steward.channels.private_telegram import PrivateTelegram, member_role
from steward.domain.clock import parse_datetime, utc_now
from steward.store import ConcurrencyConflict, WorkflowArtifact
from steward.store.workflow import stable_id


def current_group(store):
    rows = store.artifacts_for(kind="telegram.group.v1")
    return max(rows, key=lambda a: a.payload["version"]) if rows else None


def group_ids(store, settings):
    current = current_group(store)
    if current:
        return (
            frozenset({current.payload["chat_id"]})
            if current.payload["status"] == "connected"
            else frozenset()
        )
    return settings.telegram_allowed_chat_ids


class TelegramGroups:
    def __init__(self, runtime):
        self.rt, self.store = runtime, runtime.store

    def ready(self):
        cfg = self.rt._settings
        return bool(
            self.rt.telegram_enabled and cfg.telegram_bot_token and cfg.telegram_bot_username
        )

    def call(self, method, **params):
        if not self.ready():
            raise ValueError("The application operator must configure the Telegram bot first")
        token = self.rt._settings.telegram_bot_token.get_secret_value()
        try:
            response = httpx.post(
                f"https://api.telegram.org/bot{token}/{method}", json=params, timeout=10
            )
        except httpx.HTTPError:
            raise RuntimeError("Telegram is unavailable; retry the connection") from None
        if response.status_code >= 500 or response.status_code == 429:
            raise RuntimeError("Telegram is temporarily unavailable; retry shortly")
        result = response.json()
        if not result.get("ok"):
            raise ValueError(
                "Telegram could not verify access. "
                "Add the bot as a group administrator and try again."
            )
        return result["result"]

    def issue(self, actor_id, expected_version):
        if member_role(self.rt, actor_id) != "manager":
            raise PermissionError("Only a community manager can connect a group")
        if not self.ready():
            raise ValueError("The application operator must configure the Telegram bot first")
        binding = next(
            (a for a in PrivateTelegram(self.rt).bindings() if a.payload["actor_id"] == actor_id),
            None,
        )
        if not binding:
            raise ValueError("Connect your personal Telegram account first")
        with self.store.atomic():
            current = current_group(self.store)
            version = current.payload["version"] if current else 0
            if version != expected_version:
                raise ConcurrencyConflict("Group connection changed. Refresh the page.")
            if current and current.payload["status"] == "connected":
                raise ValueError("Disconnect the current group before choosing another")
            token = "group_" + secrets.token_urlsafe(24)
            digest = hashlib.sha256(token.encode()).hexdigest()
            self.store.put_configuration(
                WorkflowArtifact(
                    artifact_id=digest,
                    kind="telegram.group-token.v1",
                    created_at=utc_now(),
                    payload={
                        "actor_id": actor_id,
                        "telegram_user_id": binding.payload["chat_id"],
                        "version": version,
                        "expires_at": (utc_now() + timedelta(minutes=10)).isoformat(),
                    },
                )
            )
        return {
            "url": f"https://t.me/{self.rt._settings.telegram_bot_username}?startgroup={token}",
            "expires_in_seconds": 600,
        }

    def handle(self, update, secret_header):
        body = update.get("message", {})
        parts = body.get("text", "").strip().split()
        membership = update.get("my_chat_member")
        connection = (
            len(parts) == 2 and parts[0].split("@")[0] == "/start" and parts[1].startswith("group_")
        )
        if not membership and not connection:
            return None
        configured = self.rt._settings.telegram_webhook_secret
        if not configured or not hmac.compare_digest(
            secret_header or "", configured.get_secret_value()
        ):
            raise PermissionError("Telegram webhook authentication failed")
        if membership:
            with self.store.atomic():
                current = current_group(self.store)
                if (
                    current
                    and str(membership["chat"]["id"]) == current.payload["chat_id"]
                    and current.payload["status"] == "connected"
                    and membership["new_chat_member"]["status"] != "administrator"
                ):
                    self._save(
                        current,
                        "disconnected",
                        "Bot group permissions were removed",
                        "telegram",
                    )
            return {"accepted": True}
        digest = hashlib.sha256(parts[1].encode()).hexdigest()
        token = self.store.artifact("telegram.group-token.v1", digest)
        chat, sender = body.get("chat", {}), body.get("from", {})
        if (
            not token
            or str(sender.get("id")) != token.payload["telegram_user_id"]
            or sender.get("is_bot")
            or body.get("sender_chat")
        ):
            raise PermissionError("Use the group link from your own linked manager account")
        actor = token.payload["actor_id"]
        if member_role(self.rt, actor) != "manager":
            raise PermissionError("Management membership is no longer active")
        if chat.get("type") not in ("group", "supergroup"):
            raise ValueError("Choose a Telegram group")
        used = self.store.artifact("telegram.group-used.v1", digest)
        if used:
            if used.payload["chat_id"] != str(chat["id"]):
                raise ConcurrencyConflict("This group link has already been used")
            return {"accepted": True}
        if parse_datetime(token.payload["expires_at"]) <= utc_now():
            raise ValueError("Group link expired. Create a new link in Steward.")
        try:
            bot = self.call("getMe")
            if bot.get("username", "").lower() != self.rt._settings.telegram_bot_username.lower():
                raise ValueError("Configured bot username does not match its token")
            bot_member = self.call("getChatMember", chat_id=chat["id"], user_id=bot["id"])
            if bot_member["status"] != "administrator":
                raise ValueError(
                    "Make Steward a group administrator, then open the group link again"
                )
            person = self.call("getChatMember", chat_id=chat["id"], user_id=sender["id"])
            if person["status"] not in ("creator", "administrator"):
                raise ValueError("Your linked Telegram account must manage the selected group")
        except ValueError as exc:
            self.store.put_configuration(
                WorkflowArtifact(
                    artifact_id=stable_id("group-attempt", str(update["update_id"])),
                    kind="telegram.group-attempt.v1",
                    created_at=utc_now(),
                    payload={"actor_id": actor, "reason": str(exc)},
                )
            )
            return {"accepted": False, "reason": str(exc)}
        # Provider reads are outside the transaction. Version/expiry are checked again here.
        with self.store.atomic():
            current = current_group(self.store)
            version = current.payload["version"] if current else 0
            used = self.store.artifact("telegram.group-used.v1", digest)
            if used:
                if used.payload["chat_id"] != str(chat["id"]):
                    raise ConcurrencyConflict("This group link has already been used")
                return {"accepted": True}
            if (
                version != token.payload["version"]
                or parse_datetime(token.payload["expires_at"]) <= utc_now()
            ):
                raise ConcurrencyConflict("Connection changed or link expired. Request a new link.")
            self.store.put_configuration(
                WorkflowArtifact(
                    artifact_id=stable_id("telegram-group", str(version + 1)),
                    kind="telegram.group.v1",
                    created_at=utc_now(),
                    payload={
                        "version": version + 1,
                        "chat_id": str(chat["id"]),
                        "title": chat.get("title", "Community group"),
                        "status": "connected",
                        "bot_id": bot["id"],
                        "bot_status": bot_member["status"],
                        "manager_status": person["status"],
                        "actor_id": actor,
                        "reason": "Telegram group and manager permissions verified",
                    },
                )
            )
            self.store.put_configuration(
                WorkflowArtifact(
                    artifact_id=digest,
                    kind="telegram.group-used.v1",
                    created_at=utc_now(),
                    payload={"chat_id": str(chat["id"])},
                )
            )
        return {"accepted": True}

    def _save(self, current, status, reason, actor_id):
        version = current.payload["version"] + 1
        self.store.put_configuration(
            WorkflowArtifact(
                artifact_id=stable_id("telegram-group", str(version)),
                kind="telegram.group.v1",
                created_at=utc_now(),
                payload={
                    **current.payload,
                    "version": version,
                    "status": status,
                    "reason": reason,
                    "actor_id": actor_id,
                },
            )
        )

    def disconnect(self, actor_id, expected_version):
        if member_role(self.rt, actor_id) != "manager":
            raise PermissionError("Only a community manager can disconnect a group")
        with self.store.atomic():
            current = current_group(self.store)
            if not current or current.payload["version"] != expected_version:
                raise ConcurrencyConflict("Group connection changed. Refresh the page.")
            if current.payload["status"] != "connected":
                return
            self._save(current, "disconnected", "Disconnected by management", actor_id)

    def status(self, actor_id):
        current = current_group(self.store)
        self.rt.refresh_policy()
        test = None
        if current:
            identity = stable_id(
                "notice", f"telegram-group-test:{current.artifact_id}", current.payload["chat_id"]
            )
            item = self.store.outbox_item(identity)
            if item:
                test = {
                    "status": (
                        "simulated"
                        if (item.provider_message_id or "").startswith("dry-run:")
                        else item.status.value
                    ),
                    "delivered_at": item.delivered_at,
                    "provider_message_id": item.provider_message_id,
                    "error": item.last_error_code,
                }
        attempts = [
            a
            for a in self.store.artifacts_for(kind="telegram.group-attempt.v1")
            if a.payload["actor_id"] == actor_id
        ]
        messages = []
        if current:
            receipts = [
                a
                for a in self.store.artifacts_for(kind="telegram.group-receipt.v1")
                if a.payload["chat_id"] == current.payload["chat_id"]
            ]
            for receipt in receipts[-5:]:
                item = self.store.inbox_item("telegram_shadow", receipt.payload["external_id"])
                if not item or not item.message:
                    continue
                cases = [
                    c.case_id
                    for c in self.store.list_cases()
                    if item.message.message_id in c.source_message_ids
                ]
                messages.append(
                    {
                        "received_at": receipt.created_at,
                        "status": item.status.value,
                        "text": item.message.text,
                        "case_ids": cases,
                        "error": item.last_error_code,
                    }
                )
        return {
            "configured": self.ready(),
            "delivery_mode": self.rt._settings.telegram_delivery_mode.value,
            "delivery_paused": self.rt._policy.settings.kill_switch,
            "test_delivery": test,
            "bot_username": self.rt._settings.telegram_bot_username,
            "personal_linked": any(
                a.payload["actor_id"] == actor_id for a in PrivateTelegram(self.rt).bindings()
            ),
            "version": current.payload["version"] if current else 0,
            "connection": current.payload if current else None,
            "recent_messages": messages,
            "last_issue": attempts[-1].payload["reason"]
            if attempts and (not current or attempts[-1].created_at > current.created_at)
            else None,
        }
