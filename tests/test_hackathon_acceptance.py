"""The offline evidence gate cannot certify missing, duplicated or skipped work."""

import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "acceptance_tool", Path(__file__).resolve().parents[1] / "tools/hackathon_acceptance.py"
)
acceptance = importlib.util.module_from_spec(spec)
spec.loader.exec_module(acceptance)


def reports(directory, *, skipped=0):
    (directory / "tests.xml").write_text(
        f'<testsuites><testsuite tests="61" failures="0" errors="0" skipped="{skipped}"/>'
        "</testsuites>"
    )
    (directory / "scenarios").mkdir()
    for name in acceptance.expected_scenarios():
        for repeat in (1, 2, 3):
            (directory / "scenarios" / f"{name}-{repeat}.json").write_text(
                json.dumps(
                    {
                        "scenario": name,
                        "repetition": repeat,
                        "passed": True,
                        "simulated_inputs": True,
                        "live_model_calls": False,
                        "outcome": "verified_outcome",
                        "api_events": [{"method": "POST"}],
                    }
                )
            )


def test_acceptance_requires_complete_fresh_matrix(tmp_path):
    reports(tmp_path)
    assert acceptance.inspect_reports(tmp_path, process_exit_code=0)["passed"]
    next((tmp_path / "scenarios").glob("*.json")).unlink()
    assert not acceptance.inspect_reports(tmp_path, process_exit_code=0)["passed"]


@pytest.mark.parametrize("skipped,exit_code", [(1, 0), (0, 1)])
def test_acceptance_rejects_skips_and_failed_process(tmp_path, skipped, exit_code):
    reports(tmp_path, skipped=skipped)
    assert not acceptance.inspect_reports(tmp_path, process_exit_code=exit_code)["passed"]


def test_acceptance_rejects_unknown_scenario_even_with_sixty_reports(tmp_path):
    reports(tmp_path)
    for path in (tmp_path / "scenarios").glob("community_procurement-*.json"):
        row = json.loads(path.read_text())
        row["scenario"] = "unverified_replacement"
        path.write_text(json.dumps(row))
    assert not acceptance.inspect_reports(tmp_path, process_exit_code=0)["passed"]
