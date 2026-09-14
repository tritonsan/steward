"""Structural validation for the loaded demo world.

The loader can refuse malformed JSON on its own; pydantic does that. What it
cannot see is whether the *assembled* world hangs together: that a selected
vendor actually quoted, that a recurrence link points both ways, that every
figure the console will display can be traced to a record, that no synthetic
email names a party outside the domains this deployment owns, and that the
enrichment left the archive complete rather than half-filled.

``validate_demo_world`` is the single place those cross-record invariants are
enforced. It is deliberately strict and raises on the first violation, because
a demo-world that is quietly inconsistent produces a confident, wrong claim on
screen, which is worse than a crash at load time.
"""

from __future__ import annotations

from steward.seed.loader import SeedBundle

__all__ = ["DemoWorldError", "validate_demo_world"]

# Target floors. The world may exceed these; it may not fall short.
_MIN_HISTORY = 60
_MIN_THREADS = 15
_MIN_SPAN_MONTHS = 24

# The only domains a synthetic address may name.
_ALLOWED_EMAIL_DOMAINS = (
    "site.narrativenode-labs.cloud",
    "vendors.narrativenode-labs.cloud",
)

# Fields that would carry a raw platform identity if they ever leaked in.
# A resident message must never expose any of these beyond a display name.
_FORBIDDEN_IDENTITY_FIELDS = (
    "phone",
    "phone_number",
    "user_id",
    "telegram_id",
    "from_id",
    "sender_id",
    "username",
    "email",
)


class DemoWorldError(ValueError):
    """Raised when the assembled demo world violates an invariant."""


def validate_demo_world(bundle: SeedBundle) -> SeedBundle:
    """Validate cross-record invariants of an assembled bundle. Returns it.

    Raises :class:`DemoWorldError` on the first violation found.
    """
    _validate_unique_ids(bundle)
    _validate_cross_references(bundle)
    _validate_chronology(bundle)
    _validate_recurrence_symmetry(bundle)
    _validate_selected_quote_exists(bundle)
    _validate_source_completeness(bundle)
    _validate_safe_email_domains(bundle)
    _validate_no_raw_identity(bundle)
    _validate_target_counts(bundle)
    return bundle


def _fail(message: str) -> None:
    raise DemoWorldError(message)


def _validate_unique_ids(bundle: SeedBundle) -> None:
    case_ids = [case.case_id for case in bundle.history]
    if len(case_ids) != len(set(case_ids)):
        dupes = sorted({cid for cid in case_ids if case_ids.count(cid) > 1})
        _fail(f"duplicate case ids in history: {dupes}")

    quote_ids: list[str] = []
    for case in bundle.history:
        for quote in case.quotes:
            if quote.quote_id:
                quote_ids.append(quote.quote_id)
    if len(quote_ids) != len(set(quote_ids)):
        dupes = sorted({qid for qid in quote_ids if quote_ids.count(qid) > 1})
        _fail(f"duplicate quote ids across history: {dupes}")


def _validate_cross_references(bundle: SeedBundle) -> None:
    known_assets = {asset.asset_id for asset in bundle.assets}
    known_vendors = {vendor.vendor_id for vendor in bundle.vendors}

    for case in bundle.history:
        if case.asset_id is not None and case.asset_id not in known_assets:
            _fail(f"{case.case_id} references unknown asset {case.asset_id!r}")
        for quote in case.quotes:
            if quote.vendor_id not in known_vendors:
                _fail(f"{case.case_id} quotes unknown vendor {quote.vendor_id!r}")
        for vendor_id in case.vendors_contacted:
            if vendor_id not in known_vendors:
                _fail(f"{case.case_id} contacted unknown vendor {vendor_id!r}")
        if case.selected_vendor_id is not None and case.selected_vendor_id not in known_vendors:
            _fail(f"{case.case_id} selected unknown vendor {case.selected_vendor_id!r}")

    known_cases = {case.case_id for case in bundle.history}
    for case_id in bundle.threads:
        if case_id not in known_cases:
            _fail(f"thread {case_id!r} references no case in history")


def _validate_chronology(bundle: SeedBundle) -> None:
    for case in bundle.history:
        stages = [
            ("opened", case.opened_at),
            ("rfq", case.rfq_sent_at),
            ("onsite", case.onsite_at),
            ("resolved", case.resolved_at),
            ("closed", case.closed_at),
        ]
        seen = [(name, at) for name, at in stages if at is not None]
        for (earlier_name, earlier), (later_name, later) in zip(seen, seen[1:], strict=False):
            if earlier > later:
                _fail(f"{case.case_id}: {later_name} precedes {earlier_name}")


def _validate_recurrence_symmetry(bundle: SeedBundle) -> None:
    by_id = {case.case_id: case for case in bundle.history}
    for case in bundle.history:
        if case.recurred_as_case_id:
            successor = by_id.get(case.recurred_as_case_id)
            if successor is None:
                _fail(f"{case.case_id} points at a missing successor")
            if successor.recurrence_of_case_id != case.case_id:
                _fail(f"recurrence link from {case.case_id} is not symmetric")
            if case.resolved_at is not None and not (successor.opened_at > case.resolved_at):
                _fail(f"recurrence {successor.case_id} does not follow {case.case_id}'s repair")
        if case.recurrence_of_case_id:
            predecessor = by_id.get(case.recurrence_of_case_id)
            if predecessor is None:
                _fail(f"{case.case_id} points back at a missing predecessor")
            if predecessor.recurred_as_case_id != case.case_id:
                _fail(f"back-link from {case.case_id} is not symmetric")


def _validate_selected_quote_exists(bundle: SeedBundle) -> None:
    for case in bundle.history:
        if case.selected_vendor_id is None:
            continue
        if case.quote_from(case.selected_vendor_id) is None:
            _fail(f"{case.case_id} awarded work to a vendor with no quote on file")


def _validate_source_completeness(bundle: SeedBundle) -> None:
    """Enrichment must have left the archive complete, not half-filled."""
    for case in bundle.history:
        if not case.is_simulated:
            _fail(f"{case.case_id} is demo history but is not marked simulated")
        for quote in case.quotes:
            if not quote.quote_id:
                _fail(f"{case.case_id} has a quote with no id after enrichment")
            if not quote.source_email_message_id:
                _fail(f"{case.case_id} has a quote with no source Message-ID")
        if case.selected_vendor_id is not None and not case.selection_source_ids:
            _fail(f"{case.case_id} selected a vendor with no selection sources")
        if case.resolved_at is not None:
            if not case.resolution_source_ids:
                _fail(f"{case.case_id} is resolved with no resolution sources")
            if not case.outcome_verified:
                _fail(f"{case.case_id} is resolved but not marked outcome_verified")
            if not case.verification_source_ids:
                _fail(f"{case.case_id} is resolved with no verification sources")


def _validate_safe_email_domains(bundle: SeedBundle) -> None:
    def domain_of(address: str) -> str:
        return address.rpartition("@")[2].strip(" >").lower()

    for vendor in bundle.vendors:
        if domain_of(vendor.email) not in _ALLOWED_EMAIL_DOMAINS:
            _fail(f"vendor {vendor.vendor_id} email domain is not a safe synthetic domain")

    for case_id, thread in bundle.threads.items():
        for message in thread.messages:
            addresses = [message.from_, *message.to]
            if message.reply_to:
                addresses.append(message.reply_to)
            for address in addresses:
                if domain_of(address) not in _ALLOWED_EMAIL_DOMAINS:
                    _fail(f"thread {case_id} names email domain outside the safe set: {address!r}")


def _validate_no_raw_identity(bundle: SeedBundle) -> None:
    for demo in bundle.demo_messages:
        fields = set(demo.message.model_dump().keys())
        leaked = fields & set(_FORBIDDEN_IDENTITY_FIELDS)
        if leaked:
            _fail(f"resident message exposes raw identity fields: {sorted(leaked)}")


def _validate_target_counts(bundle: SeedBundle) -> None:
    if len(bundle.history) < _MIN_HISTORY:
        _fail(f"loaded history has {len(bundle.history)} cases, need at least {_MIN_HISTORY}")
    if len(bundle.threads) < _MIN_THREADS:
        _fail(f"loaded world has {len(bundle.threads)} threads, need at least {_MIN_THREADS}")

    opened = [case.opened_at for case in bundle.history]
    span_days = (max(opened) - min(opened)).days
    if span_days < _MIN_SPAN_MONTHS * 30:
        _fail(
            f"history spans {span_days} days, need at least {_MIN_SPAN_MONTHS} months of coverage"
        )
