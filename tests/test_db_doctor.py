import os
import sqlite3

from litreview.db_doctor import inspect_databases
from litreview.graph_schema import init_graph_schema
from litreview.snowball import init_cache_db


def _production(path):
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE papers (id TEXT PRIMARY KEY, mineru_md TEXT);
        CREATE TABLE paper_details (doc_id TEXT PRIMARY KEY, doi TEXT, quarantine_status TEXT);
        CREATE TABLE review_chunks (
            chunk_id INTEGER PRIMARY KEY, doc_id TEXT, content TEXT, embedding BLOB);
        CREATE TABLE review_index_meta (
            doc_id TEXT PRIMARY KEY, embedded INTEGER, n_chunks INTEGER);
        INSERT INTO papers VALUES ('p1', 'clean'), ('p2', char(0) || 'dirty'), ('p3', 'not synced');
        INSERT INTO paper_details VALUES
            ('p1', '10.1/SAME', 'NORMAL'), ('p2', '10.1/same', 'FAILED'),
            ('p3', '10.1/unique', 'NORMAL');
        INSERT INTO review_chunks VALUES
            (1, 'p1', 'ok', X'00000000'), (2, 'p2', char(0) || 'bad', NULL);
        INSERT INTO review_index_meta VALUES ('p1', 1, 1), ('p2', 1, 1);
    """)
    conn.commit()
    conn.close()


def _graph(path):
    conn = sqlite3.connect(path)
    init_graph_schema(conn)
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("""INSERT INTO works
        (work_id, canonical_title, quarantine_status, in_library, created_at, updated_at)
        VALUES ('p1', 'One', 'NORMAL', 1, 'now', 'now')""")
    conn.execute("""INSERT INTO works
        (work_id, canonical_title, quarantine_status, in_library, created_at, updated_at)
        VALUES ('p2', 'Two', 'NORMAL', 1, 'now', 'now')""")
    conn.execute("""INSERT INTO citation_assertions
        (assertion_id, edge_id, source_type, asserted_at)
        VALUES ('a1', 'missing-edge', 'LOCAL_PDF_PARSED', 'now')""")
    conn.commit()
    conn.close()


def _cache(path):
    init_cache_db(str(path))
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO discovery_runs VALUES ('r1','[]','both',1,'{}','{}','now')")
    conn.execute("INSERT INTO discovery_runs VALUES ('r2','[]','both',1,'{}','{}','now')")
    template = """INSERT INTO discovered_candidates
        (candidate_id, query_run_id, openalex_id, title, track, direction,
         source_seeds_json, co_citation_count, recommendation_reason, in_library,
         data_source, response_hash, lifecycle_status, discovered_at)
        VALUES (?, ?, 'W1', 'Title', 'FOUNDATIONAL', 'backward', '[]', 0, '', 0,
                'openalex', '', ?, 'now')"""
    conn.execute(template, ('c1', 'r1', 'DISCOVERED'))
    conn.execute(template, ('c2', 'r2', 'VECTOR_INDEXED'))
    conn.commit()
    conn.close()


def test_inspect_databases_detects_cross_database_health_issues(tmp_path):
    prod, graph, cache = (tmp_path / name for name in ("prod.db", "graph.db", "cache.db"))
    _production(prod)
    _graph(graph)
    _cache(cache)
    before = {p: (p.stat().st_size, p.stat().st_mtime_ns) for p in (prod, graph, cache)}

    report = inspect_databases(str(prod), str(graph), str(cache))

    codes = {issue.code for issue in report.issues}
    assert {"false_complete_index", "chunks_without_embeddings", "nul_in_chunks",
            "nul_in_source_text", "duplicate_doi_groups", "orphan_citation_assertions",
            "production_missing_from_graph", "quarantine_status_drift",
            "duplicate_candidate_entities", "candidate_status_conflicts"} <= codes
    assert report.metrics["production_false_complete_docs"] == 1
    assert report.metrics["graph_orphan_assertions"] == 1
    assert report.metrics["candidate_status_conflict_groups"] == 1
    assert report.to_dict()["databases"][0]["name"] == "production"
    assert before == {p: (p.stat().st_size, p.stat().st_mtime_ns) for p in (prod, graph, cache)}


def test_inspect_databases_reports_missing_database_without_aborting(tmp_path):
    prod = tmp_path / "prod.db"
    _production(prod)

    report = inspect_databases(str(prod), str(tmp_path / "missing-graph.db"),
                               str(tmp_path / "missing-cache.db"))

    assert sum(issue.code == "database_missing" for issue in report.issues) == 2
    assert report.metrics["production_false_complete_docs"] == 1
