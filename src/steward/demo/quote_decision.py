"""Live Nova quote extraction and deterministic dry-run decision smoke.

All resident/vendor data is synthetic. RFQs use RecordingMailTransport and the
winning commitment is authorized but never sent.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any

from steward.agents import QuoteExtractor, StrandsQuoteExtractor
from steward.domain.clock import FrozenClock
from steward.domain.enums import Category, Urgency, group_of
from steward.domain.models import Case
from steward.mail import (
    AddressScheme,
    InboundMessage,
    RecipientGuard,
    RecordingMailTransport,
)
from steward.memory import StructuredMemoryRetriever
from steward.policy import PolicyEngine, Rule
from steward.procurement import (
    CommitmentDisposition,
    QuoteDecisionBuilder,
    QuoteIngestor,
    RecordingAuditSink,
    RfqDispatcher,
)
from steward.seed import SeedBundle, load_seed

DEFAULT_MODEL_ID = "amazon.nova-pro-v1:0"
DEFAULT_REGION = "us-east-1"
SCHEME = AddressScheme(
    management_domain="site.narrativenode-labs.cloud",
    vendor_domain="vendors.narrativenode-labs.cloud",
    management_display_name="Northgate Residence Management",
)


@dataclass(frozen=True, slots=True)
class QuoteSmokeItem:
    quote_id: str
    vendor_id: str
    amount: str
    currency: str
    scope: str
    earliest_onsite_at: str | None
    source_email_message_id: str | None


@dataclass(frozen=True, slots=True)
class QuoteDecisionSmokeReport:
    rfq_vendor_ids: tuple[str, ...]
    rfq_provider_message_ids: tuple[str, ...]
    rfq_audit_count: int
    quotes: tuple[QuoteSmokeItem, ...]
    nonresponding_vendor_ids: tuple[str, ...]
    recommended_vendor_id: str
    recommended_amount: str
    price_premium: str
    decision_reason_codes: tuple[str, ...]
    decision_source_ids: tuple[str, ...]
    commitment_rule_id: str
    commitment_disposition: str
    commitment_sent: bool
    errors: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "safe_mode": {
                "rfq_transport": "RecordingMailTransport",
                "commitment_sent": self.commitment_sent,
            },
            "rfq_vendor_ids": list(self.rfq_vendor_ids),
            "rfq_provider_message_ids": list(self.rfq_provider_message_ids),
            "rfq_audit_count": self.rfq_audit_count,
            "quotes": [asdict(item) for item in self.quotes],
            "nonresponding_vendor_ids": list(self.nonresponding_vendor_ids),
            "decision": {
                "recommended_vendor_id": self.recommended_vendor_id,
                "recommended_amount": self.recommended_amount,
                "price_premium": self.price_premium,
                "reason_codes": list(self.decision_reason_codes),
                "source_ids": list(self.decision_source_ids),
                "commitment_rule_id": self.commitment_rule_id,
                "commitment_disposition": self.commitment_disposition,
                "commitment_sent": self.commitment_sent,
            },
            "errors": list(self.errors),
        }


class _StableIds:
    def __init__(self) -> None:
        self._counts: defaultdict[str, int] = defaultdict(int)

    def __call__(self, prefix: str) -> str:
        self._counts[prefix] += 1
        return f"{prefix}-quote-smoke-{self._counts[prefix]:03d}"


def run_quote_decision_smoke(
    extractor: QuoteExtractor,
    *,
    seed: SeedBundle | None = None,
) -> QuoteDecisionSmokeReport:
    bundle = seed or load_seed()
    ids = _StableIds()
    primary_message = bundle.demo_messages[0].message
    opened_at = primary_message.sent_at
    rfq_at = opened_at + timedelta(minutes=18)
    case = Case(
        case_id="case-quote-smoke-001",
        reply_token="abcdef123456",
        title="A Block elevator shudders near the fourth floor",
        category=Category.ELEVATOR,
        group=group_of(Category.ELEVATOR),
        urgency=Urgency.HIGH,
        asset_id="elevator-a",
        opened_at=opened_at,
        updated_at=opened_at,
        currency="USD",
    )
    memory = StructuredMemoryRetriever(
        bundle.history,
        recurrence_window_days=bundle.settings.recurrence_window_days,
    ).recall(
        category=case.category,
        asset_id=case.asset_id,
        query_text="\n".join(
            (
                primary_message.text,
                case.title,
                "Recurring shuddering and grinding on ascent near floor four.",
            )
        ),
        as_of=primary_message.ingested_at,
    )
    case.related_case_ids = list(memory.related_case_ids)

    policy = PolicyEngine(bundle.policies, bundle.settings)
    guard = RecipientGuard.of(SCHEME.vendor_domain)
    transport = RecordingMailTransport(guard=guard)
    rfq_audit = RecordingAuditSink()
    batch = RfqDispatcher(
        policy=policy,
        transport=transport,
        recipient_guard=guard,
        address_scheme=SCHEME,
        audit_sink=rfq_audit,
        clock=FrozenClock(rfq_at),
        id_factory=ids,
    ).dispatch(
        case=case,
        triage_confidence=0.9,
        asset=bundle.asset("elevator-a"),
        vendors=bundle.vendors,
    )

    vendor_map = {vendor.vendor_id: vendor for vendor in bundle.vendors}
    reply_scripts = {reply.vendor_id: reply for reply in bundle.vendor_replies}
    ingestor = QuoteIngestor(
        extractor=extractor,
        address_scheme=SCHEME,
        id_factory=ids,
    )
    ingestions = []
    nonresponding: list[str] = []
    for vendor_id in batch.contacted_vendor_ids:
        script = reply_scripts[vendor_id]
        if script.stays_silent:
            nonresponding.append(vendor_id)
            continue
        assert script.delay_hours_after_rfq is not None  # noqa: S101
        assert script.body is not None  # noqa: S101
        vendor = vendor_map[vendor_id]
        received_at = rfq_at + timedelta(hours=script.delay_hours_after_rfq)
        inbound = InboundMessage(
            message_id=f"<demo-{vendor_id}-quote@{SCHEME.vendor_domain}>",
            from_address=vendor.email,
            from_display_name=vendor.name,
            to_addresses=(SCHEME.case_reply_address(case.reply_token),),
            cc_addresses=(),
            delivered_to=SCHEME.case_reply_address(case.reply_token),
            subject=f"Re: Quote request: {case.title}",
            body_text=script.body,
            body_full_text=script.body,
            received_at=received_at,
            in_reply_to=next(
                dispatch.provider_message_id
                for dispatch in batch.dispatches
                if dispatch.vendor_id == vendor_id
            ),
        )
        ingestions.append(
            ingestor.ingest(
                case=case,
                vendor=vendor,
                inbound=inbound,
                rfq_sent_at=rfq_at,
            )
        )

    decision = QuoteDecisionBuilder(
        policy=policy,
        clock=FrozenClock(rfq_at + timedelta(hours=30)),
        id_factory=ids,
    ).build(
        case=case,
        triage_confidence=0.9,
        quotes=(item.quote for item in ingestions),
        vendors=bundle.vendors,
        memory=memory,
        nonresponding_vendor_ids=nonresponding,
    )

    quote_items = tuple(
        QuoteSmokeItem(
            quote_id=item.quote.quote_id,
            vendor_id=item.quote.vendor_id,
            amount=_money(item.quote.amount),
            currency=item.quote.currency,
            scope=item.quote.scope,
            earliest_onsite_at=(
                item.quote.earliest_onsite_at.isoformat() if item.quote.earliest_onsite_at else None
            ),
            source_email_message_id=item.quote.source_email_message_id,
        )
        for item in ingestions
    )
    errors = _acceptance_errors(
        bundle=bundle,
        batch=batch,
        audit_count=len(rfq_audit.entries),
        quotes=quote_items,
        nonresponding=tuple(nonresponding),
        decision=decision,
    )
    return QuoteDecisionSmokeReport(
        rfq_vendor_ids=batch.contacted_vendor_ids,
        rfq_provider_message_ids=tuple(
            dispatch.provider_message_id for dispatch in batch.dispatches
        ),
        rfq_audit_count=len(rfq_audit.entries),
        quotes=quote_items,
        nonresponding_vendor_ids=tuple(nonresponding),
        recommended_vendor_id=decision.recommended_quote.vendor_id,
        recommended_amount=_money(decision.recommended_quote.amount),
        price_premium=_money(decision.price_premium),
        decision_reason_codes=tuple(reason.code.value for reason in decision.reasons),
        decision_source_ids=decision.source_ids,
        commitment_rule_id=decision.commitment_decision.rule_id,
        commitment_disposition=decision.commitment_disposition.value,
        commitment_sent=decision.commitment_sent,
        errors=tuple(errors),
    )


def _acceptance_errors(*, bundle, batch, audit_count, quotes, nonresponding, decision):
    errors: list[str] = []
    expected_vendors = (
        "meridian-lift",
        "coastline-elevator",
        "pinnacle-vertical",
    )
    if batch.contacted_vendor_ids != expected_vendors:
        errors.append(
            f"RFQ vendors differed: expected {expected_vendors}, got {batch.contacted_vendor_ids}"
        )
    if audit_count != len(expected_vendors):
        errors.append("every RFQ must have one pre-send audit record")

    amounts = {item.vendor_id: Decimal(item.amount) for item in quotes}
    expected_amounts = {
        reply.vendor_id: reply.amount
        for reply in bundle.vendor_replies
        if reply.for_demo_beat == "quote_collection" and reply.amount is not None
    }
    if amounts != expected_amounts:
        errors.append(f"quote amounts differed: expected {expected_amounts}, got {amounts}")
    missing_onsite = [item.vendor_id for item in quotes if item.earliest_onsite_at is None]
    if missing_onsite:
        errors.append(f"earliest onsite was not extracted for: {missing_onsite}")
    if nonresponding != ("pinnacle-vertical",):
        errors.append(f"expected Pinnacle to be silent, got {nonresponding}")
    if decision.recommended_quote.vendor_id != "meridian-lift":
        errors.append("decision did not recommend Meridian")
    if decision.recommended_quote.amount != Decimal("705.00"):
        errors.append("recommended amount was not 705 USD")
    if decision.price_premium != Decimal("165.00"):
        errors.append("price premium over Coastline was not 165 USD")
    if decision.commitment_disposition is not CommitmentDisposition.AUTHORIZED_NOT_SENT:
        errors.append("commitment was not authorized in dry-run state")
    if decision.commitment_decision.rule_id != Rule.COMMIT_WITHIN_POLICY:
        errors.append("winning quote did not pass the commitment policy gate")
    if decision.commitment_sent:
        errors.append("dry-run decision unexpectedly sent a commitment")
    return errors


QuoteExtractorFactory = Callable[..., QuoteExtractor]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="steward-quote-smoke",
        description="Run synthetic RFQ/quote/decision flow without sending real mail.",
    )
    parser.add_argument(
        "--model-id",
        default=os.environ.get("STEWARD_BEDROCK_MODEL_ID", DEFAULT_MODEL_ID),
    )
    parser.add_argument("--profile", default=os.environ.get("AWS_PROFILE", "default"))
    parser.add_argument(
        "--region",
        default=os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", DEFAULT_REGION)),
    )
    parser.add_argument("--max-tokens", type=_positive_int, default=768)
    parser.add_argument("--json", action="store_true")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    extractor_factory: QuoteExtractorFactory | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    factory = extractor_factory or StrandsQuoteExtractor.bedrock
    extractor = factory(
        args.model_id,
        profile_name=args.profile,
        region_name=args.region,
        temperature=0.0,
        max_tokens=args.max_tokens,
    )
    try:
        report = run_quote_decision_smoke(extractor)
    except Exception as exc:
        payload = {
            "passed": False,
            "safe_mode": {
                "rfq_transport": "RecordingMailTransport",
                "commitment_sent": False,
            },
            "model_id": args.model_id,
            "region": args.region,
            "error": f"{type(exc).__name__}: {exc}",
        }
        if args.json:
            print(json.dumps(payload))
        else:
            print(f"QUOTE SMOKE ERROR: {payload['error']}", file=sys.stderr)
        return 2

    payload = {
        "model_id": args.model_id,
        "region": args.region,
        **report.to_dict(),
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print("PASS" if report.passed else "FAIL")
        print("SAFE MODE: RFQs recorded; commitment not sent")
        print(
            f"Recommended {report.recommended_vendor_id} "
            f"at {report.recommended_amount} USD "
            f"({report.price_premium} USD over cheapest)"
        )
        print(f"Commitment: {report.commitment_disposition} under {report.commitment_rule_id}")
        for error in report.errors:
            print(f"ERROR: {error}")
    return 0 if report.passed else 1


def _money(value: Decimal) -> str:
    return format(value, ".2f")


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


if __name__ == "__main__":
    raise SystemExit(main())
