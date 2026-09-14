"""Loading the hand-written demo archive.

The seed data is authored JSON rather than generated, so the loader's real job is
to refuse to load anything that does not fit the domain models. Validating on the
way in means the archive cannot quietly drift away from the schema while nobody
is looking, and it makes loading it a meaningful test rather than a formality.
"""

from steward.seed.loader import (
    ArchivedEmail,
    ArchivedThread,
    DemoMessage,
    SeedBundle,
    VendorReplyScript,
    default_seed_dir,
    load_seed,
)
from steward.seed.validation import DemoWorldError, validate_demo_world
from steward.seed.world import (
    enrich_case,
    enrich_history,
    generate_background_cases,
    load_manifest,
    quote_id_for,
    source_message_id_for,
)

__all__ = [
    "ArchivedEmail",
    "ArchivedThread",
    "DemoMessage",
    "DemoWorldError",
    "SeedBundle",
    "VendorReplyScript",
    "default_seed_dir",
    "enrich_case",
    "enrich_history",
    "generate_background_cases",
    "load_manifest",
    "load_seed",
    "quote_id_for",
    "source_message_id_for",
    "validate_demo_world",
]
