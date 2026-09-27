import os
import sqlite3

import pytest

from litreview.library_store import (
    get_dashboard_stratified_stats,
    list_library_documents,
    get_document_full_detail,
    list_discovery_candidates_paginated
)

import lit_seed as seed

# 生产库只读核对（作者机器）：LIT_ALLOW_PROD_READONLY=1 pytest -m prod_readonly
PROD_DB = "/mnt/ripe/literature_analyzer_data/batch_tracking.db"


def _assert_partition_closes(graph):
    """5 级互斥分区必须恰好覆盖全部 works。"""
    total_works = (
        graph["production_baseline_normal"] +
        graph["production_baseline_quarantined"] +
        graph["verified_ingested_works"] +
        (graph["quarantined_works_total"] - graph["production_baseline_quarantined"]) +
        graph["external_unresolved_placeholders"]
    )
    assert total_works == graph["works_total"]


def test_library_store_dashboard_stats(seeded_dbs):
    """测试 LibraryStore 独立计算仪表盘指标与 5 级闭合分区"""
    stats = get_dashboard_stratified_stats(seeded_dbs.prod_db, seeded_dbs.citation_db, seeded_dbs.cache_db)
    graph = stats["graph"]
    _assert_partition_closes(graph)
    assert graph["production_baseline_normal"] == 3
    assert graph["production_baseline_quarantined"] == 1
    assert graph["verified_ingested_works"] == 1
    assert graph["quarantined_works_total"] == 2  # 隔离文献 Q + 孤儿 Work
    assert graph["external_unresolved_placeholders"] == 2
    assert graph["edges_total"] == graph["confirmed_edges"] + graph["candidate_edges"] + graph["unresolved_edges"]
    assert stats["library"]["total_docs"] == 4
    assert stats["candidates"]["total_candidates"] == 3


def test_library_store_dashboard_stats_missing_graph_db(seeded_dbs, tmp_path):
    """图谱库缺失时不能抛异常，也不能把别处的数字冒充成本库统计。"""
    stats = get_dashboard_stratified_stats(seeded_dbs.prod_db, str(tmp_path / "missing.db"), seeded_dbs.cache_db)
    assert stats["library"]["total_docs"] == 4
    assert "graph" in stats


def test_library_store_list_documents(seeded_dbs):
    """测试 LibraryStore 独立多维分页检索馆藏"""
    res = list_library_documents(seeded_dbs.prod_db, seeded_dbs.citation_db, page=1, page_size=10, query="")
    assert res["total"] == 4
    by_id = {it["id"]: it for it in res["items"]}
    assert by_id[seed.DOC_A]["incoming_citations"] == 3
    assert by_id[seed.DOC_B]["outgoing_citations"] == 2
    assert by_id[seed.DOC_C]["outgoing_citations"] == 1

    hit = list_library_documents(seeded_dbs.prod_db, seeded_dbs.citation_db, page=1, page_size=10,
                                 query="urea")
    assert [it["id"] for it in hit["items"]] == [seed.DOC_C]


def test_library_store_document_detail(seeded_dbs):
    """测试 LibraryStore 独立单篇文献详情与引文消歧聚合"""
    detail = get_document_full_detail(seed.DOC_C, seeded_dbs.prod_db, seeded_dbs.citation_db)
    assert detail["doc_id"] == seed.DOC_C
    assert detail["paper"]["id"] == seed.DOC_C
    assert len(detail["reference_mentions"]) == 2
    stats = detail["citation_network"]["network_statistics"]
    assert stats["confirmed_in_library_outgoing_count"] == 1
    assert stats["in_library_cited_by_count"] == 1  # B → C


def test_library_store_list_discovery_candidates(seeded_dbs):
    res = list_discovery_candidates_paginated(seeded_dbs.cache_db, page=1, page_size=10, status="DISCOVERED")
    assert {c["candidate_id"] for c in res["items"]} == {seed.CAND_DISCOVERED, seed.CAND_DISCOVERED_2}


@pytest.mark.prod_readonly
def test_library_store_prod_partition_closes():
    """作者机器上的真实库：分区闭合。图谱库路径用 LIT_PROD_CITATION_DB 指定（真实生产库的图谱副本）。"""
    cit_db = os.getenv("LIT_PROD_CITATION_DB")
    if not cit_db:
        pytest.skip("set LIT_PROD_CITATION_DB to the real test_citation_network.db")
    stats = get_dashboard_stratified_stats(PROD_DB, cit_db, os.getenv("LIT_PROD_CACHE_DB", ""))
    _assert_partition_closes(stats["graph"])


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
