"""Configure isolated judge access on the existing stack, without owner credentials.

Secrets remain in the existing registry and ignored .scratch operator files.
No resources, emails or service restarts are created by configure. Deploy with
reviewEnabled=true afterwards, then explicitly bootstrap the isolated dataset.
"""

import argparse
import json
import secrets
from pathlib import Path

import boto3
from botocore.config import Config

ROOT = Path(__file__).resolve().parents[1]
STACK = "StewardNorthgateLive"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["configure", "bootstrap", "status"])
    args = parser.parse_args()
    session = boto3.Session(region_name="us-east-1")
    config = Config(connect_timeout=5, read_timeout=20, retries={"max_attempts": 1})
    cf = session.client("cloudformation", config=config)
    stack = cf.describe_stacks(StackName=STACK)["Stacks"][0]
    outputs = {row["OutputKey"]: row["OutputValue"] for row in stack.get("Outputs", [])}
    if stack["StackStatus"] not in ("CREATE_COMPLETE", "UPDATE_COMPLETE"):
        raise RuntimeError("Wait for a successful stack deployment")
    scratch = ROOT / ".scratch"
    scratch.mkdir(exist_ok=True)
    if args.action == "configure":
        sm = session.client("secretsmanager", config=config)
        value = sm.get_secret_value(SecretId=outputs["MemberRegistrySecret"])
        registry = json.loads(value["SecretString"])
        if not isinstance(registry, dict):
            raise RuntimeError("Unexpected member registry; no changes made")
        code = registry.get("__review_access_code")
        if code is None:
            code = secrets.token_urlsafe(32)
            (scratch / "review-registry-before.local.json").write_text(
                value["SecretString"], encoding="utf-8"
            )
            registry["__review_access_code"] = code
            sm.put_secret_value(
                SecretId=outputs["MemberRegistrySecret"], SecretString=json.dumps(registry)
            )
        if not isinstance(code, str) or len(code) < 24:
            raise RuntimeError("Invalid existing review code; refusing automatic rotation")
        access = {
            "url": outputs["ConsoleUrl"].rstrip("/") + "/?mode=judge",
            "public_url": outputs["ConsoleUrl"].rstrip("/") + "/?mode=preview",
            "access_code": code,
            "scope": "isolated synthetic review workspace; no live channel delivery",
        }
        (scratch / "steward-review-access.local.json").write_text(
            json.dumps(access, indent=2), encoding="utf-8"
        )
        print("Review access configured; saved only to .scratch/steward-review-access.local.json")
        return
    ecs = session.client("ecs", config=config)
    if args.action == "status":
        task_file = scratch / "review-bootstrap-task.local.json"
        if not task_file.exists():
            print("Review bootstrap has not been started from this checkout.")
            return
        saved = json.loads(task_file.read_text(encoding="utf-8"))
        tasks = ecs.describe_tasks(cluster=outputs["ClusterName"], tasks=[saved["task_arn"]])
        for task in tasks.get("tasks", []):
            print(
                json.dumps(
                    {
                        "status": task["lastStatus"],
                        "containers": [
                            {"name": c["name"], "exit_code": c.get("exitCode")}
                            for c in task["containers"]
                        ],
                    }
                )
            )
        return
    service = ecs.describe_services(
        cluster=outputs["ClusterName"], services=[outputs["WorkerServiceName"]]
    )["services"][0]
    definition = ecs.describe_task_definition(taskDefinition=service["taskDefinition"])[
        "taskDefinition"
    ]
    container = definition["containerDefinitions"][0]
    environment = {v["name"]: v["value"] for v in container.get("environment", [])}
    if environment.get("STEWARD_REVIEW_ENABLED") != "true":
        raise RuntimeError("Deploy the review-enabled worker before bootstrap")
    result = ecs.run_task(
        cluster=outputs["ClusterName"],
        launchType="FARGATE",
        taskDefinition=service["taskDefinition"],
        networkConfiguration=service["networkConfiguration"],
        count=1,
        startedBy="steward-review-bootstrap",
        overrides={
            "containerOverrides": [
                {"name": container["name"], "command": ["python", "-m", "steward.demo.review_seed"]}
            ]
        },
    )
    if result.get("failures") or not result.get("tasks"):
        raise RuntimeError("Review bootstrap task could not be started")
    (scratch / "review-bootstrap-task.local.json").write_text(
        json.dumps(
            {
                "task_arn": result["tasks"][0]["taskArn"],
                "log_group": container["logConfiguration"]["options"]["awslogs-group"],
                "container": container["name"],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print("Isolated review bootstrap started; use status to verify completion.")


if __name__ == "__main__":
    main()
