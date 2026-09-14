"""Collect existing verification evidence; never turns an unrun gate into a pass."""

import json
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "artifacts" / "validation"


def junit(name):
    suites = ET.parse(REPORTS / name).getroot()
    values = {
        key: sum(int(s.get(key, 0)) for s in suites.iter("testsuite"))
        for key in ("tests", "failures", "errors", "skipped")
    }
    values["passed"] = values["tests"] - sum(values[k] for k in ("failures", "errors", "skipped"))
    return values


def read(name):
    return json.loads((REPORTS / name).read_text(encoding="utf-8"))


def latest_junit(*names):
    available = [REPORTS / name for name in names if (REPORTS / name).is_file()]
    if not available:
        return {"available": False}
    chosen = max(available, key=lambda path: path.stat().st_mtime_ns)
    return {"report": chosen.name, **junit(chosen.name)}


def main():
    triage = read("triage-reserve-v2.json")
    quotes = read("quote-portfolios-regression.json")
    scenario_runs = [
        json.loads(p.read_text(encoding="utf-8"))
        for p in (REPORTS / "scenario-runs-v1").glob("*.json")
    ]
    template_path = ROOT / "infra/cdk.out/StewardNorthgate.template.json"
    template = (
        json.loads(template_path.read_text(encoding="utf-8")) if template_path.is_file() else None
    )
    telegram = (
        read("cloud-telegram-current.json")
        if (REPORTS / "cloud-telegram-current.json").is_file()
        else {}
    )
    mailbox = (
        read("cloud-test-mailbox.json") if (REPORTS / "cloud-test-mailbox.json").is_file() else {}
    )
    offline = (
        read("hackathon-acceptance.json")
        if (REPORTS / "hackathon-acceptance.json").is_file()
        else None
    )
    summary = {
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "full_plan_complete": False,
        "python": latest_junit(
            "python-tests.xml", "telegram-live-fix-regression.xml", "final-audit-regression.xml"
        ),
        "postgres": latest_junit(
            "postgres-tests.xml", "four-stage-postgres.xml", "hackathon-postgres-current.xml"
        ),
        "offline_product_acceptance": offline,
        "scenarios": {
            **latest_junit("scenarios.xml", "hackathon-scenarios-current.xml"),
            "unique_scenarios": len({r["scenario"] for r in scenario_runs}),
            "runs": len(scenario_runs),
            "successful_runs": sum(r["passed"] for r in scenario_runs),
            "live_model_calls": False,
        },
        "live_reserved_triage": {k: triage[k] for k in ("sample_count", "accuracy", "passed")},
        "live_quote_extraction_regression": {
            k: quotes[k]
            for k in ("portfolio_count", "quote_count", "passing_quotes", "passing_portfolios")
        },
        "live_memory_counterfactual_passed": read("memory-counterfactuals.json")["passed"],
        "semantic_index": read("semantic-index.json"),
        "sqlite_restore_passed": read("sqlite-restore.json")["passed"],
        "postgres_restore_passed": read("postgres-restore.json")["passed"],
        "cdk": {
            "synthesized_resources": len(template["Resources"]) if template else None,
            "deployed": telegram.get("stack") in ("CREATE_COMPLETE", "UPDATE_COMPLETE"),
            "deployment_evidence_recorded_at": telegram.get("checked_at"),
        },
        "live_channel_evidence": {
            "telegram_processed_messages": sum(
                message.get("status") == "processed"
                for message in telegram.get("status", {}).get("recent_messages", [])
            ),
            "ses_controlled_mailbox_roundtrip": bool(
                mailbox.get("ses_send_accepted")
                and mailbox.get("received_in_s3")
                and mailbox.get("subject_matched")
                and mailbox.get("dmarc_pass")
            ),
            "mailbox_scope": "Self-addressed transport test; not a case quote lifecycle",
        },
        "web": read("web-review.json"),
        "role_ui_refinement": read("role-ui-review.json"),
        "four_stage_continuity": read("four-stage-continuity.json"),
        "open_acceptance_gates": [
            "Controlled live four-stage channel run and reserved new-type model evaluation",
            "Matched manual/memoryless/full-Steward impact benchmark",
            "Full mobile/desktop accessibility and usability acceptance",
            "Exhaustive crash injection across all persistence boundaries",
            "RDS recovery and previous application image rollback",
        ],
        "limitations": [
            "All community and vendor evaluation inputs are synthetic.",
            "Only reports explicitly marked live used provider inference.",
            "Quote regression measures extraction, not complete decision quality.",
            "The reserved triage corpus is synthetic with shared templates; it is now measured.",
            "Local PostgreSQL required restarting WSL; startup failure report is retained.",
            "No measured field impact or 50 percent intervention reduction is claimed.",
            "Evidence reports were produced during development, not all from one final build.",
        ],
    }
    destination = REPORTS / "summary.json"
    destination.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"summary": str(destination), "full_plan_complete": False}))


if __name__ == "__main__":
    main()
