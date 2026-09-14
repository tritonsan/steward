"""Reproduce the offline product acceptance evidence without cloud credentials.

Run from a source checkout after installing ``.[dev]``. Real model quality,
deployed services and channel delivery are deliberately separate measurements.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
TEST_FILES = (
    "tests/test_scenario_runs.py",
    "tests/test_four_stage_continuity.py",
    "tests/test_restart_workflow.py",
    "tests/test_failure_recovery.py",
    "tests/test_product_boundaries.py",
    "tests/test_resident_views.py",
    "tests/test_telegram_groups.py",
    "tests/test_mail.py",
    "tests/test_ses.py",
    "tests/test_vendor_replies.py",
    "tests/test_outbox_dispatcher.py",
    "tests/test_evaluation_gates.py",
    "tests/test_hackathon_acceptance.py",
)


def expected_scenarios() -> tuple[str, ...]:
    tree = ast.parse((ROOT / "tests/test_scenario_runs.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "SCENARIOS" for target in node.targets
        ):
            return ast.literal_eval(node.value)
    raise ValueError("scenario manifest is missing")


def inspect_reports(directory: Path, *, process_exit_code: int) -> dict:
    """A previous run, missing case, skipped test or failure can never pass."""
    junit_file = directory / "tests.xml"
    counts = {name: 0 for name in ("tests", "failures", "errors", "skipped")}
    errors = []
    try:
        suites = ET.parse(junit_file).getroot()
        for suite in suites.iter("testsuite"):
            for key in counts:
                counts[key] += int(suite.get(key, 0))
    except (OSError, ET.ParseError, ValueError) as exc:
        errors.append(f"JUnit report unavailable: {type(exc).__name__}")
    rows = []
    for file in sorted((directory / "scenarios").glob("*.json")):
        try:
            rows.append(json.loads(file.read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            errors.append(f"Invalid scenario report {file.name}: {type(exc).__name__}")
    identities = Counter((row.get("scenario"), row.get("repetition")) for row in rows)
    names = {name for name, _ in identities}
    matrix_complete = (
        names == set(expected_scenarios())
        and len(rows) == 60
        and all(identities[name, repeat] == 1 for name in names for repeat in (1, 2, 3))
    )
    verified = all(
        row.get("passed") is True
        and row.get("simulated_inputs") is True
        and row.get("live_model_calls") is False
        and row.get("outcome") in ("verified_outcome", "human_review")
        and row.get("api_events")
        for row in rows
    )
    passed = (
        process_exit_code == 0
        and not errors
        and counts["tests"] >= 60
        and not any(counts[key] for key in ("failures", "errors", "skipped"))
        and matrix_complete
        and verified
    )
    return {
        "passed": passed,
        "pytest_exit_code": process_exit_code,
        "tests": {
            **counts,
            "passed": counts["tests"] - sum(counts[k] for k in ("failures", "errors", "skipped")),
        },
        "scenarios": {
            "unique_scenarios": len(names),
            "runs": len(rows),
            "complete_twenty_by_three_matrix": matrix_complete,
            "successful_runs": sum(row.get("passed") is True for row in rows),
            "outcomes": dict(Counter(row.get("outcome", "missing") for row in rows)),
        },
        "errors": errors,
    }


def source_fingerprint() -> str:
    digest = hashlib.sha256()
    files = [*ROOT.joinpath("src").rglob("*.py"), *ROOT.joinpath("tests").rglob("*.py")]
    files.extend((ROOT / "requirements.lock", ROOT / "pyproject.toml", Path(__file__)))
    for file in sorted(files):
        digest.update(file.relative_to(ROOT).as_posix().encode("utf-8") + b"\0")
        digest.update(file.read_bytes() + b"\0")
    return digest.hexdigest()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="artifacts/validation/hackathon-acceptance.json")
    args = parser.parse_args(argv)
    output = Path(args.output).resolve()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid4().hex[:8]
    directory = output.parent / "hackathon-runs" / run_id
    directory.mkdir(parents=True, exist_ok=False)
    # Never inherit production stores, live transports, queues or model endpoints.
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith(("STEWARD_", "AWS_", "PYTEST_"))
    }
    env.update(
        {
            "AWS_EC2_METADATA_DISABLED": "true",
            "STEWARD_EXECUTION_MODE": "dry_run",
            "STEWARD_SCENARIO_REPORT_DIR": str(directory / "scenarios"),
        }
    )
    before = source_fingerprint()
    command = [
        sys.executable,
        "-X",
        "utf8",
        "-m",
        "pytest",
        *TEST_FILES,
        "--junitxml=" + str(directory / "tests.xml"),
    ]
    print("Running isolated API/worker scenarios and product boundary checks.", flush=True)
    result = subprocess.run(command, cwd=ROOT, env=env, check=False)
    after = source_fingerprint()
    report = inspect_reports(directory, process_exit_code=result.returncode)
    if before != after:
        report["passed"] = False
        report["errors"].append("Python source changed during validation; rerun on a stable tree.")
    report.update(
        {
            "run_id": run_id,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "python_source_sha256": after,
            "evidence_directory": str(directory.relative_to(output.parent)),
            "simulated_inputs": True,
            "live_model_calls": False,
            "real_channel_delivery": False,
            "scope": "Maintenance and community decisions, API roles, durable continuation, "
            "channel input validation and outbox boundaries on isolated SQLite stores.",
            "limitations": [
                "Model extraction and planning ports use deterministic test fixtures.",
                "Deployed AWS services and real Telegram/SES delivery need separate evidence.",
                "PostgreSQL, browser accessibility and live model quality need separate evidence.",
                "No measured field impact or reduction in human intervention is claimed.",
            ],
        }
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "passed": report["passed"],
                "report": str(output),
                "tests": report["tests"],
                "scenarios": report["scenarios"],
            }
        )
    )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
