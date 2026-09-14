"""Twenty product scenarios, three repetitions, driven solely through public APIs.

Model ports are deterministic here. Live language/quote measurements are separate.
The runner can inject time, channel events and process restarts; it cannot change
case state or call maintenance/meeting service methods.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from test_quote_decisions import (
    COASTLINE_BODY,
    MERIDIAN_BODY,
    OVER_CAP_BODY,
    PINNACLE_BODY,
    _ElevatorClassifier,
    _QuoteExtractor,
    _SelectingRecommender,
)

from steward.agents import QuoteExtraction
from steward.api import create_app
from steward.config import StewardSettings
from steward.domain.clock import FrozenClock
from steward.runtime import build_runtime

SCENARIOS = (
    "repair_verified",
    "repair_rejected",
    "rework_verified",
    "duplicate_channel_event",
    "restart_before_intake",
    "restart_waiting_for_quotes",
    "restart_after_delivery",
    "one_quote_after_wait",
    "three_quote_portfolio",
    "no_vendor_response",
    "no_price_in_reply",
    "foreign_currency_conflict",
    "incident_cap_exceeded",
    "monthly_budget_exhausted",
    "prepare_only_approval",
    "kill_switch_after_rfq",
    "low_confidence_review",
    "vendor_no_show",
    "community_decision",
    "community_procurement",
)


class ScenarioExtractor(_QuoteExtractor):
    def extract(self, **kwargs):
        if kwargs["body_text"] == "No price is available yet.":
            return QuoteExtraction(has_quote=False, no_quote_reason="Please provide a total price.")
        if kwargs["body_text"] == "Total EUR 705. Replace guide shoes.":
            # A bad model answer must be caught by the deterministic monetary boundary.
            return QuoteExtraction(
                has_quote=True,
                amount="705",
                currency="USD",
                currency_inferred_from_rfq=True,
                amount_evidence="EUR 705",
                scope_evidence="Replace guide shoes.",
            )
        return super().extract(**kwargs)


def maintenance_scenario(tmp_path, name):
    clock = FrozenClock(datetime(2026, 9, 14, 10, tzinfo=timezone.utc))
    settings = StewardSettings(database_path=tmp_path / "scenario.db")
    runtime = None
    client = None
    serial = 0

    def start():
        nonlocal runtime, client
        runtime = build_runtime(
            settings,
            clock=clock,
            classifier=_ElevatorClassifier(
                confidence=0.4 if name == "low_confidence_review" else 0.97
            ),
            quote_extractor=ScenarioExtractor(),
            decision_recommender=_SelectingRecommender(),
        )
        client = TestClient(
            create_app(
                runtime,
                simulation=True,
                tokens={"scenario-manager": {"actor_id": "Simon O.", "role": "manager"}},
            )
        )

    def restart():
        client.close()
        runtime.close()
        start()

    def post(path, body=None, key=None):
        nonlocal serial
        serial += 1
        response = client.post(
            "/api" + path,
            json=body,
            headers={
                "Authorization": "Bearer scenario-manager",
                "Idempotency-Key": key or str(serial),
            },
        )
        assert response.status_code == 200, response.text
        result = response.json()
        if path == "/simulation/tick":
            assert not result["workflow_failures"], result
        return result

    def get(path):
        response = client.get("/api" + path, headers={"Authorization": "Bearer scenario-manager"})
        assert response.status_code == 200, response.text
        return response.json()

    def permissions(**changes):
        config = get("/settings")
        for key, value in changes.items():
            if key == "kill_switch":
                config["settings"][key] = value
            else:
                config["policies"]["elevator"][key] = value
        response = client.put(
            "/api/settings",
            json={
                "expected_version": config["version"],
                "settings": config["settings"],
                "policies": config["policies"],
            },
            headers={"Authorization": "Bearer scenario-manager", "Idempotency-Key": "policy"},
        )
        assert response.status_code == 200, response.text

    def command(cid, action, accepted=True, data=None):
        return post(
            f"/cases/{cid}/commands",
            {
                "action": action,
                "accepted": accepted,
                "notes": "Scenario actor supplied observed completion or decision evidence.",
                "data": data or {},
                "expected_version": get("/cases/" + cid)["version"],
            },
        )

    start()
    try:
        body = {"sender": "James D.", "text": "The A Block elevator shudders near floor four."}
        post("/simulation/messages", body, key="resident-message")
        if name == "duplicate_channel_event":
            post("/simulation/messages", body, key="resident-message")
        if name == "restart_before_intake":
            restart()
        post("/simulation/tick")
        cases = get("/cases")
        assert len(cases) == 1
        cid = cases[0]["case_id"]
        if name == "low_confidence_review":
            assert get("/tasks") and not get("/cases/" + cid)["outbox"]
            return "human_review"
        if name == "restart_waiting_for_quotes":
            restart()
        if name == "no_vendor_response":
            for _ in range(4):
                post("/simulation/advance?hours=24")
                post("/simulation/tick")
            assert any(t["kind"] == "vendor_overdue" for t in get("/tasks"))
            return "human_review"
        if name == "prepare_only_approval":
            permissions(mode="prepare_only")
        if name == "monthly_budget_exhausted":
            permissions(monthly_cap="0")
        if name == "kill_switch_after_rfq":
            permissions(kill_switch=True)
        first = MERIDIAN_BODY
        if name == "incident_cap_exceeded":
            first = OVER_CAP_BODY
        if name == "no_price_in_reply":
            first = "No price is available yet."
        if name == "foreign_currency_conflict":
            first = "Total EUR 705. Replace guide shoes."
        post(
            "/simulation/vendor-replies",
            {"case_id": cid, "vendor_id": "meridian-lift", "text": first},
        )
        if name not in ("one_quote_after_wait", "no_price_in_reply", "foreign_currency_conflict"):
            post(
                "/simulation/vendor-replies",
                {"case_id": cid, "vendor_id": "coastline-elevator", "text": COASTLINE_BODY},
            )
        if name == "three_quote_portfolio":
            post(
                "/simulation/vendor-replies",
                {"case_id": cid, "vendor_id": "pinnacle-vertical", "text": PINNACLE_BODY},
            )
        # Receipt alone cannot trigger an early first-quote commitment.
        post("/simulation/tick")
        assert not get("/cases/" + cid)["accepted_quote_id"]
        post("/simulation/advance?hours=24")
        post("/simulation/tick")
        if name in ("no_price_in_reply", "foreign_currency_conflict"):
            for _ in range(3):
                post("/simulation/advance?hours=24")
                post("/simulation/tick")
            assert get("/tasks") and not get("/cases/" + cid)["accepted_quote_id"]
            return "human_review"
        if name in ("incident_cap_exceeded", "monthly_budget_exhausted", "kill_switch_after_rfq"):
            assert get("/tasks") and not get("/cases/" + cid)["accepted_quote_id"]
            return "human_review"
        if name == "prepare_only_approval":
            current = get("/cases/" + cid)
            assert current["status"] == "quotes_received"
            command(
                cid, "approve_quote", data={"decision_id": current["decisions"][-1]["artifact_id"]}
            )
            post("/simulation/tick")
        assert get("/cases/" + cid)["status"] == "awaiting_appointment"
        from appointment_support import confirm_appointment

        confirm_appointment(runtime, clock, cid)
        assert get("/cases/" + cid)["status"] == "scheduled"
        if name == "restart_after_delivery":
            restart()
            post("/simulation/tick")
        if name == "vendor_no_show":
            post("/simulation/advance?hours=168")
            post("/simulation/tick")
            for _ in range(3):
                post("/simulation/advance?hours=24")
                post("/simulation/tick")
            assert any(t["kind"] == "vendor_overdue" for t in get("/tasks"))
            return "human_review"
        command(cid, "record_completion")
        command(cid, "verify", accepted=name not in ("repair_rejected", "rework_verified"))
        if name == "repair_rejected":
            assert get("/cases/" + cid)["status"] == "warranty_review"
            return "human_review"
        if name == "rework_verified":
            post("/simulation/tick")
            command(cid, "record_completion")
            command(cid, "verify")
        assert get("/cases/" + cid)["status"] == "closed"
        assert any(r["case_id"] == cid and r["outcome_verified"] for r in get("/memory"))
        assert (
            len(
                [
                    o
                    for o in get("/cases/" + cid)["outbox"]
                    if o["payload"].get("purpose") == "commitment" and o["status"] == "delivered"
                ]
            )
            == 1
        )
        return "verified_outcome"
    finally:
        client.close()
        runtime.close()


@pytest.mark.parametrize("repetition", [1, 2, 3])
@pytest.mark.parametrize("scenario", SCENARIOS)
def test_product_scenario(tmp_path, scenario, repetition):
    calls = []
    original = TestClient.request

    def record(self, method, url, *args, **kwargs):
        response = original(self, method, url, *args, **kwargs)
        calls.append(
            {
                "method": method,
                "path": url,
                "status": response.status_code,
                "action": (kwargs.get("json") or {}).get("action"),
            }
        )
        return response

    with patch.object(TestClient, "request", record):
        if scenario.startswith("community_"):
            from test_community_product import (
                test_community_events_reach_verified_memory_without_manual_service_calls,
                test_confirmed_community_action_reaches_linked_verified_maintenance,
            )

            if scenario == "community_decision":
                test_community_events_reach_verified_memory_without_manual_service_calls(tmp_path)
            else:
                test_confirmed_community_action_reaches_linked_verified_maintenance(tmp_path)
            outcome = "verified_outcome"
        else:
            outcome = maintenance_scenario(tmp_path, scenario)
    report = {
        "scenario": scenario,
        "repetition": repetition,
        "passed": True,
        "outcome": outcome,
        "simulated_inputs": True,
        "live_model_calls": False,
        "model_ports": "deterministic fixtures",
        "human_commands": sum(
            c["method"] in ("POST", "PUT")
            and (
                bool(c["action"])
                or c["path"].endswith("/availability")
                or c["path"] == "/api/settings"
            )
            and c["status"] == 200
            for c in calls
        ),
        "limitations": [
            "Functional lifecycle measurement; not a time-saving or field-impact estimate."
        ],
        "api_events": calls,
    }
    path = Path(
        os.environ.get("STEWARD_SCENARIO_REPORT_DIR", "artifacts/validation/scenario-runs-v1")
    ) / f"{scenario}-{repetition}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
