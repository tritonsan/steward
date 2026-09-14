"""PostgreSQL adapter sharing the operational transition implementation.

The single-community database serializes writes with a transaction advisory lock,
matching SQLite BEGIN IMMEDIATE semantics across API and worker processes. Nested
transactions use savepoints. Network/model calls must remain outside atomic().
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager

from steward.store.sqlite import _SCHEMA, SqliteOperationalStore


def postgres_sql(sql):
    statement = sql.strip().rstrip(";")
    if statement.startswith("INSERT OR IGNORE"):
        statement = statement.replace("INSERT OR IGNORE", "INSERT", 1) + " ON CONFLICT DO NOTHING"
    return statement.replace("? IS NULL", "CAST(? AS TEXT) IS NULL").replace("?", "%s")


class _Connection:
    def __init__(self, connection):
        self.raw = connection

    def execute(self, sql, params=()):
        import psycopg

        try:
            return self.raw.execute(postgres_sql(sql), params)
        except psycopg.IntegrityError as exc:
            # Existing domain translation catches SQL constraint violations.
            raise sqlite3.IntegrityError(str(exc)) from exc

    def close(self):
        self.raw.close()

    def executemany(self, sql, values):
        import psycopg

        try:
            with self.raw.cursor() as cursor:
                cursor.executemany(postgres_sql(sql), values)
        except psycopg.IntegrityError as exc:
            raise sqlite3.IntegrityError(str(exc)) from exc


class PostgresOperationalStore(SqliteOperationalStore):
    def __init__(
        self,
        dsn,
        *,
        schema=None,
        recurrence_window_days=90,
        same_fault_threshold=0.35,
        max_case_hits=10,
    ):
        import psycopg
        from psycopg.rows import dict_row

        if schema is not None:
            import re

            if (
                not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", schema)
                or schema in ("public", "pg_catalog", "information_schema")
                or schema.startswith("pg_")
            ):
                raise ValueError("unsafe operational schema")
        self._path = "postgresql"
        self._recurrence_window_days = recurrence_window_days
        self._same_fault_threshold = same_fault_threshold
        self._max_case_hits = max_case_hits
        self._lock = threading.RLock()
        self._conn = _Connection(
            psycopg.connect(dsn, autocommit=True, row_factory=dict_row, connect_timeout=10)
        )
        self.semantic_index = None
        try:
            if schema is not None:
                from psycopg import sql

                # API and worker may start together; serialize first schema creation too.
                with self._conn.raw.transaction():
                    self._conn.raw.execute("SELECT pg_advisory_xact_lock(1937007986)")
                    self._conn.raw.execute(
                        sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema))
                    )
                    # Never fall back to owner tables in public when a review table is absent.
                    self._conn.raw.execute(
                        sql.SQL("SET search_path TO {}, pg_catalog").format(sql.Identifier(schema))
                    )
            with self._transaction() as conn:
                for sql in _SCHEMA.split(";"):
                    if sql.strip():
                        conn.execute(sql)
                self._migrate_schema()
                conn.execute(
                    "ALTER TABLE timeline ADD COLUMN IF NOT EXISTS rowid "
                    "BIGINT GENERATED ALWAYS AS IDENTITY"
                )
        except Exception:
            self.close()
            raise

    @contextmanager
    def _transaction(self):
        with self._lock, self._conn.raw.transaction():
            self._conn.execute("SELECT pg_advisory_xact_lock(1937007986)")
            yield self._conn

    def recall(self, **kwargs):
        recall = super().recall(**kwargs)
        return (
            self.semantic_index.enrich(recall, kwargs["query_text"])
            if self.semantic_index
            else recall
        )
