import os
import sqlite3

import pytest

from litreview.db_repair import (
    normalize_doi,
    repair_graph_shadow,
    repair_production_database,
    sanitize_extracted_text,
)
from litreview.graph_schema import init_graph_schema


def _fixtures(tmp_path):
    prod = tmp_path / "prod.db"
    graph = tmp_path / "graph.db"
    p = sqlite3.connect(prod)
    p.executescript("""
        CREATE TABLE papers(id TEXT PRIMARY KEY, title TEXT, processed_at TEXT);
        CREATE TABLE paper_details(
            doc_id TEXT PRIMARY KEY, title_zh TEXT, title_en TEXT, authors TEXT,
            journal TEXT, year TEXT, doc_type TEXT, doi TEXT, isbn TEXT,
            patent_no TEXT, doc_no TEXT, quarantine_status TEXT);
        INSERT INTO papers VALUES ('p1','First','2026-01-01'),('p2','Second','2026-01-02');
        INSERT INTO paper_details VALUES
          ('p1',NULL,NULL,'A','J','2025','paper','10.1/ONE',NULL,NULL,NULL,'NORMAL'),
          ('p2','第二篇',NULL,'B','J','2024','standard',NULL,NULL,NULL,'GB/T 1','FAILED');
    """)
    p.commit()
    p.close()
    g = sqlite3.connect(graph)
    init_graph_schema(g)
    g.execute("PRAGMA foreign_keys=OFF")
    g.execute("""INSERT INTO works
        (work_id, canonical_title, in_library, created_at, updated_at)
        VALUES ('p1','First',1,'now','now')""")
    g.execute("""INSERT INTO reference_mentions
        (mention_id,citing_work_id,ref_index,raw_text,raw_length,created_at)
        VALUES ('m1','p1',1,'ref',3,'now')""")
    g.execute("""INSERT INTO citation_assertions
        (assertion_id,edge_id,source_type,source_mention_id,asserted_at)
        VALUES ('a1','missing','LOCAL_PDF_PARSED','m1','now')""")
    g.commit()
    g.close()
    return prod, graph


def test_graph_repair_defaults_to_readonly_plan(tmp_path):
    prod, graph = _fixtures(tmp_path)
    before = (graph.stat().st_size, graph.stat().st_mtime_ns)

    report = repair_graph_shadow(str(prod), str(graph))

    assert report.applied is False
    assert report.metrics["documents_to_sync"] == 1
    assert report.metrics["orphan_assertions_to_quarantine"] == 1
    assert before == (graph.stat().st_size, graph.stat().st_mtime_ns)


def test_graph_repair_builds_validated_shadow_without_replacing_source(tmp_path):
    prod, graph = _fixtures(tmp_path)
    shadow = tmp_path / "shadow.db"
    source_before = graph.read_bytes()

    report = repair_graph_shadow(str(prod), str(graph), apply=True, shadow_path=str(shadow))

    assert report.applied is True
    assert report.metrics["documents_synced"] == 1
    assert report.metrics["assertions_quarantined"] == 1
    assert graph.read_bytes() == source_before
    conn = sqlite3.connect(shadow)
    try:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("SELECT COUNT(*) FROM citation_assertions").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM citation_assertion_quarantine").fetchone()[0] == 1
        assert conn.execute("SELECT quarantine_status FROM works WHERE work_id='p2'").fetchone()[0] == "QUARANTINED"
        assert conn.execute("SELECT provenance_source FROM reference_mentions").fetchone()[0] == "legacy_import"
        assert conn.execute("SELECT id_value FROM work_identifiers WHERE id_type='standard_no'").fetchone()[0] == "gb/t 1"
    finally:
        conn.close()


def test_graph_repair_refuses_live_path_or_overwrite(tmp_path):
    prod, graph = _fixtures(tmp_path)
    with pytest.raises(ValueError):
        repair_graph_shadow(str(prod), str(graph), apply=True, shadow_path=str(graph))
    existing = tmp_path / "exists.db"
    existing.write_bytes(b"do not overwrite")
    with pytest.raises(FileExistsError):
        repair_graph_shadow(str(prod), str(graph), apply=True, shadow_path=str(existing))


def _production_repair_fixture(path):
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE papers(id TEXT PRIMARY KEY, mineru_md TEXT);
        CREATE TABLE paper_details(doc_id TEXT PRIMARY KEY, doi TEXT);
        CREATE TABLE review_chunks(
            chunk_id INTEGER PRIMARY KEY, doc_id TEXT, content TEXT, embedding BLOB);
        CREATE TABLE review_index_meta(
            doc_id TEXT PRIMARY KEY, md_hash TEXT, n_chunks INTEGER,
            embedded INTEGER, indexed_at TEXT);
        CREATE TABLE review_evidence_cache(cache_key TEXT PRIMARY KEY, chunk_id INTEGER);
        INSERT INTO papers VALUES ('dirty', 'a' || char(0) || char(1) || char(10) || 'b'),
                                  ('false', 'clean');
        INSERT INTO paper_details VALUES ('dirty','10.1000/clean'),('false','10.1000/other');
        INSERT INTO review_chunks VALUES (1,'dirty','stale',X'00000000');
        INSERT INTO review_index_meta VALUES ('dirty','old',1,1,'now'),('false','old',0,1,'now');
        INSERT INTO review_evidence_cache VALUES ('cache',1);
    """)
    conn.commit()
    conn.close()


def test_sanitizers_are_deterministic_and_conservative():
    assert sanitize_extracted_text("a\x00\x01\t\n\rb") == "a\t\n\rb"
    assert normalize_doi(" https://doi.org/10.1000/ABC. ") == "10.1000/abc"


def test_production_repair_is_dry_run_by_default(tmp_path):
    prod = tmp_path / "prod.db"
    _production_repair_fixture(prod)
    before = prod.read_bytes()
    report = repair_production_database(str(prod))
    assert report.applied is False
    assert report.metrics["documents_with_unsafe_text"] == 1
    assert report.metrics["false_complete_index_docs"] == 1
    assert prod.read_bytes() == before


def test_production_repair_backs_up_and_marks_cleaned_text_pending(tmp_path):
    prod = tmp_path / "prod.db"
    backup = tmp_path / "prod.backup.db"
    _production_repair_fixture(prod)

    report = repair_production_database(str(prod), apply=True, backup_path=str(backup))

    assert report.applied is True
    assert backup.exists()
    backup_conn = sqlite3.connect(backup)
    try:
        assert "\x00" in backup_conn.execute(
            "SELECT mineru_md FROM papers WHERE id='dirty'").fetchone()[0]
    finally:
        backup_conn.close()
    conn = sqlite3.connect(prod)
    try:
        assert conn.execute("SELECT mineru_md FROM papers WHERE id='dirty'").fetchone()[0] == "a\nb"
        assert conn.execute("SELECT COUNT(*) FROM review_chunks WHERE doc_id='dirty'").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM review_evidence_cache").fetchone()[0] == 0
        assert conn.execute("SELECT embedded FROM review_index_meta WHERE doc_id='dirty'").fetchone()[0] == 0
        assert conn.execute("SELECT embedded FROM review_index_meta WHERE doc_id='false'").fetchone()[0] == 0
    finally:
        conn.close()
