"""Tests for OpenAlex & Crossref Citation Snowball Engine (Phase 3 MVP)."""
import os
import sqlite3
from unittest.mock import patch

import pytest

from litreview.snowball import (
    OpenAlexClient,
    CrossrefClient,
    expand_citations,
    format_snowball_markdown_report,
    init_cache_db
)
from litreview.graph_schema import init_graph_schema


def test_openalex_client_parameters():
    """验证 OpenAlex API 客户端 User-Agent 与 api_key/mailto 查询参数拼接。"""
    client = OpenAlexClient(email="user@test.org", api_key="test_oa_key")
    url = client._build_url("https://api.openalex.org/works", {"filter": "openalex:W100"})
    assert "api_key=test_oa_key" in url
    assert "mailto=user%40test.org" in url
    assert "literature_analyzer/1.0" in client.user_agent
    assert "(mailto:user@test.org)" in client.user_agent


def test_crossref_client_parameters():
    """验证 Crossref API 客户端 User-Agent 与 mailto 参数。"""
    client = CrossrefClient(email="user@test.org")
    assert "literature_analyzer/1.0" in client.user_agent
    assert "(mailto:user@test.org)" in client.user_agent


def test_canonical_candidate_view_prefers_state_over_recency(tmp_path):
    cache = tmp_path / "cache.db"
    init_cache_db(str(cache))
    conn = sqlite3.connect(cache)
    conn.execute("INSERT INTO discovery_runs VALUES ('r1','[]','both',1,'{}','{}','2026-01-01')")
    conn.execute("INSERT INTO discovery_runs VALUES ('r2','[]','both',1,'{}','{}','2026-08-01')")
    sql = """INSERT INTO discovered_candidates
        (candidate_id,query_run_id,openalex_id,title,track,direction,source_seeds_json,
         co_citation_count,recommendation_reason,in_library,data_source,response_hash,
         lifecycle_status,discovered_at)
        VALUES (?,?, 'W1','Same','FOUNDATIONAL','backward','[]',0,'',0,'openalex','',?,?)"""
    conn.execute(sql, ('old-indexed', 'r1', 'VECTOR_INDEXED', '2026-01-01'))
    conn.execute(sql, ('new-discovered', 'r2', 'DISCOVERED', '2026-08-01'))
    row = conn.execute("SELECT candidate_id, canonical_key FROM v_canonical_candidates").fetchone()
    conn.close()
    assert row == ('old-indexed', 'openalex:w1')


def test_snowball_ledger_and_governance_fixed_fixture(tmp_path):
    """基于固定 OpenAlex 响应夹具验证账本闭环、无虚构施引关系与独立缓存库存储。"""
    # 构建本地测试馆藏库供碰撞检测
    net_db = tmp_path / "test_net.db"
    conn = sqlite3.connect(str(net_db))
    init_graph_schema(conn)
    conn.execute("INSERT INTO works (work_id, canonical_title, publication_year, in_library, quarantine_status, eligible_for_citation_graph, created_at, updated_at) VALUES ('w_local_1', 'Caking of amorphous powders', 2010, 1, 'NORMAL', 1, '2026-08-01', '2026-08-01')")
    conn.execute("INSERT INTO work_identifiers (work_id, id_type, id_value, is_primary, created_at) VALUES ('w_local_1', 'doi', '10.1016/j.powtec.2010.04.014', 1, '2026-08-01')")
    conn.commit()
    conn.close()

    cache_db = tmp_path / "external_discovery_cache.db"

    mock_seed_cf = {
        "id": "https://openalex.org/W2609485513",
        "doi": "https://doi.org/10.1016/j.powtec.2017.04.052",
        "title": "Caking of crystals: Characterization, mechanisms and prevention",
        "publication_year": 2017,
        "cited_by_count": 83,
        "referenced_works": ["https://openalex.org/W100", "https://openalex.org/W200"]
    }
    mock_seed_6a = {
        "id": "https://openalex.org/W2589978917",
        "doi": "https://doi.org/10.1016/j.powtec.2017.02.024",
        "title": "A review of bulk powder caking",
        "publication_year": 2017,
        "cited_by_count": 177,
        "referenced_works": ["https://openalex.org/W100", "https://openalex.org/W300"]
    }

    mock_batch_refs = [
        {
            "id": "https://openalex.org/W100",
            "doi": "https://doi.org/10.1016/j.powtec.2010.04.014",
            "title": "Caking of amorphous powders",
            "publication_year": 2010,
            "cited_by_count": 132,
            "authorships": [{"author": {"display_name": "Author A"}}],
            "primary_location": {"source": {"display_name": "Powder Technology"}},
            "open_access": {"is_oa": True, "oa_url": "https://example.com/oa.pdf"}
        },
        {
            "id": "https://openalex.org/W200",
            "doi": "https://doi.org/10.1016/j.powtec.2005.05.018",
            "title": "Investigations on the caking behaviour of bulk solids",
            "publication_year": 2005,
            "cited_by_count": 47,
            "authorships": [{"author": {"display_name": "Author B"}}],
            "primary_location": {"source": {"display_name": "Powder Technology"}},
            "open_access": {"is_oa": False}
        },
        {
            "id": "https://openalex.org/W300",
            "doi": "https://doi.org/10.1016/j.powtec.2008.04.062",
            "title": "Understanding powder caking",
            "publication_year": 2008,
            "cited_by_count": 48,
            "authorships": [{"author": {"display_name": "Author D"}}],
            "primary_location": {"source": {"display_name": "Powder Technology"}},
            "open_access": {"is_oa": False}
        }
    ]

    # Citing works: 一个明确引用种子 W2609485513，另一个引用未知种子 (未匹配)
    mock_citing = [
        {
            "id": "https://openalex.org/W500",
            "doi": "https://doi.org/10.3390/cryst14080676",
            "title": "Prevention of Crystal Agglomeration: Mechanisms, Factors, and Impact",
            "publication_year": 2024,
            "cited_by_count": 52,
            "authorships": [{"author": {"display_name": "Author C"}}],
            "primary_location": {"source": {"display_name": "Crystals"}},
            "open_access": {"is_oa": True, "oa_url": "https://mdpi.com/pdf"},
            "referenced_works": ["https://openalex.org/W2609485513"]
        },
        {
            "id": "https://openalex.org/W600",
            "doi": "https://doi.org/10.1016/j.partic.2024.06.013",
            "title": "Spreadability of powders for additive manufacturing",
            "publication_year": 2024,
            "cited_by_count": 30,
            "authorships": [{"author": {"display_name": "Author E"}}],
            "primary_location": {"source": {"display_name": "Particuology"}},
            "open_access": {"is_oa": False},
            "referenced_works": ["https://openalex.org/W99999"]  # 未直接列出两篇种子
        }
    ]

    with patch.object(OpenAlexClient, "get_work_by_doi") as mock_get_doi, \
         patch.object(OpenAlexClient, "batch_get_works") as mock_batch, \
         patch.object(OpenAlexClient, "get_citing_works") as mock_cites:

        def side_doi(doi):
            if "2017.04.052" in doi:
                return mock_seed_cf
            elif "2017.02.024" in doi:
                return mock_seed_6a
            return None

        mock_get_doi.side_effect = side_doi
        mock_batch.return_value = mock_batch_refs
        mock_cites.return_value = (2, mock_citing)

        res = expand_citations(
            seed_ids=["10.1016/j.powtec.2017.04.052", "10.1016/j.powtec.2017.02.024"],
            direction="both",
            depth=1,
            limit_per_track=5,
            db_path=str(net_db),
            cache_db_path=str(cache_db)
        )

        assert "error" not in res
        stats = res["statistics"]

        # 账本断言
        assert stats["seeds_count"] == 2
        assert stats["backward_raw_mentions"] == 4  # 2 + 2
        assert stats["backward_unique_ids_before_limit"] == 3  # W100, W200, W300
        assert stats["backward_metadata_requested"] == 3
        assert stats["backward_metadata_returned"] == 3
        assert stats["metadata_coverage"] == 1.0
        assert stats["forward_total_available"] == 2
        assert stats["forward_records_retrieved"] == 2
        assert stats["forward_records_truncated"] is False
        assert stats["materialized_candidates_before_dedup"] == 5  # 3 found + 2 emerg
        assert stats["unique_candidates_after_dedup"] == 5

        # 验证碰撞匹配 (W100 命中本地馆藏 w_local_1)
        assert stats["in_library_matches_count"] == 1
        assert stats["new_external_candidates_count"] == 4
        assert res["in_library_matches"][0]["in_library_work_id"] == "w_local_1"
        assert res["in_library_matches"][0]["match_method"] == "exact_doi"

        # 验证无虚构施引关系：W600 的 source_seeds 必须为 SOURCE_SEED_UNKNOWN
        w600 = [e for e in res["top_emerging_literature"] if e["openalex_id"] == "https://openalex.org/W600"][0]
        assert w600["source_seeds"] == ["SOURCE_SEED_UNKNOWN"]

        # 验证缓存数据库持久化成功
        assert os.path.exists(cache_db)
        cconn = sqlite3.connect(str(cache_db))
        r_cnt = cconn.execute("SELECT COUNT(*) FROM discovery_runs").fetchone()[0]
        assert r_cnt == 1
        cand_cnt = cconn.execute("SELECT COUNT(*) FROM discovered_candidates WHERE lifecycle_status='DISCOVERED'").fetchone()[0]
        assert cand_cnt == 5
        cconn.close()

        # 报告渲染断言
        report_md = format_snowball_markdown_report(res)
        assert f"Run ID: `{res['query_run_id']}`" in report_md
        assert "原始引用提及 **4** 条" in report_md
        assert "唯一 OpenAlex ID **3** 篇" in report_md
        assert "API 请求成功率" in report_md
        assert "引文覆盖率" not in report_md


@pytest.mark.network
def test_snowball_real_api_smoke():
    """保留真实 OpenAlex API 连通性 Smoke Test。"""
    client = OpenAlexClient()
    w = client.get_work_by_doi("10.1016/j.powtec.2017.04.052")
    assert w is not None
    assert w.get("id") == "https://openalex.org/W2609485513"
    assert w.get("publication_year") == 2017
