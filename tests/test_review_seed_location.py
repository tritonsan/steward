"""Review seed discovery follows the Docker filesystem, not Python installation."""

from pathlib import Path

import pytest

import steward.review as review
from steward.config import StewardSettings


def test_installed_module_uses_docker_archive_and_ignores_owner_override(tmp_path, monkeypatch):
    source_seed = review.bundled_review_seed_dir()
    installed_module = tmp_path / "python" / "site-packages" / "steward" / "review.py"
    monkeypatch.setattr(review, "__file__", str(installed_module))
    # Mapping the container's absolute /app directory keeps this test portable.
    monkeypatch.setattr(review, "DOCKER_SEED_DIR", source_seed)
    monkeypatch.setenv("STEWARD_SEED_DIR", str(tmp_path / "owner-private-seed"))
    with review.build_review_runtime(
        StewardSettings(
            database_path=tmp_path / "owner.db",
            database_url=None,
            database_secret=None,
            seed_dir=tmp_path / "another-owner-seed",
        )
    ) as runtime:
        assert runtime._settings.seed_dir == source_seed
        assert len(runtime._bundle.history) == 63
        assert all(record.is_simulated for record in runtime._bundle.history)
        assert runtime.store.records() == ()


def test_review_seed_does_not_fall_back_to_cwd_or_owner_environment(tmp_path, monkeypatch):
    monkeypatch.setattr(review, "__file__", str(tmp_path / "site-packages/steward/review.py"))
    monkeypatch.setattr(review, "DOCKER_SEED_DIR", tmp_path / "absent-container-data")
    decoy = tmp_path / "untrusted-working-directory/data/seed"
    decoy.mkdir(parents=True)
    monkeypatch.chdir(decoy.parents[1])
    monkeypatch.setenv("STEWARD_SEED_DIR", str(decoy))
    with pytest.raises(FileNotFoundError, match="Bundled review seed is missing"):
        review.bundled_review_seed_dir()


def test_review_source_checkout_fallback_remains_available(tmp_path, monkeypatch):
    monkeypatch.setattr(review, "DOCKER_SEED_DIR", tmp_path / "absent-container-data")
    expected = Path(__file__).resolve().parents[1] / "data" / "seed"
    assert review.bundled_review_seed_dir() == expected.resolve()
