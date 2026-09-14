"""Read-only live case and worker evidence; credentials are never printed."""

import json
from datetime import datetime, timezone
from pathlib import Path

import boto3
import httpx

ROOT = Path(__file__).resolve().parents[1]
s = boto3.Session(region_name="us-east-1")
stack = s.client("cloudformation").describe_stacks(StackName="StewardNorthgateLive")["Stacks"][0]
o = {i["OutputKey"]: i["OutputValue"] for i in stack["Outputs"]}
creds = json.loads((ROOT / ".scratch/steward-cloud-login.local.json").read_text())
auth = s.client("cognito-idp").admin_initiate_auth(
    UserPoolId=o["UserPoolId"],
    ClientId=o["UserPoolClientId"],
    AuthFlow="ADMIN_USER_PASSWORD_AUTH",
    AuthParameters={"USERNAME": creds["email"], "PASSWORD": creds["password"]},
)
report = {"checked_at": datetime.now(timezone.utc).isoformat(), "stack": stack["StackStatus"]}
with httpx.Client(
    base_url=o["ConsoleUrl"],
    headers={"Authorization": "Bearer " + auth["AuthenticationResult"]["IdToken"]},
    timeout=30,
) as client:
    response = client.get("/api/cases")
    response.raise_for_status()
    cases = response.json()
    report["cases"] = []
    for case in cases:
        response = client.get("/api/cases/" + case["case_id"])
        response.raise_for_status()
        report["cases"].append(response.json())
ecs = s.client("ecs")
task_ids = ecs.list_tasks(cluster=o["ClusterName"], serviceName=o["WorkerServiceName"])["taskArns"]
report["workers"] = []
for task in ecs.describe_tasks(cluster=o["ClusterName"], tasks=task_ids)["tasks"]:
    definition = ecs.describe_task_definition(taskDefinition=task["taskDefinitionArn"])[
        "taskDefinition"
    ]
    container = definition["containerDefinitions"][0]
    opt = container["logConfiguration"]["options"]
    stream = (
        opt["awslogs-stream-prefix"]
        + "/"
        + container["name"]
        + "/"
        + task["taskArn"].rsplit("/", 1)[1]
    )
    events = s.client("logs").get_log_events(
        logGroupName=opt["awslogs-group"], logStreamName=stream, limit=40
    )["events"]
    report["workers"].append(
        {
            "task": task["taskArn"],
            "status": task["lastStatus"],
            "logs": [e["message"] for e in events],
        }
    )
(ROOT / "artifacts/validation/cloud-runtime.json").write_text(json.dumps(report, indent=2))
print(
    json.dumps(
        {
            "stack": report["stack"],
            "cases": [
                {
                    "case_id": c["case_id"],
                    "status": c.get("status"),
                    "timeline_events": len(c.get("timeline", [])),
                    "outbox": len(c.get("outbox", [])),
                }
                for c in report["cases"]
            ],
            "workers": report["workers"],
        }
    )
)
