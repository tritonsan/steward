"""Email as a replaceable transport.

Steward genuinely emails vendors and genuinely reads their replies. Which
provider carries those bytes is an implementation detail behind
`MailTransport`, so the agent layer never learns whether it is talking to SES,
an IMAP mailbox, or a recorder in a test.
"""

from steward.mail.addressing import AddressScheme, new_reply_token
from steward.mail.events import (
    S3MailNotificationError,
    S3MailObject,
    parse_s3_mail_event,
)
from steward.mail.transport import (
    InboundMessage,
    MailDeliveryError,
    MailTransport,
    OutboundMessage,
    RecipientGuard,
    RecipientNotAllowed,
    RecordingMailTransport,
    build_mime,
    parse_inbound,
    strip_quoted_reply,
)

__all__ = [
    "AddressScheme",
    "InboundMessage",
    "MailDeliveryError",
    "MailTransport",
    "OutboundMessage",
    "RecipientGuard",
    "RecipientNotAllowed",
    "RecordingMailTransport",
    "S3MailNotificationError",
    "S3MailObject",
    "build_mime",
    "new_reply_token",
    "parse_inbound",
    "parse_s3_mail_event",
    "strip_quoted_reply",
]
