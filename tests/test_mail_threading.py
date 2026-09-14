"""Real mail clients reply to provider IDs and to later messages in the case."""

from dataclasses import replace
from datetime import timedelta

import pytest
from test_quote_decisions import (
    MERIDIAN_BODY,
    _prepare_runtime,
    _record_reply,
    _SelectingRecommender,
)
from test_vendor_replies import _prepare_runtime as prepare_quote_intake
from test_vendor_replies import _reply, _StaticExtractor

from steward.agents.order_reply import OrderReply
from steward.mail import AddressScheme, InboundMessage
from steward.procurement import VendorReplyAuthorizationError


@pytest.mark.parametrize("domain", ["email.amazonses.com", "attacker.example"])
def test_ses_rewritten_rfq_header_matches_only_the_delivered_provider_id(tmp_path, domain):
    extractor = _StaticExtractor()
    rt, clock, case = prepare_quote_intake(tmp_path, extractor)
    item = next(
        o for o in rt.store.outbox_for_case(case.case_id)
        if o.payload.get("vendor_id") == "meridian-lift"
    )
    header = f"<{item.provider_message_id}@{domain}>"
    inbound = replace(_reply(rt, clock, case), in_reply_to=header, references=(header,))
    if domain == "email.amazonses.com":
        result = rt.handle_vendor_reply(inbound, source_id="ses-rfq-reply")
        assert result.quote is not None
        assert len(extractor.calls) == 1
    else:
        with pytest.raises(VendorReplyAuthorizationError, match="threading"):
            rt.handle_vendor_reply(inbound, source_id="wrong-provider-domain")
        assert not extractor.calls
    rt.close()


@pytest.mark.parametrize("purpose", ["commitment", "follow_up"])
@pytest.mark.parametrize("provider_header", [False, True])
def test_reply_to_order_or_appointment_reaches_durable_order_extraction(
    tmp_path, purpose, provider_header
):
    rt, clock, _, case = _prepare_runtime(tmp_path, _SelectingRecommender())
    _record_reply(rt, clock, case, "meridian-lift", MERIDIAN_BODY)
    clock.advance(timedelta(hours=24))
    assert not rt.tick().workflow_failures
    if purpose == "follow_up":
        from appointment_support import confirm_appointment

        confirm_appointment(rt, clock, case.case_id)
    item = next(
        o for o in rt.store.outbox_for_case(case.case_id)
        if o.payload.get("purpose") == purpose
        and o.payload.get("vendor_id") == "meridian-lift"
        and o.delivered_at
    )
    vendor = next(v for v in rt._vendors if v.vendor_id == "meridian-lift")
    scheme = AddressScheme(
        management_domain=rt._settings.management_domain,
        vendor_domain=rt._settings.vendor_domains[0],
    )
    header = (
        f"<{item.provider_message_id}@email.amazonses.com>"
        if provider_header else item.payload["message"]["message_id"]
    )
    inbound = InboundMessage(
        message_id="<order-response@vendor.example>",
        from_address=vendor.email,
        from_display_name=vendor.name,
        to_addresses=(scheme.case_reply_address(case.reply_token),),
        cc_addresses=(),
        delivered_to=None,
        subject="Re: " + item.payload["message"]["subject"],
        body_text="We accept the existing terms.",
        body_full_text="We accept the existing terms.",
        received_at=clock.now(),
        in_reply_to=header,
        references=(header,),
    )

    class Extractor:
        def extract(self, **kwargs):
            return OrderReply(kind="accepted", evidence=kwargs["body_text"])

    rt._order_reply_extractor = Extractor()
    result = rt.handle_vendor_reply(inbound, source_id="ses-order-reply")
    assert result.kind == "order.inbound.v1"
    assert any(j.kind == "appointment.extract" for j in rt.store.workflow_jobs(case.case_id))
    assert not rt.tick().workflow_failures
    assert any(
        a.payload["facts"]["kind"] == "accepted"
        for a in rt.store.artifacts_for(kind="order.reply.v1", case_id=case.case_id)
    )
    # A normal order response is not ingested as a missing-price quote.
    assert len(rt.store.artifacts_for(kind="vendor_quote.v1", case_id=case.case_id)) == 1
    rt.close()
