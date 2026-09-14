"""A controlled vendor reply reaches scheduling through public APIs and the worker."""

from datetime import timedelta

from fastapi.testclient import TestClient

from steward.agents.order_reply import OrderReply
from steward.api import create_app


class OrderExtractor:
    def extract(self, **kwargs):
        from datetime import datetime

        text = kwargs["body_text"]
        if not text.startswith("Appointment "):
            return OrderReply(kind="quote_change", evidence=text)
        _, start, end = text.splitlines()[0].split()
        return OrderReply(
            kind="appointment",
            evidence=text,
            starts_at=datetime.fromisoformat(start),
            ends_at=datetime.fromisoformat(end),
            time_evidence=text.splitlines()[0],
            unchanged_terms_evidence="Same price and scope.",
            access_evidence="Access arrangements agreed.",
        )


def confirm_appointment(runtime, clock, case_id):
    runtime._order_reply_extractor = OrderExtractor()
    client = TestClient(
        create_app(
            runtime,
            simulation=True,
            tokens={"appointments": {"actor_id": "Simon O.", "role": "manager"}},
        )
    )
    headers = {"Authorization": "Bearer appointments", "Idempotency-Key": "hours"}
    rules = client.get("/api/appointment-rules", headers=headers).json()
    if not rules["rules"]:
        result = client.put(
            "/api/appointment-rules",
            headers=headers,
            json={
                "action": "configure",
                "expected_version": rules["version"] + 1,
                "data": {
                    "weekdays": list(range(7)),
                    "start_hour": 8,
                    "end_hour": 20,
                    "minimum_notice_hours": 1,
                    "access_instructions": "Use the management entrance.",
                },
            },
        )
        assert result.status_code == 200, result.text
    case = runtime.store.get_case(case_id)
    quote = runtime.store.artifact("vendor_quote.v1", case.accepted_quote_id)
    start = (clock.now() + timedelta(days=2)).replace(hour=10, minute=0)
    text = (
        f"Appointment {start.isoformat()} {(start + timedelta(hours=1)).isoformat()}\n"
        "Same price and scope. Access arrangements agreed."
    )
    result = client.post(
        "/api/simulation/vendor-replies",
        headers={**headers, "Idempotency-Key": case_id + "-appointment"},
        json={"case_id": case_id, "vendor_id": quote.payload["quote"]["vendor_id"], "text": text},
    )
    assert result.status_code == 200, result.text
    result = client.post("/api/simulation/tick", headers=headers)
    assert not result.json()["workflow_failures"], result.text
    assert runtime.store.get_case(case_id).status.value == "scheduled"
    client.close()
