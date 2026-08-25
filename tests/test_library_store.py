import os
import sqlite3
import pytest
from litreview.library_store import (
    get_dashboard_stratified_stats,
    list_library_documents,
    get_document_full_detail,
    list_discovery_candidates_paginated
)

PROD_DB = "/mnt/ripe/literature_analyzer_data/batch_tracking.db"
TEST_CIT_DB = os.path.join(os.path.dirname(__file__), "..", "data", "test_citation_network.db")
CACHE_DB = os.path.join(os.path.dirname(__file__), "..", "data", "external_discovery_cache.db")


def test_library_store_dashboard_stats():
    """测试 LibraryStore 独立计算仪表盘指标与 5 级闭合分区"""
    stats = get_dashboard_stratified_stats(PROD_DB, TEST_CIT_DB, CACHE_DB)
    assert "library" in stats
    assert "graph" in stats
    assert "candidates" in stats
    
    # 验证 5 级互斥分区随当前数据库动态闭合
    graph = stats["graph"]
    total_works = (
        graph["production_baseline_normal"] +
        graph["production_baseline_quarantined"] +
        graph["verified_ingested_works"] +
        (graph["quarantined_works_total"] - graph["production_baseline_quarantined"]) +
        graph["external_unresolved_placeholders"]
    )
    assert total_works == graph["works_total"]


def test_library_store_list_documents():
    """测试 LibraryStore 独立多维分页检索馆藏"""
    try:
        res = list_library_documents(PROD_DB, TEST_CIT_DB, page=1, page_size=10, query="")
    except sqlite3.OperationalError as exc:
        if "unable to open database file" in str(exc):
            pytest.skip("production database is not readable in this environment")
        raise
    assert "items" in res
    assert res["total"] >= len(res["items"])
    assert len(res["items"]) <= 10
    
    first = res["items"][0]
    assert "id" in first
    assert "outgoing_citations" in first
    assert "incoming_citations" in first


def test_library_store_document_detail():
    """测试 LibraryStore 独立单篇文献详情与引文消歧聚合"""
    try:
        res = list_library_documents(PROD_DB, TEST_CIT_DB, page=1, page_size=1)
    except sqlite3.OperationalError as exc:
        if "unable to open database file" in str(exc):
            pytest.skip("production database is not readable in this environment")
        raise
    if res["items"]:
        doc_id = res["items"][0]["id"]
        detail = get_document_full_detail(doc_id, PROD_DB, TEST_CIT_DB)
        assert detail is not None
        assert detail["doc_id"] == doc_id
        assert "paper" in detail
        assert "reference_mentions" in detail


def test_library_store_enrich_document_metadata_persists_volume_issue_pages(tmp_path, monkeypatch):
    """测试 enrich_document_metadata 能够将 volume, issue, pages 持久化回写至 works 表。"""
    from litreview.library_store import enrich_document_metadata
    from litreview.graph_schema import GRAPH_SCHEMA_DDL

    prod_db = str(tmp_path / "prod.db")
    cit_db = str(tmp_path / "cit.db")

    conn_p = sqlite3.connect(prod_db)
    conn_p.execute("CREATE TABLE papers (id TEXT PRIMARY KEY, title TEXT, language TEXT, status TEXT, result_json TEXT)")
    conn_p.execute("""CREATE TABLE paper_details (
        doc_id TEXT PRIMARY KEY, title TEXT, title_zh TEXT, title_en TEXT, doi TEXT,
        authors TEXT, journal TEXT, year TEXT, keywords TEXT, n_figures INTEGER,
        n_tables INTEGER, n_refs INTEGER, enriched_at TEXT, source TEXT
    )""")
    conn_p.execute("INSERT INTO papers (id, title, status) VALUES ('doc_test_123', 'Test Phosphorus Paper', 'EXPORTED')")
    conn_p.execute("INSERT INTO paper_details (doc_id, title, doi) VALUES ('doc_test_123', 'Test Phosphorus Paper', '10.1016/j.cej.2023.141234')")
    conn_p.commit()
    conn_p.close()

    conn_c = sqlite3.connect(cit_db)
    conn_c.executescript(GRAPH_SCHEMA_DDL)
    conn_c.execute("""
        INSERT INTO works (work_id, work_type, canonical_title, in_library, created_at, updated_at)
        VALUES ('doc_test_123', 'paper', 'Test Phosphorus Paper', 1, '2026-08-25 10:00:00', '2026-08-25 10:00:00')
    """)
    conn_c.commit()
    conn_c.close()

    # Mock fetch_openalex_by_doi to return volume, issue, pages
    def mock_fetch_openalex(doi, timeout=8):
        return {
            "doi": doi,
            "title": "Test Phosphorus Paper Canonical",
            "authors": "Zhang San, Li Si",
            "authors_list": ["Zhang San", "Li Si"],
            "journal": "Chemical Engineering Journal",
            "volume": "450",
            "issue": "2",
            "pages": "123-134",
            "year": "2023",
            "publication_year": 2023,
            "openalex_id": "W1234567890",
            "source": "openalex"
        }

    monkeypatch.setattr("litreview.fetcher.fetch_openalex_by_doi", mock_fetch_openalex)

    res = enrich_document_metadata("doc_test_123", prod_db_path=prod_db, test_citation_db_path=cit_db)
    assert res["success"] is True

    conn_c = sqlite3.connect(cit_db)
    c = conn_c.cursor()
    row = c.execute("SELECT journal_or_venue, publication_year, volume, issue, pages FROM works WHERE work_id = 'doc_test_123'").fetchone()
    conn_c.close()

    assert row is not None
    assert row[0] == "Chemical Engineering Journal"
    assert row[1] == 2023
    assert row[2] == "450"
    assert row[3] == "2"
    assert row[4] == "123-134"
