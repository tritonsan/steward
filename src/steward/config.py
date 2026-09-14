"""Validated process configuration for Steward's operational runtime."""

from __future__ import annotations

from enum import Enum
from pathlib import Path

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from steward.domain.enums import Category

__all__ = ["RuntimeExecutionMode", "StewardSettings", "TelegramDeliveryMode"]


class RuntimeExecutionMode(str, Enum):
    """Maximum external authority enabled for one runtime process."""

    DRY_RUN = "dry_run"
    LIVE_RFQ = "live_rfq"
    LIVE_COMMITMENT = "live_commitment"


class TelegramDeliveryMode(str, Enum):
    """Telegram authority is independent of procurement and the demo clock."""

    DRY_RUN = "dry_run"
    LIVE = "live"


class StewardSettings(BaseSettings):
    """Single fail-fast settings surface; environment variables use STEWARD_."""

    model_config = SettingsConfigDict(
        env_prefix="STEWARD_",
        case_sensitive=False,
        extra="ignore",
        enable_decoding=False,
    )

    database_path: Path = Path("steward.db")
    database_url: SecretStr | None = None
    database_secret: SecretStr | None = None
    simulation_clock_start: str | None = None
    seed_dir: Path | None = None
    execution_mode: RuntimeExecutionMode = RuntimeExecutionMode.DRY_RUN
    auto_queue_rfq_categories: frozenset[Category] = frozenset({Category.ELEVATOR})

    lease_seconds: int = Field(default=300, gt=0, le=3600)
    inference_lease_seconds: int = Field(default=300, gt=0, le=3600)
    inference_max_attempts: int = Field(default=3, gt=0, le=20)
    inbound_batch_limit: int = Field(default=100, gt=0, le=1000)
    outbox_batch_limit: int = Field(default=100, gt=0, le=1000)
    inbound_max_attempts: int = Field(default=3, gt=0, le=20)
    outbox_max_attempts: int = Field(default=3, gt=0, le=20)

    management_domain: str = "site.narrativenode-labs.cloud"
    vendor_domains: tuple[str, ...] = ("vendors.narrativenode-labs.cloud",)
    management_local_part: str = "management"
    management_display_name: str = "Steward Property Management"

    telegram_enabled: bool = False
    telegram_delivery_mode: TelegramDeliveryMode = TelegramDeliveryMode.DRY_RUN
    telegram_test_polling: bool = False
    telegram_bot_username: str | None = None
    telegram_bot_token: SecretStr | None = None
    channel_configuration: SecretStr | None = None
    telegram_member_names: dict[str, str] = Field(default_factory=dict)
    public_url: str | None = None
    telegram_webhook_secret: SecretStr | None = None
    telegram_allowed_chat_ids: frozenset[str] = frozenset()

    bedrock_model_id: str = "amazon.nova-pro-v1:0"
    bedrock_max_tokens: int = Field(default=512, gt=0, le=4096)
    quote_max_tokens: int = Field(default=1024, gt=0, le=4096)
    decision_max_tokens: int = Field(default=1280, gt=0, le=4096)
    planning_max_tokens: int = Field(default=768, gt=0, le=4096)
    meeting_max_tokens: int = Field(default=4096, gt=0, le=4096)
    minutes_max_tokens: int = Field(default=1280, gt=0, le=4096)
    aws_profile: str | None = None
    aws_region: str = "us-east-1"
    agentcore_runtime_arn: str | None = None
    semantic_memory_enabled: bool = False

    ses_configuration_set_name: str | None = None
    ses_inbound_enabled: bool = False
    ses_inbound_bucket: str | None = None
    ses_queue_url: str | None = None
    allow_live_commitments: bool = False

    @field_validator("auto_queue_rfq_categories", mode="before")
    @classmethod
    def _parse_auto_rfq_categories(cls, value):
        if isinstance(value, str):
            return frozenset(part.strip() for part in value.split(",") if part.strip())
        return value

    @field_validator("vendor_domains", mode="before")
    @classmethod
    def _parse_domains(cls, value):
        if isinstance(value, str):
            return tuple(part.strip() for part in value.split(",") if part.strip())
        return value

    @field_validator("telegram_allowed_chat_ids", mode="before")
    @classmethod
    def _parse_chat_ids(cls, value):
        if isinstance(value, str):
            return frozenset(part.strip() for part in value.split(",") if part.strip())
        return value

    @field_validator("telegram_member_names", mode="before")
    @classmethod
    def _parse_members(cls, value):
        import json

        return json.loads(value) if isinstance(value, str) else value

    @field_validator("management_domain")
    @classmethod
    def _management_domain_is_bare(cls, value: str) -> str:
        return _bare_domain(value)

    @field_validator("vendor_domains")
    @classmethod
    def _vendor_domains_are_bare(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(dict.fromkeys(_bare_domain(domain) for domain in value))
        if not cleaned:
            raise ValueError("at least one vendor domain is required")
        return cleaned

    @field_validator(
        "management_local_part",
        "management_display_name",
        "bedrock_model_id",
        "aws_region",
    )
    @classmethod
    def _required_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("configuration value must not be blank")
        return cleaned

    @field_validator("ses_configuration_set_name", "ses_inbound_bucket")
    @classmethod
    def _optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None

    @model_validator(mode="after")
    def _live_requirements(self) -> StewardSettings:
        if (
            not self.channel_configuration
            and self.telegram_delivery_mode is TelegramDeliveryMode.LIVE
            and not (self.telegram_enabled and self.telegram_bot_token)
        ):
            raise ValueError("live Telegram delivery requires an enabled bot and token")
        if self.simulation_clock_start and self.execution_mode != RuntimeExecutionMode.DRY_RUN:
            raise ValueError("virtual time is restricted to dry-run execution")
        if self.telegram_enabled:
            if self.telegram_webhook_secret is None:
                raise ValueError("Telegram runtime requires telegram_webhook_secret")
            if not self.telegram_allowed_chat_ids and not (
                self.telegram_bot_token and self.telegram_bot_username
            ):
                raise ValueError("Telegram runtime requires allowed chat ids")
        if (
            self.execution_mode is not RuntimeExecutionMode.DRY_RUN
            and not self.ses_configuration_set_name
        ):
            raise ValueError("live mail requires an SES configuration set")
        if self.ses_inbound_enabled and not self.ses_inbound_bucket:
            raise ValueError("SES inbound runtime requires ses_inbound_bucket")
        if (
            self.execution_mode is RuntimeExecutionMode.LIVE_COMMITMENT
            and not self.allow_live_commitments
        ):
            raise ValueError("LIVE_COMMITMENT requires explicit allow_live_commitments=true")
        return self


def _bare_domain(value: str) -> str:
    cleaned = value.strip().lower().lstrip("@")
    if not cleaned or "@" in cleaned or "://" in cleaned or "/" in cleaned or "." not in cleaned:
        raise ValueError("mail domains must be bare DNS names")
    return cleaned
