import os
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
    
    # 验证 5 级互斥分区
    graph = stats["graph"]
    assert graph["production_baseline_normal"] == 341
    assert graph["production_baseline_quarantined"] == 3
    assert graph["verified_ingested_works"] == 3
    assert graph["quarantined_works_total"] == 4
    assert graph["external_unresolved_placeholders"] == 8766
    
    # 严格闭合求和: 341 + 3 + 3 + 1 + 8766 = 9114
    total_works = (
        graph["production_baseline_normal"] +
        graph["production_baseline_quarantined"] +
        graph["verified_ingested_works"] +
        (graph["quarantined_works_total"] - graph["production_baseline_quarantined"]) +
        graph["external_unresolved_placeholders"]
    )
    assert total_works == 9114
    assert graph["works_total"] == 9114


def test_library_store_list_documents():
    """测试 LibraryStore 独立多维分页检索馆藏"""
    res = list_library_documents(PROD_DB, TEST_CIT_DB, page=1, page_size=10, query="")
    assert "items" in res
    assert res["total"] >= 344
    assert len(res["items"]) == 10
    
    first = res["items"][0]
    assert "id" in first
    assert "outgoing_citations" in first
    assert "incoming_citations" in first


def test_library_store_document_detail():
    """测试 LibraryStore 独立单篇文献详情与引文消歧聚合"""
    res = list_library_documents(PROD_DB, TEST_CIT_DB, page=1, page_size=1)
    if res["items"]:
        doc_id = res["items"][0]["id"]
        detail = get_document_full_detail(doc_id, PROD_DB, TEST_CIT_DB)
        assert detail is not None
        assert detail["doc_id"] == doc_id
        assert "paper" in detail
        assert "reference_mentions" in detail
