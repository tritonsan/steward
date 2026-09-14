"""Authenticated management API. No model calls occur on read endpoints."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from steward.domain.clock import parse_datetime
from steward.domain.enums import TERMINAL_STATUSES, CaseStatus, Category
from steward.domain.models import ApprovalPolicy, GlobalSettings, ResidentMessage
from steward.runtime import build_runtime
from steward.store import ConcurrencyConflict, IdempotencyConflict, InboxItem, WorkflowArtifact
from steward.store.workflow import stable_id


class Principal(BaseModel):
    actor_id: str
    role: str


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    action: str
    notes: str = Field(default="", max_length=5000)
    accepted: bool = True
    data: dict[str, Any] = Field(default_factory=dict)


class TelegramGroupCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str
    expected_version: int = Field(ge=0)


class SimulationMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sender: str
    text: str = Field(min_length=1, max_length=4000)


class SuggestionDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    accepted: bool
    notes: str = Field(min_length=1, max_length=4000)


class Availability(BaseModel):
    model_config = ConfigDict(extra="forbid")
    available_slot_ids: list[str] = Field(max_length=12)
    expected_version: int = Field(ge=1)
    schedule_id: str | None = None


class SimulationVendorReply(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_id: str
    vendor_id: str
    text: str = Field(min_length=1, max_length=10000)


class SettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=0)
    settings: GlobalSettings
    policies: dict[Category, ApprovalPolicy]


def create_app(runtime=None, *, tokens: dict[str, dict] | None = None, simulation=None):
    if simulation is None:
        simulation = os.getenv("STEWARD_SIMULATION") == "true"
    owned = runtime is None
    runtime = runtime or build_runtime()
    configured_tokens = (
        tokens if tokens is not None else json.loads(os.getenv("STEWARD_API_TOKENS", "{}"))
    )
    members = json.loads(os.getenv("STEWARD_MEMBERS", "{}"))
    issuer = os.getenv("STEWARD_COGNITO_ISSUER")
    client_id = os.getenv("STEWARD_COGNITO_CLIENT_ID")
    jwks = None
    if issuer and client_id:
        from jwt import PyJWKClient

        jwks = PyJWKClient(issuer.rstrip("/") + "/.well-known/jwks.json")

    @asynccontextmanager
    async def lifespan(_app):
        yield
        if owned:
            runtime.close()

    app = FastAPI(title="Steward", version="0.2.0", lifespan=lifespan)
    app.state.runtime = runtime

    def principal(authorization: str = Header(default="")):
        token = authorization.removeprefix("Bearer ")
        for expected, actor in configured_tokens.items():
            if hmac.compare_digest(token, expected):
                return Principal.model_validate(actor)
        if jwks:
            import jwt

            try:
                claims = jwt.decode(
                    token,
                    jwks.get_signing_key_from_jwt(token).key,
                    algorithms=["RS256"],
                    audience=client_id,
                    issuer=issuer,
                    options={"require": ["exp", "iss", "sub", "aud"]},
                )
                if claims.get("token_use") != "id" or claims["sub"] not in members:
                    raise ValueError("unregistered member")
                return Principal.model_validate(members[claims["sub"]])
            except Exception:
                pass
        raise HTTPException(401, "Sign in to continue", headers={"WWW-Authenticate": "Bearer"})

    def manager(actor=Depends(principal)):
        if (
            actor.role != "manager"
            or actor.actor_id not in runtime._bundle.property_profile.management_committee
        ):
            raise HTTPException(403, "Management access required")
        return actor

    @app.exception_handler(ConcurrencyConflict)
    @app.exception_handler(IdempotencyConflict)
    async def conflict(_request, exc):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(ValueError)
    async def bad_request(_request, exc):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(PermissionError)
    async def forbidden(_request, exc):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=403, content={"detail": str(exc)})

    def case_view(case):
        jobs = [
            j
            for j in runtime.store.workflow_jobs(case.case_id)
            if j.status.value in ("pending", "running")
        ]
        tasks = [t for t in runtime.store.human_tasks(case.case_id) if t.status == "open"]
        jobs.sort(key=lambda j: j.due_at)
        tasks.sort(
            key=lambda t: (
                t.kind not in ("delivery_review", "appointment_delivery"),
                t.due_at,
                t.created_at,
            )
        )
        labels = {
            "case.opened": ("Review the new report", "Steward"),
            "procurement.review": ("Collect and compare vendor quotes", "Vendors"),
            "attendance.check": ("Await the vendor’s appointment proposal", "Selected vendor"),
            "warranty.followup": ("Follow up on the repair under warranty", "Selected vendor"),
            "community.prepare": (
                "Prepare a meeting from participant availability",
                "Participants",
            ),
            "clarification.reassess": ("Review the new clarification", "Steward"),
            "clarification.deadline": (
                "Await the reporting resident’s answer",
                "Reporting resident",
            ),
            "appointment.reply": ("Review the vendor’s appointment", "Steward"),
            "appointment.extract": ("Read the vendor’s appointment reply", "Steward"),
            "appointment.check": ("Check the visit outcome", "Selected vendor"),
            "mail.process": ("Read the received vendor message", "Steward"),
            "community.deadline": ("Collect meeting availability", "Participants"),
            "community.minutes": ("Prepare decisions for confirmation", "Steward"),
            "community.route": ("Prepare options for a community decision", "Steward"),
            "community.draft": ("Prepare the meeting agenda and discussion options", "Steward"),
        }
        next_job = labels.get(jobs[0].kind, ("Review the next step", "Steward")) if jobs else None
        progress = (
            (next_job[0], next_job[1], jobs[0].due_at)
            if next_job
            else ("Review case", "Steward", None)
        )
        if case.status in TERMINAL_STATUSES:
            record = runtime.store.get(case.case_id)
            progress = (
                "Case cancelled"
                if case.status == CaseStatus.CANCELLED
                else "Verified outcome saved to community memory"
                if record and record.outcome_verified
                else "Case closed",
                "No one",
                None,
            )
        elif tasks:
            task = tasks[0]
            progress = (
                task.title,
                "Reporting resident or manager"
                if task.kind == "verify_completion"
                else ", ".join(task.assigned_actor_ids)
                if task.assigned_actor_ids
                else "Management",
                task.due_at,
            )
        elif case.status in (
            CaseStatus.COMMITTED,
            CaseStatus.AWAITING_APPOINTMENT,
            CaseStatus.SCHEDULED,
        ):
            orders = [
                o
                for o in runtime.store.outbox_for_case(case.case_id)
                if o.payload.get("purpose") == "commitment"
                and o.last_error_code != "superseded_before_send"
            ]
            order = max(orders, key=lambda o: o.created_at) if orders else None
            confirmed = runtime.store.artifacts_for(
                kind="appointment.confirmed.v1", case_id=case.case_id
            )
            latest_confirmation = (
                max(confirmed, key=lambda a: a.payload["revision"]) if confirmed else None
            )
            proposals = runtime.store.artifacts_for(
                kind="appointment.proposal.v1", case_id=case.case_id
            )
            pending_acceptances = [
                item
                for proposal in proposals
                if not latest_confirmation
                or proposal.payload["revision"] > latest_confirmation.payload["revision"]
                if (
                    item := runtime.store.outbox_item(
                        stable_id("appointment-accept", proposal.artifact_id)
                    )
                )
            ]
            acceptance = pending_acceptances[-1] if pending_acceptances else None
            inbound_job = next(
                (
                    j
                    for j in jobs
                    if j.kind in ("mail.process", "appointment.extract", "appointment.reply")
                ),
                None,
            )
            if case.status == CaseStatus.COMMITTED and (
                not order or order.status.value != "delivered"
            ):
                progress = (
                    ("Check service order delivery", "Management", None)
                    if not order or order.status.value in ("ambiguous", "dead_letter")
                    else ("Confirm service order delivery", "Steward", None)
                    if order.status.value == "dispatching"
                    else ("Send the approved service order", "Steward", None)
                )
            elif inbound_job:
                progress = (*labels[inbound_job.kind], inbound_job.due_at)
            elif acceptance:
                progress = (
                    ("Check appointment acceptance delivery", "Management", None)
                    if acceptance.status.value in ("ambiguous", "dead_letter")
                    else ("Record the confirmed appointment", "Steward", None)
                    if acceptance.status.value == "delivered"
                    else ("Confirm appointment acceptance delivery", "Steward", None)
                    if acceptance.status.value == "dispatching"
                    else ("Send appointment confirmation", "Steward", None)
                )
            elif case.status == CaseStatus.SCHEDULED:
                proposal = (
                    runtime.store.artifact(
                        "appointment.proposal.v1", latest_confirmation.payload["appointment_id"]
                    )
                    if latest_confirmation
                    else None
                )
                if proposal:
                    facts = proposal.payload["facts"]
                    start = parse_datetime(facts["starts_at"])
                    end = parse_datetime(facts["ends_at"])
                    now = runtime._clock.now()
                    progress = (
                        ("Attend the confirmed repair visit", "Selected vendor", start)
                        if now < start
                        else ("Complete the confirmed repair visit", "Selected vendor", end)
                        if now < end
                        else (
                            "Check the visit outcome",
                            "Selected vendor",
                            next(
                                (
                                    j.due_at
                                    for j in jobs
                                    if j.kind == "appointment.check"
                                    and j.payload.get("appointment_id", j.source_id)
                                    == proposal.artifact_id
                                ),
                                end + timedelta(hours=1),
                            ),
                        )
                    )
                else:
                    progress = ("Confirm the appointment details", "Management", None)
            else:
                progress = (
                    "Await the vendor’s appointment proposal",
                    "Selected vendor",
                    next(
                        (j.due_at for j in jobs if j.kind == "attendance.check"),
                        case.next_action_due_at,
                    ),
                )
        return {
            **case.model_dump(mode="json"),
            "version": runtime.store.case_version(case.case_id),
            "next_step": progress[0],
            "waiting_for": progress[1],
            "due_at": progress[2].isoformat() if progress[2] else None,
        }

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/api/auth/config")
    def auth_configuration():
        return {
            "domain": os.getenv("STEWARD_COGNITO_DOMAIN"),
            "client_id": os.getenv("STEWARD_COGNITO_CLIENT_ID"),
        }

    @app.post("/api/channels/telegram")
    def telegram_ingress(
        update: dict[str, Any], x_telegram_bot_api_secret_token: str = Header(default="")
    ):
        if not runtime.telegram_enabled:
            raise HTTPException(404, "Telegram is not enabled")
        secret = runtime._settings.telegram_webhook_secret
        if not secret or not hmac.compare_digest(
            x_telegram_bot_api_secret_token, secret.get_secret_value()
        ):
            raise HTTPException(403, "Telegram webhook authentication failed")
        try:
            return runtime.handle_telegram_update(
                update, secret_header=x_telegram_bot_api_secret_token
            )
        except (ValueError, PermissionError) as exc:
            from steward.store.memory import StoreInvariantError

            if isinstance(exc, StoreInvariantError):
                raise
            # Telegram retries non-2xx deliveries. Persist terminal input rejection
            # before acknowledging so an unlinked group's message cannot block
            # the subsequent /start command that authorizes that very group.
            # Runtime/transport/storage failures remain retryable non-2xx errors.
            update_id = update.get("update_id")
            if not isinstance(update_id, int) or isinstance(update_id, bool) or update_id < 0:
                raise
            digest = hashlib.sha256(json.dumps(update, sort_keys=True).encode()).hexdigest()
            key = stable_id("telegram-webhook-rejection", str(update_id), digest)
            with runtime.store.atomic():
                if not runtime.store.artifact("telegram.webhook-rejected.v1", key):
                    runtime.store.put_configuration(
                        WorkflowArtifact(
                            artifact_id=key,
                            kind="telegram.webhook-rejected.v1",
                            created_at=runtime._clock.now(),
                            payload={"update": update, "reason": str(exc), "payload_hash": digest},
                        )
                    )
            return {"accepted": False, "reason": str(exc), "recorded": True}

    @app.post("/api/telegram/link")
    def link_telegram(actor=Depends(principal)):
        from steward.channels.private_telegram import PrivateTelegram

        if actor.role == "manager":
            manager(actor)
        else:
            resident(actor)
        if not runtime.telegram_enabled or not runtime._settings.telegram_bot_username:
            raise ValueError("The test Telegram bot has not been configured")
        token = PrivateTelegram(runtime).issue(actor.actor_id)
        return {
            "url": f"https://t.me/{runtime._settings.telegram_bot_username}?start={token}",
            "expires_in_seconds": 600,
        }

    @app.get("/api/telegram/group")
    def telegram_group(actor=Depends(manager)):
        from steward.channels.telegram_groups import TelegramGroups

        return TelegramGroups(runtime).status(actor.actor_id)

    @app.post("/api/telegram/group")
    def telegram_group_command(body: TelegramGroupCommand, actor=Depends(manager)):
        from steward.channels.telegram_groups import TelegramGroups

        groups = TelegramGroups(runtime)
        if body.action == "connect":
            return groups.issue(actor.actor_id, body.expected_version)
        if body.action == "disconnect":
            groups.disconnect(actor.actor_id, body.expected_version)
            return {"disconnected": True}
        if body.action == "test_delivery":
            from steward.channels.notifications import TelegramNotices

            identity = TelegramNotices(runtime).queue_group_test(
                actor.actor_id, body.expected_version
            )
            return {"outbox_id": identity, "queued": True}
        raise ValueError("Choose connect, disconnect or test_delivery")

    @app.get("/api/me")
    def me(actor=Depends(principal)):
        return {**actor.model_dump(), "simulation": simulation}

    @app.get("/api/directory")
    def directory(_actor=Depends(manager)):
        return {
            "residents": [r.display_name for r in runtime._bundle.residents],
            "managers": runtime._bundle.property_profile.management_committee,
            "vendors": [{"vendor_id": v.vendor_id, "name": v.name} for v in runtime._vendors],
            "assets": [{"asset_id": a.asset_id, "label": a.label} for a in runtime._bundle.assets],
        }

    @app.get("/api/cases/{case_id}/sources/{source_id}")
    def source_evidence(case_id: str, source_id: str, _actor=Depends(manager)):
        case = runtime.store.get_case(case_id)
        if case is None:
            raise HTTPException(404, "Unknown case")
        rows = runtime.store._all("SELECT doc_json FROM artifacts WHERE case_id=?", (case_id,))
        artifacts = [WorkflowArtifact.model_validate_json(r["doc_json"]) for r in rows]
        allowed = set(case.source_message_ids + case.related_case_ids)
        for item in artifacts:
            allowed.add(item.artifact_id)
            allowed.update(item.source_ids)
        if source_id not in allowed:
            raise HTTPException(404, "Source is not attached to this case")
        message = runtime.store.get_message(source_id)
        if message:
            return {
                "title": "Resident message",
                "text": message.text,
                "source_id": source_id,
                "author": message.sender_display,
                "at": message.sent_at,
            }
        record = runtime.store.get(source_id)
        if record:
            return {
                "title": record.title,
                "text": record.work_performed + "\n" + record.resolution_notes,
                "source_id": source_id,
                "at": record.closed_at,
                "verified": record.outcome_verified,
            }
        artifact = next((a for a in artifacts if a.artifact_id == source_id), None)
        if artifact:
            payload = artifact.payload
            return {
                "title": artifact.kind.replace("_", " ").replace(".v1", ""),
                "text": payload.get("body_text")
                or payload.get("notes")
                or payload.get("recommendation", {}).get("rationale")
                or payload.get("quote", {}).get("scope")
                or "Structured source record",
                "source_id": source_id,
                "at": artifact.created_at,
            }
        raise HTTPException(404, "This reference has no readable source document")

    @app.get("/api/suggestions")
    def suggestions(_actor=Depends(manager)):
        return runtime.store.list_suggestions()

    @app.post("/api/suggestions/{suggestion_id}/decision")
    def decide_suggestion(
        suggestion_id: str,
        body: SuggestionDecision,
        idempotency_key: str = Header(min_length=1, max_length=200),
        actor=Depends(manager),
    ):
        from steward.proactive import SuggestionStatus
        from steward.store import InboxStatus

        if not body.notes.strip():
            raise ValueError("Decision evidence is required")
        source = stable_id("suggestion-decision", suggestion_id, idempotency_key)
        payload = {**body.model_dump(), "actor_id": actor.actor_id}
        now = runtime._clock.now()
        with runtime.store.atomic():
            prior = runtime.store.artifact("suggestion.decision.v1", source)
            if prior:
                if prior.payload != payload:
                    raise IdempotencyConflict("Decision key reused with different input")
                return {"applied": False, "message_id": source if body.accepted else None}
            suggestion = next(
                (s for s in runtime.store.list_suggestions() if s.suggestion_id == suggestion_id),
                None,
            )
            if suggestion is None:
                raise HTTPException(404, "Suggestion not found")
            if suggestion.status != SuggestionStatus.SUGGESTED:
                raise ConcurrencyConflict("This suggestion already has a decision")
            runtime.store.decide_suggestion(
                suggestion_id,
                status=SuggestionStatus.ACCEPTED if body.accepted else SuggestionStatus.DISMISSED,
                decided_by=actor.actor_id,
                decided_at=now,
            )
            if body.accepted:
                asset = next(
                    (a for a in runtime._bundle.assets if a.asset_id == suggestion.asset_id), None
                )
                message = ResidentMessage(
                    message_id=source,
                    source="proactive_approval",
                    chat_id="northgate-management",
                    sender_display=actor.actor_id,
                    text=f"Maintenance review requested: {suggestion.title}. "
                    f"Asset: {asset.label if asset else 'not specified'}. "
                    f"Evidence: {suggestion.reason}. Manager notes: {body.notes}",
                    sent_at=now,
                    ingested_at=now,
                )
                runtime.store.record_inbound(
                    InboxItem(
                        source="proactive_approval",
                        external_id=source,
                        status=InboxStatus.PENDING,
                        received_at=now,
                        payload_hash=hashlib.sha256(message.text.encode()).hexdigest(),
                        message=message,
                    )
                )
            runtime.store.put_configuration(
                WorkflowArtifact(
                    artifact_id=source,
                    kind="suggestion.decision.v1",
                    created_at=now,
                    source_ids=(suggestion_id, *suggestion.source_ids),
                    payload=payload,
                )
            )
        return {"applied": True, "message_id": source if body.accepted else None}

    @app.get("/api/inbound-reviews")
    def inbound_reviews(_actor=Depends(manager)):
        return [
            a
            for a in runtime.store.artifacts_for(kind="ses.receipt.completed.v1")
            if a.payload.get("status") == "quarantined"
            and not runtime.store.artifact("ses.review.closed.v1", a.artifact_id)
        ]

    @app.post("/api/inbound-reviews/{receipt_id}/commands")
    def review_inbound(
        receipt_id: str,
        body: Command,
        actor=Depends(manager),
        idempotency_key: str = Header(min_length=1, max_length=200),
    ):
        key = stable_id("mail-review", receipt_id, idempotency_key)
        review = runtime.store.artifact("ses.receipt.completed.v1", receipt_id)
        if not review or review.payload.get("status") != "quarantined" or not body.notes.strip():
            raise ValueError("Select a quarantined message and record a reason")
        payload = {**body.model_dump(), "actor_id": actor.actor_id}
        if body.action not in ("dismiss", "request_replacement", "reprocess"):
            raise ValueError("Unknown mail review action")
        # Failed authentication is never overridable from the UI.
        if (
            body.action != "dismiss"
            and review.payload.get("reason") == "sender_authentication_or_content_check"
        ):
            raise PermissionError(
                "Ask the known vendor through its verified channel; this message cannot be trusted"
            )
        with runtime.store.atomic():
            prior = runtime.store.artifact("ses.review.action.v1", key)
            if prior:
                if prior.payload != payload:
                    raise IdempotencyConflict("Mail review key reused")
                return {"applied": False}
            if runtime.store.artifact("ses.review.closed.v1", receipt_id):
                raise ConcurrencyConflict("This review has already been closed")
            raw = runtime.store.artifact("ses.raw.v1", receipt_id)
            if body.action != "dismiss":
                if not raw or any(v != "PASS" for v in raw.payload["verdicts"].values()):
                    raise ValueError(
                        "A source-verified, routed message is required; "
                        "request a fresh email from the vendor"
                    )
                case = runtime.store.get_case(raw.case_id)
                if body.action == "reprocess":
                    if raw.payload["inbound"].get("unreadable_attachments"):
                        raise ValueError(
                            "Request a readable replacement before processing this attachment"
                        )
                    runtime.store.enqueue_job(
                        runtime._maintenance.job(
                            case, "mail.process", due_at=runtime._clock.now(), source_id=receipt_id
                        ).model_copy(update={"job_id": key})
                    )
                else:
                    from steward.mail import InboundMessage

                    data = dict(raw.payload["inbound"])
                    data["received_at"] = parse_datetime(data["received_at"])
                    route = runtime._vendor_replies._route(
                        InboundMessage(**data), allow_unreadable_attachment=True
                    )
                    runtime.store.enqueue_job(
                        runtime._maintenance.job(
                            case,
                            "vendor.clarify",
                            due_at=runtime._clock.now(),
                            source_id=key,
                            payload={"vendor_id": route.vendor.vendor_id, "reason": body.notes},
                        )
                    )
            runtime.store.put_configuration(
                WorkflowArtifact(
                    artifact_id=key,
                    kind="ses.review.action.v1",
                    created_at=runtime._clock.now(),
                    payload=payload,
                )
            )
            runtime.store.put_configuration(
                WorkflowArtifact(
                    artifact_id=receipt_id,
                    kind="ses.review.closed.v1",
                    created_at=runtime._clock.now(),
                    payload={"action_id": key},
                )
            )
        return {"applied": True}

    @app.get("/api/cases")
    def cases(actor=Depends(principal)):
        if actor.role == "manager":
            manager(actor)
            return [case_view(c) for c in runtime.store.list_open_cases()]
        resident(actor)
        return [resident_case_view(case) for case in runtime.store.list_open_cases()]

    def resident(actor):
        if actor.role != "resident" or actor.actor_id not in {
            r.display_name for r in runtime._bundle.residents
        }:
            raise HTTPException(403, "Resident membership required")
        return actor

    def resident_case_view(case):
        notices = {
            "scheduled": "Maintenance is being arranged.",
            "awaiting_verification": "The work is complete and awaiting confirmation.",
            "warranty_review": "The repair needs another check. Management is following up.",
            "meeting_ready": "A community meeting has been arranged.",
            "closed": "Resolved.",
        }
        return {
            "case_id": case.case_id,
            "title": case.title,
            "category": case.category.value,
            "status": case.status.value,
            "version": runtime.store.case_version(case.case_id),
            "update": notices.get(case.status.value, "Management is following up on this case."),
            "updated_at": case.updated_at,
        }

    def resident_invitation(case, actor):
        if runtime.store.artifacts_for(kind="meeting.invalidated.v1", case_id=case.case_id):
            return None
        schedules = sorted(
            runtime.store.artifacts_for(kind="community.schedule.v1", case_id=case.case_id),
            key=lambda a: a.payload.get("round", 1),
        )
        if (
            not schedules
            or actor.actor_id not in schedules[-1].payload["policy"]["eligible_participant_ids"]
        ):
            return None
        packets = runtime.store.artifacts_for(kind="meeting_packet.v1", case_id=case.case_id)
        packet = packets[-1].payload if packets else None
        responses = [
            a
            for a in runtime.store.artifacts_for(
                kind="community.availability.v1", case_id=case.case_id
            )
            if a.payload["participant_id"] == actor.actor_id
            and a.payload.get(
                "schedule_id",
                schedules[-1].artifact_id if schedules[-1].payload.get("round", 1) == 1 else None,
            )
            == schedules[-1].artifact_id
        ]
        latest = max(responses, key=lambda a: a.payload["version"]) if responses else None
        return {
            "case_id": case.case_id,
            "title": case.title,
            "version": runtime.store.case_version(case.case_id),
            "schedule_id": schedules[-1].artifact_id,
            "round": schedules[-1].payload.get("round", 1),
            "starts_at": packet["schedule"]["selected_slot"]["starts_at"] if packet else None,
            "slots": schedules[-1].payload["slots"] if not packet else [],
            "agenda": [item["title"] for item in packet["agenda"]["agenda_items"]]
            if packet
            else [],
            "available_slot_ids": latest.payload["available_slot_ids"] if latest else [],
            "responded": latest is not None,
        }

    @app.get("/api/resident/overview")
    def resident_overview(actor=Depends(principal)):
        resident(actor)
        open_cases = runtime.store.list_open_cases()
        return {
            "cases": [resident_case_view(case) for case in open_cases],
            "requests": [
                {
                    "request_id": t.task_id,
                    "case_id": t.case_id,
                    "title": t.title,
                    "kind": t.kind,
                    "version": runtime.store.case_version(t.case_id),
                    "due_at": t.due_at,
                }
                for t in runtime.store.human_tasks()
                if t.status == "open"
                and t.kind in ("clarification", "appointment_access", "verify_completion")
                and actor.actor_id in t.assigned_actor_ids
            ],
            "invitations": [
                invite for case in open_cases if (invite := resident_invitation(case, actor))
            ],
        }

    @app.get("/api/cases/{case_id}")
    def case_detail(case_id: str, actor=Depends(principal)):
        case = runtime.store.get_case(case_id)
        if case is None:
            raise HTTPException(404, "Case not found")
        if actor.role != "manager":
            resident(actor)
            return resident_case_view(case)
        manager(actor)
        return {
            **case_view(case),
            "timeline": runtime.store.timeline_for(case_id),
            "tasks": [t for t in runtime.store.human_tasks(case_id) if t.status == "open"],
            "outbox": runtime.store.outbox_for_case(case_id),
            "decisions": sorted(
                runtime.store.artifacts_for(kind="quote_decision.v1", case_id=case_id),
                key=lambda a: runtime.store.artifact(
                    "quote_portfolio.v1", a.payload["portfolio_id"]
                ).payload.get("revision", 1),
            ),
            "quotes": runtime.store.artifacts_for(kind="vendor_quote.v1", case_id=case_id),
            "quote_rejections": runtime.store.artifacts_for(
                kind="quote.rejection.v1", case_id=case_id
            ),
            "quote_supersessions": runtime.store.artifacts_for(
                kind="quote.supersession.v1", case_id=case_id
            ),
            "revisions": runtime.store.artifacts_for(kind="community.revision.v1", case_id=case_id),
            "confirmed_revisions": runtime.store.artifacts_for(
                kind="community.revision.confirmed.v1", case_id=case_id
            ),
            "assignments": runtime.store.artifacts_for(
                kind="meeting.assignment.v1", case_id=case_id
            ),
            "appointments": runtime.store.artifacts_for(
                kind="appointment.proposal.v1", case_id=case_id
            ),
            "confirmed_appointments": runtime.store.artifacts_for(
                kind="appointment.confirmed.v1", case_id=case_id
            ),
            "meetings": runtime.store.artifacts_for(kind="meeting_decision.v1", case_id=case_id),
        }

    @app.get("/api/tasks")
    def tasks(_actor=Depends(manager)):
        return [
            t.model_copy(update={"expected_version": runtime.store.case_version(t.case_id)})
            for t in runtime.store.human_tasks()
            if t.status == "open"
        ]

    @app.get("/api/cases/{case_id}/meeting")
    def meeting(case_id: str, actor=Depends(principal)):
        if actor.role != "manager":
            resident(actor)
            case = runtime.store.get_case(case_id)
            invite = resident_invitation(case, actor) if case else None
            if invite is None:
                raise HTTPException(403, "This meeting is not shared with you")
            return invite
        manager(actor)
        from steward.meetings.dossier import draft_for_plan
        from steward.orchestration.planning import ResolutionPlanningIntegrityError

        try:
            plan = runtime._resolution_planning.load(case_id)
        except ResolutionPlanningIntegrityError:
            plan = None
        draft = draft_for_plan(runtime.store, case_id, plan.plan.plan_id) if plan else None
        schedules = runtime.store.artifacts_for(kind="community.schedule.v1", case_id=case_id)
        if not schedules and not draft:
            raise HTTPException(404, "No meeting times have been offered")
        schedule = max(schedules, key=lambda a: a.payload.get("round", 1)) if schedules else None
        config = schedule.payload if schedule else None
        if (
            actor.role != "manager"
            and actor.actor_id not in config["policy"]["eligible_participant_ids"]
        ):
            raise HTTPException(403, "This meeting is not shared with you")
        if actor.role == "manager":
            manager(actor)
        own_invitation = resident_invitation(runtime.store.get_case(case_id), actor)
        kinds = (
            "meeting_packet.v1",
            "meeting_decision_candidates.v1",
            "meeting_decision.v1",
            "meeting_action_completion.v1",
        )
        return {
            "schedule": config,
            "schedule_id": schedule.artifact_id if schedule else None,
            "preparation": draft,
            "available_slot_ids": own_invitation["available_slot_ids"] if own_invitation else [],
            "responded": own_invitation["responded"] if own_invitation else False,
            "procurement_requests": runtime.store.artifacts_for(
                kind="community.procurement.request.v1", case_id=case_id
            ),
            "maintenance": [
                {
                    **a.payload,
                    "verified": bool(
                        runtime.store.get(a.payload["child_case_id"])
                        and runtime.store.get(a.payload["child_case_id"]).outcome_verified
                    ),
                }
                for a in runtime.store.artifacts_for(
                    kind="community.procurement.link.v1", case_id=case_id
                )
            ],
            "version": runtime.store.case_version(case_id),
            "records": {k: runtime.store.artifacts_for(kind=k, case_id=case_id) for k in kinds},
        }

    @app.post("/api/cases/{case_id}/availability")
    def availability(
        case_id: str,
        body: Availability,
        idempotency_key: str = Header(min_length=1, max_length=200),
        actor=Depends(principal),
    ):
        source = stable_id("availability", case_id, actor.actor_id, idempotency_key)
        with runtime.store.atomic():
            schedules = runtime.store.artifacts_for(kind="community.schedule.v1", case_id=case_id)
            if not schedules:
                raise HTTPException(404, "No meeting times offered")
            schedule = max(schedules, key=lambda a: a.payload.get("round", 1))
            if runtime.store.artifacts_for(kind="meeting.invalidated.v1", case_id=case_id):
                raise ConcurrencyConflict("This invitation was replaced")
            if (body.schedule_id and body.schedule_id != schedule.artifact_id) or (
                not body.schedule_id and schedule.payload.get("round", 1) > 1
            ):
                raise ConcurrencyConflict("Reply to the current scheduling round")
            if (
                schedule.payload.get("response_deadline")
                and parse_datetime(schedule.payload["response_deadline"]) <= runtime._clock.now()
            ):
                raise ConcurrencyConflict("This scheduling round has expired")
            if actor.actor_id not in schedule.payload["policy"]["eligible_participant_ids"]:
                raise HTTPException(403, "You are not an eligible participant")
            if not set(body.available_slot_ids) <= {
                s["slot_id"] for s in schedule.payload["slots"]
            }:
                raise ValueError("unknown meeting slot")
            payload = {
                **body.model_dump(),
                "participant_id": actor.actor_id,
                "schedule_id": schedule.artifact_id,
                "version": body.expected_version,
            }
            previous = runtime.store.artifact("community.availability.v1", source)
            if previous:
                if previous.payload != payload:
                    raise IdempotencyConflict("availability key reused")
                return {"saved": True}
            if runtime.store.case_version(case_id) != body.expected_version:
                raise ConcurrencyConflict("Meeting changed. Reload the available times.")
            case = runtime.store.get_case(case_id)
            if runtime.store.artifacts_for(kind="meeting_packet.v1", case_id=case_id):
                raise ConcurrencyConflict("Meeting is already scheduled")
            runtime.store.save_transition(
                case=case.model_copy(update={"updated_at": runtime._clock.now()}),
                expected_version=body.expected_version,
                idempotency_key=source,
                artifacts=(
                    WorkflowArtifact(
                        artifact_id=source,
                        kind="community.availability.v1",
                        case_id=case_id,
                        created_at=runtime._clock.now(),
                        payload=payload,
                    ),
                ),
                workflow_jobs=(
                    runtime._maintenance.job(
                        case,
                        "community.prepare",
                        due_at=runtime._clock.now(),
                        source_id=schedule.artifact_id,
                        payload={"response": source},
                    ).model_copy(update={"job_id": stable_id("prepare", source)}),
                ),
            )
        return {"saved": True}

    @app.get("/api/appointment-rules")
    def appointment_rules(_actor=Depends(manager)):
        rows = runtime.store.artifacts_for(kind="appointment.rules.v1")
        return {
            "version": len(rows),
            "rules": rows[-1].payload["rules"] if rows else None,
            "timezone": runtime._bundle.property_profile.timezone,
        }

    @app.put("/api/appointment-rules")
    def configure_appointment_rules(
        body: Command,
        actor=Depends(manager),
        idempotency_key: str = Header(min_length=1, max_length=200),
    ):
        from steward.operations.appointments import AppointmentRules

        rules = AppointmentRules.model_validate(body.data)
        if rules.access_actor_id and rules.access_actor_id not in {
            r.display_name for r in runtime._bundle.residents
        }:
            raise ValueError("Access contact must be a registered resident")
        key = stable_id("appointment-rules", idempotency_key)
        with runtime.store.atomic():
            rows = runtime.store.artifacts_for(kind="appointment.rules.v1")
            prior = runtime.store.artifact("appointment.rules.v1", key)
            payload = {
                "version": body.expected_version,
                "rules": rules.model_dump(mode="json"),
                "actor_id": actor.actor_id,
                "reason": body.notes,
            }
            if prior:
                if prior.payload != payload:
                    raise IdempotencyConflict("Rules key reused")
                return {"saved": True}
            if body.expected_version != len(rows) + 1:
                raise ConcurrencyConflict("Appointment rules changed")
            runtime.store.put_configuration(
                WorkflowArtifact(
                    artifact_id=key,
                    kind="appointment.rules.v1",
                    created_at=runtime._clock.now(),
                    payload=payload,
                )
            )
        return {"saved": True}

    @app.get("/api/memory")
    def memory(_actor=Depends(manager)):
        operational_ids = {case.case_id for case in runtime.store.list_cases()}
        return [
            {**record.model_dump(mode="json"), "has_case_record": record.case_id in operational_ids}
            for record in runtime.store.records()
        ]

    @app.get("/api/settings")
    def settings(_actor=Depends(manager)):
        runtime.refresh_policy()
        return {
            "version": len(runtime.store.artifacts_for(kind="configuration.v1")),
            "settings": runtime._policy.settings,
            "policies": {
                c.value: runtime._policy.policy_for(c)
                for c in Category
                if runtime._policy.policy_for(c)
            },
        }

    @app.put("/api/settings")
    def update_settings(
        body: SettingsUpdate,
        idempotency_key: str = Header(min_length=1, max_length=200),
        actor=Depends(manager),
    ):
        for category, policy in body.policies.items():
            if policy.category != category:
                raise ValueError("policy category key differs from its record")
        payload = body.model_dump(mode="json")
        payload["actor_id"] = actor.actor_id
        request_hash = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        with runtime.store.atomic():
            previous = runtime.store.artifact("configuration.v1", stable_id(idempotency_key))
            if previous:
                if previous.payload.get("request_hash") != request_hash:
                    raise IdempotencyConflict("settings request key reused")
                return {"saved": True}
            current = len(runtime.store.artifacts_for(kind="configuration.v1"))
            if current != body.expected_version:
                raise ConcurrencyConflict("Settings changed. Reload before saving.")
            for policy in payload["policies"].values():
                policy["updated_by"] = actor.actor_id
                policy["updated_at"] = runtime._clock.now().isoformat()
                if any(
                    policy.get(k) is not None and float(policy[k]) < 0
                    for k in ("per_incident_cap", "monthly_cap")
                ):
                    raise ValueError("budget caps cannot be negative")
            payload["request_hash"] = request_hash
            runtime.store.put_configuration(
                WorkflowArtifact(
                    artifact_id=stable_id(idempotency_key),
                    kind="configuration.v1",
                    created_at=runtime._clock.now(),
                    source_ids=(actor.actor_id,),
                    payload=payload,
                )
            )
            from steward.domain.enums import CaseStatus

            for case in runtime.store.list_open_cases():
                if case.status in (CaseStatus.QUOTES_RECEIVED, CaseStatus.COMMITTED):
                    runtime.store.enqueue_job(
                        runtime._maintenance.job(
                            case,
                            "procurement.review",
                            due_at=runtime._clock.now(),
                            source_id=stable_id(idempotency_key),
                        )
                    )
        runtime.refresh_policy()
        return {"saved": True}

    @app.post("/api/cases/{case_id}/commands")
    def command(
        case_id: str,
        body: Command,
        idempotency_key: str = Header(min_length=1, max_length=200),
        actor=Depends(principal),
    ):
        if actor.role == "manager":
            manager(actor)
        elif actor.role != "resident" or body.action not in (
            "verify",
            "answer_clarification",
            "appointment_access",
        ):
            raise HTTPException(403, "This action requires management access")
        else:
            resident(actor)
        source = stable_id("command", case_id, idempotency_key)
        payload = {**body.model_dump(), "actor_id": actor.actor_id}
        with runtime.store.atomic():
            prior = runtime.store.artifact("api.command.v1", source)
            if prior:
                if prior.payload != payload:
                    raise IdempotencyConflict("command key reused with different input")
                return {"applied": False}
            if runtime.store.case_version(case_id) != body.expected_version:
                raise ConcurrencyConflict("Case changed. Review the current evidence.")
            if body.action == "reconcile_appointment_delivery":
                proposal = runtime.store.artifact(
                    "appointment.proposal.v1", body.data.get("appointment_id", "")
                )
                if not proposal or proposal.case_id != case_id:
                    raise ValueError("Select the appointment for this case")
                runtime.store.reconcile_delivered_order(
                    stable_id("appointment-accept", proposal.artifact_id),
                    case_id=case_id,
                    expected_version=body.expected_version,
                    actor_id=actor.actor_id,
                    provider_message_id=body.data.get("provider_message_id", ""),
                    evidence=body.notes,
                    source_id=source,
                    now=runtime._clock.now(),
                    appointment_id=proposal.artifact_id,
                )
                from steward.operations.appointments import Appointments

                Appointments(runtime).reconcile()
                from steward.operations.community import CommunityWorkflow

                CommunityWorkflow(runtime).resolve(
                    case_id, {"appointment_delivery"}, body.notes, actor.actor_id
                )
            elif body.action == "appointment_accept":
                from steward.agents.order_reply import OrderReply
                from steward.operations.appointments import Appointments

                proposals = runtime.store.artifacts_for(
                    kind="appointment.proposal.v1", case_id=case_id
                )
                proposal = next(
                    (a for a in proposals if a.artifact_id == body.data.get("appointment_id")), None
                )
                if not proposal or proposal != proposals[-1] or not body.notes.strip():
                    raise ValueError("Choose the current proposal and explain the exception")
                facts = OrderReply.model_validate(proposal.payload["facts"])
                if (
                    not facts.starts_at
                    or not facts.ends_at
                    or facts.starts_at <= runtime._clock.now()
                    or facts.ends_at <= facts.starts_at
                    or not facts.unchanged_terms_evidence
                    or not facts.access_evidence
                ):
                    raise ValueError(
                        "Explicit future times, unchanged terms and access evidence are required"
                    )
                rules = runtime.store.artifacts_for(kind="appointment.rules.v1")
                if not rules:
                    raise ValueError("Configure access rules before accepting an appointment")
                runtime.store.put_configuration(
                    WorkflowArtifact(
                        artifact_id=source,
                        kind="appointment.override.v1",
                        case_id=case_id,
                        created_at=runtime._clock.now(),
                        source_ids=(proposal.artifact_id, actor.actor_id),
                        payload={
                            "appointment_id": proposal.artifact_id,
                            "reason": body.notes,
                            "rules_id": rules[-1].artifact_id,
                        },
                    )
                )
                Appointments(runtime).accept(
                    runtime.store.get_case(case_id),
                    proposal.model_copy(
                        update={"payload": {**proposal.payload, "rules_id": rules[-1].artifact_id}}
                    ),
                )
            elif body.action == "appointment_access":
                from steward.operations.appointments import Appointments

                task = next(
                    (
                        t
                        for t in runtime.store.human_tasks(case_id)
                        if t.task_id == body.data.get("task_id") and t.kind == "appointment_access"
                    ),
                    None,
                )
                if not task:
                    raise ValueError("Unknown appointment access request")
                runtime.store.resolve_human_task(
                    task.task_id,
                    actor_id=actor.actor_id,
                    role=actor.role,
                    expected_version=body.expected_version,
                    response={"notes": body.notes, "accepted": body.accepted},
                )
                proposals = runtime.store.artifacts_for(
                    kind="appointment.proposal.v1", case_id=case_id
                )
                proposal = next(
                    (
                        a
                        for a in proposals
                        if stable_id("appointment-access", a.artifact_id) == task.task_id
                    ),
                    None,
                )
                if proposal != proposals[-1]:
                    raise ConcurrencyConflict("A newer appointment proposal needs review")
                if body.accepted:
                    Appointments(runtime).accept(runtime.store.get_case(case_id), proposal)
                else:
                    runtime._maintenance.task(
                        runtime.store.get_case(case_id),
                        "appointment_review",
                        "Access was declined",
                        body.notes,
                        suffix=source,
                    )
            elif body.action == "answer_clarification":
                from steward.operations.clarifications import Clarifications

                Clarifications(runtime).answer(
                    runtime.store.get_case(case_id),
                    actor_id=actor.actor_id,
                    role=actor.role,
                    request_id=body.data.get("request_id", ""),
                    notes=body.notes,
                    source=source,
                )
            elif body.action in ("reject_quote", "clarify_quote"):
                quote = runtime.store.artifact("vendor_quote.v1", body.data.get("quote_id", ""))
                if not quote or quote.case_id != case_id or not body.notes.strip():
                    raise ValueError("Select a case quotation and provide a reason")
                case = runtime.store.get_case(case_id)
                if case.accepted_quote_id:
                    orders = [
                        o
                        for o in runtime.store.outbox_for_case(case_id)
                        if o.payload.get("purpose") == "commitment"
                        and o.last_error_code != "superseded_before_send"
                    ]
                    if len(orders) != 1 or orders[0].delivery_started_at is not None:
                        raise ConcurrencyConflict(
                            "Review the delivered or uncertain order before changing its quotation"
                        )
                    runtime._maintenance.withdraw_unsent_order(case, orders[0], source)
                    case = runtime.store.get_case(case_id)
                runtime.store.put_configuration(
                    WorkflowArtifact(
                        artifact_id=source,
                        case_id=case_id,
                        kind="quote.rejection.v1"
                        if body.action == "reject_quote"
                        else "quote.clarification.v1",
                        created_at=runtime._clock.now(),
                        source_ids=(quote.artifact_id, actor.actor_id),
                        payload={"quote_id": quote.artifact_id, "reason": body.notes},
                    )
                )
                runtime.store.enqueue_job(
                    runtime._maintenance.job(
                        case,
                        "procurement.review" if body.action == "reject_quote" else "vendor.clarify",
                        due_at=runtime._clock.now(),
                        source_id=source,
                        payload={
                            "vendor_id": quote.payload["quote"]["vendor_id"],
                            "reason": body.notes,
                        },
                    )
                )
            elif body.action == "reconcile_order_delivery":
                runtime.store.reconcile_delivered_order(
                    body.data.get("outbox_id", ""),
                    case_id=case_id,
                    expected_version=body.expected_version,
                    actor_id=actor.actor_id,
                    provider_message_id=body.data.get("provider_message_id", ""),
                    evidence=body.notes,
                    source_id=source,
                    now=runtime._clock.now(),
                )
                runtime._maintenance.reconcile_orders()
                from steward.operations.community import CommunityWorkflow

                CommunityWorkflow(runtime).resolve(
                    case_id, {"delivery_review"}, body.notes, actor.actor_id
                )
            elif body.action == "record_completion":
                runtime._maintenance.claim_completion(
                    case_id,
                    actor_id=actor.actor_id,
                    notes=body.notes,
                    expected_version=body.expected_version,
                    source_id=source,
                )
            elif body.action == "verify":
                runtime._maintenance.verify(
                    case_id,
                    actor_id=actor.actor_id,
                    accepted=body.accepted,
                    notes=body.notes,
                    expected_version=body.expected_version,
                    source_id=source,
                )
                for task in runtime.store.human_tasks(case_id):
                    if task.kind == "verify_completion" and task.status == "open":
                        runtime.store.resolve_human_task(
                            task.task_id,
                            actor_id=actor.actor_id,
                            role=actor.role,
                            expected_version=runtime.store.case_version(case_id),
                            response={"accepted": body.accepted, "notes": body.notes},
                        )
                if body.accepted:
                    for task in runtime.store.human_tasks(case_id):
                        if task.status == "open" and task.kind in (
                            "warranty_review",
                            "vendor_overdue",
                        ):
                            runtime.store.resolve_human_task(
                                task.task_id,
                                actor_id=actor.actor_id,
                                role="manager",
                                expected_version=runtime.store.case_version(case_id),
                                response={"superseded_by_verified_outcome": source},
                            )
            elif body.action.startswith("meeting_"):
                from steward.operations.community import CommunityWorkflow

                CommunityWorkflow(runtime).command(
                    runtime.store.get_case(case_id),
                    body.action,
                    {**body.data, "notes": body.notes},
                    actor.actor_id,
                    source,
                )
            elif body.action == "approve_quote":
                if not body.notes.strip():
                    raise ValueError("Approval requires a reason")
                from steward.procurement.portfolio import DurableQuoteDecision

                decisions = runtime.store.artifacts_for(kind="quote_decision.v1", case_id=case_id)
                chosen = next(
                    (a for a in decisions if a.artifact_id == body.data.get("decision_id")), None
                )
                if chosen is None:
                    raise ValueError("select a current decision to approve")
                runtime._maintenance.commit(
                    DurableQuoteDecision.model_validate(chosen.payload),
                    actor_id=actor.actor_id,
                    source_id=source,
                )
                if runtime.store.get_case(case_id).accepted_quote_id:
                    from steward.operations.community import CommunityWorkflow

                    CommunityWorkflow(runtime).resolve(
                        case_id, {"quote_approval", "budget_review"}, body.notes, actor.actor_id
                    )
            elif body.action == "review_resolution":
                case = runtime.store.get_case(case_id)
                if not body.notes.strip():
                    raise ValueError("Explain the missing facts or why the plan needs review")
                if case.status.value not in ("detected", "planning"):
                    raise ConcurrencyConflict(
                        "Resolution review requires an uncommitted planning case"
                    )
                runtime.store.enqueue_job(
                    runtime._maintenance.job(
                        case, "community.route", due_at=runtime._clock.now(), source_id=source
                    )
                )
            elif body.action == "continue_after_warranty_check":
                if not body.notes.strip():
                    raise ValueError("Record the warranty check and why a paid review is needed")
                case = runtime.store.get_case(case_id)
                if case.contacted_vendor_ids or case.accepted_quote_id:
                    raise ConcurrencyConflict("Procurement has already started")
                runtime.store.put_configuration(
                    WorkflowArtifact(
                        artifact_id=source,
                        kind="warranty.check.v1",
                        case_id=case_id,
                        created_at=runtime._clock.now(),
                        payload={"actor_id": actor.actor_id, "evidence": body.notes},
                    )
                )
                runtime.store.enqueue_job(
                    runtime._maintenance.job(
                        case, "case.opened", due_at=runtime._clock.now(), source_id=source
                    )
                )
                from steward.operations.community import CommunityWorkflow

                CommunityWorkflow(runtime).resolve(
                    case_id, {"warranty_check"}, body.notes, actor.actor_id
                )
            elif body.action == "supersede_quote":
                case = runtime.store.get_case(case_id)
                if case.accepted_quote_id:
                    raise ConcurrencyConflict("An ordered quote cannot be silently replaced")
                old = runtime.store.artifact(
                    "vendor_quote.v1", body.data.get("previous_quote_id", "")
                )
                new = runtime.store.artifact(
                    "vendor_quote.v1", body.data.get("replacement_quote_id", "")
                )
                if not old or not new or old.case_id != case_id or new.case_id != case_id:
                    raise ValueError("both quotes must belong to this case")
                if (
                    old.payload["quote"]["vendor_id"] != new.payload["quote"]["vendor_id"]
                    or new.created_at < old.created_at
                    or old.artifact_id == new.artifact_id
                ):
                    raise ValueError("replacement must be a newer quote from the same vendor")
                if not body.notes.strip():
                    raise ValueError("quote correction evidence is required")
                runtime.store.put_configuration(
                    WorkflowArtifact(
                        artifact_id=source,
                        kind="quote.supersession.v1",
                        case_id=case_id,
                        created_at=runtime._clock.now(),
                        source_ids=(old.artifact_id, new.artifact_id, actor.actor_id),
                        payload={
                            "previous_quote_id": old.artifact_id,
                            "replacement_quote_id": new.artifact_id,
                            "reason": body.notes,
                        },
                    )
                )
                runtime.store.enqueue_job(
                    runtime._maintenance.job(
                        case, "procurement.review", due_at=runtime._clock.now(), source_id=source
                    )
                )
            else:
                raise ValueError("unsupported case action")
            if runtime.store.case_version(case_id) == body.expected_version:
                case = runtime.store.get_case(case_id)
                runtime.store.save_transition(
                    case=case.model_copy(update={"updated_at": runtime._clock.now()}),
                    expected_version=body.expected_version,
                    idempotency_key=source,
                )
            runtime.store.put_configuration(
                WorkflowArtifact(
                    artifact_id=source,
                    case_id=case_id,
                    kind="api.command.v1",
                    created_at=runtime._clock.now(),
                    payload=payload,
                )
            )
        return {"applied": True}

    @app.get("/api/events")
    async def events(request: Request, _actor=Depends(manager)):
        async def stream():
            previous = None
            while not await request.is_disconnected():
                versions = [
                    (c.case_id, runtime.store.case_version(c.case_id))
                    for c in runtime.store.list_open_cases()
                ]
                fingerprint = stable_id(
                    json.dumps(versions),
                    json.dumps(
                        [t.model_dump(mode="json") for t in runtime.store.human_tasks()],
                        sort_keys=True,
                    ),
                )
                if fingerprint != previous:
                    yield "event: refresh\ndata: " + json.dumps({"revision": fingerprint}) + "\n\n"
                    previous = fingerprint
                else:
                    yield ": heartbeat\n\n"
                await asyncio.sleep(2)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/simulation/messages")
    def simulate_message(
        body: SimulationMessage,
        idempotency_key: str = Header(min_length=1, max_length=200),
        _actor=Depends(manager),
    ):
        if not simulation or runtime.execution_mode.value != "dry_run":
            raise HTTPException(403, "Simulation is disabled")
        if body.sender not in {r.display_name for r in runtime._bundle.residents}:
            raise ValueError("unknown simulated resident")
        now = runtime._clock.now()
        mid = "sim:" + stable_id(idempotency_key)
        message = ResidentMessage(
            message_id=mid,
            source="simulation",
            chat_id="northgate-simulation",
            sender_display=body.sender,
            text=body.text,
            sent_at=now,
            ingested_at=now,
        )
        from steward.store import InboxStatus

        inserted = runtime.store.record_inbound(
            InboxItem(
                source="simulation",
                external_id=mid,
                status=InboxStatus.PENDING,
                payload_hash=hashlib.sha256(body.model_dump_json().encode()).hexdigest(),
                received_at=now,
                message=message,
            )
        )
        return {"accepted": inserted, "message_id": mid}

    @app.post("/api/simulation/tick")
    def tick(_actor=Depends(manager)):
        if not simulation or runtime.execution_mode.value != "dry_run":
            raise HTTPException(403, "Simulation is disabled")
        return runtime.tick()

    @app.post("/api/simulation/vendor-replies")
    def simulate_vendor_reply(
        body: SimulationVendorReply,
        idempotency_key: str = Header(min_length=1, max_length=200),
        _actor=Depends(manager),
    ):
        if not simulation or runtime.execution_mode.value != "dry_run":
            raise HTTPException(403, "Simulation is disabled")
        from steward.mail import AddressScheme, InboundMessage

        case = runtime.store.get_case(body.case_id)
        vendor = next((v for v in runtime._vendors if v.vendor_id == body.vendor_id), None)
        if case is None or vendor is None:
            raise HTTPException(404, "Unknown case or vendor")
        mid = "<simulation-" + stable_id(idempotency_key) + "@northgate.invalid>"
        scheme = AddressScheme(
            management_domain=runtime._settings.management_domain,
            vendor_domain=runtime._settings.vendor_domains[0],
        )
        inbound = InboundMessage(
            message_id=mid,
            from_address=vendor.email,
            from_display_name=vendor.name,
            to_addresses=(scheme.case_reply_address(case.reply_token),),
            cc_addresses=(),
            delivered_to=None,
            subject="Quotation: " + case.title,
            body_text=body.text,
            body_full_text=body.text,
            received_at=runtime._clock.now(),
            raw_ref="simulation:" + mid,
        )
        return runtime.handle_vendor_reply(inbound, source_id="simulation:" + mid)

    @app.post("/api/simulation/advance")
    def advance(hours: int, _actor=Depends(manager)):
        if not simulation or not hasattr(runtime._clock, "advance"):
            raise HTTPException(403, "A virtual clock is required")
        if not 1 <= hours <= 168:
            raise ValueError("advance must be between 1 and 168 hours")
        runtime._clock.advance(timedelta(hours=hours))
        return {"now": runtime._clock.now()}

    web_root = Path(
        os.getenv("STEWARD_WEB_ROOT", str(Path(__file__).resolve().parents[2] / "web" / "dist"))
    )
    if web_root.is_dir():
        app.mount("/", StaticFiles(directory=web_root, html=True), name="web")
    return app


def main():
    import uvicorn

    uvicorn.run(
        create_app(simulation=os.getenv("STEWARD_SIMULATION") == "true"),
        host="127.0.0.1",
        port=int(os.getenv("PORT", "8000")),
    )


if __name__ == "__main__":
    main()
