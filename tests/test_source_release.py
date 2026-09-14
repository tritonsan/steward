"""Release artifacts contain reproducible source, never working credentials or state."""

import hashlib
import importlib.util
import json
import zipfile
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "prepare_source", Path(__file__).resolve().parents[1] / "tools" / "prepare_source.py"
)
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


def write(root, name, content=b"fixture source\n"):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


@pytest.fixture
def checkout(tmp_path):
    for name in release.REQUIRED:
        write(tmp_path, name)
    return tmp_path


def test_release_excludes_private_state_and_builds_and_has_verifiable_manifest(checkout):
    private_paths = (
        ".env", ".env.production", ".scratch/credentials.json", "credentials.json",
        "src/steward/__pycache__/cached.py", "src/steward/example.egg-info/generated.py",
        "tools/credentials.py", "tests/test.local.json", "data/seed/archive.db",
        "web/node_modules/package/index.js", "web/dist/index.html", "infra/cdk.out/template.json",
        "artifacts/validation/cloud-runtime.json", "artifacts/release/previous.zip",
    )
    for name in private_paths:
        write(checkout, name, b"private and excluded")
    for name in (".env.example", "docs/ARCHITECTURE.md", "web/src/main.tsx"):
        write(checkout, name)
    result = release.build_source_archive(checkout)
    manifest = json.loads(Path(result["manifest"]).read_bytes())
    with zipfile.ZipFile(result["archive"]) as archive:
        names = set(archive.namelist())
        assert not names.intersection(private_paths)
        assert set(release.REQUIRED) <= names
        assert {".env.example", "docs/ARCHITECTURE.md", "web/src/main.tsx"} <= names
        assert json.loads(archive.read("SOURCE_MANIFEST.json")) == manifest
        assert len(names) == manifest["file_count"] + 1
        for item in manifest["files"]:
            data = archive.read(item["path"])
            assert item["bytes"] == len(data)
            assert item["sha256"] == hashlib.sha256(data).hexdigest()
    # Even different source mtimes cannot make a new release identity.
    first_bytes = Path(result["archive"]).read_bytes()
    (checkout / "README.md").touch()
    again = release.build_source_archive(checkout)
    assert again == result
    assert Path(again["archive"]).read_bytes() == first_bytes


@pytest.mark.parametrize(
    ("label", "content"),
    [
        ("aws_access_key", b"AK" + b"IA" + b"X" * 16),
        ("aws_secret_key_assignment", b"aws_secret_access_key = '" + b"x" * 40 + b"'"),
        ("telegram_bot_token", b"1234567890:" + b"x" * 35),
        ("private_key", b"-----BEGIN " + b"RSA PRIVATE KEY-----"),
        ("npm_auth_token", b"_authToken=" + b"x" * 30),
    ],
)
def test_secret_gate_fails_before_output_and_does_not_echo_secret(checkout, label, content):
    write(checkout, "docs/accidental.md", content)
    with pytest.raises(release.SourcePreparationError) as error:
        release.build_source_archive(checkout)
    assert label in str(error.value) and "docs/accidental.md" in str(error.value)
    assert content.decode() not in str(error.value)
    assert not (checkout / "artifacts/release").exists()


def test_missing_required_file_fails_before_writing_a_release(checkout):
    (checkout / "LICENSE").unlink()
    with pytest.raises(release.SourcePreparationError, match="LICENSE"):
        release.build_source_archive(checkout)
    assert not (checkout / "artifacts/release").exists()


def test_cli_failure_is_nonzero_and_redacts_matching_content(checkout, capsys):
    sensitive = b"AS" + b"IA" + b"Y" * 16
    write(checkout, "docs/accidental.md", sensitive)
    with pytest.raises(SystemExit) as error:
        release.main(["--root", str(checkout)])
    assert error.value.code == 1
    captured = capsys.readouterr()
    assert "aws_access_key" in captured.err
    assert sensitive.decode() not in captured.err + captured.out


def test_only_exact_reviewed_evidence_paths_are_included_and_missing_are_reported(checkout):
    reviewed = release.PUBLIC_EVIDENCE[0]
    content = b'{"simulated_inputs":true,"sample_count":200,"passed":true}\n'
    write(checkout, reviewed, content)
    for name in (
        "artifacts/validation/cloud-agentcore-live.json",
        "artifacts/validation/triage-reserve-v2.jsonl",
        "artifacts/validation/nested/triage-reserve-v2.json",
        "artifacts/validation/final-review.xml",
    ):
        write(checkout, name, b"private operational data")
    result = release.build_source_archive(checkout)
    with zipfile.ZipFile(result["archive"]) as archive:
        evidence = [name for name in archive.namelist() if name.startswith("artifacts/")]
        assert evidence == [reviewed]
        assert archive.read(reviewed) == content
        manifest = json.loads(archive.read("SOURCE_MANIFEST.json"))
    assert manifest["public_evidence"]["included"] == [reviewed]
    assert manifest["public_evidence"]["missing_optional"] == list(release.PUBLIC_EVIDENCE[1:])


def test_allowlisted_evidence_still_passes_secret_gate(checkout):
    sensitive = b"1234567890:" + b"x" * 35
    write(checkout, release.PUBLIC_EVIDENCE[-1], b'{"accidental":"' + sensitive + b'"}')
    with pytest.raises(release.SourcePreparationError, match="telegram_bot_token") as error:
        release.build_source_archive(checkout)
    assert sensitive.decode() not in str(error.value)
    assert not (checkout / "artifacts/release").exists()


def test_invalid_allowlisted_evidence_fails_before_release(checkout):
    write(checkout, release.PUBLIC_EVIDENCE[-1], b"not valid JSON")
    with pytest.raises(release.SourcePreparationError, match="Invalid public evidence JSON"):
        release.build_source_archive(checkout)
    assert not (checkout / "artifacts/release").exists()
