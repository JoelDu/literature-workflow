"""Central SQLite connection and additive-migration policy.

Read connections are truly read-only.  Write connections get bounded lock
waiting and WAL where supported.  Foreign keys are explicit per database so a
legacy graph with known violations cannot be accidentally bricked.
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from typing import Callable, Iterable


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    apply: Callable[[sqlite3.Connection], None]


def connect_sqlite(path: str, *, readonly: bool = False,
                   foreign_keys: bool = False,
                   timeout_ms: int = 30_000) -> sqlite3.Connection:
    """Open SQLite with one predictable runtime policy.

    ``foreign_keys`` defaults to false deliberately: the production library has
    application-level relationships, and old graph snapshots may contain
    orphan assertions.  Callers may enable it only for a validated database.
    """
    absolute = os.path.abspath(path)
    if readonly:
        conn = sqlite3.connect(f"file:{absolute}?mode=ro", uri=True,
                               timeout=timeout_ms / 1000)
        conn.execute("PRAGMA query_only=ON")
    else:
        parent = os.path.dirname(absolute)
        if parent:
            os.makedirs(parent, exist_ok=True)
        conn = sqlite3.connect(absolute, timeout=timeout_ms / 1000)
        conn.execute(f"PRAGMA busy_timeout={int(timeout_ms)}")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(f"PRAGMA foreign_keys={'ON' if foreign_keys else 'OFF'}")
    return conn


def apply_migrations(conn: sqlite3.Connection, component: str,
                     migrations: Iterable[Migration]) -> list[int]:
    """Apply ordered, additive migrations transactionally and idempotently."""
    conn.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
        component TEXT NOT NULL,
        version INTEGER NOT NULL,
        name TEXT NOT NULL,
        applied_at TEXT NOT NULL DEFAULT (datetime('now')),
        PRIMARY KEY (component, version)
    )""")
    applied = {
        int(row[0]) for row in conn.execute(
            "SELECT version FROM schema_migrations WHERE component=?", (component,)
        )
    }
    completed: list[int] = []
    for migration in sorted(migrations, key=lambda item: item.version):
        if migration.version in applied:
            continue
        with conn:
            migration.apply(conn)
            conn.execute(
                "INSERT INTO schema_migrations(component, version, name) VALUES (?, ?, ?)",
                (component, migration.version, migration.name),
            )
        completed.append(migration.version)
    return completed

