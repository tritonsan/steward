"""Inspect live Telegram delivery or explicitly queue one labeled group test.

Uses the existing manager login and normal API/outbox/worker boundary. Never sends
directly through the Bot API, prints credentials, or changes procurement authority.
"""

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import boto3
import httpx
from botocore.config import Config

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["status", "test"])
    args = parser.parse_args()
    session = boto3.Session(region_name="us-east-1")
    config = Config(connect_timeout=5, read_timeout=20, retries={"total_max_attempts": 2})
    stack = session.client("cloudformation", config=config).describe_stacks(
        StackName="StewardNorthgateLive"
    )["Stacks"][0]
    outputs = {r["OutputKey"]: r["OutputValue"] for r in stack["Outputs"]}
    assert outputs["ConsoleUrl"] == "https://d35nbywkoth58f.cloudfront.net"
    login = json.loads((ROOT / ".scratch/steward-cloud-login.local.json").read_text())
    auth = session.client("cognito-idp", config=config).admin_initiate_auth(
        UserPoolId=outputs["UserPoolId"],
        ClientId=outputs["UserPoolClientId"],
        AuthFlow="ADMIN_USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": login["email"], "PASSWORD": login["password"]},
    )
    report = {"checked_at": datetime.now(timezone.utc).isoformat(), "stack": stack["StackStatus"]}
    ecs = session.client("ecs", config=config)
    report["services"] = []
    for name in ("ApiServiceName", "WorkerServiceName"):
        service = ecs.describe_services(cluster=outputs["ClusterName"], services=[outputs[name]])[
            "services"
        ][0]
        definition = ecs.describe_task_definition(taskDefinition=service["taskDefinition"])[
            "taskDefinition"
        ]["containerDefinitions"][0]
        env = {e["name"]: e["value"] for e in definition.get("environment", [])}
        item = {
            "kind": "api" if name == "ApiServiceName" else "worker",
            "running": service["runningCount"],
            "pending": service["pendingCount"],
            "stable": all(d.get("rolloutState") == "COMPLETED" for d in service["deployments"]),
            "image": definition["image"].split(":")[-1],
            "mail_execution_mode": env.get("STEWARD_EXECUTION_MODE"),
            "telegram_delivery_mode": env.get("STEWARD_TELEGRAM_DELIVERY_MODE", "dry_run"),
        }
        report["services"].append(item)
        if args.action == "test":
            assert stack["StackStatus"] == "UPDATE_COMPLETE"
            assert item["stable"] and item["running"] == 1 and not item["pending"]
            assert item["mail_execution_mode"] == "dry_run"
            assert item["telegram_delivery_mode"] == "live"

    with httpx.Client(base_url=outputs["ConsoleUrl"], timeout=30, trust_env=False) as client:
        client.headers["Authorization"] = "Bearer " + auth["AuthenticationResult"]["IdToken"]
        response = client.get("/api/telegram/group")
        response.raise_for_status()
        status = response.json()
        report["group_connected"] = (status.get("connection") or {}).get("status") == "connected"
        report["telegram_delivery_mode"] = status.get("delivery_mode", "dry_run")
        report["delivery_paused"] = status.get("delivery_paused")
        report["test_delivery"] = status.get("test_delivery")
        if args.action == "test":
            assert report["group_connected"] and status["configured"]
            assert status["delivery_mode"] == "live" and not status["delivery_paused"]
            response = client.post(
                "/api/telegram/group",
                json={"action": "test_delivery", "expected_version": status["version"]},
            )
            response.raise_for_status()
            report["test_request"] = response.json()
    target = ROOT / "artifacts/validation/cloud-telegram-delivery.json"
    target.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
