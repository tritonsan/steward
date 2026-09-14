"""Read actual group receipt and case evidence without model or channel mutations."""

import json
from datetime import datetime, timezone
from pathlib import Path

import boto3
import httpx
from botocore.config import Config

ROOT = Path(__file__).resolve().parents[1]
s = boto3.Session(region_name="us-east-1")
cfg = Config(connect_timeout=5, read_timeout=15, retries={"max_attempts": 1})
stack = s.client("cloudformation", config=cfg).describe_stacks(StackName="StewardNorthgateLive")[
    "Stacks"
][0]
outputs = {r["OutputKey"]: r["OutputValue"] for r in stack["Outputs"]}
login = json.loads((ROOT / ".scratch/steward-cloud-login.local.json").read_text())
auth = s.client("cognito-idp", config=cfg).admin_initiate_auth(
    UserPoolId=outputs["UserPoolId"],
    ClientId=outputs["UserPoolClientId"],
    AuthFlow="ADMIN_USER_PASSWORD_AUTH",
    AuthParameters={"USERNAME": login["email"], "PASSWORD": login["password"]},
)
with httpx.Client(
    base_url=outputs["ConsoleUrl"],
    headers={"Authorization": "Bearer " + auth["AuthenticationResult"]["IdToken"]},
    timeout=20,
    trust_env=False,
) as client:
    response = client.get("/api/telegram/group")
    response.raise_for_status()
    status = response.json()
    cases = []
    for cid in {cid for message in status["recent_messages"] for cid in message["case_ids"]}:
        response = client.get("/api/cases/" + cid)
        response.raise_for_status()
        case = response.json()
        cases.append(
            {
                "case_id": cid,
                "status": case["status"],
                "title": case.get("title"),
                "timeline": [
                    {"kind": e["kind"], "summary": e["summary"]} for e in case["timeline"]
                ],
            }
        )
report = {
    "checked_at": datetime.now(timezone.utc).isoformat(),
    "stack": stack["StackStatus"],
    "status": status,
    "cases": cases,
}
(ROOT / "artifacts/validation/cloud-telegram-current.json").write_text(json.dumps(report, indent=2))
print(json.dumps(report))
