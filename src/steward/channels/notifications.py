"""Durable, sparse Telegram notices using the same fenced delivery boundary as mail."""

from uuid import uuid4

import httpx

from steward.channels.telegram_groups import current_group, group_ids
from steward.config import TelegramDeliveryMode
from steward.domain.clock import utc_now
from steward.store import OutboxItem, StaleLeaseToken
from steward.store.workflow import stable_id

KIND = "telegram.notice.v1"


class TelegramNotices:
    def __init__(self, runtime, client=None):
        self.runtime, self.store, self.clock = runtime, runtime.store, runtime._clock
        self.client = client

    def queue(self, case, source, chat, text, **metadata):
        identity = stable_id("notice", source, chat)
        with self.store.atomic() as conn:
            if self.store.outbox_item(identity):
                return identity
            group = current_group(self.store)
            if metadata.get("audience") == "group" and group:
                metadata.setdefault("group_version", group.payload["version"])
            self.store._insert_outbox(
                conn,
                (
                    OutboxItem(
                        outbox_id=identity,
                        case_id=case.case_id if case else None,
                        dedup_key=identity,
                        kind=KIND,
                        created_at=self.clock.now(),
                        payload={
                            "chat_id": chat,
                            "text": text,
                            **metadata,
                            "delivery_mode": self.runtime._settings.telegram_delivery_mode.value,
                        },
                    ),
                ),
            )
        return identity

    def queue_group_test(self, actor_id, expected_version):
        """One explicitly requested, fixed-text test per group connection."""
        from steward.channels.private_telegram import member_role

        runtime = self.runtime
        if member_role(runtime, actor_id) != "manager":
            raise PermissionError("Only a community manager can test group delivery")
        if runtime._settings.telegram_delivery_mode is not TelegramDeliveryMode.LIVE:
            raise ValueError("Telegram delivery is simulated; ask your operator to enable it")
        runtime.refresh_policy()
        if runtime._policy.settings.kill_switch:
            raise ValueError("Outbound delivery is paused by the emergency stop")
        with self.store.atomic():
            group = current_group(self.store)
            if not group or group.payload["status"] != "connected":
                raise ValueError("Connect a Telegram group first")
            if group.payload["version"] != expected_version:
                from steward.store import ConcurrencyConflict

                raise ConcurrencyConflict("Group connection changed. Refresh the page.")
            source = f"telegram-group-test:{group.artifact_id}"
            identity = stable_id("notice", source, group.payload["chat_id"])
            self.queue(
                None,
                source,
                group.payload["chat_id"],
                "Steward connection test\n"
                "Group notifications are now connected. Steward can post shared status updates "
                "and verified outcomes here. This is a test message; no case or service order "
                "has been created.",
                audience="group",
                group_version=expected_version,
                connection_test=True,
                requested_by=actor_id,
            )
            return identity

    def collect(self):
        from zoneinfo import ZoneInfo

        from steward.channels.private_telegram import PrivateTelegram, member_role
        from steward.operations.meeting_continuity import active_schedule

        bindings = {
            a.payload["actor_id"]: a.payload["chat_id"]
            for a in PrivateTelegram(self.runtime).bindings()
            if member_role(self.runtime, a.payload["actor_id"])
        }
        managers = self.runtime._bundle.property_profile.management_committee
        for task in self.store.human_tasks():
            if task.status != "open":
                continue
            case = self.store.get_case(task.case_id)
            recipients = task.assigned_actor_ids or tuple(managers)
            for actor in recipients:
                if actor in bindings:
                    command = {
                        "clarification": "/reply",
                        "verify_completion": "/verify",
                        "appointment_access": "/access",
                    }.get(task.kind)
                    text = f"{case.title}\n{task.title}\nDue: {task.due_at.isoformat()}"
                    if command:
                        text += f"\n{command} {task.task_id} " + (
                            "your answer" if command == "/reply" else "yes/no and evidence"
                        )
                    if self.runtime._settings.public_url:
                        text += "\n" + self.runtime._settings.public_url.rstrip("/")
                    self.queue(
                        case,
                        stable_id(task.task_id, task.due_at.isoformat()),
                        bindings[actor],
                        text,
                        actor_id=actor,
                        task_id=task.task_id,
                        task_due_at=task.due_at.isoformat(),
                    )
        now = self.clock.now().astimezone(ZoneInfo(self.runtime._bundle.property_profile.timezone))
        for case in self.store.list_cases():
            schedule = active_schedule(self.store, case.case_id)
            invalidated = self.store.artifacts_for(
                kind="meeting.invalidated.v1", case_id=case.case_id
            )
            if schedule:
                packets = self.store.artifacts_for(kind="meeting_packet.v1", case_id=case.case_id)
                source = (
                    invalidated[-1].artifact_id
                    if invalidated
                    else packets[-1].artifact_id
                    if packets
                    else schedule.artifact_id
                )
                for actor in schedule.payload["policy"]["eligible_participant_ids"]:
                    if actor not in bindings:
                        continue
                    text = (
                        "Meeting changed; see your new invitation in Steward."
                        if invalidated
                        else (
                            "Meeting confirmed: "
                            + packets[-1].payload["schedule"]["selected_slot"]["starts_at"]
                            if packets
                            else "You are invited. Share your availability in Steward."
                        )
                    )
                    self.queue(
                        case,
                        source,
                        bindings[actor],
                        case.title + "\n" + text,
                        actor_id=actor,
                        schedule_id=schedule.artifact_id,
                        invalidation=bool(invalidated),
                    )
            for event in self.store.timeline_for(case.case_id):
                if event.kind.value not in (
                    "scheduled",
                    "verification_confirmed",
                    "closed",
                    "warranty_opened",
                ):
                    continue
                for chat in group_ids(self.store, self.runtime._settings):
                    # Public text contains only the community title and outcome label.
                    self.queue(
                        case,
                        event.event_id,
                        chat,
                        case.title + "\n" + event.kind.value.replace("_", " ").capitalize(),
                        audience="group",
                    )
            if now.hour >= 18:
                for chat in group_ids(self.store, self.runtime._settings):
                    recorded = {
                        event_id
                        for o in self.store.outbox_for_case(case.case_id)
                        if o.kind == KIND and o.payload.get("chat_id") == chat
                        for event_id in o.payload.get("digest_event_ids", [])
                    }
                    cutoff = now.replace(hour=18, minute=0, second=0, microsecond=0)
                    events = [
                        e
                        for e in self.store.timeline_for(case.case_id)
                        if e.at <= cutoff and e.event_id not in recorded
                    ]
                    if events:
                        self.queue(
                            case,
                            "digest:" + now.date().isoformat() + ":" + case.case_id,
                            chat,
                            f"Daily update — {case.title}: {case.status.value.replace('_', ' ')}.",
                            audience="group",
                            digest_event_ids=[e.event_id for e in events],
                        )
            for reminder in self.store.artifacts_for(
                kind="intake.reminder.v1", case_id=case.case_id
            ):
                task = next(
                    (
                        t
                        for t in self.store.human_tasks(case.case_id)
                        if t.task_id == reminder.payload["request_id"] and t.status == "open"
                    ),
                    None,
                )
                if task:
                    for actor in task.assigned_actor_ids:
                        if actor in bindings:
                            self.queue(
                                case,
                                reminder.artifact_id,
                                bindings[actor],
                                "Reminder: " + task.title,
                                actor_id=actor,
                                task_id=task.task_id,
                            )

    def authorized(self, item):
        from steward.channels.private_telegram import PrivateTelegram, member_role
        from steward.operations.meeting_continuity import active_schedule

        actor = item.payload.get("actor_id")
        if actor:
            if not member_role(self.runtime, actor) or not any(
                a.payload == {"actor_id": actor, "chat_id": item.payload["chat_id"]}
                for a in PrivateTelegram(self.runtime).bindings()
            ):
                return False
            if item.payload.get("task_id"):
                task = next(
                    (
                        t
                        for t in self.store.human_tasks(item.case_id)
                        if t.task_id == item.payload["task_id"]
                    ),
                    None,
                )
                return bool(
                    task
                    and task.status == "open"
                    and (
                        not item.payload.get("task_due_at")
                        or item.payload["task_due_at"] == task.due_at.isoformat()
                    )
                    and (
                        actor in task.assigned_actor_ids
                        or (
                            not task.assigned_actor_ids
                            and member_role(self.runtime, actor) == "manager"
                        )
                    )
                )
            if item.payload.get("schedule_id"):
                schedule = active_schedule(self.store, item.case_id)
                invalidated = bool(
                    self.store.artifacts_for(kind="meeting.invalidated.v1", case_id=item.case_id)
                )
                return bool(
                    schedule
                    and schedule.artifact_id == item.payload["schedule_id"]
                    and actor in schedule.payload["policy"]["eligible_participant_ids"]
                    and invalidated == item.payload.get("invalidation", False)
                )
            return False
        group = current_group(self.store)
        return (
            item.payload.get("audience") == "group"
            and item.payload["chat_id"] in group_ids(self.store, self.runtime._settings)
            and (
                not item.payload.get("group_version")
                or (group and item.payload["group_version"] == group.payload["version"])
            )
            and (
                not item.payload.get("requested_by")
                or member_role(self.runtime, item.payload["requested_by"]) == "manager"
            )
        )

    def dispatch(self):
        runtime = self.runtime
        if not runtime.telegram_enabled or not runtime._settings.telegram_bot_token:
            return 0
        live = runtime._settings.telegram_delivery_mode is TelegramDeliveryMode.LIVE
        now = utc_now if live else self.clock.now
        delivered = 0
        self.collect()
        for _ in range(20):
            runtime.refresh_policy()
            if runtime._policy.settings.kill_switch:
                break
            token = uuid4().hex
            jobs = self.store.claim_outbox(token=token, now=now(), limit=1, kind=KIND)
            if not jobs:
                break
            item = jobs[0]
            started = False
            try:
                if not self.authorized(item):
                    raise ValueError("notification chat is no longer authorized")
                if not self.store.begin_outbox_delivery(
                    outbox_id=item.outbox_id, token=token, started_at=now()
                ):
                    continue
                started = True
                # Never replay historical simulated intents when enabling the channel.
                if not live or item.payload.get("delivery_mode") != "live":
                    provider = "dry-run:" + item.outbox_id
                else:
                    # Wall-time HTTP timeout/rate limits are independent of virtual time.
                    with httpx.Client(timeout=15) as own_client:
                        response = (self.client or own_client).post(
                            "https://api.telegram.org/bot"
                            + runtime._settings.telegram_bot_token.get_secret_value()
                            + "/sendMessage",
                            json={
                                "chat_id": item.payload["chat_id"],
                                "text": item.payload["text"],
                                "link_preview_options": {"is_disabled": True},
                            },
                        )
                    response.raise_for_status()
                    payload = response.json()
                    if not payload.get("ok"):
                        raise ValueError("Telegram did not acknowledge delivery")
                    provider = str(payload["result"]["message_id"])
                self.store.complete_outbox_claim(
                    outbox_id=item.outbox_id,
                    token=token,
                    delivered_at=now(),
                    provider_message_id=provider,
                )
                delivered += 1
            except StaleLeaseToken:
                continue
            except Exception as exc:
                self.store.fail_outbox_claim(
                    outbox_id=item.outbox_id,
                    token=token,
                    failed_at=now(),
                    error_code=type(exc).__name__,
                    retryable=False,
                    max_attempts=3,
                    ambiguous=started,
                )
        return delivered
