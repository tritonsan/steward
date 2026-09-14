"""Deterministic demo-world enrichment and background history.

The authored archive (``history.json``) is a hand-written argument and must not
move: its thirty-three records and the elevator scorecards computed from them
are the point of the demo. This module does two things around that fixed centre,
and neither is allowed to disturb it.

**Enrichment.** Loaded historical quotes were authored without machine ids,
because a human writing a story does not invent RFC Message-IDs. The console,
though, wants to point at a specific quote and the email it was read from. So
every loaded quote missing an id is given a stable, human-readable quote id and
a Message-ID on the vendors subdomain, and every case that selected a vendor,
resolved, or was verified is given the source references and explicit
``outcome_verified`` flag those states imply. The transformation is *pure* and
*idempotent*: it reads a record, returns a new record, invents nothing random,
and running it twice yields byte-identical output. Crucially it touches only
fields the scorecard arithmetic ignores (ids and source lists), never a cost, a
timestamp, a vendor selection, or a recurrence link.

**Background history.** A real community's archive is not thirty-three cases of
elevators and a handful of other trades; it is years of unremarkable pool
openings and lobby cleans with the occasional elevator drama on top. The demo
needs that texture so the elevator story reads as the exception it is. So this
module expands a compact manifest (``demo_world.json``) into full case records,
entirely deterministically. Every generated case is *non-elevator* by
construction, uses assets and vendors that already exist, and carries verified
sources, precisely so it can be added to the loaded history without moving a
single elevator number.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from steward.domain.enums import Category, Urgency
from steward.memory.records import CaseRecord, QuoteRecord

__all__ = [
    "MANIFEST_FILENAME",
    "background_thread_docs",
    "enrich_case",
    "enrich_history",
    "generate_background_cases",
    "load_manifest",
    "quote_id_for",
    "source_message_id_for",
    "vendor_slug",
]

MANIFEST_FILENAME = "demo_world.json"

# The two subdomains this deployment owns. Every synthetic address produced here
# lands on one of them, so the validator's safe-domain check has something real
# to enforce and no generated correspondence can name an outside party.
_VENDOR_DOMAIN = "vendors.narrativenode-labs.cloud"
_MANAGEMENT_DOMAIN = "site.narrativenode-labs.cloud"


# ----------------------------------------------------------------------
# Stable id derivation
# ----------------------------------------------------------------------


def vendor_slug(vendor_id: str) -> str:
    """A compact, stable slug for a vendor id, used inside ids and addresses."""
    return vendor_id.strip().lower()


def quote_id_for(case_id: str, vendor_id: str) -> str:
    """A stable, human-readable quote id.

    Derived from the case and vendor, so the same quote always gets the same id
    no matter how many times enrichment runs. Readable on purpose: a reviewer
    seeing ``q-hist-2025-002-coastline-elevator`` can tell what it refers to.
    """
    return f"q-{case_id}-{vendor_slug(vendor_id)}"


def source_message_id_for(case_id: str, vendor_id: str) -> str:
    """A stable RFC-style Message-ID on the vendors subdomain.

    A short hash keeps it plausible as a real Message-ID while remaining a pure
    function of its inputs, so it is identical on every load.
    """
    digest = hashlib.sha1(f"{case_id}:{vendor_id}".encode()).hexdigest()[:16]  # noqa: S324
    return f"<{case_id}.{vendor_slug(vendor_id)}.{digest}@{_VENDOR_DOMAIN}>"


# ----------------------------------------------------------------------
# Enrichment (pure, idempotent)
# ----------------------------------------------------------------------


def enrich_case(case: CaseRecord) -> CaseRecord:
    """Return a new ``CaseRecord`` with ids and source references filled in.

    Pure and idempotent. Only ever *fills* missing ids and *derives* source
    reference lists and the ``outcome_verified`` flag; it never overwrites an id
    a human supplied, and never touches a cost, a timestamp, a vendor selection,
    or a recurrence link. Those are the fields the scorecards are built from, so
    leaving them alone is what guarantees enrichment cannot move a number.
    """
    enriched_quotes: list[QuoteRecord] = []
    for quote in case.quotes:
        quote_id = quote.quote_id or quote_id_for(case.case_id, quote.vendor_id)
        message_id = quote.source_email_message_id or source_message_id_for(
            case.case_id, quote.vendor_id
        )
        enriched_quotes.append(
            quote.model_copy(
                update={
                    "quote_id": quote_id,
                    "source_email_message_id": message_id,
                }
            )
        )

    updates: dict[str, Any] = {
        "quotes": enriched_quotes,
        "is_simulated": True,
    }

    # A selected vendor's quote is the evidence behind the selection.
    if case.selected_vendor_id is not None:
        selected_quote = next(
            (q for q in enriched_quotes if q.vendor_id == case.selected_vendor_id),
            None,
        )
        source_ids = _dedupe(
            case.selection_source_ids
            + ([selected_quote.quote_id] if selected_quote and selected_quote.quote_id else [])
            + (
                [selected_quote.source_email_message_id]
                if selected_quote and selected_quote.source_email_message_id
                else []
            )
        )
        updates["selection_source_ids"] = source_ids

    # A resolved case's resolution is backed by the case's own record.
    if case.resolved_at is not None:
        updates["resolution_source_ids"] = _dedupe(
            case.resolution_source_ids + [f"case:{case.case_id}"]
        )
        # These records are the closed, remembered archive; their outcomes were
        # verified when the case was written back to memory. Make that explicit.
        updates["outcome_verified"] = True
        updates["verification_source_ids"] = _dedupe(
            case.verification_source_ids + [f"verification:{case.case_id}"]
        )

    return case.model_copy(update=updates)


def enrich_history(history: tuple[CaseRecord, ...]) -> tuple[CaseRecord, ...]:
    """Enrich every case. Pure and idempotent over the whole archive."""
    return tuple(enrich_case(case) for case in history)


def _dedupe(items: list[str]) -> list[str]:
    """Order-preserving de-duplication, so a second enrichment adds nothing."""
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


# ----------------------------------------------------------------------
# Background history generation (deterministic)
# ----------------------------------------------------------------------


def load_manifest(seed_dir: Path) -> dict[str, Any]:
    """Read the compact demo-world manifest from the seed directory."""
    path = seed_dir / MANIFEST_FILENAME
    if not path.is_file():
        raise FileNotFoundError(f"demo-world manifest not found at {path}")
    with path.open(encoding="utf-8") as handle:
        doc = json.load(handle)
    return {k: v for k, v in doc.items() if not str(k).startswith("$")}


def _parse_day(value: str) -> datetime:
    """Parse a ``YYYY-MM-DD`` manifest date into a UTC datetime at 08:00."""
    year, month, day = (int(part) for part in value.split("-"))
    return datetime(year, month, day, 8, 0, tzinfo=timezone.utc)


def generate_background_cases(manifest: dict[str, Any]) -> tuple[CaseRecord, ...]:
    """Expand the manifest rows into full, enriched, non-elevator case records.

    Deterministic: identical input yields identical output, with no randomness
    and no dependence on wall-clock time. Every row is validated to be a
    non-elevator category before it is expanded, because the whole safety
    argument for adding these cases rests on them never touching an elevator
    scorecard.
    """
    cases: list[CaseRecord] = []
    for row in manifest.get("background_cases", []):
        cases.append(_expand_row(row))
    return tuple(cases)


def _expand_row(row: dict[str, Any]) -> CaseRecord:
    category = Category(row["category"])
    if category is Category.ELEVATOR:
        raise ValueError(
            "background manifest rows must never be elevator cases; the elevator "
            "scorecards are authored and must not move"
        )

    case_id = f"hist-bg-{row['opened'][:4]}-{int(row['seq']):02d}"
    vendor_id = row["vendor"]
    amount = Decimal(str(row["amount"]))
    planned = bool(row.get("planned", False))

    opened_at = _parse_day(row["opened"])
    rfq_sent_at = opened_at + timedelta(minutes=40)
    first_response_at = rfq_sent_at + timedelta(hours=float(row["response_hours"]))
    onsite_at = rfq_sent_at + timedelta(days=int(row["onsite_days"]))
    resolved_at = onsite_at + timedelta(hours=float(row["duration_hours"]))
    closed_at = resolved_at + timedelta(days=1)

    quote = QuoteRecord(
        quote_id=quote_id_for(case_id, vendor_id),
        source_email_message_id=source_message_id_for(case_id, vendor_id),
        vendor_id=vendor_id,
        amount=amount,
        first_response_at=first_response_at,
        earliest_onsite_at=onsite_at,
        scope=row["work"],
    )

    record = CaseRecord(
        case_id=case_id,
        title=row["title"],
        category=category,
        asset_id=row["asset_id"],
        urgency=Urgency(row["urgency"]),
        is_planned_maintenance=planned,
        is_simulated=True,
        outcome_verified=True,
        is_recall=False,
        opened_at=opened_at,
        raised_by=row["raised_by"],
        raised_as=row["problem"],
        problem=row["problem"],
        rfq_sent_at=rfq_sent_at,
        vendors_contacted=[vendor_id],
        quotes=[quote],
        selected_vendor_id=vendor_id,
        selection_rationale="Background history: routine engagement of the standing vendor.",
        onsite_at=onsite_at,
        resolved_at=resolved_at,
        cost=amount,
        currency="USD",
        work_performed=row["work"],
        resolution_notes="Background history, resolved and verified.",
        recurrence_of_case_id=None,
        recurred_as_case_id=None,
        closed_at=closed_at,
    )
    # Route through the same enrichment so source references are populated
    # exactly as they are for authored cases. Idempotent, so this is safe.
    return enrich_case(record)


# ----------------------------------------------------------------------
# Background thread synthesis (deterministic)
# ----------------------------------------------------------------------


def background_thread_docs(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """Synthesize short email threads for the flagged background cases.

    Returns plain dicts shaped like the authored thread files, so the loader can
    build ``ArchivedThread`` objects from them with the same code path. Ordered
    chronologically by the case open date and fully deterministic. Every address
    is on a domain this deployment owns.
    """
    rows = [row for row in manifest.get("background_cases", []) if row.get("thread")]
    rows.sort(key=lambda r: (r["opened"], int(r["seq"])))

    docs: list[dict[str, Any]] = []
    for row in rows:
        docs.append(_thread_for_row(row))
    return docs


def _thread_for_row(row: dict[str, Any]) -> dict[str, Any]:
    case_id = f"hist-bg-{row['opened'][:4]}-{int(row['seq']):02d}"
    vendor_id = row["vendor"]
    slug = vendor_slug(vendor_id)
    vendor_addr = f"{slug}@{_VENDOR_DOMAIN}"
    mgmt_addr = f"management@{_MANAGEMENT_DOMAIN}"
    reply_to = f"case-bg{int(row['seq']):02d}@{_MANAGEMENT_DOMAIN}"

    opened_at = _parse_day(row["opened"])
    rfq_at = opened_at + timedelta(minutes=40)
    reply_at = rfq_at + timedelta(hours=float(row["response_hours"]))
    done_at = (
        rfq_at
        + timedelta(days=int(row["onsite_days"]))
        + timedelta(hours=float(row["duration_hours"]))
    )

    def stamp(when: datetime) -> str:
        return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    return {
        "case_id": case_id,
        "subject": f"Quote request: {row['title']}",
        "messages": [
            {
                "seq": 1,
                "direction": "outbound",
                "from": f"Northgate Residence Management <{mgmt_addr}>",
                "to": [vendor_addr],
                "reply_to": reply_to,
                "sent_at": stamp(rfq_at),
                "body": (
                    f"Good morning,\n\n{row['problem']} "
                    "Could you send a quote and your earliest attendance date.\n\n"
                    "Northgate Residence Management"
                ),
            },
            {
                "seq": 2,
                "direction": "inbound",
                "from": f"{vendor_id} <{vendor_addr}>",
                "to": [reply_to],
                "sent_at": stamp(reply_at),
                "body": (
                    f"Hello,\n\nWe can do this for {row['amount']}. "
                    f"Scope: {row['work']}\n\nEarliest attendance as discussed."
                ),
            },
            {
                "seq": 3,
                "direction": "inbound",
                "from": f"{vendor_id} <{vendor_addr}>",
                "to": [reply_to],
                "sent_at": stamp(done_at),
                "body": f"Completed. {row['work']} Invoice to follow at {row['amount']}.",
            },
        ],
        "outcome_note": "Background history correspondence, synthetic.",
    }
