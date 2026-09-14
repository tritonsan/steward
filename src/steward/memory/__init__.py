"""Institutional memory.

Two kinds of recall live here and they are deliberately separate.

`records` holds the structured archive: what happened, who was asked, what they
charged, how long they took, and whether the repair held. Questions like "which
company answers fastest" are answered from this by arithmetic.

Semantic retrieval over the narrative text of those same records answers the
other kind of question, "has anything like this happened before", and it lives
behind the retrieval interface rather than in here.

The split matters because the two failure modes are different. A semantic search
that returns a slightly wrong case is a mild inconvenience. A vendor performance
figure that a language model estimated from prose is a number someone will spend
money on, so that number is computed and carries the case ids it came from.
"""

from steward.memory.archive import (
    InMemoryMemoryArchive,
    MemoryArchive,
    MemoryConflictError,
)
from steward.memory.records import CaseRecord, QuoteRecord
from steward.memory.retrieval import (
    CaseMemoryHit,
    MemoryRecall,
    MemoryRelation,
    MemoryRetriever,
    StructuredMemoryRetriever,
    VendorScorecardEvidence,
)
from steward.memory.scorecard import ScorecardKey, build_scorecards, scorecard_table

__all__ = [
    "CaseMemoryHit",
    "CaseRecord",
    "InMemoryMemoryArchive",
    "MemoryArchive",
    "MemoryConflictError",
    "MemoryRecall",
    "MemoryRelation",
    "MemoryRetriever",
    "QuoteRecord",
    "ScorecardKey",
    "StructuredMemoryRetriever",
    "VendorScorecardEvidence",
    "build_scorecards",
    "scorecard_table",
]
