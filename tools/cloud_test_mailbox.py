"""A private SES/S3 vendor test mailbox, with explicit operator-only sends."""

import argparse
import json
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from pathlib import Path
from uuid import uuid4

import boto3
from botocore.config import Config

ROOT = Path(__file__).resolve().parents[1]
ADDRESS = "test-vendor@steward.narrativenode-labs.cloud"
MANAGEMENT = "management@steward.narrativenode-labs.cloud"
PREFIX = "mail/test-vendor/"
RULE = "steward-test-vendor-mailbox"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["create", "list", "read", "send", "smoke"])
    parser.add_argument("--key")
    parser.add_argument("--subject")
    parser.add_argument("--body-file", type=Path)
    args = parser.parse_args()
    session = boto3.Session(region_name="us-east-1")
    config = Config(connect_timeout=5, read_timeout=20, retries={"max_attempts": 1})
    cf, ses, s3 = (session.client(name, config=config) for name in ("cloudformation", "ses", "s3"))
    rows = cf.list_stack_resources(StackName="StewardNorthgateLive")["StackResourceSummaries"]
    bucket = next(
        r["PhysicalResourceId"]
        for r in rows
        if r["ResourceType"] == "AWS::S3::Bucket"
        and r["LogicalResourceId"].startswith("MailArchive")
    )
    active = ses.describe_active_receipt_rule_set()
    ruleset = active.get("Metadata", {}).get("Name")
    if not ruleset:
        raise RuntimeError("Expected an existing SES active rule set")
    if args.action == "create":
        rule = {
            "Name": RULE,
            "Enabled": True,
            "TlsPolicy": "Require",
            "Recipients": [ADDRESS],
            "ScanEnabled": True,
            "Actions": [
                {"S3Action": {"BucketName": bucket, "ObjectKeyPrefix": PREFIX}},
                {"StopAction": {"Scope": "RuleSet"}},
            ],
        }
        existing = next((r for r in active["Rules"] if r["Name"] == RULE), None)
        if existing and existing != rule:
            raise RuntimeError("Existing mailbox rule differs; review before changing it")
        if not existing:
            ses.create_receipt_rule(RuleSetName=ruleset, Rule=rule)
        saved = ses.describe_active_receipt_rule_set()["Rules"]
        assert all(r in saved for r in active["Rules"])
        assert next(i for i, r in enumerate(saved) if r["Name"] == RULE) < next(
            i for i, r in enumerate(saved) if r["Name"] == "steward-northgate-intake"
        )
        report = {
            "address": ADDRESS,
            "bucket": bucket,
            "prefix": PREFIX,
            "ruleset": ruleset,
            "rule": RULE,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "webmail_login": False,
        }
        (ROOT / ".scratch/cloud-test-mailbox.local.json").write_text(json.dumps(report, indent=2))
        print(json.dumps({"address": ADDRESS, "configured": True, "other_rules_preserved": True}))
    elif args.action == "list":
        messages = []
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=PREFIX):
            messages.extend(
                {"key": o["Key"], "received_at": o["LastModified"].isoformat(), "bytes": o["Size"]}
                for o in page.get("Contents", [])
                if not o["Key"].endswith("AMAZON_SES_SETUP_NOTIFICATION")
            )
        print(json.dumps(messages))
    elif args.action == "read":
        if not args.key or not args.key.startswith(PREFIX):
            parser.error("--key must identify a message in the test mailbox prefix")
        with s3.get_object(Bucket=bucket, Key=args.key)["Body"] as body:
            message = BytesParser(policy=policy.default).parsebytes(body.read())
        plain = message.get_body(preferencelist=("plain",))
        print(
            json.dumps(
                {
                    "from": message.get("From"),
                    "to": message.get("To"),
                    "subject": message.get("Subject"),
                    "body": plain.get_content() if plain else None,
                }
            )
        )
    else:
        # Smoke goes only to this same controlled mailbox. Explicit send goes only
        # to Steward management; no arbitrary recipient or paid work is supported.
        if args.action == "smoke":
            subject = "Steward mailbox verification " + uuid4().hex[:12]
            text = (
                "Controlled mailbox delivery test. "
                "No quote, service order, or payment is requested."
            )
            recipient = ADDRESS
        else:
            if not args.subject or not args.body_file:
                parser.error("send requires --subject and --body-file")
            subject, text, recipient = (
                args.subject,
                args.body_file.read_text(encoding="utf-8"),
                MANAGEMENT,
            )
        # No automatic send retries: a timeout may mean delivery is ambiguous.
        sender = session.client(
            "ses", config=Config(connect_timeout=5, read_timeout=20, retries={"max_attempts": 0})
        )
        result = sender.send_email(
            Source=ADDRESS,
            Destination={"ToAddresses": [recipient]},
            Message={
                "Subject": {"Data": subject, "Charset": "UTF-8"},
                "Body": {"Text": {"Data": text, "Charset": "UTF-8"}},
            },
        )
        report = {
            "from": ADDRESS,
            "to": recipient,
            "subject": subject,
            "ses_message_id": result["MessageId"],
            "sent_at": datetime.now(timezone.utc).isoformat(),
        }
        (
            ROOT
            / (
                ".scratch/cloud-mailbox-smoke.local.json"
                if args.action == "smoke"
                else ".scratch/cloud-mailbox-send.local.json"
            )
        ).write_text(json.dumps(report, indent=2))
        print(json.dumps(report))


if __name__ == "__main__":
    main()
