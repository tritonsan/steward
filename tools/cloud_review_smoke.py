"""Check the deployed jury access boundary without displaying credentials.

Default execution only reads product state and exercises denied authentication/channel
requests. --inject additionally submits one fixed, idempotent synthetic message; it never
calls the tick endpoint. --status observes that message after the ordinary worker runs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx

ROOT = Path(__file__).resolve().parents[1]
ACCESS_FILE = ROOT / ".scratch" / "steward-review-access.local.json"
EVIDENCE_FILE = ROOT / "artifacts" / "validation" / "cloud-jury-access.json"
EXPECTED_HOST = "d35nbywkoth58f.cloudfront.net"
MESSAGE_KEY = "cloud-jury-plumbing-smoke-v1"
MESSAGE_ID = "sim:" + hashlib.sha256(MESSAGE_KEY.encode()).hexdigest()
MESSAGE = {
    "sender": "Daniel K.",
    "text": (
        "Simulated jury test: Water is leaking from a pipe joint inside the Water Booster "
        "Pump Room at Northgate. The floor is wet and upper-floor taps have lost pressure. "
        "The caretaker has closed the nearby isolation valve. Please arrange a plumbing "
        "inspection of the pipe joint and pump connections."
    ),
}


class SmokeFailure(Exception):
    """Only an authored check label reaches console output, never an HTTP response body."""


def check(report, name, passed, *, status=None):
    result = {"name": name, "passed": bool(passed)}
    if status is not None:
        result["http_status"] = status
    report["checks"].append(result)
    if not passed:
        raise SmokeFailure(name)


def request(client, report, method, path, expected, name, **kwargs):
    response = client.request(method, path, **kwargs)
    check(report, name, response.status_code == expected, status=response.status_code)
    return response


def workflow_evidence(detail, source):
    """Keep provenance identifiers and service stages; never retain raw source text."""
    timeline = detail.get("timeline", [])
    intake = next((event for event in timeline if event.get("kind") == "case_opened"), {})
    plan = next(
        (event for event in reversed(timeline) if event.get("kind") == "resolution_planned"), {}
    )
    tasks = detail.get("tasks", [])
    intake_payload, plan_payload = intake.get("payload", {}), plan.get("payload", {})
    return {
        "case_id": detail["case_id"],
        "case_version": detail.get("version"),
        "source_id": source.get("source_id"),
        "source_text_sha256": hashlib.sha256(source.get("text", "").encode()).hexdigest(),
        "source_at": source.get("at"),
        "workflow_clock": "simulated",
        "stage": "awaiting_human" if tasks else "plan_prepared" if plan else "intake_completed",
        "intake": {
            "event_id": intake.get("event_id"),
            "at": intake.get("at"),
            **{
                key: intake_payload.get(key)
                for key in (
                    "category",
                    "asset_id",
                    "triage_confidence",
                    "triage_reconciliation_attempts",
                )
            },
        },
        "plan": {
            "event_id": plan.get("event_id"),
            "at": plan.get("at"),
            **{
                key: plan_payload.get(key)
                for key in ("plan_id", "context_id", "resolution_path", "model_output_is_authority")
            },
        }
        if plan
        else None,
        "open_tasks": [
            {key: task.get(key) for key in ("task_id", "kind", "expected_version", "due_at")}
            for task in tasks
        ],
        "provider_trace_available_in_api": False,
    }


def run_checks(client, code, report, *, inject=False, observe=False):
    request(client, report, "GET", "/?mode=preview", 200, "public_preview_http")
    config = request(client, report, "GET", "/api/review/config", 200, "review_config_http").json()
    check(report, "review_enabled", config.get("enabled") is True)
    for path, name in (
        ("/api/cases", "owner_requires_auth"),
        ("/review/api/cases", "review_requires_auth"),
    ):
        request(client, report, "GET", path, 401, name)
    request(
        client,
        report,
        "POST",
        "/api/review/session",
        401,
        "wrong_code_denied",
        json={"access_code": "deliberately-invalid-review-code", "role": "manager"},
    )
    roles = {}
    for role in ("manager", "resident"):
        response = request(
            client,
            report,
            "POST",
            "/api/review/session",
            200,
            f"{role}_login",
            json={"access_code": code, "role": role},
        )
        body = response.json()
        check(
            report,
            f"{role}_session_not_cached",
            response.headers.get("cache-control") == "no-store",
        )
        check(
            report,
            f"{role}_session_scope",
            body.get("role") == role
            and str(body.get("token", "")).startswith(f"review.v1.{role}."),
        )
        roles[role] = {"Authorization": "Bearer " + body["token"]}
        request(
            client,
            report,
            "GET",
            "/api/me",
            401,
            f"owner_denies_{role}_review_token",
            headers=roles[role],
        )
    manager, resident = roles["manager"], roles["resident"]
    auth = request(
        client, report, "GET", "/review/api/auth/config", 200, "isolated_auth_config"
    ).json()
    check(report, "owner_cognito_not_inherited", auth == {"domain": None, "client_id": None})
    request(
        client,
        report,
        "GET",
        "/review/api/settings",
        403,
        "resident_denied_management_settings",
        headers=resident,
    )
    request(
        client,
        report,
        "POST",
        "/review/api/telegram/link",
        403,
        "real_telegram_link_blocked",
        headers=manager,
    )
    telegram = request(
        client,
        report,
        "GET",
        "/review/api/telegram/group",
        200,
        "telegram_configuration_read",
        headers=manager,
    ).json()
    check(
        report,
        "telegram_isolated",
        telegram.get("configured") is False
        and telegram.get("delivery_mode") == "dry_run"
        and telegram.get("connection") is None
        and telegram.get("bot_username") is None,
    )
    history = request(
        client, report, "GET", "/review/api/memory", 200, "manager_memory_read", headers=manager
    ).json()
    check(report, "synthetic_history_available", isinstance(history, list) and len(history) >= 63)
    cases = request(
        client, report, "GET", "/review/api/cases", 200, "manager_case_read", headers=manager
    ).json()
    categories = Counter(case.get("category") for case in cases)
    check(
        report,
        "prepared_scenarios_available",
        categories["elevator"] >= 1 and categories["meeting_admin"] >= 1,
    )
    deliveries, quote_sets, decision_sets = [], 0, 0
    for case in cases:
        detail = request(
            client,
            report,
            "GET",
            f"/review/api/cases/{case['case_id']}",
            200,
            "case_detail_read",
            headers=manager,
        ).json()
        deliveries.extend(item for item in detail.get("outbox", []) if item.get("delivered_at"))
        quote_sets += len(detail.get("quotes", [])) >= 2
        decision_sets += bool(detail.get("decisions"))
    check(report, "prepared_quotes_and_decision_available", quote_sets >= 1 and decision_sets >= 1)
    check(
        report,
        "recorded_deliveries_only",
        len(deliveries) >= 1
        and all(
            str(item.get("provider_message_id", "")).startswith(("recorded-", "dry-run:"))
            for item in deliveries
        ),
    )
    overview = request(
        client,
        report,
        "GET",
        "/review/api/resident/overview",
        200,
        "resident_overview_read",
        headers=resident,
    ).json()
    check(report, "resident_cases_available", len(overview.get("cases", [])) >= 2)
    check(
        report,
        "resident_operational_data_hidden",
        all(
            not {"outbox", "decisions", "source_message_ids", "quotes"}.intersection(case)
            for case in overview.get("cases", [])
        ),
    )
    report["aggregate"] = {
        "history_count": len(history),
        "open_case_count": len(cases),
        "case_categories": dict(categories),
        "recorded_deliveries": len(deliveries),
        "resident_invitation_count": len(overview.get("invitations", [])),
        "external_telegram_configured": False,
    }
    if inject:
        response = request(
            client,
            report,
            "POST",
            "/review/api/simulation/messages",
            200,
            "synthetic_message_durably_accepted",
            headers={**manager, "Idempotency-Key": MESSAGE_KEY},
            json=MESSAGE,
        )
        body = response.json()
        check(report, "synthetic_message_identity", body.get("message_id") == MESSAGE_ID)
        report["injection"] = {
            "message_id": MESSAGE_ID,
            "newly_accepted": body.get("accepted"),
            "is_simulated": True,
            "worker_tick_requested": False,
        }
    if inject or observe:
        # One read only; invoke --status later to observe normal background processing.
        current = request(
            client, report, "GET", "/review/api/cases", 200, "worker_progress_read", headers=manager
        ).json()
        matched = [case for case in current if MESSAGE_ID in case.get("source_message_ids", [])]
        report["workflow_observation"] = {
            "message_id": MESSAGE_ID,
            "case_count": len(matched),
            "state": "case_created" if matched else "not_observed_yet",
            "cases": [
                {
                    key: case.get(key)
                    for key in ("case_id", "category", "status", "next_step", "waiting_for")
                }
                for case in matched
            ],
            "model_trace_verified": False,
        }
        evidence = []
        for case in matched:
            case_id = case["case_id"]
            detail = request(
                client,
                report,
                "GET",
                f"/review/api/cases/{case_id}",
                200,
                "observed_case_provenance_read",
                headers=manager,
            ).json()
            source = request(
                client,
                report,
                "GET",
                f"/review/api/cases/{case_id}/sources/{MESSAGE_ID}",
                200,
                "observed_input_source_read",
                headers=manager,
            ).json()
            check(
                report,
                "observed_source_matches_submitted_message",
                source.get("source_id") == MESSAGE_ID and source.get("text") == MESSAGE["text"],
            )
            entry = workflow_evidence(detail, source)
            if entry["plan"]:
                for field in ("plan_id", "context_id"):
                    source_id = entry["plan"].get(field)
                    check(report, f"observed_{field}_present", bool(source_id))
                    request(
                        client,
                        report,
                        "GET",
                        f"/review/api/cases/{case_id}/sources/{source_id}",
                        200,
                        f"observed_{field}_attached_to_case",
                        headers=manager,
                    )
                entry["plan"]["artifacts_attached_to_case"] = True
            evidence.append(entry)
        report["workflow_observation"]["provenance"] = evidence


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--inject", action="store_true")
    modes.add_argument("--status", action="store_true")
    args = parser.parse_args(argv)
    report = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "checks": [],
        "owner_credentials_used": False,
        "owner_token_against_review": "not_retested_live; covered by local isolation tests",
        "raw_messages_or_credentials_saved": False,
    }
    try:
        saved = json.loads(ACCESS_FILE.read_text(encoding="utf-8"))
        address = urlsplit(saved["url"])
        if address.scheme != "https" or address.netloc != EXPECTED_HOST:
            raise SmokeFailure("saved_access_origin_mismatch")
        code = saved["access_code"]
        if not isinstance(code, str) or len(code) < 24:
            raise SmokeFailure("saved_access_code_invalid")
        origin = f"https://{EXPECTED_HOST}"
        report["origin"] = origin
        with httpx.Client(base_url=origin, timeout=25, follow_redirects=False) as client:
            run_checks(client, code, report, inject=args.inject, observe=args.status)
        report["passed"] = True
    except SmokeFailure as exc:
        report.update(passed=False, error=str(exc))
    except Exception as exc:
        # Neither request objects nor response bodies nor saved access data are rendered.
        report.update(passed=False, error=type(exc).__name__)
    EVIDENCE_FILE.parent.mkdir(parents=True, exist_ok=True)
    if EVIDENCE_FILE.exists():
        try:
            previous = json.loads(EVIDENCE_FILE.read_text())
            if "injection" not in report and "injection" in previous:
                report["injection"] = previous["injection"]
        except (ValueError, OSError):
            pass
    EVIDENCE_FILE.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "passed": report["passed"],
                "checks": len(report["checks"]),
                "error": report.get("error"),
                "evidence": str(EVIDENCE_FILE),
            }
        )
    )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
