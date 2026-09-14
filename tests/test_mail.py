"""Tests for addressing, the recipient guard, and MIME handling."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from steward.mail import (
    AddressScheme,
    OutboundMessage,
    RecipientGuard,
    RecipientNotAllowed,
    RecordingMailTransport,
    build_mime,
    new_reply_token,
    parse_inbound,
    strip_quoted_reply,
)

RECEIVED_AT = datetime(2026, 3, 14, 9, 30, tzinfo=timezone.utc)

SCHEME = AddressScheme(
    management_domain="site.narrativenode-labs.cloud",
    vendor_domain="vendors.narrativenode-labs.cloud",
    management_display_name="Northgate Residence Management",
)


# ----------------------------------------------------------------------
# Addressing
# ----------------------------------------------------------------------


def test_reply_tokens_are_hex_and_unique():
    tokens = {new_reply_token() for _ in range(200)}

    assert len(tokens) == 200, "tokens must not collide"
    assert all(len(t) == 12 and t == t.lower() for t in tokens)
    assert all(all(c in "0123456789abcdef" for c in t) for t in tokens)


def test_short_tokens_are_refused():
    with pytest.raises(ValueError, match="at least 4 bytes"):
        new_reply_token(2)


def test_addresses_are_built_from_the_scheme():
    assert SCHEME.management_address == "management@site.narrativenode-labs.cloud"
    assert SCHEME.management_from_header == (
        "Northgate Residence Management <management@site.narrativenode-labs.cloud>"
    )
    assert SCHEME.vendor_address("acme") == "acme@vendors.narrativenode-labs.cloud"


def test_case_reply_address_embeds_the_token():
    address = SCHEME.case_reply_address("a1b2c3d4e5f6")

    assert address == "case-a1b2c3d4e5f6@site.narrativenode-labs.cloud"
    assert SCHEME.parse_case_token(address) == "a1b2c3d4e5f6"


def test_case_token_round_trips_through_a_full_header_value():
    header = '"Acme Elevator Services" <case-a1b2c3d4e5f6@site.narrativenode-labs.cloud>'

    assert SCHEME.parse_case_token(header) == "a1b2c3d4e5f6"


def test_a_valid_token_on_a_foreign_domain_is_rejected():
    """Otherwise anyone could route a reply into someone else's case."""
    assert SCHEME.parse_case_token("case-a1b2c3d4e5f6@attacker.example") is None


@pytest.mark.parametrize(
    "address",
    [
        "management@site.narrativenode-labs.cloud",
        "casea1b2c3d4e5f6@site.narrativenode-labs.cloud",
        "case-NOTHEX@site.narrativenode-labs.cloud",
        "case-abc@site.narrativenode-labs.cloud",
        "case-@site.narrativenode-labs.cloud",
        "not-an-address",
        "",
    ],
)
def test_non_case_addresses_yield_no_token(address):
    assert SCHEME.parse_case_token(address) is None


def test_malformed_tokens_are_refused_at_construction_time():
    with pytest.raises(ValueError, match="invalid reply token"):
        SCHEME.case_reply_address("../etc/passwd")


def test_find_case_token_scans_every_recipient_header():
    token = SCHEME.find_case_token(
        "dispatch@vendors.narrativenode-labs.cloud",
        "Someone <case-0f1e2d3c4b5a@site.narrativenode-labs.cloud>",
        None,
    )

    assert token == "0f1e2d3c4b5a"


def test_scheme_rejects_domains_that_are_not_bare():
    with pytest.raises(ValueError, match="bare domain"):
        AddressScheme(
            management_domain="management@site.example",
            vendor_domain="vendors.example",
        )


def test_owns_recognises_only_our_own_domains():
    assert SCHEME.owns("anything@site.narrativenode-labs.cloud")
    assert SCHEME.owns("acme@vendors.narrativenode-labs.cloud")
    assert not SCHEME.owns("someone@gmail.com")


# ----------------------------------------------------------------------
# Recipient guard
# ----------------------------------------------------------------------


def test_guard_allows_domains_on_the_list():
    guard = RecipientGuard.of(SCHEME.vendor_domain, SCHEME.management_domain)

    guard.check(["acme@vendors.narrativenode-labs.cloud"])


def test_guard_blocks_everything_else_and_names_what_it_blocked():
    guard = RecipientGuard.of(SCHEME.vendor_domain)

    with pytest.raises(RecipientNotAllowed) as excinfo:
        guard.check(["acme@vendors.narrativenode-labs.cloud", "attacker@evil.example"])

    assert "attacker@evil.example" in str(excinfo.value)
    assert "acme@vendors.narrativenode-labs.cloud" not in str(excinfo.value)


def test_a_guard_with_no_domains_is_a_configuration_error():
    with pytest.raises(ValueError, match="block everything"):
        RecipientGuard.of()


def test_transport_refuses_a_send_the_guard_rejects():
    transport = RecordingMailTransport(guard=RecipientGuard.of(SCHEME.vendor_domain))
    message = OutboundMessage(
        to=("attacker@evil.example",),
        subject="Approve payment",
        body_text="as instructed",
        from_address=SCHEME.management_address,
    )

    with pytest.raises(RecipientNotAllowed):
        transport.send(message)

    assert transport.sent == [], "nothing may be recorded as sent"


# ----------------------------------------------------------------------
# MIME
# ----------------------------------------------------------------------


def build_rfq() -> OutboundMessage:
    return OutboundMessage(
        to=("acme@vendors.narrativenode-labs.cloud",),
        subject="Quote request: A Block elevator out of service",
        body_text="The A Block elevator is out of service. Could you send a quote?",
        from_address=SCHEME.management_address,
        from_display_name=SCHEME.management_display_name,
        reply_to=SCHEME.case_reply_address("a1b2c3d4e5f6"),
    )


def test_outbound_message_requires_recipient_and_subject():
    with pytest.raises(ValueError, match="at least one recipient"):
        OutboundMessage(to=(), subject="x", body_text="y", from_address="a@b.example")

    with pytest.raises(ValueError, match="needs a subject"):
        OutboundMessage(
            to=("a@b.example",), subject="   ", body_text="y", from_address="a@b.example"
        )


def test_built_mime_carries_the_case_reply_address():
    parsed = parse_inbound(build_mime(build_rfq()), received_at=RECEIVED_AT)

    assert parsed.subject == "Quote request: A Block elevator out of service"
    assert parsed.to_addresses == ("acme@vendors.narrativenode-labs.cloud",)
    assert "quote" in parsed.body_text.lower()
    assert parsed.message_id


def test_threading_headers_survive_a_round_trip():
    original = build_mime(build_rfq())
    first = parse_inbound(original, received_at=RECEIVED_AT)

    reply = OutboundMessage(
        to=("case-a1b2c3d4e5f6@site.narrativenode-labs.cloud",),
        subject="Re: Quote request",
        body_text="We can attend Tuesday.",
        from_address="acme@vendors.narrativenode-labs.cloud",
        in_reply_to=first.message_id,
        references=(first.message_id,),
    )
    parsed_reply = parse_inbound(build_mime(reply), received_at=RECEIVED_AT)

    assert parsed_reply.in_reply_to == first.message_id
    assert parsed_reply.references == (first.message_id,)


def test_reply_to_header_routes_a_vendor_reply_back_to_its_case():
    """The one header the whole correlation scheme depends on."""
    raw = build_mime(build_rfq()).decode()
    reply_to_line = next(line for line in raw.splitlines() if line.startswith("Reply-To:"))
    address = reply_to_line.removeprefix("Reply-To:").strip()

    assert SCHEME.parse_case_token(address) == "a1b2c3d4e5f6"


def test_html_only_mail_is_reduced_to_text():
    raw = (
        b"From: acme@vendors.narrativenode-labs.cloud\r\n"
        b"To: case-a1b2c3d4e5f6@site.narrativenode-labs.cloud\r\n"
        b"Subject: Re: Quote request\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: text/html; charset=utf-8\r\n"
        b"\r\n"
        b"<html><body><p>Our price is <b>1,200 USD</b>.</p>"
        b"<p>We can attend Tuesday.</p></body></html>\r\n"
    )

    parsed = parse_inbound(raw, received_at=RECEIVED_AT)

    assert "1,200 USD" in parsed.body_text
    assert "<b>" not in parsed.body_text


def test_malformed_mail_does_not_raise():
    parsed = parse_inbound(b"this is not a valid email at all", received_at=RECEIVED_AT)

    assert parsed.from_address == ""
    assert parsed.received_at == RECEIVED_AT


# ----------------------------------------------------------------------
# Quoted reply removal
# ----------------------------------------------------------------------


def test_gmail_style_quote_is_removed():
    body = (
        "Our price for the drive unit is 1,200 USD and we can attend Tuesday.\n"
        "\n"
        "On Sat, 14 Mar 2026 at 09:12, Northgate Residence Management "
        "<management@site.narrativenode-labs.cloud> wrote:\n"
        "> The A Block elevator is out of service.\n"
        "> Our previous repair was 980 USD.\n"
    )

    cleaned = strip_quoted_reply(body)

    assert cleaned == "Our price for the drive unit is 1,200 USD and we can attend Tuesday."
    assert "980" not in cleaned, "last time's price must not be readable as this quote"


def test_plain_angle_quotes_are_removed():
    cleaned = strip_quoted_reply("Confirmed for Tuesday.\n> please send a quote\n")

    assert cleaned == "Confirmed for Tuesday."


def test_outlook_original_message_divider_is_removed():
    body = "1,200 USD, Tuesday.\n\n-----Original Message-----\nFrom: management\nSent: yesterday\n"

    assert strip_quoted_reply(body) == "1,200 USD, Tuesday."


def test_outlook_underscore_divider_is_removed():
    body = "1,200 USD, Tuesday.\n\n" + "_" * 32 + "\nFrom: management\n"

    assert strip_quoted_reply(body) == "1,200 USD, Tuesday."


def test_signature_delimiter_is_removed():
    body = "1,200 USD, Tuesday.\n--\nAcme Elevator Services\n+1 555 0100\n"

    assert strip_quoted_reply(body) == "1,200 USD, Tuesday."


def test_quoted_header_block_is_removed():
    body = "1,200 USD.\n\nFrom: Northgate\nSent: Friday\nSubject: Quote request\n"

    assert strip_quoted_reply(body) == "1,200 USD."


def test_prose_beginning_with_from_is_not_mistaken_for_a_quote_header():
    """A false positive here would silently delete the vendor's actual answer."""
    body = "From: our Ankara warehouse we can dispatch a technician Tuesday. Price 1,200 USD."

    assert strip_quoted_reply(body) == body


def test_empty_body_is_handled():
    assert strip_quoted_reply("") == ""
    assert strip_quoted_reply("\n\n") == ""
