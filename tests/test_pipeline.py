"""Tests for Candidate Ingestion & Graph Realignment Pipeline (Phase 3B)."""
import os
import io
import json
import sqlite3
import pypdf
import pytest
from unittest.mock import patch, MagicMock

from litreview.pipeline import (
    list_discovery_candidates,
    approve_candidates,
    acquire_fulltext,
    ingest_acquired_candidates,
    realign_citation_graph
)
from litreview.snowball import init_cache_db
from litreview.graph_schema import init_graph_schema


@pytest.fixture
def pipeline_env(tmp_path):
    """构建独立测试环境（缓存库、测试引文库、暂存目录）。"""
    cache_db = str(tmp_path / "external_discovery_cache.db")
    test_db = str(tmp_path / "test_citation_network.db")
    staging_dir = str(tmp_path / "staging_pdf")

    init_cache_db(cache_db)
    
    test_conn = sqlite3.connect(test_db)
    init_graph_schema(test_conn)
    # 插入一篇测试种子文献
    test_conn.execute("INSERT INTO works (work_id, canonical_title, publication_year, in_library, quarantine_status, eligible_for_citation_graph, created_at, updated_at) VALUES ('seed_1', '晶体结块机理与控制', 2017, 1, 'NORMAL', 1, '2026-08-01', '2026-08-01')")
    test_conn.execute("INSERT INTO work_identifiers (work_id, id_type, id_value, is_primary, created_at) VALUES ('seed_1', 'doi', '10.1016/j.powtec.2017.04.052', 1, '2026-08-01')")
    test_conn.commit()
    test_conn.close()

    # 在缓存库插入 2 条发现候选
    c_conn = sqlite3.connect(cache_db)
    c_conn.execute("""
        INSERT INTO discovery_runs (query_run_id, seeds_json, direction, depth, parameters_json, statistics_json, created_at)
        VALUES ('run_pipe_1', '["seed_1"]', 'both', 1, '{}', '{}', '2026-08-01 10:00:00')
    """)
    c_conn.execute("""
        INSERT INTO discovered_candidates (
            candidate_id, query_run_id, openalex_id, doi, title, authors_json,
            journal_or_venue, publication_year, cited_by_count, is_open_access,
            oa_url, landing_page_url, track, direction, source_seeds_json,
            co_citation_count, recommendation_reason, in_library, in_library_work_id,
            match_method, match_evidence_json, data_source, response_hash,
            lifecycle_status, discovered_at
        ) VALUES (
            'cand_oa_1', 'run_pipe_1', 'https://openalex.org/W999', '10.1038/s41598-024-55876-y',
            'Self-replicating segregation patterns in horizontally vibrated granular matter',
            '["Author A", "Author B"]', 'Scientific Reports', 2024, 5, 1,
            'https://example.com/fake_valid.pdf', 'https://doi.org/10.1038/s41598-024-55876-y',
            'EMERGING', 'forward', '["seed_1"]', 1, '近3年最新施引', 0, NULL, NULL, NULL,
            'openalex', 'h1', 'DISCOVERED', '2026-08-01 10:00:00'
        )
    """)
    c_conn.execute("""
        INSERT INTO discovered_candidates (
            candidate_id, query_run_id, openalex_id, doi, title, authors_json,
            journal_or_venue, publication_year, cited_by_count, is_open_access,
            oa_url, landing_page_url, track, direction, source_seeds_json,
            co_citation_count, recommendation_reason, in_library, in_library_work_id,
            match_method, match_evidence_json, data_source, response_hash,
            lifecycle_status, discovered_at
        ) VALUES (
            'cand_no_oa_2', 'run_pipe_1', 'https://openalex.org/W888', '10.1016/j.powtec.2024.120',
            'Closed Access Paper', '["Author C"]', 'Powder Tech', 2024, 2, 0,
            NULL, 'https://doi.org/10.1016/j.powtec.2024.120',
            'EMERGING', 'forward', '["seed_1"]', 1, '近3年施引', 0, NULL, NULL, NULL,
            'openalex', 'h2', 'DISCOVERED', '2026-08-01 10:00:00'
        )
    """)
    c_conn.commit()
    c_conn.close()

    return cache_db, test_db, staging_dir


def test_pipeline_list_and_approve(pipeline_env):
    """测试候选池查询过滤与审批流转。"""
    cache_db, test_db, staging_dir = pipeline_env

    # 1. 列表查询
    res = list_discovery_candidates(cache_db_path=cache_db, is_open_access=True)
    assert res["total_count"] == 1
    assert res["candidates"][0]["candidate_id"] == "cand_oa_1"
    assert res["candidates"][0]["lifecycle_status"] == "DISCOVERED"

    # 2. 审批通过 cand_oa_1
    app_res = approve_candidates(cache_db_path=cache_db, candidate_ids=["cand_oa_1"], action="APPROVE", reason="用户确认")
    assert app_res["updated_count"] == 1
    assert app_res["updated"][0]["new_status"] == "APPROVED"

    # 再次查询验证状态已更新
    res_app = list_discovery_candidates(cache_db_path=cache_db, lifecycle_status="APPROVED")
    assert res_app["total_count"] == 1
    assert res_app["candidates"][0]["candidate_id"] == "cand_oa_1"

    # 3. 状态机防倒退/防重复测试：再次对 APPROVED 状态的候选执行 APPROVE 应被拦截
    app_res_dup = approve_candidates(cache_db_path=cache_db, candidate_ids=["cand_oa_1"], action="APPROVE")
    assert app_res_dup["updated_count"] == 0
    assert app_res_dup["rejected_count"] == 1
    assert "非法生命周期状态流转" in app_res_dup["rejected"][0]["error"]

    # 4. 验证 candidate_decisions 触发器阻止 UPDATE 和 DELETE
    c_conn = sqlite3.connect(cache_db)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        c_conn.execute("UPDATE candidate_decisions SET action = 'HACKED' WHERE candidate_id = 'cand_oa_1'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        c_conn.execute("DELETE FROM candidate_decisions WHERE candidate_id = 'cand_oa_1'")
    c_conn.close()


def test_pipeline_acquisition_and_ingestion(pipeline_env):
    """测试合法 OA PDF 下载、解析入库与引文对齐全链路。"""
    cache_db, test_db, staging_dir = pipeline_env

    # 审批
    approve_candidates(cache_db_path=cache_db, candidate_ids=["cand_oa_1"], action="APPROVE")

    # 创建一个极小合法测试 PDF
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=200, height=200)
    pdf_buf = io.BytesIO()
    writer.write(pdf_buf)
    valid_pdf_bytes = pdf_buf.getvalue()

    # Mock 下载
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_resp = MagicMock()
        mock_resp.read.return_value = valid_pdf_bytes
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        acq_res = acquire_fulltext(cache_db_path=cache_db, staging_dir=staging_dir, max_fetch=2)
        assert acq_res["acquired_count"] == 1
        assert acq_res["results"][0]["status"] == "FULLTEXT_ACQUIRED"
        assert os.path.exists(acq_res["results"][0]["file_path"])

    # 执行入库与切块
    ing_res = ingest_acquired_candidates(
        cache_db_path=cache_db, test_db_path=test_db, staging_dir=staging_dir, max_ingest=2
    )
    assert ing_res["ingested_count"] == 1
    ingested = ing_res["ingested_works"][0]
    assert ingested["lifecycle_status"] in ("VECTOR_INDEXED", "VECTOR_INDEXED_TEST")
    assert ingested["chunks_persisted"] > 0
    assert ingested["embeddings_persisted"] > 0
    assert ingested["new_work_id"].startswith("work_oa_")

    # 验证测试引文库数据已写入
    conn = sqlite3.connect(test_db)
    w_row = conn.execute("SELECT canonical_title, in_library, quarantine_status FROM works WHERE work_id = ?", (ingested["new_work_id"],)).fetchone()
    assert w_row[0] == "Self-replicating segregation patterns in horizontally vibrated granular matter"
    assert w_row[1] == 1
    assert w_row[2] == "NORMAL"

    id_doi = conn.execute("SELECT id_value FROM work_identifiers WHERE work_id = ? AND id_type = 'doi'", (ingested["new_work_id"],)).fetchone()[0]
    assert id_doi == "10.1038/s41598-024-55876-y"

    # 验证 document_assets 资产字段完整性
    asset_row = conn.execute("SELECT file_size_bytes, page_count, mime_type, source_url, acquired_at FROM document_assets WHERE work_id = ?", (ingested["new_work_id"],)).fetchone()
    assert asset_row is not None
    assert asset_row[0] == len(valid_pdf_bytes)
    assert asset_row[1] == 1
    assert asset_row[2] == "application/pdf"
    assert asset_row[3] is not None
    assert asset_row[4] is not None

    # 验证 review_chunks 与 review_index_meta 已写入
    chk_cnt = conn.execute("SELECT COUNT(*) FROM review_chunks WHERE doc_id = ?", (ingested["new_work_id"],)).fetchone()[0]
    assert chk_cnt > 0
    meta_cnt = conn.execute("SELECT COUNT(*) FROM review_index_meta WHERE doc_id = ?", (ingested["new_work_id"],)).fetchone()[0]
    assert meta_cnt == 1
    conn.close()

    # 验证 candidate_decisions 审计记录
    c_conn = sqlite3.connect(cache_db)
    d_row = c_conn.execute("SELECT action, decision_source, previous_status, new_status FROM candidate_decisions WHERE candidate_id = 'cand_oa_1'").fetchone()
    assert d_row is not None
    assert d_row[0] == "APPROVE"
    assert d_row[1] == "USER"
    assert d_row[2] == "DISCOVERED"
    assert d_row[3] == "APPROVED"
    c_conn.close()
