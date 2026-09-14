"""Add only Steward's narrow recipient rule to the existing SES active rule set."""

import json
from pathlib import Path

import boto3
from botocore.config import Config

ROOT = Path(__file__).resolve().parents[1]
session = boto3.Session(region_name="us-east-1")
config = Config(connect_timeout=5, read_timeout=20, retries={"max_attempts": 1})
cf = session.client("cloudformation", config=config)
resources = cf.list_stack_resources(StackName="StewardNorthgateLive")["StackResourceSummaries"]
source = next(
    r["PhysicalResourceId"] for r in resources if r["ResourceType"] == "AWS::SES::ReceiptRuleSet"
)
ses = session.client("ses", config=config)
rules = ses.describe_receipt_rule_set(RuleSetName=source)["Rules"]
assert len(rules) == 1 and rules[0]["Recipients"] == ["steward.narrativenode-labs.cloud"]
active = ses.describe_active_receipt_rule_set()
target = active.get("Metadata", {}).get("Name")
if not target:
    ses.set_active_receipt_rule_set(RuleSetName=source)
    target = source
elif target != source:
    rule = {**rules[0], "Name": "steward-northgate-intake"}
    existing = next((r for r in active.get("Rules", []) if r["Name"] == rule["Name"]), None)
    if existing and existing != rule:
        raise RuntimeError("Existing Steward rule differs; review before replacing")
    if not existing:
        # Preserve other projects and put this recipient-specific rule first.
        ses.create_receipt_rule(RuleSetName=target, Rule=rule)
result = ses.describe_active_receipt_rule_set()
assert result["Metadata"]["Name"] == target
for previous in active.get("Rules", []):
    assert previous in result["Rules"], "An unrelated active rule changed"
report = {
    "source_ruleset": source,
    "active_ruleset": target,
    "steward_recipient": rules[0]["Recipients"][0],
    "existing_rules_preserved": True,
    "dns_verification_required": True,
}
(ROOT / "artifacts/validation/cloud-mail-routing.json").write_text(json.dumps(report, indent=2))
print(json.dumps(report))
