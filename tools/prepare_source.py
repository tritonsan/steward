"""Create a deterministic public source archive without local or cloud state.

Run from the checkout with ``python tools/prepare_source.py``. This is local
preparation only: no Git repository, upload, publication or credentials are created.
The pattern gate is a useful release check, not a substitute for human review.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = (
    ".dockerignore", ".env.example", ".gitignore", "Dockerfile", "Dockerfile.agentcore",
    "IMPLEMENTATION.md", "LICENSE", "README.md", "pyproject.toml", "requirements.lock",
    "infra/app.py", "infra/cdk.json", "infra/requirements.txt", "web/.npmrc", "web/index.html",
    "web/package.json", "web/package-lock.json", "web/tsconfig.json", "web/vite.config.ts",
)
TREE_TYPES = {
    "src/steward": {".py"},
    "tests": {".py", ".json", ".jsonl"},
    "tools": {".py"},
    "docs": {".md", ".mmd", ".mermaid", ".svg"},
    "data/seed": {".json", ".md"},
    "data/evaluation": {".json", ".jsonl", ".md"},
    "web/src": {".ts", ".tsx", ".js", ".jsx", ".css", ".json", ".svg"},
    "web/public": {".png", ".svg", ".jpg", ".jpeg", ".webp", ".ico", ".woff", ".woff2"},
}
# Reviewed synthetic-model measurements and redacted aggregate checks only.
# Never replace this exact list with an artifacts/validation directory glob.
PUBLIC_EVIDENCE = (
    "artifacts/validation/triage-reserve-v2.json",
    "artifacts/validation/quote-portfolios-regression.json",
    "artifacts/validation/memory-counterfactuals.json",
    "artifacts/validation/dependency-audit.json",
    "artifacts/validation/hackathon-acceptance.json",
    "artifacts/validation/final-review.json",
    "artifacts/validation/final-ui-review.json",
    "artifacts/validation/controlled-mail-roundtrip.json",
    "artifacts/validation/cloud-telegram-delivery.json",
)
REQUIRED = (
    "LICENSE", "README.md", "pyproject.toml", "requirements.lock", "Dockerfile",
    "Dockerfile.agentcore", "src/steward/__init__.py", "data/seed/property.json",
    "web/package.json", "web/package-lock.json", "web/index.html", "web/public/steward-logo.png",
    "infra/app.py", "infra/requirements.txt",
)
EXCLUDED_DIRS = {
    ".scratch", ".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
    ".ruff_cache", ".mypy_cache", "build", "dist", "local_state", "artifacts", ".vite",
}
SECRET_PATTERNS = {
    "aws_access_key": re.compile(rb"(?<![A-Z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Z0-9])"),
    "aws_secret_key_assignment": re.compile(
        rb"aws_secret_access_key[\s\"']*[:=][\s\"']*[A-Za-z0-9/+=]{40}(?![A-Za-z0-9/+=])",
        re.IGNORECASE,
    ),
    "telegram_bot_token": re.compile(rb"(?<![0-9])[0-9]{8,12}:[A-Za-z0-9_-]{30,}(?![\w-])"),
    "private_key": re.compile(
        rb"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"
    ),
    "npm_auth_token": re.compile(rb"_authToken\s*=\s*(?!\$\{)[^\s$]{12,}"),
}


class SourcePreparationError(ValueError):
    """A source release failed a boundary, completeness or secret check."""


def excluded(path: Path) -> bool:
    parts = [part.lower() for part in path.parts]
    return any(
        part in EXCLUDED_DIRS or part.startswith("cdk.") or part.endswith(".egg-info")
        for part in parts[:-1]
    ) or (
        path.name != ".env.example"
        and (
            path.name.startswith(".env")
            or path.name.endswith(".local.json")
            or path.name.lower().startswith(("credentials", "app_password"))
            or path.suffix.lower() in {".db", ".sqlite", ".sqlite3", ".pem", ".key", ".pfx"}
        )
    )


def collect_source(root: Path) -> dict[str, bytes]:
    root = root.resolve()
    paths = {
        root / name for name in (*ROOT_FILES, *PUBLIC_EVIDENCE) if (root / name).is_file()
    }
    for name, extensions in TREE_TYPES.items():
        directory = root / name
        if directory.is_symlink():
            raise SourcePreparationError(f"Symbolic link is not allowed: {name}")
        if not directory.is_dir():
            continue
        for current, directories, filenames in os.walk(directory, followlinks=False):
            for child in list(directories):
                relative = (Path(current) / child).relative_to(root)
                if excluded(relative / "placeholder"):
                    directories.remove(child)
                elif (root / relative).is_symlink():
                    raise SourcePreparationError(f"Symbolic link is not allowed: {relative}")
            paths.update(
                Path(current) / filename for filename in filenames
                if Path(filename).suffix.lower() in extensions
            )
    files = {}
    findings = []
    for path in sorted(paths):
        relative = path.relative_to(root)
        is_public_evidence = relative.as_posix() in PUBLIC_EVIDENCE
        if excluded(relative) and not is_public_evidence:
            continue
        if path.is_symlink() or path.resolve() != path:
            raise SourcePreparationError(f"Source path escapes checkout or is a link: {relative}")
        content = path.read_bytes()
        if is_public_evidence:
            try:
                report = json.loads(content)
            except (ValueError, UnicodeError):
                raise SourcePreparationError(f"Invalid public evidence JSON: {relative}") from None
            if not isinstance(report, dict):
                raise SourcePreparationError(f"Public evidence must be a report object: {relative}")
        findings.extend(
            f"{relative.as_posix()}: {name}"
            for name, pattern in SECRET_PATTERNS.items() if pattern.search(content)
        )
        files[relative.as_posix()] = content
    if findings:
        # Never include matching contents in errors, logs or the failed release.
        raise SourcePreparationError("Possible credentials detected; release not written:\n"
                                     + "\n".join(findings))
    missing = sorted(set(REQUIRED) - set(files))
    if missing:
        raise SourcePreparationError("Required source files are missing: " + ", ".join(missing))
    return dict(sorted(files.items()))


def build_source_archive(root: Path = ROOT) -> dict:
    root = root.resolve()
    files = collect_source(root)
    entries = [
        {"path": path, "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}
        for path, content in files.items()
    ]
    fingerprint = hashlib.sha256(
        json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    manifest = {
        "schema_version": 1,
        "purpose": "Public source preparation; no publication performed",
        "source_sha256": fingerprint,
        "file_count": len(entries),
        "excluded": "Local state, credentials, build output and raw real cloud evidence",
        "public_evidence": {
            "included": [name for name in PUBLIC_EVIDENCE if name in files],
            "missing_optional": [name for name in PUBLIC_EVIDENCE if name not in files],
        },
        "files": entries,
    }
    manifest_bytes = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    directory = root / "artifacts" / "release"
    if not directory.resolve().is_relative_to(root) or directory.is_symlink():
        raise SourcePreparationError("Release directory must remain inside the checkout")
    directory.mkdir(parents=True, exist_ok=True)
    basename = "steward-source-" + fingerprint[:16]
    archive = directory / (basename + ".zip")
    manifest_path = directory / (basename + ".manifest.json")
    if archive.is_symlink() or manifest_path.is_symlink():
        raise SourcePreparationError("Release files cannot be symbolic links")
    with tempfile.NamedTemporaryFile(dir=directory, suffix=".tmp", delete=False) as handle:
        temporary = Path(handle.name)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as output:
            for path, content in {**files, "SOURCE_MANIFEST.json": manifest_bytes}.items():
                info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                output.writestr(info, content)
        temporary.replace(archive)
    finally:
        temporary.unlink(missing_ok=True)
    manifest_path.write_bytes(manifest_bytes)
    return {
        "archive": str(archive), "manifest": str(manifest_path),
        "source_sha256": fingerprint, "file_count": len(entries),
        "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    try:
        result = build_source_archive(args.root)
    except SourcePreparationError as exc:
        parser.exit(1, str(exc) + "\n")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
