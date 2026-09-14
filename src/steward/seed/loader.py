"""Read the authored archive into validated domain objects."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from steward.domain.enums import Category
from steward.domain.models import (
    ApprovalPolicy,
    Asset,
    Block,
    GlobalSettings,
    PropertyProfile,
    Resident,
    ResidentMessage,
    UtcDatetime,
    Vendor,
)
from steward.memory.records import CaseRecord
from steward.seed.world import (
    background_thread_docs,
    enrich_history,
    generate_background_cases,
    load_manifest,
)

__all__ = [
    "ArchivedEmail",
    "ArchivedThread",
    "DemoMessage",
    "SeedBundle",
    "VendorReplyScript",
    "default_seed_dir",
    "load_seed",
]

_ENV_VAR = "STEWARD_SEED_DIR"


class _Doc(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ArchivedEmail(_Doc):
    """One message from a historical thread, kept for display in the console."""

    seq: int
    direction: str
    from_: str = Field(alias="from")
    to: list[str] = Field(default_factory=list)
    reply_to: str | None = None
    sent_at: UtcDatetime
    body: str

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class ArchivedThread(_Doc):
    """The correspondence behind one historical case.

    Shown when Steward says a problem was handled before, because a committee
    reading "Coastline reported the rails as within tolerance" in the vendor's
    own words is persuaded by something a summary cannot carry.
    """

    case_id: str
    subject: str
    messages: list[ArchivedEmail]
    outcome_note: str = ""


class DemoMessage(_Doc):
    """A seeded chat message plus the assertion it exists to demonstrate."""

    message: ResidentMessage
    demo_beat: str = ""
    expected_behaviour: str = ""


class VendorReplyScript(_Doc):
    """How a simulated vendor behaves during the demo.

    `delay_hours_after_rfq` of None means the vendor never answers, which is not
    a gap in the script. Silence is the input the follow-up loop exists to
    handle, so it has to be expressible.
    """

    vendor_id: str
    for_demo_beat: str = ""
    delay_hours_after_rfq: float | None = None
    amount: Decimal | None = None
    earliest_onsite_offset_hours: float | None = None
    body: str | None = None
    note: str = ""

    @property
    def stays_silent(self) -> bool:
        return self.delay_hours_after_rfq is None


@dataclass(frozen=True, slots=True)
class SeedBundle:
    """Everything the authored archive contains, validated."""

    reference_date: Any
    property_profile: PropertyProfile
    blocks: tuple[Block, ...]
    assets: tuple[Asset, ...]
    residents: tuple[Resident, ...]
    vendors: tuple[Vendor, ...]
    settings: GlobalSettings
    policies: dict[Category, ApprovalPolicy]
    unconfigured_categories: tuple[str, ...]
    history: tuple[CaseRecord, ...]
    demo_messages: tuple[DemoMessage, ...]
    vendor_replies: tuple[VendorReplyScript, ...]
    threads: dict[str, ArchivedThread]

    def asset(self, asset_id: str) -> Asset | None:
        return next((a for a in self.assets if a.asset_id == asset_id), None)

    def vendor(self, vendor_id: str) -> Vendor | None:
        return next((v for v in self.vendors if v.vendor_id == vendor_id), None)

    def case(self, case_id: str) -> CaseRecord | None:
        return next((c for c in self.history if c.case_id == case_id), None)

    def cases_for_asset(self, asset_id: str) -> tuple[CaseRecord, ...]:
        return tuple(c for c in self.history if c.asset_id == asset_id)

    def allowlisted_vendor_ids(self) -> frozenset[str]:
        return frozenset(v.vendor_id for v in self.vendors if v.allowlisted)


def default_seed_dir() -> Path:
    """Locate `data/seed`, honouring an override for deployed environments."""
    override = os.environ.get(_ENV_VAR)
    if override:
        return Path(override).expanduser().resolve()
    return Path(__file__).resolve().parents[3] / "data" / "seed"


def load_seed(
    seed_dir: Path | str | None = None,
    *,
    include_demo_world: bool = True,
    validate: bool = True,
) -> SeedBundle:
    """Load and assemble the demo world.

    Raises rather than skipping on malformed input. A partially loaded archive
    would produce vendor scorecards that are quietly wrong, and a wrong number
    presented confidently is worse than a crash.

    Beyond reading the authored files, this:

    * enriches every loaded historical quote with a stable, human-readable quote
      id and a vendors-domain Message-ID, and fills the selection, resolution
      and verification source references the case's state implies. The
      enrichment is pure and idempotent and touches no field the scorecards read,
      so the authored elevator arithmetic is unchanged.
    * merges deterministic, non-elevator background history and its synthesized
      threads from the compact ``demo_world.json`` manifest, when
      ``include_demo_world`` is true, so the loaded world is large and long
      enough to read as a real archive.

    When ``validate`` is true (the default) the assembled bundle is checked
    against :func:`steward.seed.validation.validate_demo_world` before it is
    returned. The validator is imported lazily here to keep the module import
    graph acyclic.
    """
    root = Path(seed_dir).expanduser().resolve() if seed_dir else default_seed_dir()
    if not root.is_dir():
        raise FileNotFoundError(
            f"seed directory not found at {root}. Set {_ENV_VAR} or pass seed_dir explicitly."
        )

    property_doc = _read_json(root / "property.json")
    vendors_doc = _read_json(root / "vendors.json")
    policies_doc = _read_json(root / "policies.json")
    history_doc = _read_json(root / "history.json")
    messages_doc = _read_json(root / "live_messages.json")

    # Enrich the authored archive: fill quote ids, Message-IDs and source
    # references. Pure and idempotent; touches nothing the scorecards read.
    authored_history = enrich_history(tuple(CaseRecord(**case) for case in history_doc["cases"]))
    threads = _load_threads(root / "threads")

    history = authored_history
    if include_demo_world:
        manifest = load_manifest(root)
        background = generate_background_cases(manifest)
        history = authored_history + background
        for doc in background_thread_docs(manifest):
            thread = ArchivedThread(**doc)
            threads[thread.case_id] = thread

    bundle = SeedBundle(
        reference_date=property_doc.get("reference_date"),
        property_profile=_load_property(property_doc["property"]),
        blocks=tuple(Block(**b) for b in property_doc["blocks"]),
        assets=tuple(Asset(**a) for a in property_doc["assets"]),
        residents=tuple(Resident(**r) for r in property_doc["residents"]),
        vendors=tuple(_load_vendor(v) for v in vendors_doc["vendors"]),
        settings=GlobalSettings(**policies_doc["global_settings"]),
        policies=_load_policies(policies_doc["policies"]),
        unconfigured_categories=tuple(
            entry["category"] for entry in policies_doc.get("deliberately_unconfigured", [])
        ),
        history=history,
        demo_messages=_load_demo_messages(messages_doc),
        vendor_replies=tuple(
            VendorReplyScript(**reply) for reply in messages_doc.get("vendor_replies", [])
        ),
        threads=threads,
    )

    if validate and include_demo_world:
        # Imported here rather than at module scope so the loader and the
        # validator do not form an import cycle.
        from steward.seed.validation import validate_demo_world

        validate_demo_world(bundle)

    return bundle


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"expected seed file {path}")
    with path.open(encoding="utf-8") as handle:
        return _strip_comments(json.load(handle))


def _strip_comments(value: Any) -> Any:
    """Drop `$`-prefixed keys.

    The archive carries explanatory notes inline, next to the data they explain,
    because a reviewer reading `history.json` should not have to hold a separate
    document open. The models forbid unknown fields, so the notes are removed
    here rather than tolerated everywhere.
    """
    if isinstance(value, dict):
        return {k: _strip_comments(v) for k, v in value.items() if not k.startswith("$")}
    if isinstance(value, list):
        return [_strip_comments(item) for item in value]
    return value


def _load_property(doc: dict[str, Any]) -> PropertyProfile:
    chat = doc.get("chat") or {}
    fields = {k: v for k, v in doc.items() if k != "chat"}
    return PropertyProfile(
        **fields,
        chat_source=chat.get("source", "telegram"),
        chat_id=chat.get("chat_id"),
        chat_title=chat.get("title"),
    )


def _load_vendor(doc: dict[str, Any]) -> Vendor:
    """Fold the authored character sketch into the vendor's notes.

    The sketch is why the archive reads as a story rather than a table, and
    discarding it at load time would throw away the part a reviewer finds
    persuasive.
    """
    fields = dict(doc)
    character = (fields.pop("character", "") or "").strip()
    notes = (fields.pop("notes", "") or "").strip()
    fields["notes"] = "\n\n".join(part for part in (character, notes) if part)
    return Vendor(**fields)


def _load_policies(docs: list[dict[str, Any]]) -> dict[Category, ApprovalPolicy]:
    policies: dict[Category, ApprovalPolicy] = {}
    for doc in docs:
        policy = ApprovalPolicy(**doc)
        if policy.category in policies:
            raise ValueError(
                f"duplicate approval policy for category '{policy.category.value}'; "
                "two rows granting different authority to the same category is "
                "ambiguous and the ambiguity must not be resolved silently"
            )
        policies[policy.category] = policy
    return policies


def _load_demo_messages(doc: dict[str, Any]) -> tuple[DemoMessage, ...]:
    chat = doc.get("chat") or {}
    source = chat.get("source", "telegram")
    chat_id = str(chat.get("chat_id", ""))

    loaded: list[DemoMessage] = []
    for entry in doc["messages"]:
        sent_at = entry["sent_at"]
        loaded.append(
            DemoMessage(
                message=ResidentMessage(
                    message_id=entry["message_id"],
                    source=source,
                    chat_id=chat_id,
                    sender_display=entry["sender_display"],
                    text=entry["text"],
                    sent_at=sent_at,
                    ingested_at=sent_at,
                ),
                demo_beat=entry.get("demo_beat", ""),
                expected_behaviour=entry.get("expected_behaviour", ""),
            )
        )
    return tuple(loaded)


def _load_threads(threads_dir: Path) -> dict[str, ArchivedThread]:
    if not threads_dir.is_dir():
        return {}
    threads: dict[str, ArchivedThread] = {}
    for path in sorted(threads_dir.glob("*.json")):
        thread = ArchivedThread(**_read_json(path))
        threads[thread.case_id] = thread
    return threads
