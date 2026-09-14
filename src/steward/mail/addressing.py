"""Per-case reply addresses.

When a vendor answers, Steward has to know which of possibly dozens of open
cases the reply belongs to. Guessing from the subject line is unreliable and
guessing with a language model is worse, because a wrong guess attaches a price
to the wrong problem and then commits money against it.

Instead every case gets its own recipient address, `case-<token>@<domain>`, set
as `Reply-To` on everything Steward sends about that case. A vendor replying
normally lands on that address, SES catch-all accepts it, and the token
identifies the case with a dictionary lookup. No inference involved.

Tokens are random rather than derived from the case id. A guessable address is
an invitation to inject a fake vendor reply into a case from outside.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from email.utils import formataddr, getaddresses, parseaddr

__all__ = ["AddressScheme", "TOKEN_PATTERN", "new_reply_token"]

TOKEN_PATTERN = re.compile(r"^[0-9a-f]{8,32}$")
"""Tokens are lowercase hex only, so a token can never carry routing syntax."""

_DEFAULT_TOKEN_BYTES = 6


def new_reply_token(n_bytes: int = _DEFAULT_TOKEN_BYTES) -> str:
    """Generate an unguessable per-case token.

    Six bytes gives twelve hex characters, which keeps the address short enough
    to read out in a demo while leaving far too much space to enumerate.
    """
    if n_bytes < 4:
        raise ValueError("reply tokens must be at least 4 bytes to resist guessing")
    return secrets.token_hex(n_bytes)


@dataclass(frozen=True, slots=True)
class AddressScheme:
    """The addresses this deployment owns.

    Management and simulated vendors live on separate subdomains. That split is
    partly routing, since SES receipt rules dispatch on recipient domain, and
    partly disclosure: a reviewer looking at `vendors.` can see at a glance
    which side of the conversation is a demo counterparty.
    """

    management_domain: str
    vendor_domain: str
    management_local: str = "management"
    case_prefix: str = "case"
    management_display_name: str = "Steward"

    def __post_init__(self) -> None:
        for field_name in ("management_domain", "vendor_domain"):
            value = getattr(self, field_name)
            if not value or "@" in value or value.strip() != value:
                raise ValueError(f"{field_name} must be a bare domain, got {value!r}")

    # -- outbound -------------------------------------------------------

    @property
    def management_address(self) -> str:
        """The address Steward sends from."""
        return f"{self.management_local}@{self.management_domain}"

    @property
    def management_from_header(self) -> str:
        """`From` header value, display name included."""
        return formataddr((self.management_display_name, self.management_address))

    def case_reply_address(self, token: str) -> str:
        """The `Reply-To` address that routes a vendor's answer to one case."""
        self.require_valid_token(token)
        return f"{self.case_prefix}-{token}@{self.management_domain}"

    def vendor_address(self, local_part: str) -> str:
        """Address of a simulated vendor mailbox."""
        if not local_part or "@" in local_part:
            raise ValueError(f"vendor local part must be a bare mailbox name, got {local_part!r}")
        return f"{local_part}@{self.vendor_domain}"

    # -- inbound --------------------------------------------------------

    def parse_case_token(self, address: str) -> str | None:
        """Extract a case token from a recipient address, or None.

        Accepts either a bare address or a full header value such as
        ``"Acme Elevator" <case-a1b2c3d4e5f6@site.example>``. Returns None for
        anything that is not one of this deployment's case addresses, including
        a well-formed token on a domain we do not own, which would otherwise be
        an easy way to smuggle a reply into someone else's case.
        """
        _, addr = parseaddr(address or "")
        if "@" not in addr:
            return None

        local, _, domain = addr.rpartition("@")
        if domain.lower() != self.management_domain.lower():
            return None

        prefix = f"{self.case_prefix}-"
        if not local.lower().startswith(prefix):
            return None

        token = local[len(prefix) :].lower()
        return token if TOKEN_PATTERN.match(token) else None

    def find_case_token(self, *header_values: str | None) -> str | None:
        """Scan several recipient headers and return the first case token found.

        A reply may address the case token in `To`, in `Cc`, or only in
        `Delivered-To` depending on how the vendor's mail client behaves, so all
        of them are checked rather than assuming one.
        """
        candidates = [value for value in header_values if value]
        for _, addr in getaddresses(candidates):
            token = self.parse_case_token(addr)
            if token:
                return token
        return None

    def owns(self, address: str) -> bool:
        """True when an address belongs to a domain this deployment controls."""
        _, addr = parseaddr(address or "")
        domain = addr.rpartition("@")[2].lower()
        return domain in {self.management_domain.lower(), self.vendor_domain.lower()}

    @staticmethod
    def require_valid_token(token: str) -> None:
        if not TOKEN_PATTERN.match(token or ""):
            raise ValueError(
                f"invalid reply token {token!r}: expected 8 to 32 lowercase hex characters"
            )
