"""Build Steward images on AWS CodeBuild without a local Docker daemon.

Explicit operator command; creates only steward-prefixed build resources.
The upload is an allowlisted source archive and never includes local state or credentials.
"""

import argparse
import hashlib
import json
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import boto3
from botocore.config import Config

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["build", "status"])
    parser.add_argument("--target", choices=["all", "api"], default="all")
    args = parser.parse_args()
    session = boto3.Session(region_name="us-east-1")
    config = Config(connect_timeout=10, read_timeout=30, retries={"max_attempts": 2})
    cb = session.client("codebuild", config=config)
    state_path = ROOT / ".scratch/cloud-build.local.json"
    previous = json.loads(state_path.read_text()) if state_path.exists() else {}
    if args.action == "build" and args.target == "api" and not previous.get("inferenceImageUri"):
        parser.error("An API-only build requires a recorded existing inference image")
    if args.action == "status":
        state = json.loads(state_path.read_text())
        builds = cb.batch_get_builds(ids=state["build_ids"])["builds"]
        for build in builds:
            print(
                json.dumps(
                    {
                        "id": build["id"],
                        "status": build["buildStatus"],
                        "phase": build.get("currentPhase"),
                        "logs": build.get("logs"),
                        "errors": [
                            p.get("contexts")
                            for p in build.get("phases", [])
                            if p.get("phaseStatus") in ("FAILED", "FAULT", "TIMED_OUT")
                        ],
                    }
                )
            )
        return
    account = session.client("sts", config=config).get_caller_identity()["Account"]
    bucket = f"steward-build-{account}-us-east-1"
    s3 = session.client("s3", config=config)
    buckets = {b["Name"] for b in s3.list_buckets()["Buckets"]}
    if bucket not in buckets:
        s3.create_bucket(Bucket=bucket)
    s3.put_public_access_block(
        Bucket=bucket,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True,
            "IgnorePublicAcls": True,
            "BlockPublicPolicy": True,
            "RestrictPublicBuckets": True,
        },
    )
    s3.put_bucket_encryption(
        Bucket=bucket,
        ServerSideEncryptionConfiguration={
            "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]
        },
    )
    s3.put_bucket_lifecycle_configuration(
        Bucket=bucket,
        LifecycleConfiguration={
            "Rules": [
                {
                    "ID": "expire-build-sources",
                    "Status": "Enabled",
                    "Filter": {"Prefix": "source/"},
                    "Expiration": {"Days": 14},
                }
            ]
        },
    )
    ecr = session.client("ecr", config=config)
    for repo in ("steward-api", "steward-inference"):
        try:
            ecr.describe_repositories(repositoryNames=[repo])
        except ecr.exceptions.RepositoryNotFoundException:
            ecr.create_repository(
                repositoryName=repo,
                imageTagMutability="IMMUTABLE",
                imageScanningConfiguration={"scanOnPush": True},
            )
    archive = ROOT / ".scratch/steward-build.zip"
    files = [
        ROOT / p
        for p in [
            "Dockerfile",
            "Dockerfile.agentcore",
            ".dockerignore",
            "pyproject.toml",
            "README.md",
            "LICENSE",
            "requirements.lock",
        ]
    ]
    for directory in ["src", "data/seed", "web/src", "web/public"]:
        files.extend(
            p for p in (ROOT / directory).rglob("*") if p.is_file() and "__pycache__" not in p.parts
        )
    files.extend(
        p
        for p in (ROOT / "web").iterdir()
        if p.is_file() and (p.name in [".npmrc", "index.html"] or p.suffix in [".json", ".ts"])
    )
    buildspec = {
        "version": "0.2",
        "phases": {
            "pre_build": {
                "commands": [
                    "aws ecr get-login-password --region us-east-1 | "
                    "docker login --username AWS --password-stdin $REGISTRY"
                ]
            },
            "build": {
                "commands": [
                    "docker build --progress=plain -f $DOCKERFILE "
                    "-t $REGISTRY/$REPOSITORY:$RELEASE ."
                ]
            },
            "post_build": {
                "commands": [
                    'test "$CODEBUILD_BUILD_SUCCEEDING" = "1"',
                    "docker push $REGISTRY/$REPOSITORY:$RELEASE",
                ]
            },
        },
    }
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        for path in sorted(set(files)):
            z.write(path, path.relative_to(ROOT).as_posix())
        z.writestr("buildspec.yml", json.dumps(buildspec))
    release = hashlib.sha256(archive.read_bytes()).hexdigest()[:16]
    key = f"source/{release}.zip"
    s3.upload_file(str(archive), bucket, key)
    iam = session.client("iam", config=config)
    role_name = "StewardImageBuilder"
    try:
        role = iam.get_role(RoleName=role_name)["Role"]
    except iam.exceptions.NoSuchEntityException:
        role = iam.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=json.dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Principal": {"Service": "codebuild.amazonaws.com"},
                            "Action": "sts:AssumeRole",
                        }
                    ],
                }
            ),
        )["Role"]
    iam.put_role_policy(
        RoleName=role_name,
        PolicyName="StewardBuildOnly",
        PolicyDocument=json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": ["s3:GetObject", "s3:GetObjectVersion"],
                        "Resource": f"arn:aws:s3:::{bucket}/source/*",
                    },
                    {
                        "Effect": "Allow",
                        "Action": ["s3:GetBucketLocation"],
                        "Resource": f"arn:aws:s3:::{bucket}",
                    },
                    {"Effect": "Allow", "Action": "ecr:GetAuthorizationToken", "Resource": "*"},
                    {
                        "Effect": "Allow",
                        "Action": [
                            "ecr:BatchCheckLayerAvailability",
                            "ecr:InitiateLayerUpload",
                            "ecr:UploadLayerPart",
                            "ecr:CompleteLayerUpload",
                            "ecr:PutImage",
                            "ecr:BatchGetImage",
                            "ecr:GetDownloadUrlForLayer",
                        ],
                        "Resource": f"arn:aws:ecr:us-east-1:{account}:repository/steward-*",
                    },
                    {
                        "Effect": "Allow",
                        "Action": [
                            "logs:CreateLogGroup",
                            "logs:CreateLogStream",
                            "logs:PutLogEvents",
                        ],
                        "Resource": (
                            f"arn:aws:logs:us-east-1:{account}:log-group:/aws/codebuild/steward-*:*"
                        ),
                    },
                ],
            }
        ),
    )
    registry = f"{account}.dkr.ecr.us-east-1.amazonaws.com"
    ids = []
    for name, dockerfile, env_type, image in [
        ("api", "Dockerfile", "LINUX_CONTAINER", "aws/codebuild/standard:7.0"),
        (
            "inference",
            "Dockerfile.agentcore",
            "ARM_CONTAINER",
            "aws/codebuild/amazonlinux-aarch64-standard:3.0",
        ),
    ]:
        if args.target == "api" and name != "api":
            continue
        project = "steward-build-" + name
        values = dict(
            name=project,
            serviceRole=role["Arn"],
            source={"type": "S3", "location": f"{bucket}/{key}"},
            artifacts={"type": "NO_ARTIFACTS"},
            timeoutInMinutes=30,
            environment={
                "type": env_type,
                "image": image,
                "computeType": "BUILD_GENERAL1_SMALL",
                "privilegedMode": True,
                "environmentVariables": [
                    {"name": k, "value": v}
                    for k, v in {
                        "REGISTRY": registry,
                        "REPOSITORY": "steward-" + name,
                        "DOCKERFILE": dockerfile,
                        "RELEASE": release,
                    }.items()
                ],
            },
        )
        if cb.batch_get_projects(names=[project])["projects"]:
            cb.update_project(**values)
        else:
            for attempt in range(4):
                try:
                    cb.create_project(**values)
                    break
                except cb.exceptions.InvalidInputException as exc:
                    if "sts:AssumeRole" not in str(exc) or attempt == 3:
                        raise
                    # A newly created role can take a few seconds to propagate.
                    time.sleep(5)
        ids.append(cb.start_build(projectName=project)["build"]["id"])
    state = {
        "release": release,
        "build_ids": ids,
        "applicationImageUri": f"{registry}/steward-api:{release}",
        "inferenceImageUri": (
            previous["inferenceImageUri"]
            if args.target == "api"
            else f"{registry}/steward-inference:{release}"
        ),
    }
    state["demoClock"] = previous.get("demoClock", datetime.now(timezone.utc).isoformat())
    state_path.write_text(json.dumps(state, indent=2))
    print(json.dumps({"release": release, "build_ids": ids}))


if __name__ == "__main__":
    main()
