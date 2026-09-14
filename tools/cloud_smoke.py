"""Authenticated cloud smoke check; injects one labelled, idempotent demo message."""

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import boto3
import httpx

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--inject", action="store_true")
    args = parser.parse_args()
    session = boto3.Session(region_name="us-east-1")
    stack = session.client("cloudformation").describe_stacks(StackName="StewardNorthgateLive")[
        "Stacks"
    ][0]
    outputs = {row["OutputKey"]: row["OutputValue"] for row in stack["Outputs"]}
    credentials = json.loads((ROOT / ".scratch/steward-cloud-login.local.json").read_text())
    auth = session.client("cognito-idp").admin_initiate_auth(
        UserPoolId=outputs["UserPoolId"],
        ClientId=outputs["UserPoolClientId"],
        AuthFlow="ADMIN_USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": credentials["email"], "PASSWORD": credentials["password"]},
    )
    token = auth["AuthenticationResult"]["IdToken"]
    report = {"checked_at": datetime.now(timezone.utc).isoformat(), "url": outputs["ConsoleUrl"]}
    with httpx.Client(base_url=outputs["ConsoleUrl"], timeout=60) as client:
        public = client.get("/")
        assert public.status_code == 200 and "Steward" in public.text
        report["frontend"] = public.status_code
        anonymous = client.get("/api/me")
        assert anonymous.status_code == 401
        report["anonymous_api"] = anonymous.status_code
        client.headers["Authorization"] = "Bearer " + token
        for name in ("me", "memory", "cases", "tasks"):
            response = client.get("/api/" + name)
            assert response.status_code == 200, (name, response.status_code, response.text[:200])
            payload = response.json()
            if name == "me":
                assert payload["role"] == "manager" and payload["simulation"]
                report["principal"] = payload
            elif isinstance(payload, list):
                report[name + "_count"] = len(payload)
            else:
                report[name + "_shape"] = list(payload)
        if args.inject:
            response = client.post(
                "/api/simulation/messages",
                headers={"Idempotency-Key": "cloud-launch-elevator-smoke-v1"},
                json={
                    "sender": "James D.",
                    "text": (
                        "The A Block elevator is shuddering again near the fourth floor. "
                        "The doors keep reopening and nobody is trapped. "
                        "Please arrange an inspection."
                    ),
                },
            )
            assert response.status_code == 200, (response.status_code, response.text[:200])
            report["simulated_message"] = response.json()
    path = ROOT / "artifacts/validation/cloud-smoke.json"
    path.write_text(json.dumps(report, indent=2))
    print(json.dumps(report))


if __name__ == "__main__":
    main()
