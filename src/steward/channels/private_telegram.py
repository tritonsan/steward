"""Single-use account linking and explicit, durable personal replies."""

import hashlib
import hmac
import secrets
from datetime import timedelta

from steward.domain.clock import parse_datetime, utc_now
from steward.store import ConcurrencyConflict, WorkflowArtifact
from steward.store.workflow import stable_id


def member_role(runtime, actor):
    if actor in runtime._bundle.property_profile.management_committee:
        return "manager"
    if actor in {r.display_name for r in runtime._bundle.residents}:
        return "resident"
    return None


class PrivateTelegram:
    def __init__(self, runtime):
        self.rt, self.store, self.clock = runtime, runtime.store, runtime._clock

    def issue(self, actor_id):
        if not member_role(self.rt, actor_id):
            raise PermissionError("Registered membership is required")
        token = secrets.token_urlsafe(24)
        digest = hashlib.sha256(token.encode()).hexdigest()
        self.store.put_configuration(
            WorkflowArtifact(
                artifact_id=digest,
                kind="telegram.link-token.v1",
                created_at=self.clock.now(),
                payload={
                    "actor_id": actor_id,
                    "expires_at": (utc_now() + timedelta(minutes=10)).isoformat(),
                },
            )
        )
        return token

    def bindings(self):
        return self.store.artifacts_for(kind="telegram.binding.v1")

    def handle(self, update, secret_header):
        secret = self.rt._settings.telegram_webhook_secret
        if not secret or not hmac.compare_digest(secret_header, secret.get_secret_value()):
            raise PermissionError("Telegram webhook authentication failed")
        body = update.get("message", {})
        chat = body.get("chat", {})
        sender = body.get("from", {})
        if (
            chat.get("type") != "private"
            or sender.get("is_bot")
            or chat.get("id") != sender.get("id")
        ):
            raise PermissionError("Personal replies require a private user chat")
        text = body.get("text", "").strip()
        if len(text) > 5000:
            raise ValueError("Reply is too long")
        key = stable_id("telegram-private", str(update["update_id"]))
        digest = hashlib.sha256(text.encode()).hexdigest()
        with self.store.atomic():
            prior = self.store.artifact("telegram.private-input.v1", key)
            if prior:
                if prior.payload["digest"] != digest or prior.payload["chat_id"] != str(chat["id"]):
                    raise ConcurrencyConflict("Telegram update was reused")
                return {"accepted": True}
            binding = next(
                (a for a in self.bindings() if a.payload["chat_id"] == str(chat["id"])), None
            )
            if text.startswith("/start "):
                token_id = hashlib.sha256(text.split(maxsplit=1)[1].encode()).hexdigest()
                token = self.store.artifact("telegram.link-token.v1", token_id)
                if not token or parse_datetime(token.payload["expires_at"]) <= utc_now():
                    raise ValueError("Link expired. Request a new link in Steward.")
                if self.store.artifact("telegram.link-used.v1", token_id):
                    raise ValueError("Link has already been used")
                actor = token.payload["actor_id"]
                if not member_role(self.rt, actor):
                    raise PermissionError("Membership is no longer active")
                if binding and binding.payload["actor_id"] != actor:
                    raise PermissionError("This Telegram account is linked to another member")
                existing = next(
                    (a for a in self.bindings() if a.payload["actor_id"] == actor), None
                )
                if existing and existing.payload["chat_id"] != str(chat["id"]):
                    raise PermissionError("This member already has a different Telegram binding")
                if not existing:
                    self.store.put_configuration(
                        WorkflowArtifact(
                            artifact_id=stable_id("telegram-binding", actor),
                            kind="telegram.binding.v1",
                            created_at=self.clock.now(),
                            payload={"actor_id": actor, "chat_id": str(chat["id"])},
                        )
                    )
                self.store.put_configuration(
                    WorkflowArtifact(
                        artifact_id=token_id,
                        kind="telegram.link-used.v1",
                        created_at=self.clock.now(),
                        payload={"consumed": True},
                    )
                )
            else:
                if not binding or not member_role(self.rt, binding.payload["actor_id"]):
                    raise PermissionError("Connect your Telegram account from Steward first")
                actor = binding.payload["actor_id"]
                role = member_role(self.rt, actor)
                parts = text.split(maxsplit=2)
                task = None
                if len(parts) == 3 and parts[0] in ("/reply", "/verify", "/access"):
                    task = next(
                        (t for t in self.store.human_tasks() if t.task_id == parts[1]), None
                    )
                    notes = parts[2]
                else:
                    reply_id = str(body.get("reply_to_message", {}).get("message_id", ""))
                    notice = next(
                        (
                            o
                            for c in self.store.list_open_cases()
                            for o in self.store.outbox_for_case(c.case_id)
                            if o.kind == "telegram.notice.v1"
                            and o.provider_message_id == reply_id
                            and o.payload.get("chat_id") == str(chat["id"])
                        ),
                        None,
                    )
                    if notice:
                        task = next(
                            (
                                t
                                for t in self.store.human_tasks()
                                if t.task_id == notice.payload.get("task_id")
                            ),
                            None,
                        )
                    notes = text
                if not task or task.status != "open":
                    raise ValueError("Reply to an open personal question or include its request ID")
                if role != "manager" and actor not in task.assigned_actor_ids:
                    raise PermissionError("This request belongs to another member")
                case = self.store.get_case(task.case_id)
                if task.kind == "clarification":
                    from steward.operations.clarifications import Clarifications

                    Clarifications(self.rt).answer(
                        case,
                        actor_id=actor,
                        role=role,
                        request_id=task.task_id,
                        notes=notes,
                        source=key,
                    )
                elif task.kind in ("verify_completion", "appointment_access"):
                    verdict, _, evidence = notes.partition(" ")
                    if verdict.lower() not in ("yes", "no") or not evidence.strip():
                        raise ValueError("Reply yes or no followed by your evidence")
                    accepted = verdict.lower() == "yes"
                    if task.kind == "verify_completion":
                        self.rt._maintenance.verify(
                            case.case_id,
                            actor_id=actor,
                            accepted=accepted,
                            notes=evidence,
                            expected_version=self.store.case_version(case.case_id),
                            source_id=key,
                        )
                    else:
                        from steward.operations.appointments import Appointments

                        proposals = self.store.artifacts_for(
                            kind="appointment.proposal.v1", case_id=case.case_id
                        )
                        proposal = proposals[-1]
                        if stable_id("appointment-access", proposal.artifact_id) != task.task_id:
                            raise ConcurrencyConflict("A newer appointment needs review")
                        if accepted:
                            Appointments(self.rt).accept(case, proposal)
                        else:
                            self.rt._maintenance.task(
                                case,
                                "appointment_review",
                                "Access was declined",
                                evidence,
                                suffix=key,
                            )
                    self.store.resolve_human_task(
                        task.task_id,
                        actor_id=actor,
                        role=role,
                        expected_version=self.store.case_version(case.case_id),
                        response={"notes": evidence, "accepted": accepted},
                    )
                else:
                    raise ValueError("Use the linked web page for this request")
                self.store.save_transition(
                    case=self.store.get_case(case.case_id),
                    expected_version=self.store.case_version(case.case_id),
                    idempotency_key=key,
                )
            self.store.put_configuration(
                WorkflowArtifact(
                    artifact_id=key,
                    kind="telegram.private-input.v1",
                    created_at=self.clock.now(),
                    payload={"digest": digest, "chat_id": str(chat["id"])},
                )
            )
        return {"accepted": True}


class TestTelegramPoller:
    """Explicit test-bot mode only; cursor advances after durable ingress."""

    def __init__(self, runtime, client=None):
        self.rt, self.client = runtime, client

    def poll(self):
        import httpx

        if not self.rt._settings.telegram_test_polling:
            raise ValueError("Long polling must be explicitly enabled for a test bot")
        rows = self.rt.store.artifacts_for(kind="telegram.poll-cursor.v1")
        offset = max((a.payload["offset"] for a in rows), default=0)
        with httpx.Client(timeout=15) as own:
            response = (self.client or own).get(
                "https://api.telegram.org/bot"
                + self.rt._settings.telegram_bot_token.get_secret_value()
                + "/getUpdates",
                params={"offset": offset, "timeout": 1, "limit": 20},
            )
            response.raise_for_status()
            result = response.json()
        if not result.get("ok"):
            raise ValueError("Telegram polling was not acknowledged")
        for update in result["result"]:
            try:
                self.rt.handle_telegram_update(
                    update,
                    secret_header=self.rt._settings.telegram_webhook_secret.get_secret_value(),
                )
            except (ValueError, PermissionError) as exc:
                from steward.store.memory import StoreInvariantError

                if isinstance(exc, StoreInvariantError):
                    # A concurrent or incomplete write needs provider replay, exactly
                    # like webhook intake. Advancing the cursor would lose the input.
                    raise
                # A rejected external input is durable too; it cannot poison the polling cursor.
                identity = stable_id("telegram-rejected", str(update["update_id"]))
                if not self.rt.store.artifact("telegram.rejected.v1", identity):
                    self.rt.store.put_configuration(
                        WorkflowArtifact(
                            artifact_id=identity,
                            kind="telegram.rejected.v1",
                            created_at=self.rt._clock.now(),
                            payload={
                                "update_id": update["update_id"],
                                "reason_code": type(exc).__name__,
                            },
                        )
                    )
            next_offset = update["update_id"] + 1
            self.rt.store.put_configuration(
                WorkflowArtifact(
                    artifact_id=str(next_offset),
                    kind="telegram.poll-cursor.v1",
                    created_at=self.rt._clock.now(),
                    payload={"offset": next_offset},
                )
            )
        return len(result["result"])
