"""Tests for Local Citation Network MVP (Phase 2B)."""
import os
import json
import sqlite3
import pytest
from pathlib import Path

from litreview.citation_network import (
    build_test_citation_network_db,
    query_citation_network
)
from litreview.graph_schema import init_graph_schema


@pytest.fixture(scope="module")
def network_db(tmp_path_factory):
    """构建独立测试引文网络数据库用于单元测试。"""
    prod_db = "/mnt/ripe/literature_analyzer_data/batch_tracking.db"
    test_db = str(tmp_path_factory.mktemp("cit_net") / "test_citation_network.db")
    
    try:
        stats = build_test_citation_network_db(prod_db, test_db)
    except sqlite3.OperationalError as exc:
        if "unable to open database file" in str(exc):
            pytest.skip("production database is not readable in this environment")
        raise
    return test_db, stats


def test_network_db_build_and_accounting(network_db):
    """验证独立测试引文网络库的构建账本、实体解耦与数据闭环。"""
    db_path, stats = network_db
    
    assert stats["total_canonical_in_library_works"] > 0
    assert stats["total_references_processed"] == (
        stats["confirmed_in_library_links"] + stats["candidate_matches_count"]
        + stats["unresolved_external_references"])
    # 多条原始引用可解析到同一 (citing, cited) Work 对；边会去重，断言保留来源。
    assert stats["total_citation_edges"] <= stats["total_references_processed"]
    assert stats["total_citation_edges"] <= stats["total_citation_assertions"]
    assert stats["total_citation_assertions"] <= stats["total_references_processed"]
    assert stats["total_works_in_db"] >= stats["total_canonical_in_library_works"]

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    c = conn.cursor()

    # 1. 验证当前生产馆藏 work 全部进入图谱，不依赖历史快照数量。
    in_lib_works = c.execute("SELECT COUNT(*) FROM works WHERE in_library = 1").fetchone()[0]
    assert in_lib_works == stats["total_canonical_in_library_works"]

    quarantined = c.execute("SELECT COUNT(*) FROM works WHERE quarantine_status = 'QUARANTINED'").fetchone()[0]
    assert quarantined == 3  # a93f1740, a5620039, 8d73f880

    # 2. 验证外部临时 Work 状态均为 UNRESOLVED
    ext_works = c.execute("SELECT COUNT(*) FROM works WHERE in_library = 0").fetchone()[0]
    assert ext_works == stats["total_works_in_db"] - in_lib_works
    unresolved_ext = c.execute("SELECT COUNT(*) FROM works WHERE in_library = 0 AND quarantine_status = 'UNRESOLVED'").fetchone()[0]
    assert unresolved_ext == ext_works

    # 3. 验证 CONFIRMED 边及其可引用馆藏汇总视图保持闭合。
    confirmed_edges = c.execute("SELECT COUNT(*) FROM citation_edges WHERE edge_status = 'CONFIRMED'").fetchone()[0]
    assert confirmed_edges <= stats["confirmed_in_library_links"]

    summary_in_lib = c.execute("SELECT COUNT(*) FROM v_citation_edges_summary WHERE in_library_edge = 1").fetchone()[0]
    assert summary_in_lib <= confirmed_edges

    # 4. 验证 CANDIDATE 边未自动合并 Work 且 edge_status 严格为 CANDIDATE
    cand_edges = c.execute("SELECT COUNT(*) FROM citation_edges WHERE edge_status = 'CANDIDATE'").fetchone()[0]
    assert cand_edges <= stats["candidate_matches_count"]

    # 5. 验证无自环引用 (citing_work_id != cited_work_id)
    self_citations = c.execute("SELECT COUNT(*) FROM citation_edges WHERE citing_work_id = cited_work_id").fetchone()[0]
    assert self_citations == 0

    conn.close()


def test_seed_1_cf693c6c_citation_subgraph(network_db):
    """实测种子文献 1: cf693c6c (Chen et al., Caking of crystals) 的入度与被引关系。"""
    db_path, _ = network_db
    res = query_citation_network(db_path, "cf693c6c", include_candidates=True)
    
    assert "error" not in res
    assert res["seed_work"]["work_id"].startswith("cf693c6c")
    assert res["network_statistics"]["total_references_in_seed"] == 126
    assert res["network_statistics"]["in_library_cited_by_count"] == 4

    # 验证 4 篇施引文献
    citing_ids = [inc["citing_work_id"][:8] for inc in res["incoming_citations"]["confirmed_in_library"]]
    assert "bba4a59d" in citing_ids
    assert "5324d9dd" in citing_ids


def test_seed_2_bba4a59d_citation_subgraph(network_db):
    """实测种子文献 2: bba4a59d (Understanding caking phenomena) 的出度与共被引。"""
    db_path, _ = network_db
    res = query_citation_network(db_path, "bba4a59d", include_candidates=True)
    
    assert "error" not in res
    assert res["seed_work"]["work_id"].startswith("bba4a59d")
    assert res["network_statistics"]["total_references_in_seed"] == 55
    assert res["network_statistics"]["confirmed_in_library_outgoing_count"] == 5

    # 验证引用的 5 篇馆藏文献包含 cf693c6c, b1841cec, 6a3736de 等
    cited_ids = [out["work_id"][:8] for out in res["outgoing_citations"]["confirmed_in_library"]]
    assert "cf693c6c" in cited_ids
    assert "b1841cec" in cited_ids
    assert "6a3736de" in cited_ids

    # 验证共被引关联
    assert res["network_statistics"]["top_bibliographic_coupling_count"] >= 1


def test_seed_3_bab0f660_citation_subgraph(network_db):
    """实测种子文献 3: bab0f660 (包膜缓释肥料综述) 的出度引文网络。"""
    db_path, _ = network_db
    res = query_citation_network(db_path, "bab0f660", include_candidates=True)
    
    assert "error" not in res
    assert res["seed_work"]["work_id"].startswith("bab0f660")
    assert res["network_statistics"]["total_references_in_seed"] == 211
    assert res["network_statistics"]["confirmed_in_library_outgoing_count"] == 3

    cited_ids = [out["work_id"][:8] for out in res["outgoing_citations"]["confirmed_in_library"]]
    assert "1470d81b" in cited_ids
    assert "6138acc3" in cited_ids
    assert "69243c4e" in cited_ids
