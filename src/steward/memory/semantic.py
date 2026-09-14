"""Disposable pgvector index; original case records remain authoritative."""

import json
import math
from dataclasses import replace
from hashlib import sha256

from steward.memory.retrieval import CaseMemoryHit, MemoryRelation

MODEL = "amazon.titan-embed-text-v2:0"


class SemanticIndex:
    def __init__(self, store, *, region, profile=None, client=None):
        import boto3

        self.store = store
        self.client = client or boto3.Session(profile_name=profile, region_name=region).client(
            "bedrock-runtime"
        )

    def embed(self, text):
        response = self.client.invoke_model(
            modelId=MODEL,
            contentType="application/json",
            accept="application/json",
            body=json.dumps({"inputText": text[:20000], "dimensions": 1024, "normalize": True}),
        )
        body = response["body"]
        try:
            vector = json.loads(body.read())["embedding"]
        finally:
            body.close()
        if len(vector) != 1024 or any(not math.isfinite(v) for v in vector):
            raise ValueError("invalid embedding vector")
        return json.dumps(vector)

    def rebuild(self):
        with self.store.atomic() as conn:
            conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS memory_vectors (case_id TEXT PRIMARY KEY, "
                "content_hash TEXT NOT NULL, model TEXT NOT NULL, embedding vector(1024) NOT NULL)"
            )
        count = 0
        for record in self.store.records():
            text = "\n".join(
                (record.title, record.problem, record.work_performed, record.resolution_notes)
            )
            digest = sha256(text.encode()).hexdigest()
            previous = self.store._one(
                "SELECT content_hash FROM memory_vectors WHERE case_id=?", (record.case_id,)
            )
            if previous and previous["content_hash"] == digest:
                continue
            vector = self.embed(text)  # Never hold a database transaction across a model call.
            with self.store.atomic() as conn:
                conn.execute(
                    "INSERT INTO memory_vectors VALUES (?,?,?,?::vector) ON CONFLICT(case_id) "
                    "DO UPDATE SET content_hash=excluded.content_hash,model=excluded.model,"
                    "embedding=excluded.embedding",
                    (record.case_id, digest, MODEL, vector),
                )
            count += 1
        return count

    def enrich(self, recall, query):
        vector = self.embed(query)
        rows = self.store._all(
            "SELECT m.case_id,1-(v.embedding <=> ?::vector) AS score "
            "FROM memory_vectors v JOIN memory_records m ON m.case_id=v.case_id "
            "WHERE m.category=? AND m.closed_at<=? AND (CAST(? AS TEXT) IS NULL OR m.asset_id=?) "
            "ORDER BY v.embedding <=> ?::vector LIMIT 10",
            (
                vector,
                recall.category.value,
                recall.as_of.isoformat(),
                recall.asset_id,
                recall.asset_id,
                vector,
            ),
        )
        existing = {h.case_id: h for h in recall.case_hits}
        hits = []
        for row in rows:
            record = self.store.get(row["case_id"])
            if not record or not record.outcome_verified:
                continue
            hits.append(
                existing.pop(record.case_id, None)
                or CaseMemoryHit(
                    record=record,
                    relation=MemoryRelation.ASSET_HISTORY
                    if recall.asset_id
                    else MemoryRelation.CATEGORY_HISTORY,
                    relevance_score=max(0, min(1, float(row["score"]))),
                    matched_fields=("semantic_similarity",),
                )
            )
        return replace(recall, case_hits=tuple((*hits, *existing.values()))[:10])


def main():
    from steward.runtime import build_runtime
    from steward.store.postgres import PostgresOperationalStore

    with build_runtime() as runtime:
        if not isinstance(runtime.store, PostgresOperationalStore):
            raise ValueError("semantic indexing requires PostgreSQL")
        count = SemanticIndex(
            runtime.store,
            region=runtime._settings.aws_region,
            profile=runtime._settings.aws_profile,
        ).rebuild()
        print(json.dumps({"indexed": count, "model": MODEL}))
