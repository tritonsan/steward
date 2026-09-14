"""Controlled SES roundtrip using the production services and isolated local state.

Only management@ and test-vendor@ on the owned Steward subdomain participate.
No real supplier is contacted. Run explicitly with --run; evidence stays private
except for a redacted aggregate report. A temporary exact-case SES route prevents
the hosted worker from consuming these isolated test replies.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
from pathlib import Path
from uuid import uuid4

import boto3
from botocore.config import Config
from fastapi.testclient import TestClient

from steward.api import create_app
from steward.config import StewardSettings
from steward.domain.enums import ApprovalMode, Category
from steward.domain.models import ResidentMessage
from steward.mail import OutboundMessage, RecipientGuard
from steward.mail.receipt_queue import ReceiptQueue
from steward.mail.ses import SesMailTransport
from steward.runtime import build_runtime
from steward.seed import load_seed
from steward.store import InboxItem, InboxStatus
from steward.store.workflow import stable_id

ROOT = Path(__file__).resolve().parents[1]
DOMAIN = "steward.narrativenode-labs.cloud"
MANAGEMENT = "management@" + DOMAIN
VENDOR = "test-vendor@" + DOMAIN


class ControlledTransport:
    def __init__(self, delegate, receipts):
        self.delegate, self.receipts = delegate, receipts

    def send(self, message):
        if message.from_address != MANAGEMENT or message.all_recipients != (VENDOR,):
            raise ValueError("Controlled test permits only management -> test-vendor")
        marked = replace(
            message,
            body_text=(
                "CONTROLLED SOFTWARE TEST. No real work, visit or payment is requested.\n\n"
                + message.body_text
            ),
        )
        result = self.delegate.send(marked)
        self.receipts.append({"provider_id": result, "subject": marked.subject})
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    args = parser.parse_args()
    if not args.run:
        parser.error("Use --run explicitly to send controlled real emails")
    # Never inherit deployed databases, Telegram, virtual clocks or live member maps.
    for key in list(os.environ):
        if key.upper().startswith("STEWARD_"):
            del os.environ[key]
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:6]
    private = ROOT / ".scratch" / ("mail-roundtrip-" + run_id)
    private.mkdir(parents=True, exist_ok=False)
    session = boto3.Session(region_name="us-east-1")
    network = Config(connect_timeout=5, read_timeout=25, retries={"max_attempts": 1})
    clients = {
        name: session.client(name, config=network)
        for name in ("ses", "sns", "sqs", "s3", "cloudformation", "sts")
    }
    ses, sns, sqs, s3 = (clients[name] for name in ("ses", "sns", "sqs", "s3"))
    account = clients["sts"].get_caller_identity()["Account"]
    rows = clients["cloudformation"].list_stack_resources(StackName="StewardNorthgateLive")[
        "StackResourceSummaries"
    ]
    bucket = next(
        r["PhysicalResourceId"]
        for r in rows
        if r["ResourceType"] == "AWS::S3::Bucket"
        and r["LogicalResourceId"].startswith("MailArchive")
    )
    config_set = next(
        r["PhysicalResourceId"] for r in rows if r["ResourceType"] == "AWS::SES::ConfigurationSet"
    )
    active = ses.describe_active_receipt_rule_set()
    ruleset, original_rules = active["Metadata"]["Name"], active["Rules"]
    resource_name = "steward-mail-roundtrip-" + run_id.lower()
    topic = queue = runtime = client = None
    report = None
    try:
        topic = sns.create_topic(Name=resource_name)["TopicArn"]
        sns.set_topic_attributes(
            TopicArn=topic,
            AttributeName="Policy",
            AttributeValue=json.dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Principal": {"Service": "ses.amazonaws.com"},
                            "Action": "SNS:Publish",
                            "Resource": topic,
                            "Condition": {"StringEquals": {"AWS:SourceAccount": account}},
                        }
                    ],
                }
            ),
        )
        queue = sqs.create_queue(
            QueueName=resource_name,
            Attributes={"MessageRetentionPeriod": "86400", "SqsManagedSseEnabled": "true"},
        )["QueueUrl"]
        queue_arn = sqs.get_queue_attributes(QueueUrl=queue, AttributeNames=["QueueArn"])[
            "Attributes"
        ]["QueueArn"]
        sqs.set_queue_attributes(
            QueueUrl=queue,
            Attributes={
                "Policy": json.dumps(
                    {
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                "Effect": "Allow",
                                "Principal": {"Service": "sns.amazonaws.com"},
                                "Action": "sqs:SendMessage",
                                "Resource": queue_arn,
                                "Condition": {"ArnEquals": {"aws:SourceArn": topic}},
                            }
                        ],
                    }
                )
            },
        )
        sns.subscribe(TopicArn=topic, Protocol="sqs", Endpoint=queue_arn)
        state = {
            "run_id": run_id,
            "ruleset": ruleset,
            "rule": resource_name,
            "topic": topic,
            "queue": queue,
            "bucket": bucket,
            "stages": [],
        }
        state_path = private / "state.local.json"

        def record(stage, **details):
            state["stages"].append(
                {"stage": stage, "at": datetime.now(timezone.utc).isoformat(), **details}
            )
            state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
            print(json.dumps({"stage": stage, **details}), flush=True)

        record("isolated_resources_created")
        sent = []
        sender_client = session.client(
            "sesv2", config=Config(connect_timeout=5, read_timeout=25, retries={"max_attempts": 0})
        )
        transport = ControlledTransport(
            SesMailTransport(
                guard=RecipientGuard.of(DOMAIN),
                client=sender_client,
                configuration_set_name=config_set,
            ),
            sent,
        )
        bundle = load_seed()
        vendor = bundle.vendor("meridian-lift").model_copy(update={"email": VENDOR})
        policies = dict(bundle.policies)
        policies[Category.ELEVATOR] = policies[Category.ELEVATOR].model_copy(
            update={"mode": ApprovalMode.PREPARE_ONLY, "allowed_vendor_ids": [vendor.vendor_id]}
        )
        bundle = replace(bundle, vendors=(vendor,), policies=policies, history=())
        settings = StewardSettings(
            database_path=private / "runtime.db",
            execution_mode="live_commitment",
            allow_live_commitments=True,
            management_domain=DOMAIN,
            vendor_domains=(DOMAIN,),
            ses_configuration_set_name=config_set,
            ses_inbound_enabled=True,
            ses_inbound_bucket=bucket,
            ses_queue_url=queue,
        )
        runtime = build_runtime(settings, seed=bundle, transport=transport)
        client = TestClient(
            create_app(
                runtime,
                simulation=False,
                tokens={"controlled-local-review": {"actor_id": "Simon O.", "role": "manager"}},
            )
        )
        client.headers["Authorization"] = "Bearer controlled-local-review"

        def post(path, body):
            response = client.post(
                "/api" + path, json=body, headers={"Idempotency-Key": run_id + "-" + uuid4().hex}
            )
            response.raise_for_status()
            return response.json()

        def tick():
            result = runtime.tick()
            if result.workflow_failures:
                record("worker_failure", failures=list(result.workflow_failures))
                raise RuntimeError("Inspect isolated workflow job evidence")

        def mailbox(provider_id):
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                objects = s3.list_objects_v2(Bucket=bucket, Prefix="mail/test-vendor/").get(
                    "Contents", []
                )
                for row in sorted(objects, key=lambda x: x["LastModified"], reverse=True)[:12]:
                    raw = s3.get_object(Bucket=bucket, Key=row["Key"])["Body"].read()
                    msg = BytesParser(policy=policy.default).parsebytes(raw)
                    if provider_id in str(msg.get("Message-ID", "")):
                        (private / (provider_id + ".eml")).write_bytes(raw)
                        return msg
                time.sleep(3)
            raise TimeoutError("Controlled vendor mailbox delivery not found")

        def reply(original, text, label):
            target = str(original["Reply-To"])
            if target != state["reply_address"]:
                raise ValueError("Unexpected reply target")
            result = SesMailTransport(
                guard=RecipientGuard.of(DOMAIN),
                client=sender_client,
                configuration_set_name=config_set,
            ).send(
                OutboundMessage(
                    from_address=VENDOR,
                    to=(target,),
                    subject="Re: " + str(original["Subject"]),
                    body_text=text,
                    in_reply_to=str(original["Message-ID"]),
                    references=(str(original["Message-ID"]),),
                )
            )
            state.setdefault("vendor_receipts", []).append(result)
            record(label + "_sent")
            receipt_queue = ReceiptQueue(runtime, queue_url=queue, client=sqs)
            deadline = time.monotonic() + 150
            while time.monotonic() < deadline:
                if receipt_queue.poll():
                    # Creating an SES S3 notification emits a provider setup
                    # probe, not an actual vendor message. The runtime correctly
                    # quarantines its absent DMARC result; keep that evidence,
                    # but do not confuse it with the reply we are waiting for.
                    probe = stable_id("ses-receipt", "AMAZON_SES_SETUP_NOTIFICATION")
                    receipts = [
                        a
                        for a in runtime.store.artifacts_for(kind="ses.receipt.completed.v1")
                        if a.artifact_id != probe
                    ]
                    if len(receipts) < len(state["vendor_receipts"]):
                        continue
                    latest = receipts[-1].payload
                    if latest["status"] != "queued":
                        record("receipt_review", **latest)
                        raise RuntimeError("Controlled receipt was not queued")
                    tick()
                    record(label + "_processed", verdicts=latest["verdicts"])
                    return
                time.sleep(3)
            raise TimeoutError("Real SES receipt did not reach the isolated queue")

        # The public simulation API correctly forbids real outbound mode. Feed a
        # labelled synthetic external event into the normal durable inbox instead;
        # the worker alone creates and advances the case.
        now = datetime.now(timezone.utc)
        message = ResidentMessage(
            message_id=run_id,
            source="controlled_mail_test",
            chat_id="controlled-mail-test",
            sender_display="James D.",
            text="The A Block elevator shudders at the fourth floor and the doors reopen. "
            "Nobody is trapped. Please inspect the guide shoes and rail alignment.",
            sent_at=now,
            ingested_at=now,
        )
        runtime.store.record_inbound(
            InboxItem(
                source=message.source,
                external_id=run_id,
                status=InboxStatus.PENDING,
                payload_hash=hashlib.sha256(message.model_dump_json().encode()).hexdigest(),
                received_at=now,
                message=message,
            )
        )
        tick()
        case = runtime.store.list_cases()[0]
        state["case_id"] = case.case_id
        state["reply_address"] = "case-" + case.reply_token + "@" + DOMAIN
        ses.create_receipt_rule(
            RuleSetName=ruleset,
            Rule={
                "Name": resource_name,
                "Enabled": True,
                "TlsPolicy": "Require",
                "Recipients": [state["reply_address"]],
                "ScanEnabled": True,
                "Actions": [
                    {
                        "S3Action": {
                            "BucketName": bucket,
                            "ObjectKeyPrefix": "mail/",
                            "TopicArn": topic,
                        }
                    },
                    {"StopAction": {"Scope": "RuleSet"}},
                ],
            },
        )
        assert all(r in ses.describe_active_receipt_rule_set()["Rules"] for r in original_rules)
        record("isolated_route_ready", original_rules_preserved=True)
        assert sent, "The real RFQ was not dispatched"
        rfq = mailbox(sent[0]["provider_id"])
        record("rfq_delivered", received_from=MANAGEMENT, received_by=VENDOR)
        start = (datetime.now(timezone.utc) + timedelta(days=2)).replace(
            hour=10, minute=0, second=0, microsecond=0
        )
        end = start + timedelta(hours=1)
        valid = datetime.now(timezone.utc) + timedelta(days=7)
        await_text = (
            "Total price: 705 USD.\n"
            "Scope: inspect and correct guide shoes and rail alignment.\n"
            "Included in this total: all labour, travel, replacement guide shoes, "
            "rail alignment, testing and taxes.\n"
            "Exclusions: none. No additional charges.\n"
            f"Available {start.isoformat()} to {end.isoformat()}.\n"
            f"This quote is valid until {valid.isoformat()}.\n"
            "This is a controlled software test; no actual work or payment will occur."
        )
        reply(rfq, await_text, "quote")
        assert runtime.store.artifacts_for(kind="vendor_quote.v1", case_id=case.case_id)
        # Explicit operator review tests transport now; it does not claim the
        # automatic 24-hour comparison timer has elapsed or alter any timestamps.
        decision = runtime.decide_quotes(case.case_id)
        post(
            f"/cases/{case.case_id}/commands",
            {
                "action": "approve_quote",
                "expected_version": runtime.store.case_version(case.case_id),
                "notes": "Controlled mailbox test only; no real service or payment is requested.",
                "data": {"decision_id": decision.decision.decision_id},
            },
        )
        tick()
        record("order_sent", status=runtime.store.get_case(case.case_id).status.value)
        order_receipt = next(r for r in sent if r["subject"].startswith("Service order:"))
        order = mailbox(order_receipt["provider_id"])
        record("order_delivered")
        # Restart the runtime before receiving a normal Reply to the SES order.
        client.close()
        runtime.close()
        runtime = build_runtime(settings, seed=bundle, transport=transport)
        client = TestClient(
            create_app(
                runtime,
                simulation=False,
                tokens={"controlled-local-review": {"actor_id": "Simon O.", "role": "manager"}},
            )
        )
        client.headers["Authorization"] = "Bearer controlled-local-review"
        rules = client.get("/api/appointment-rules").json()
        response = client.put(
            "/api/appointment-rules",
            headers={"Idempotency-Key": run_id + "-hours"},
            json={
                "expected_version": rules["version"] + 1,
                "action": "configure",
                "data": {
                    "weekdays": list(range(7)),
                    "start_hour": 8,
                    "end_hour": 20,
                    "minimum_notice_hours": 1,
                    "access_instructions": "Use the management entrance.",
                },
            },
        )
        response.raise_for_status()
        record("runtime_restarted", order_preserved=True)
        reply(
            order,
            f"We accept the same price of 705 USD and the same scope without changes. "
            f"Appointment starts at {start.isoformat()} and ends at {end.isoformat()}. "
            "Access arrangements agreed: use the management entrance. "
            "Controlled software test only; no real visit or payment.",
            "appointment",
        )
        tick()
        current = runtime.store.get_case(case.case_id)
        assert current.status.value == "scheduled", current.status.value
        acceptance = mailbox(sent[-1]["provider_id"])
        assert "appointment" in str(acceptance["Subject"]).lower()
        assert len([r for r in sent if r["subject"].startswith("Service order:")]) == 1
        assert len(runtime.store.reservations()) == 1
        record(
            "appointment_confirmation_delivered",
            status=current.status.value,
            logical_orders=1,
            budget_reservations=len(runtime.store.reservations()),
        )
        report = {
            "run_id": run_id,
            "passed": True,
            "real_ses_delivery": True,
            "real_s3_sns_sqs_ingress": True,
            "real_model_extraction": True,
            "local_isolated_runtime": True,
            "synthetic_scenario": True,
            "actual_supplier_contacted": False,
            "real_work_or_payment": False,
            "management_address": MANAGEMENT,
            "vendor_address": VENDOR,
            "management_messages_delivered": len(sent),
            "vendor_replies_processed": len(state.get("vendor_receipts", [])),
            "runtime_restart_verified": True,
            "final_status": current.status.value,
            "stages": state["stages"],
            "limitations": [
                "An operator requested quote comparison and approved the controlled order; "
                "the normal automatic 24-hour waiting period was not measured.",
                "Application services ran locally with isolated SQLite state "
                "and real AWS channels.",
                "The hosted Northgate demo still uses dry-run outbound delivery.",
            ],
        }
        target = ROOT / "artifacts/validation/controlled-mail-roundtrip.json"
    finally:
        if client is not None:
            client.close()
        if runtime is not None:
            runtime.close()
        # Remove only the temporary exact-case route and queue/topic created above.
        rules = ses.describe_active_receipt_rule_set()["Rules"]
        if any(r["Name"] == resource_name for r in rules):
            ses.delete_receipt_rule(RuleSetName=ruleset, RuleName=resource_name)
        if queue is not None:
            sqs.delete_queue(QueueUrl=queue)
        if topic is not None:
            sns.delete_topic(TopicArn=topic)
        assert ses.describe_active_receipt_rule_set()["Rules"] == original_rules
        if "record" in locals():
            record("temporary_routes_removed", original_rules_preserved=True)
    report["cleanup_verified"] = True
    target.write_text(json.dumps(report, indent=2), encoding="utf-8")
    record("passed", report=str(target.relative_to(ROOT)))


if __name__ == "__main__":
    main()
