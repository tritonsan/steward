"""Persisted virtual time and explicit Northgate bootstrap; never sets case state."""

import json
from datetime import datetime, timezone
from uuid import uuid4

from steward.store import WorkflowArtifact


class PersistentClock:
    def __init__(self, store, start):
        self.store = store
        with store.atomic() as conn:
            if not conn.execute(
                "SELECT value FROM schema_meta WHERE key='simulation_clock'"
            ).fetchone():
                conn.execute(
                    "INSERT INTO schema_meta(key,value) VALUES ('simulation_clock',?)",
                    (start.astimezone(timezone.utc).isoformat(),),
                )

    def now(self):
        row = self.store._one("SELECT value FROM schema_meta WHERE key='simulation_clock'")
        return datetime.fromisoformat(row["value"])

    def advance(self, delta):
        if delta.total_seconds() <= 0:
            raise ValueError("simulation time must move forward")
        with self.store.atomic() as conn:
            now = self.now() + delta
            conn.execute(
                "UPDATE schema_meta SET value=? WHERE key='simulation_clock'", (now.isoformat(),)
            )
        return now


def bootstrap():
    """Import seed history explicitly without replacing operational state."""
    from steward.runtime import build_runtime

    with build_runtime() as runtime:
        imported = sum(runtime.store.write(r) for r in runtime._bundle.history)
        source = uuid4().hex
        runtime.store.put_configuration(
            WorkflowArtifact(
                artifact_id=source,
                kind="scenario.run.v1",
                created_at=runtime._clock.now(),
                payload={
                    "run_id": source,
                    "seed": "Northgate",
                    "history_count": len(runtime._bundle.history),
                    "is_simulated": True,
                    "model": runtime._settings.bedrock_model_id,
                },
            )
        )
        print(json.dumps({"imported_history": imported, "scenario_id": source}))
