"""An isolated, persistent judging workspace using the real application services.

No operational data or channel credentials are copied from the owner's workspace.
Provisioning synthetic examples is an explicit command, never an API startup effect.
"""

from __future__ import annotations

import hashlib
import hmac
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from steward.config import StewardSettings
from steward.runtime import build_runtime

REVIEW_SCHEMA = "steward_review"
REVIEW_CLOCK_START = "2026-09-14T09:00:00+00:00"
PREPARATION_KIND = "review.preparation.v1"
PREPARATION_ID = "jury-preparation-v1:complete"
DOCKER_SEED_DIR = Path("/app/data/seed")


def bundled_review_seed_dir() -> Path:
    """Locate only release-owned seed assets, independently of owner settings.

    Docker installs Python into site-packages but copies the authored archive to
    /app/data/seed. Source checkouts keep it beside src/. Neither an environment
    override nor the current working directory is an acceptable review source.
    """
    for candidate in (
        DOCKER_SEED_DIR,
        Path(__file__).resolve().parents[2] / "data" / "seed",
    ):
        if candidate.is_dir():
            return candidate.resolve()
    raise FileNotFoundError(
        "Bundled review seed is missing. The application image must copy data/seed "
        "to /app/data/seed, or run from a source checkout containing data/seed."
    )


def build_review_runtime(settings: StewardSettings | None = None, **components):
    """Compose the same services with a separate store and no external channels."""
    base = settings or StewardSettings()
    if "seed" in components:
        raise ValueError("review uses only the bundled synthetic Northgate seed")
    values = base.model_dump()
    values.update(
        database_schema=REVIEW_SCHEMA,
        database_path=base.database_path.with_name("steward-review.db"),
        review_isolated=True,
        seed_dir=bundled_review_seed_dir(),
        execution_mode="dry_run",
        allow_live_commitments=False,
        simulation_clock_start=REVIEW_CLOCK_START,
        # Clear the serialized channel secret before composition can expand it.
        channel_configuration=None,
        telegram_enabled=False,
        telegram_delivery_mode="dry_run",
        telegram_test_polling=False,
        telegram_bot_username=None,
        telegram_bot_token=None,
        telegram_webhook_secret=None,
        telegram_allowed_chat_ids=frozenset(),
        telegram_member_names={},
        ses_inbound_enabled=False,
        ses_inbound_bucket=None,
        ses_queue_url=None,
        ses_configuration_set_name=None,
        public_url=None,
        # A separate lexical archive avoids reading the owner's vector table.
        semantic_memory_enabled=False,
        inbound_batch_limit=min(base.inbound_batch_limit, 5),
    )
    if (
        not base.database_url
        and not base.database_secret
        and (
            values["database_path"].resolve() == base.database_path.resolve()
            and not base.review_isolated
        )
    ):
        raise ValueError("review database must differ from the owner database")
    return build_runtime(StewardSettings.model_validate(values), **components)


class ReviewSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    access_code: str = Field(min_length=1, max_length=512)
    role: Literal["manager", "resident"] = "manager"


class ReviewAccess:
    """Role-scoped bearer credentials derived from an operator-held random code.

    The code and derived tokens never enter the model context or the public bundle.
    Rotation invalidates both roles immediately after application restart. Sessions
    survive deploys, so a judge is not interrupted during the judging window.
    """

    def __init__(self, code: str):
        if len(code) < 24:
            raise ValueError("review access code must contain at least 24 characters")
        self._code = code.encode()
        self._lock = threading.Lock()
        self._failures: OrderedDict[str, tuple[float, int]] = OrderedDict()
        self.tokens = {
            self._token(role): {"actor_id": actor, "role": role}
            for role, actor in (("manager", "Simon O."), ("resident", "Daniel K."))
        }

    def _token(self, role: str) -> str:
        digest = hmac.new(self._code, f"steward-review-v1:{role}".encode(), hashlib.sha256)
        return f"review.v1.{role}.{digest.hexdigest()}"

    def session(self, body: ReviewSessionRequest, remote: str) -> dict:
        # ALB connections can share one apparent IP. Invalid attempts must not lock
        # a judge with the correct high-entropy code out of the application.
        if hmac.compare_digest(body.access_code.encode(), self._code):
            with self._lock:
                self._failures.pop(remote, None)
            token = self._token(body.role)
            return {"token": token, **self.tokens[token]}
        now = time.monotonic()
        with self._lock:
            started, failures = self._failures.get(remote, (now, 0))
            if now - started >= 60:
                started, failures = now, 0
            if failures >= 12:
                raise HTTPException(
                    429,
                    "Too many sign-in attempts. Try again in a minute.",
                    headers={"Retry-After": "60", "Cache-Control": "no-store"},
                )
            self._failures[remote] = (started, failures + 1)
            self._failures.move_to_end(remote)
            while len(self._failures) > 1024:
                self._failures.popitem(last=False)
            raise HTTPException(
                401,
                "The judging access code is incorrect.",
                headers={"Cache-Control": "no-store"},
            )


def assert_review_boundary(runtime):
    """Refuse worker processing when a review's immutable composition is unsafe."""
    from steward.mail import RecordingMailTransport

    settings = StewardSettings.model_validate(runtime._settings.model_dump())
    if (
        not settings.review_isolated
        or settings.database_schema != REVIEW_SCHEMA
        or not isinstance(runtime.transport, RecordingMailTransport)
        or runtime.telegram_enabled
        or runtime.inbound_mail_enabled
    ):
        raise ValueError("unsafe judging runtime composition")


def tick_review(runtime):
    assert_review_boundary(runtime)
    # Work continues on following ticks; a large review queue cannot starve the owner.
    return runtime.tick(due_limit=5)


def review_ready(runtime) -> bool:
    """Only an explicit, completed synthetic bootstrap opens the judging workspace."""
    return bool(runtime.store.artifact(PREPARATION_KIND, PREPARATION_ID))
