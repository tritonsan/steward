"""The real-provider test must never expand its recipient boundary."""

import importlib.util
from pathlib import Path

import pytest

from steward.mail import OutboundMessage

SPEC = importlib.util.spec_from_file_location(
    "controlled_mail_roundtrip",
    Path(__file__).resolve().parents[1] / "tools" / "cloud_mail_roundtrip.py",
)
roundtrip = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(roundtrip)


class Provider:
    def __init__(self):
        self.messages = []

    def send(self, message):
        self.messages.append(message)
        return "provider-receipt"


@pytest.mark.parametrize(
    "overrides",
    [
        {"to": ("supplier@example.com",)},
        {"to": ("another-person@" + roundtrip.DOMAIN,)},
        {"cc": ("supplier@example.com",)},
        {"from_address": "another-person@" + roundtrip.DOMAIN},
    ],
)
def test_controlled_test_blocks_unapproved_addresses_before_provider_call(overrides):
    provider, receipts = Provider(), []
    transport = roundtrip.ControlledTransport(provider, receipts)
    values = {
        "from_address": roundtrip.MANAGEMENT,
        "to": (roundtrip.VENDOR,),
        "subject": "Service order: test",
        "body_text": "Synthetic terms",
        **overrides,
    }
    with pytest.raises(ValueError, match="Controlled test permits"):
        transport.send(OutboundMessage(**values))
    assert not provider.messages
    assert not receipts


def test_controlled_mail_is_explicitly_labelled_and_keeps_reply_context():
    provider, receipts = Provider(), []
    transport = roundtrip.ControlledTransport(provider, receipts)
    original = OutboundMessage(
        from_address=roundtrip.MANAGEMENT,
        to=(roundtrip.VENDOR,),
        subject="Service order: test",
        body_text="Synthetic terms",
        reply_to="case-synthetic@" + roundtrip.DOMAIN,
        in_reply_to="<rfq@example.com>",
        references=("<rfq@example.com>",),
    )
    assert transport.send(original) == "provider-receipt"
    sent = provider.messages[0]
    assert "No real work, visit or payment is requested." in sent.body_text
    assert sent.reply_to == original.reply_to
    assert sent.in_reply_to == original.in_reply_to
    assert sent.references == original.references
    assert original.body_text == "Synthetic terms"
    assert receipts == [{"provider_id": "provider-receipt", "subject": original.subject}]
