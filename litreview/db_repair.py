"""Explicit, shadow-only repair operations for SQLite data stores.

No function in this module replaces a live database.  Graph repair defaults to
planning; apply mode requires a distinct output path and builds a validated
shadow copy there.
"""
from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from litreview.sqlite_runtime import connect_sqlite


@dataclass(frozen=True)
class RepairReport:
    operation: str
    applied: bool
    source_path: str
    output_path: str | None
    metrics: dict[str, int]
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}


def _normal_identifier(value: Any) -> str:
    return str(value or "").strip().lower()


def _graph_plan(prod: sqlite3.Connection, graph: sqlite3.Connection) -> dict[str, int]:
    prod_ids = {str(row[0]) for row in prod.execute("SELECT id FROM papers")}
    graph_ids = {str(row[0]) for row in graph.execute("SELECT work_id FROM works WHERE in_library=1")}
    orphan = graph.execute("""
        SELECT COUNT(*) FROM citation_assertions a
        LEFT JOIN citation_edges e ON e.edge_id=a.edge_id WHERE e.edge_id IS NULL
    """).fetchone()[0]
    return {
        "production_documents": len(prod_ids),
        "graph_library_documents": len(graph_ids),
        "documents_to_sync": len(prod_ids - graph_ids),
        "orphan_assertions_to_quarantine": int(orphan),
    }


def repair_graph_shadow(prod_db_path: str, graph_db_path: str, *, apply: bool = False,
                        shadow_path: str | None = None) -> RepairReport:
    """Plan or build a repaired graph shadow; never replace ``graph_db_path``."""
    prod = connect_sqlite(prod_db_path, readonly=True)
    graph = connect_sqlite(graph_db_path, readonly=True)
    try:
        metrics = _graph_plan(prod, graph)
        if not apply:
            return RepairReport("graph_shadow_repair", False, graph_db_path, None, metrics)
        if not shadow_path:
            raise ValueError("apply=True requires an explicit shadow_path")
        source = os.path.realpath(graph_db_path)
        destination = os.path.realpath(shadow_path)
        if source == destination:
            raise ValueError("shadow_path must differ from the source graph database")
        if os.path.exists(destination):
            raise FileExistsError(f"refusing to overwrite existing shadow database: {destination}")
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        shadow = connect_sqlite(destination, foreign_keys=False)
        try:
            graph.backup(shadow)
            _apply_graph_repairs(prod, shadow, metrics)
            quick = str(shadow.execute("PRAGMA quick_check").fetchone()[0])
            fk = list(shadow.execute("PRAGMA foreign_key_check"))
            if quick.lower() != "ok" or fk:
                raise sqlite3.IntegrityError(
                    f"shadow validation failed: quick_check={quick}, foreign_key_violations={len(fk)}"
                )
            metrics["post_repair_foreign_key_violations"] = 0
            metrics["post_repair_quick_check_ok"] = 1
        except Exception:
            shadow.close()
            if os.path.exists(destination):
                os.unlink(destination)
            raise
        else:
            shadow.close()
        return RepairReport("graph_shadow_repair", True, graph_db_path, destination, metrics)
    finally:
        graph.close()
        prod.close()


def _apply_graph_repairs(prod: sqlite3.Connection, shadow: sqlite3.Connection,
                         metrics: dict[str, int]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with shadow:
        shadow.execute("""CREATE TABLE IF NOT EXISTS citation_assertion_quarantine (
            assertion_id TEXT PRIMARY KEY,
            edge_id TEXT NOT NULL,
            source_type TEXT NOT NULL,
            source_mention_id TEXT,
            external_provenance_id TEXT,
            asserted_at TEXT NOT NULL,
            snapshot_json TEXT NOT NULL,
            quarantine_reason TEXT NOT NULL,
            quarantined_at TEXT NOT NULL
        )""")
        orphans = shadow.execute("""
            SELECT a.* FROM citation_assertions a
            LEFT JOIN citation_edges e ON e.edge_id=a.edge_id WHERE e.edge_id IS NULL
        """).fetchall()
        assertion_cols = [row[1] for row in shadow.execute("PRAGMA table_info(citation_assertions)")]
        for row in orphans:
            snapshot = dict(zip(assertion_cols, row))
            shadow.execute("""INSERT OR IGNORE INTO citation_assertion_quarantine
                (assertion_id, edge_id, source_type, source_mention_id,
                 external_provenance_id, asserted_at, snapshot_json,
                 quarantine_reason, quarantined_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'MISSING_CITATION_EDGE', ?)""",
                (snapshot["assertion_id"], snapshot["edge_id"], snapshot["source_type"],
                 snapshot.get("source_mention_id"), snapshot.get("external_provenance_id"),
                 snapshot["asserted_at"], json.dumps(snapshot, ensure_ascii=False), now))
        shadow.execute("""DELETE FROM citation_assertions WHERE edge_id NOT IN
                           (SELECT edge_id FROM citation_edges)""")

        detail_cols = _columns(prod, "paper_details")
        select = ["p.id", "p.title", "p.processed_at"]
        for col in ("title_zh", "title_en", "authors", "journal", "year", "doc_type",
                    "doi", "isbn", "patent_no", "doc_no", "quarantine_status"):
            select.append(f"d.{col}" if col in detail_cols else f"NULL AS {col}")
        rows = prod.execute(f"""SELECT {', '.join(select)} FROM papers p
            LEFT JOIN paper_details d ON d.doc_id=p.id""").fetchall()
        existing = {str(row[0]) for row in shadow.execute("SELECT work_id FROM works")}
        inserted = 0
        identifiers = 0
        for row in rows:
            (doc_id, paper_title, processed_at, title_zh, title_en, authors, venue, year,
             doc_type, doi, isbn, patent_no, doc_no, prod_quarantine) = row
            doc_id = str(doc_id)
            title = title_zh or title_en or paper_title or doc_id
            if doc_id not in existing:
                status = "QUARANTINED" if str(prod_quarantine or "NORMAL").upper() == "FAILED" else "NORMAL"
                shadow.execute("""INSERT INTO works
                    (work_id, work_type, canonical_title, publication_year,
                     journal_or_venue, authors_json, quarantine_status,
                     quarantine_reason, eligible_for_retrieval,
                     eligible_for_citation_graph, in_library, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                    (doc_id, doc_type or "journal_article", title, year, venue,
                     json.dumps([x.strip() for x in str(authors or "").split(";") if x.strip()],
                                ensure_ascii=False),
                     status, "PRODUCTION_PARSE_FAILED" if status == "QUARANTINED" else None,
                     0 if status == "QUARANTINED" else 1,
                     0 if status == "QUARANTINED" else 1,
                     processed_at or now, now))
                inserted += 1
            values = (("doi", doi), ("isbn", isbn), ("patent_no", patent_no),
                      ("standard_no", doc_no if str(doc_type or "").lower() == "standard" else None))
            for id_type, raw in values:
                value = _normal_identifier(raw)
                if not value:
                    continue
                cur = shadow.execute("""INSERT OR IGNORE INTO work_identifiers
                    (work_id, id_type, id_value, is_primary, created_at)
                    VALUES (?, ?, ?, ?, ?)""",
                    (doc_id, id_type, value, 1 if id_type == "doi" else 0, now))
                identifiers += int(cur.rowcount > 0)
        metrics["documents_synced"] = inserted
        metrics["identifiers_synced"] = identifiers
        metrics["assertions_quarantined"] = len(orphans)

        mention_cols = _columns(shadow, "reference_mentions")
        if "provenance_source" not in mention_cols:
            shadow.execute("ALTER TABLE reference_mentions ADD COLUMN provenance_source TEXT")
        shadow.execute("""UPDATE reference_mentions SET provenance_source='legacy_import'
                           WHERE source_run_id IS NULL AND
                                 (provenance_source IS NULL OR provenance_source='')""")
        metrics["legacy_mentions_tagged"] = int(shadow.execute(
            "SELECT changes()").fetchone()[0])
