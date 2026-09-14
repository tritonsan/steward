"""Run Telegram onboarding scenarios with the shared isolated PostgreSQL fixture."""

import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))


def main():
    from test_postgres_integration import postgres_environment

    plugin = ModuleType("telegram_postgres_environment")
    plugin.postgres_environment = postgres_environment

    @pytest.fixture(autouse=True)
    def isolated_telegram_database(postgres_environment):
        yield

    plugin.isolated_telegram_database = isolated_telegram_database
    return pytest.main(
        [
            str(ROOT / "tests/test_telegram_groups.py"),
            "-o",
            "addopts=",
            "-q",
            "--tb=short",
            f"--junitxml={ROOT / 'artifacts/validation/telegram-onboarding-postgres.xml'}",
        ],
        plugins=[plugin],
    )


if __name__ == "__main__":
    raise SystemExit(main())
