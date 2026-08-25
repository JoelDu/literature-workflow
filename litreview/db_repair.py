"""Explicit, shadow-only repair operations for SQLite data stores.

No function in this module replaces a live database.  Graph repair defaults to
planning; apply mode requires a distinct output path and builds a validated
shadow copy there.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import hashlib
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


_UNSAFE_C0 = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_DOI = re.compile(r"^10\.\d{4,9}/\S+$", re.IGNORECASE)


def sanitize_extracted_text(text: str | None) -> str:
    """Remove unsafe C0 controls while retaining tabs and line boundaries."""
    return _UNSAFE_C0.sub("", text or "")


def normalize_doi(value: str | None) -> str:
    """Normalize DOI wrappers conservatively; return empty for absent values."""
    doi = str(value or "").strip()
    doi = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi\s*:\s*)", "", doi,
                 flags=re.IGNORECASE)
    return doi.rstrip(".,; ").lower()


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


def repair_production_database(prod_db_path: str, *, apply: bool = False,
                               backup_path: str | None = None,
                               excel_path: str | None = None) -> RepairReport:
    """Plan or perform a micro-repair that leaves affected texts pending reindex.

    Apply mode requires a new physical backup path.  It never invents vectors:
    sanitized documents lose stale chunks/cache entries and are explicitly
    marked ``embedded=0`` for the normal indexer to rebuild later.
    """
    readonly = connect_sqlite(prod_db_path, readonly=True)
    try:
        metrics, nul_doc_ids, doi_updates = _production_plan(readonly)
    finally:
        readonly.close()
    if not apply:
        return RepairReport("production_micro_repair", False, prod_db_path, None, metrics)
    if not backup_path:
        raise ValueError("apply=True requires an explicit backup_path")
    if doi_updates and not excel_path:
        raise ValueError("DOI metadata would change; excel_path is required before apply")
    source = os.path.realpath(prod_db_path)
    backup = os.path.realpath(backup_path)
    if source == backup:
        raise ValueError("backup_path must differ from the production database")
    if os.path.exists(backup):
        raise FileExistsError(f"refusing to overwrite existing backup: {backup}")
    os.makedirs(os.path.dirname(backup), exist_ok=True)

    src = connect_sqlite(prod_db_path, readonly=True)
    dst = sqlite3.connect(backup)
    try:
        src.backup(dst)
        if str(dst.execute("PRAGMA quick_check").fetchone()[0]).lower() != "ok":
            raise sqlite3.IntegrityError("physical backup quick_check failed")
    except Exception:
        dst.close()
        src.close()
        if os.path.exists(backup):
            os.unlink(backup)
        raise
    dst.close()
    src.close()

    conn = connect_sqlite(prod_db_path, foreign_keys=False)
    try:
        with conn:
            for doc_id in nul_doc_ids:
                old = conn.execute("SELECT mineru_md FROM papers WHERE id=?", (doc_id,)).fetchone()[0]
                cleaned = sanitize_extracted_text(old)
                conn.execute("UPDATE papers SET mineru_md=? WHERE id=?", (cleaned, doc_id))
                chunk_ids = [row[0] for row in conn.execute(
                    "SELECT chunk_id FROM review_chunks WHERE doc_id=?", (doc_id,))]
                if chunk_ids and "review_evidence_cache" in {
                    row[0] for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'")
                }:
                    marks = ",".join("?" for _ in chunk_ids)
                    conn.execute(f"DELETE FROM review_evidence_cache WHERE chunk_id IN ({marks})",
                                 chunk_ids)
                conn.execute("DELETE FROM review_chunks WHERE doc_id=?", (doc_id,))
                conn.execute("""INSERT OR REPLACE INTO review_index_meta
                    (doc_id, md_hash, n_chunks, embedded, indexed_at)
                    VALUES (?, ?, 0, 0, NULL)""",
                    (doc_id, hashlib_sha256(cleaned)))
            conn.execute("""UPDATE review_index_meta SET embedded=0
                WHERE embedded=1 AND NOT EXISTS (
                    SELECT 1 FROM review_chunks c
                    WHERE c.doc_id=review_index_meta.doc_id AND c.embedding IS NOT NULL)""")
            for doc_id, normalized in doi_updates:
                conn.execute("UPDATE paper_details SET doi=? WHERE doc_id=?", (normalized, doc_id))
        if str(conn.execute("PRAGMA quick_check").fetchone()[0]).lower() != "ok":
            raise sqlite3.IntegrityError("production quick_check failed after repair")
    finally:
        conn.close()
    if doi_updates:
        from utils import sync_excel_with_db
        metrics["excel_rows_synced"] = int(sync_excel_with_db(prod_db_path, excel_path))
    metrics["documents_marked_pending_reindex"] = len(nul_doc_ids) + metrics["false_complete_index_docs"]
    return RepairReport("production_micro_repair", True, prod_db_path, backup, metrics)


def hashlib_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _production_plan(conn: sqlite3.Connection) -> tuple[dict[str, int], list[str], list[tuple[str, str]]]:
    nul_doc_ids = [str(row[0]) for row in conn.execute(
        "SELECT id FROM papers WHERE instr(mineru_md, char(0)) > 0")]
    false_complete = int(conn.execute("""SELECT COUNT(*) FROM review_index_meta m
        WHERE m.embedded=1 AND NOT EXISTS (
            SELECT 1 FROM review_chunks c
            WHERE c.doc_id=m.doc_id AND c.embedding IS NOT NULL)""").fetchone()[0])
    doi_updates: list[tuple[str, str]] = []
    invalid = 0
    for doc_id, raw in conn.execute("""SELECT doc_id, doi FROM paper_details
                                       WHERE doi IS NOT NULL AND trim(doi) != ''"""):
        normalized = normalize_doi(raw)
        if not _DOI.match(normalized):
            invalid += 1
        elif normalized != raw:
            doi_updates.append((str(doc_id), normalized))
    metrics = {
        "documents_with_unsafe_text": len(nul_doc_ids),
        "false_complete_index_docs": false_complete,
        "doi_values_to_normalize": len(doi_updates),
        "invalid_or_suspect_doi_values": invalid,
    }
    return metrics, nul_doc_ids, doi_updates


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
