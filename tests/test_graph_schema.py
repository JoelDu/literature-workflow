"""Tests for Phase 2A Graph & Master Literature Schema Architecture."""
import sqlite3
import pytest
from litreview.graph_schema import init_graph_schema


def test_init_graph_schema_creates_all_ten_tables(tmp_path):
    """验证 10 张引文图谱与主数据表结构全部正确创建。"""
    db_path = tmp_path / "test_graph.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON")
    init_graph_schema(conn)

    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {r[0] for r in cursor.fetchall()}

    expected_tables = {
        "works",
        "work_identifiers",
        "work_titles",
        "document_assets",
        "parse_runs",
        "reference_mentions",
        "resolution_candidates",
        "resolution_decisions",
        "citation_edges",
        "citation_assertions",
    }
    assert expected_tables.issubset(tables)

    # 验证主键与外键关联 (Work -> Identifier, Assets, Mentions, Edges)
    conn.execute("""
        INSERT INTO works (work_id, work_type, canonical_title, publication_year, created_at, updated_at)
        VALUES ('w_001', 'journal_article', '测试磷酸萃取研究', 2023, '2026-08-18 12:00:00', '2026-08-18 12:00:00')
    """)

    conn.execute("""
        INSERT INTO work_identifiers (work_id, id_type, id_value, is_primary, created_at)
        VALUES ('w_001', 'doi', '10.1000/test.doi', 1, '2026-08-18 12:00:00')
    """)

    conn.execute("""
        INSERT INTO work_titles (work_id, title, title_type, language, created_at)
        VALUES ('w_001', 'Phosphoric Acid Extraction', 'en_translation', 'en', '2026-08-18 12:00:00')
    """)

    conn.execute("""
        INSERT INTO document_assets (asset_id, work_id, sha256, file_path, source_status, created_at)
        VALUES ('ast_001', 'w_001', 'abcdef1234567890', '/path/to/test.pdf', 'ACQUIRED', '2026-08-18 12:00:00')
    """)

    conn.execute("""
        INSERT INTO parse_runs (run_id, asset_id, parser_name, markdown_len, chunk_count, reference_count, executed_at)
        VALUES ('run_001', 'ast_001', 'mineru', 50000, 45, 12, '2026-08-18 12:00:00')
    """)

    conn.execute("""
        INSERT INTO reference_mentions (mention_id, citing_work_id, source_run_id, ref_index, raw_text, raw_length, quality_flag, created_at)
        VALUES ('ref_001', 'w_001', 'run_001', 1, '[1] Zhang et al., 2020. Extraction of H3PO4.', 42, 'NORMAL', '2026-08-18 12:00:00')
    """)

    # 插入第二篇被引文献
    conn.execute("""
        INSERT INTO works (work_id, work_type, canonical_title, publication_year, created_at, updated_at)
        VALUES ('w_002', 'journal_article', '磷酸纯化工艺综述', 2020, '2026-08-18 12:00:00', '2026-08-18 12:00:00')
    """)

    # 插入候选实体
    conn.execute("""
        INSERT INTO resolution_candidates (candidate_id, mention_id, candidate_work_id, match_method, confidence_score, created_at)
        VALUES ('cand_001', 'ref_001', 'w_002', 'exact_title', 0.95, '2026-08-18 12:00:00')
    """)

    # 插入决策
    conn.execute("""
        INSERT INTO resolution_decisions (decision_id, mention_id, selected_work_id, decision_type, decision_source, decided_by, decided_at, is_active)
        VALUES ('dec_001', 'ref_001', 'w_002', 'LINK', 'RULE', 'rule_engine_v1', '2026-08-18 12:00:00', 1)
    """)

    # 插入引文边 (无冗余派生列)
    conn.execute("""
        INSERT INTO citation_edges (edge_id, citing_work_id, cited_work_id, first_observed_at, last_confirmed_at)
        VALUES ('edge_001', 'w_001', 'w_002', '2026-08-18 12:00:00', '2026-08-18 12:00:00')
    """)

    # 插入断言证据
    conn.execute("""
        INSERT INTO citation_assertions (assertion_id, edge_id, source_type, source_mention_id, asserted_at)
        VALUES ('ass_001', 'edge_001', 'LOCAL_PDF_PARSED', 'ref_001', '2026-08-18 12:00:00')
    """)

    # 插入历史映射审计记录
    conn.execute("""
        INSERT INTO legacy_record_mappings (legacy_record_id, original_import_hash, current_asset_hash, target_work_id, target_asset_id, mapping_reason, mapping_verified_at, mapping_verified_by)
        VALUES ('old_rec_1', '8e11398f...', 'db6b9e8f...', 'w_001', 'ast_001', 'REPARSE_SAME_ASSET', '2026-08-18 12:00:00', 'audit_agent')
    """)

    conn.commit()

    # 1. 验证视图 v_citation_edges_summary 动态计算证据数与馆藏状态
    cursor.execute("SELECT evidence_count, in_library_edge FROM v_citation_edges_summary WHERE edge_id='edge_001'")
    row = cursor.fetchone()
    assert row[0] == 1  # 动态计算出 1 条断言证据
    assert row[1] == 1  # 动态计算出两端均在馆藏中

    # 2. 验证 RESTRICT 约束：尝试物理删除正被引用的 w_001 必须报错阻断 (保护审计链)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM works WHERE work_id = 'w_001'")

    # 3. 验证自环禁止约束：插入 w_001 -> w_001 必须触发 CHECK 约束报错
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("""
            INSERT INTO citation_edges (edge_id, citing_work_id, cited_work_id, first_observed_at, last_confirmed_at)
            VALUES ('edge_self', 'w_001', 'w_001', '2026-08-18 12:00:00', '2026-08-18 12:00:00')
        """)

    # 4. 验证决策类型一致性约束：NO_MATCH 却传了 selected_work_id 必须报错
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("""
            INSERT INTO resolution_decisions (decision_id, mention_id, selected_work_id, decision_type, decision_source, decided_by, decided_at, is_active)
            VALUES ('dec_invalid', 'ref_001', 'w_002', 'NO_MATCH', 'RULE', 'rule_engine_v1', '2026-08-18 12:00:00', 0)
        """)

    # 5. 验证多版本决策审计：同一 mention 可以有多个历史决策，但至多有 1 个 is_active=1
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("""
            INSERT INTO resolution_decisions (decision_id, mention_id, selected_work_id, decision_type, decision_source, decided_by, decided_at, is_active)
            VALUES ('dec_002', 'ref_001', 'w_002', 'LINK', 'HUMAN', 'reviewer_1', '2026-08-18 12:05:00', 1)
        """)

    # 把旧决策置为 is_active=0 后，可以成功插入新决策
    conn.execute("UPDATE resolution_decisions SET is_active=0 WHERE decision_id='dec_001'")
    conn.execute("""
        INSERT INTO resolution_decisions (decision_id, mention_id, selected_work_id, decision_type, decision_source, decided_by, decided_at, supersedes_decision_id, is_active)
        VALUES ('dec_002', 'ref_001', 'w_002', 'LINK', 'HUMAN', 'reviewer_1', '2026-08-18 12:05:00', 'dec_001', 1)
    """)
    conn.commit()

    cursor.execute("SELECT COUNT(*) FROM resolution_decisions WHERE mention_id = 'ref_001'")
    assert cursor.fetchone()[0] == 2  # 完整保留 2 个版本的决策审计链！

    cursor.execute("SELECT COUNT(*) FROM resolution_decisions WHERE mention_id = 'ref_001' AND is_active = 1")
    assert cursor.fetchone()[0] == 1  # 唯一激活决策

    conn.close()
