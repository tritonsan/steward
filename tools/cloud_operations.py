"""Explicit operational commands for the deployed Steward stack.

Credentials and identifiers stay in ignored .scratch files; no email is sent.
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
    parser.add_argument("action", choices=["status", "bootstrap", "index", "user"])
    parser.add_argument("--email")
    parser.add_argument("--defer-restart", action="store_true")
    args = parser.parse_args()
    session = boto3.Session(region_name="us-east-1")
    config = Config(connect_timeout=5, read_timeout=20, retries={"max_attempts": 1})
    cf = session.client("cloudformation", config=config)
    stack = cf.describe_stacks(StackName=STACK)["Stacks"][0]
    outputs = {row["OutputKey"]: row["OutputValue"] for row in stack.get("Outputs", [])}
    if args.action == "status":
        print(json.dumps({"status": stack["StackStatus"], "url": outputs.get("ConsoleUrl")}))
        if stack["StackStatus"].endswith("IN_PROGRESS") or "FAILED" in stack["StackStatus"]:
            for event in cf.describe_stack_events(StackName=STACK)["StackEvents"][:8]:
                print(
                    event["LogicalResourceId"],
                    event["ResourceStatus"],
                    event.get("ResourceStatusReason", "")[:300],
                )
        if "ClusterName" in outputs:
            ecs = session.client("ecs", config=config)
            for service in ecs.describe_services(
                cluster=outputs["ClusterName"],
                services=[outputs["ApiServiceName"], outputs["WorkerServiceName"]],
            )["services"]:
                print(
                    json.dumps(
                        {
                            "service": service["serviceName"],
                            "running": service["runningCount"],
                            "pending": service["pendingCount"],
                            "events": [e["message"] for e in service["events"][:2]],
                        }
                    )
                )
        return
    if stack["StackStatus"] not in ("CREATE_COMPLETE", "UPDATE_COMPLETE"):
        raise RuntimeError("Wait for a successful stack deployment")
    ecs = session.client("ecs", config=config)
    if args.action in ("bootstrap", "index"):
        service = ecs.describe_services(
            cluster=outputs["ClusterName"], services=[outputs["WorkerServiceName"]]
        )["services"][0]
        definition = ecs.describe_task_definition(taskDefinition=service["taskDefinition"])[
            "taskDefinition"
        ]
        result = ecs.run_task(
            cluster=outputs["ClusterName"],
            launchType="FARGATE",
            taskDefinition=service["taskDefinition"],
            networkConfiguration=service["networkConfiguration"],
            count=1,
            startedBy="steward-explicit-bootstrap",
            overrides={
                "containerOverrides": [
                    {
                        "name": definition["containerDefinitions"][0]["name"],
                        "command": [
                            "steward-bootstrap"
                            if args.action == "bootstrap"
                            else "steward-index-memory"
                        ],
                    }
                ]
            },
        )
        if result.get("failures"):
            raise RuntimeError(result["failures"])
        path = ROOT / f".scratch/cloud-{args.action}-task.local.json"
        path.write_text(
            json.dumps({"cluster": outputs["ClusterName"], "task": result["tasks"][0]["taskArn"]})
        )
        print("Task started; reference saved to", str(path))
        return
    if not args.email:
        parser.error("--email is required for user provisioning")
    cognito = session.client("cognito-idp", config=config)
    pool = outputs["UserPoolId"]
    email = args.email.strip().lower()
    try:
        user = cognito.admin_get_user(UserPoolId=pool, Username=email)
        attributes = user["UserAttributes"]
    except cognito.exceptions.UserNotFoundException:
        user = cognito.admin_create_user(
            UserPoolId=pool,
            Username=email,
            MessageAction="SUPPRESS",
            UserAttributes=[{"Name": "email", "Value": email}],
        )["User"]
        attributes = user["Attributes"]
    sub = next(a["Value"] for a in attributes if a["Name"] == "sub")
    password = secrets.token_urlsafe(25) + "aA1!"
    cognito.admin_set_user_password(
        UserPoolId=pool, Username=email, Password=password, Permanent=True
    )
    sm = session.client("secretsmanager", config=config)
    registry = json.loads(
        sm.get_secret_value(SecretId=outputs["MemberRegistrySecret"])["SecretString"]
    )
    registry[sub] = {"actor_id": "Simon O.", "role": "manager"}
    sm.put_secret_value(SecretId=outputs["MemberRegistrySecret"], SecretString=json.dumps(registry))
    # ECS reads Secrets Manager values at task startup.
    if not args.defer_restart:
        for service_name in (outputs["ApiServiceName"], outputs["WorkerServiceName"]):
            ecs.update_service(
                cluster=outputs["ClusterName"], service=service_name, forceNewDeployment=True
            )
    private = ROOT / ".scratch/steward-cloud-login.local.json"
    private.write_text(
        json.dumps({"url": outputs["ConsoleUrl"], "email": email, "password": password}, indent=2)
    )
    print("Manager provisioned without email. Credentials saved to", str(private))


if __name__ == "__main__":
    main()
