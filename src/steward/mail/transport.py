"""Transport-agnostic email plumbing.

Two things in here are load bearing beyond mere plumbing.

`RecipientGuard` is the last line of defence in the prompt-injection story. The
policy engine decides whether Steward may contact a vendor, but the guard
decides whether these bytes may physically leave for this domain, and it knows
nothing about intent. If every layer above it were somehow talked into emailing
an attacker, the guard still refuses, because the attacker's domain is not in a
list a human wrote.

`strip_quoted_reply` exists because a reply carries the entire prior thread
underneath it. Feeding that to an extraction step invites reading last week's
price as today's quote. The demo controls both ends of every conversation, so
the formats that need handling are few and known, and this is deliberately a
small, predictable function rather than a general-purpose email parser.
"""

from __future__ import annotations

import html
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from email import policy as email_policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import formatdate, getaddresses, make_msgid, parseaddr
from typing import Protocol, runtime_checkable

__all__ = [
    "InboundMessage",
    "MailDeliveryError",
    "MailTransport",
    "OutboundMessage",
    "RecipientGuard",
    "RecipientNotAllowed",
    "RecordingMailTransport",
    "build_mime",
    "parse_inbound",
    "strip_quoted_reply",
]


class RecipientNotAllowed(RuntimeError):
    """Raised when a send is attempted to a domain outside the allowlist."""


class MailDeliveryError(RuntimeError):
    """A transport failure classified without retaining provider response text."""

    def __init__(self, code: str, *, retryable: bool, ambiguous: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.ambiguous = ambiguous


@dataclass(frozen=True, slots=True)
class OutboundMessage:
    """An email Steward wants to send."""

    to: tuple[str, ...]
    subject: str
    body_text: str
    from_address: str
    from_display_name: str | None = None
    reply_to: str | None = None
    cc: tuple[str, ...] = ()
    in_reply_to: str | None = None
    references: tuple[str, ...] = ()
    message_id: str | None = None

    def __post_init__(self) -> None:
        if not self.to:
            raise ValueError("outbound message needs at least one recipient")
        if not self.subject.strip():
            raise ValueError("outbound message needs a subject")
        if self.message_id is not None and ("\r" in self.message_id or "\n" in self.message_id):
            raise ValueError("outbound message id must be a single header value")

    @property
    def all_recipients(self) -> tuple[str, ...]:
        return tuple(self.to) + tuple(self.cc)


@dataclass(frozen=True, slots=True)
class InboundMessage:
    """A parsed inbound email.

    `body_text` is the reply with the quoted thread removed, which is what gets
    read for a quote. `body_full_text` keeps everything, so the console can show
    the original and a reviewer can check what was thrown away.
    """

    message_id: str | None
    from_address: str
    from_display_name: str
    to_addresses: tuple[str, ...]
    cc_addresses: tuple[str, ...]
    delivered_to: str | None
    subject: str
    body_text: str
    body_full_text: str
    received_at: datetime
    unreadable_attachments: tuple[str, ...] = ()
    in_reply_to: str | None = None
    references: tuple[str, ...] = ()
    raw_ref: str | None = field(
        default=None,
        metadata={"doc": "Pointer to the archived raw message, e.g. an S3 key"},
    )


@runtime_checkable
class MailTransport(Protocol):
    """Sends one message and returns the provider's message id."""

    def send(self, message: OutboundMessage) -> str:  # pragma: no cover - protocol
        ...


@dataclass(frozen=True, slots=True)
class RecipientGuard:
    """Refuses to send anywhere outside an explicitly allowed set of domains."""

    allowed_domains: frozenset[str]

    @classmethod
    def of(cls, *domains: str) -> RecipientGuard:
        cleaned = {d.strip().lower().lstrip("@") for d in domains if d and d.strip()}
        if not cleaned:
            raise ValueError("a recipient guard with no allowed domains would block everything")
        return cls(frozenset(cleaned))

    def is_allowed(self, address: str) -> bool:
        _, addr = parseaddr(address or "")
        domain = addr.rpartition("@")[2].lower()
        return bool(domain) and domain in self.allowed_domains

    def check(self, addresses: Iterable[str]) -> None:
        """Raise `RecipientNotAllowed` if any address is outside the allowlist."""
        rejected = sorted({a for a in addresses if not self.is_allowed(a)})
        if rejected:
            raise RecipientNotAllowed(
                "refusing to send to "
                + ", ".join(rejected)
                + "; allowed domains are "
                + ", ".join(sorted(self.allowed_domains))
            )


class RecordingMailTransport:
    """In-memory transport that records instead of sending. For tests and demos."""

    def __init__(self, guard: RecipientGuard | None = None) -> None:
        self.sent: list[OutboundMessage] = []
        self._guard = guard
        self._counter = 0

    def send(self, message: OutboundMessage) -> str:
        if self._guard is not None:
            self._guard.check(message.all_recipients)
        self.sent.append(message)
        self._counter += 1
        return f"recorded-{self._counter:04d}"

    @property
    def last(self) -> OutboundMessage | None:
        return self.sent[-1] if self.sent else None


# ----------------------------------------------------------------------
# MIME
# ----------------------------------------------------------------------


def build_mime(message: OutboundMessage, *, message_id_domain: str | None = None) -> bytes:
    """Render an `OutboundMessage` as RFC 5322 bytes.

    Raw MIME is used rather than a provider's simple-send API because threading
    depends on `In-Reply-To` and `References`. Without those, a vendor's mail
    client starts a fresh thread for every message and the conversation a
    reviewer sees stops looking like a conversation.
    """
    mime = EmailMessage()
    mime["From"] = (
        f"{message.from_display_name} <{message.from_address}>"
        if message.from_display_name
        else message.from_address
    )
    mime["To"] = ", ".join(message.to)
    if message.cc:
        mime["Cc"] = ", ".join(message.cc)
    mime["Subject"] = message.subject
    if message.reply_to:
        mime["Reply-To"] = message.reply_to
    if message.in_reply_to:
        mime["In-Reply-To"] = message.in_reply_to
    if message.references:
        mime["References"] = " ".join(message.references)

    domain = message_id_domain or message.from_address.rpartition("@")[2]
    mime["Message-ID"] = message.message_id or make_msgid(domain=domain or None)
    mime["Date"] = formatdate(localtime=False)
    mime.set_content(message.body_text)
    return mime.as_bytes()


def parse_inbound(
    raw: bytes,
    *,
    received_at: datetime,
    raw_ref: str | None = None,
) -> InboundMessage:
    """Parse raw inbound bytes into an `InboundMessage`."""
    parsed = BytesParser(policy=email_policy.default).parsebytes(raw)

    display_name, from_address = parseaddr(str(parsed.get("From", "")))
    full_body = _extract_body_text(parsed)
    attachment_text, unreadable = [], []
    for part in parsed.iter_attachments():
        name = part.get_filename() or "unnamed attachment"
        content = part.get_payload(decode=True) or b""
        try:
            if len(content) > 5_000_000:
                raise ValueError("attachment too large")
            if part.get_content_type() == "application/pdf":
                from io import BytesIO

                from pypdf import PdfReader

                reader = PdfReader(BytesIO(content))
                if len(reader.pages) > 30 or reader.is_encrypted:
                    raise ValueError("PDF requires manual review")
                text = "\n".join(page.extract_text() or "" for page in reader.pages)
            elif part.get_content_type() == "text/plain":
                text = content.decode(part.get_content_charset() or "utf-8")
            else:
                raise ValueError("unsupported attachment")
            if not text.strip() or len(text) > 50_000:
                raise ValueError("attachment has no bounded readable text")
            attachment_text.append(f"Attachment: {name}\n{text}")
        except Exception:
            unreadable.append(name[:200])
    extracted_body = strip_quoted_reply(full_body)
    if attachment_text:
        extracted_body += "\n\n" + "\n\n".join(attachment_text)

    references_header = str(parsed.get("References", "") or "")
    references = tuple(ref for ref in references_header.split() if ref)

    return InboundMessage(
        message_id=_clean_header(parsed.get("Message-ID")),
        from_address=from_address,
        from_display_name=display_name,
        to_addresses=_address_tuple(parsed.get_all("To")),
        cc_addresses=_address_tuple(parsed.get_all("Cc")),
        delivered_to=_clean_header(parsed.get("Delivered-To")),
        subject=str(parsed.get("Subject", "") or ""),
        body_text=extracted_body,
        unreadable_attachments=tuple(unreadable),
        body_full_text=full_body,
        received_at=received_at,
        in_reply_to=_clean_header(parsed.get("In-Reply-To")),
        references=references,
        raw_ref=raw_ref,
    )


def _clean_header(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _address_tuple(headers: list[object] | None) -> tuple[str, ...]:
    if not headers:
        return ()
    return tuple(addr for _, addr in getaddresses([str(h) for h in headers]) if addr)


def _extract_body_text(message: EmailMessage) -> str:
    """Best-effort plain text extraction, preferring a real text/plain part."""
    try:
        part = message.get_body(preferencelist=("plain",))
        if part is not None:
            return _decoded(part)
        part = message.get_body(preferencelist=("html",))
        if part is not None:
            return _html_to_text(_decoded(part))
    except Exception:  # noqa: BLE001 - malformed mail must not break intake
        pass

    payload = message.get_payload(decode=True)
    if isinstance(payload, bytes):
        return payload.decode("utf-8", errors="replace")
    return str(message.get_payload() or "")


def _decoded(part: EmailMessage) -> str:
    try:
        content = part.get_content()
    except Exception:  # noqa: BLE001
        payload = part.get_payload(decode=True)
        if isinstance(payload, bytes):
            charset = part.get_content_charset() or "utf-8"
            return payload.decode(charset, errors="replace")
        return str(part.get_payload() or "")
    return content if isinstance(content, str) else str(content)


_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b.*?</\1>", re.I | re.S)
_BREAK_RE = re.compile(r"<\s*(br|/p|/div|/tr)\s*/?\s*>", re.I)


def _html_to_text(markup: str) -> str:
    text = _SCRIPT_STYLE_RE.sub(" ", markup)
    text = _BREAK_RE.sub("\n", text)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    return re.sub(r"[ \t]{2,}", " ", text)


# ----------------------------------------------------------------------
# Quoted reply removal
# ----------------------------------------------------------------------

_ORIGINAL_MESSAGE_RE = re.compile(r"^\s*-{2,}\s*original message\s*-{2,}", re.I)
_OUTLOOK_DIVIDER_RE = re.compile(r"^\s*_{10,}\s*$")
_SIG_DELIM_RE = re.compile(r"^--\s*$")
_MOBILE_SIG_RE = re.compile(r"^\s*(sent|get) (from|outlook) ", re.I)
_HEADER_LINE_RE = re.compile(r"^\s*from\s*:\s*\S", re.I)
_FOLLOWUP_HEADER_RE = re.compile(r"^\s*(sent|to|date|subject|cc)\s*:", re.I)
_ON_WROTE_RE = re.compile(r"^\s*(on|el|le)\b.{0,400}?\bwrote\s*:\s*$", re.I | re.S)


def strip_quoted_reply(text: str) -> str:
    """Return only what the sender typed, dropping the quoted thread below it.

    Conservative by design: it cuts at the first recognised boundary and never
    tries to reassemble interleaved replies. Losing a little trailing context is
    acceptable, whereas keeping the previous message's price is not.
    """
    if not text:
        return ""

    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    cut = len(lines)

    for index, line in enumerate(lines):
        stripped = line.lstrip()

        if stripped.startswith(">"):
            cut = index
            break
        if (
            _ORIGINAL_MESSAGE_RE.match(line)
            or _OUTLOOK_DIVIDER_RE.match(line)
            or _SIG_DELIM_RE.match(line)
            or _MOBILE_SIG_RE.match(line)
        ):
            cut = index
            break
        if _is_attribution_line(lines, index):
            cut = index
            break
        if _HEADER_LINE_RE.match(line) and _has_followup_header(lines, index):
            cut = index
            break

    return "\n".join(lines[:cut]).strip()


def _is_attribution_line(lines: list[str], index: int) -> bool:
    """Detect a "On <date>, <someone> wrote:" attribution starting at `index`.

    Mail clients wrap that line at arbitrary widths, so the check grows a window
    of one, two, then three physical lines. Each candidate window is anchored at
    its own end, which is what makes it an attribution rather than prose that
    merely contains the word: the colon has to be the last thing on it.
    """
    for span in (1, 2, 3):
        window = " ".join(lines[index : index + span]).strip()
        if not window:
            continue
        if _ON_WROTE_RE.match(window):
            return True
    return False


def _has_followup_header(lines: list[str], index: int) -> bool:
    """Distinguish a quoted header block from prose that happens to say 'From:'.

    A real quoted block is followed within a few lines by another header such as
    `Sent:` or `Subject:`. A sentence beginning "From: our warehouse..." is not.
    """
    return any(_FOLLOWUP_HEADER_RE.match(line) for line in lines[index + 1 : index + 5])
