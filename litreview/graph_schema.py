"""Citation Network & Work Entity Master Schema (Phase 2A Specification).

Decouples canonical works, document assets, parse runs, reference mentions,
candidate resolutions, audit decisions, citation edges, and provenance assertions.
"""
import sqlite3
from typing import List


GRAPH_SCHEMA_DDL = """
-- 1. 规范化作品主表 (Canonical Work Entity)
CREATE TABLE IF NOT EXISTS works (
    work_id TEXT PRIMARY KEY,
    work_type TEXT NOT NULL DEFAULT 'journal_article',  -- journal_article, book, patent, standard, thesis, report
    canonical_title TEXT NOT NULL,
    publication_year INTEGER,
    publication_date TEXT,
    journal_or_venue TEXT,
    volume TEXT,
    issue TEXT,
    pages TEXT,
    authors_json TEXT,  -- JSON list of normalized author strings/objects
    quarantine_status TEXT NOT NULL DEFAULT 'NORMAL',  -- NORMAL, QUARANTINED, MERGED_ALIAS
    quarantine_reason TEXT,
    quarantined_at TEXT,
    eligible_for_retrieval INTEGER NOT NULL DEFAULT 1,  -- 0/1: 是否允许进入语义检索与综述生成
    eligible_for_citation_graph INTEGER NOT NULL DEFAULT 1,  -- 0/1: 是否允许进入引文图谱构建
    in_library INTEGER NOT NULL DEFAULT 1,  -- 1: 本地物理库文献; 0: 外部引文雪球发现候选
    is_deleted INTEGER NOT NULL DEFAULT 0,  -- 软删除标记 (保护审计链)
    deleted_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- 2. 作品外部/标准标识符表 (Work Identifiers 1:N)
CREATE TABLE IF NOT EXISTS work_identifiers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    work_id TEXT NOT NULL REFERENCES works(work_id) ON DELETE RESTRICT,
    id_type TEXT NOT NULL,  -- doi, openalex_id, isbn, patent_no, standard_no, cnki_id, pmid, arxiv_id
    id_value TEXT NOT NULL,  -- normalized lowercase stripped value
    is_primary INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE(id_type, id_value)
);
CREATE INDEX IF NOT EXISTS idx_work_identifiers_lookup ON work_identifiers(id_type, id_value);
CREATE INDEX IF NOT EXISTS idx_work_identifiers_work_id ON work_identifiers(work_id);

-- 3. 作品题名别名与译名表 (Work Titles 1:N)
CREATE TABLE IF NOT EXISTS work_titles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    work_id TEXT NOT NULL REFERENCES works(work_id) ON DELETE RESTRICT,
    title TEXT NOT NULL,
    title_type TEXT NOT NULL DEFAULT 'original',  -- original, zh_translation, en_translation, cleaned_filename, alias
    language TEXT NOT NULL DEFAULT 'zh',  -- zh, en, other
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_work_titles_lookup ON work_titles(title, title_type);
CREATE INDEX IF NOT EXISTS idx_work_titles_work_id ON work_titles(work_id);

-- 4. 物理文件资产表 (Document Assets)
CREATE TABLE IF NOT EXISTS document_assets (
    asset_id TEXT PRIMARY KEY,
    work_id TEXT REFERENCES works(work_id) ON DELETE SET NULL,
    sha256 TEXT NOT NULL,
    file_path TEXT NOT NULL,
    file_size_bytes INTEGER,
    page_count INTEGER,
    mime_type TEXT DEFAULT 'application/pdf',
    source_url TEXT,
    acquired_at TEXT,
    source_status TEXT NOT NULL DEFAULT 'ACQUIRED',  -- ACQUIRED, MISSING, PARSE_FAILED_SUSPECTED_STUB
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_document_assets_sha256 ON document_assets(sha256);
CREATE INDEX IF NOT EXISTS idx_document_assets_work_id ON document_assets(work_id);

-- 5. 解析批次记录表 (Parse Runs)
CREATE TABLE IF NOT EXISTS parse_runs (
    run_id TEXT PRIMARY KEY,
    asset_id TEXT NOT NULL REFERENCES document_assets(asset_id) ON DELETE RESTRICT,
    parser_name TEXT NOT NULL DEFAULT 'mineru',  -- mineru, pdfplumber, llm_extract, bookintake
    parser_version TEXT,
    markdown_len INTEGER NOT NULL DEFAULT 0,
    markdown_sha256 TEXT,
    chunk_count INTEGER NOT NULL DEFAULT 0,
    reference_count INTEGER NOT NULL DEFAULT 0,
    parse_status TEXT NOT NULL DEFAULT 'SUCCESS',  -- SUCCESS, PARTIAL, PARSE_FAILED_SUSPECTED_STUB, MOCK_DIRTY
    executed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_parse_runs_asset_id ON parse_runs(asset_id);

-- 6. 历史记录与资产映射审计表 (Legacy Record Mappings - 保留历史ID与文件哈希不一致的审计证据)
CREATE TABLE IF NOT EXISTS legacy_record_mappings (
    legacy_record_id TEXT PRIMARY KEY,
    original_import_hash TEXT,
    current_asset_hash TEXT NOT NULL,
    target_work_id TEXT REFERENCES works(work_id) ON DELETE SET NULL,
    target_asset_id TEXT REFERENCES document_assets(asset_id) ON DELETE SET NULL,
    mapping_reason TEXT NOT NULL,  -- REPARSE_SAME_ASSET, SHARED_ARCHIVE_FILE, MOCK_DIRTY_OVERWRITE
    mapping_verified_at TEXT NOT NULL,
    mapping_verified_by TEXT NOT NULL
);

-- 7. 原始参考文献提取与结构化记录表 (Reference Mentions)
CREATE TABLE IF NOT EXISTS reference_mentions (
    mention_id TEXT PRIMARY KEY,
    citing_work_id TEXT NOT NULL REFERENCES works(work_id) ON DELETE RESTRICT,
    source_run_id TEXT REFERENCES parse_runs(run_id) ON DELETE SET NULL,
    ref_index INTEGER NOT NULL,  -- 原文中引用的序号/顺序
    raw_text TEXT NOT NULL,  -- 完整原始引用字符串 (原样保留，绝对不截断)
    raw_length INTEGER NOT NULL,
    quality_flag TEXT NOT NULL DEFAULT 'NORMAL',  -- NORMAL, SHORT_SUSPECT, OCR_CORRUPTED, BLANK
    parsed_doi TEXT,
    parsed_title TEXT,
    parsed_authors TEXT,
    parsed_year INTEGER,
    parsed_venue TEXT,
    parse_status TEXT NOT NULL DEFAULT 'UNPARSED',  -- UNPARSED, PARSED_COMPLETE, PARSED_PARTIAL, PARSE_FAILED
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ref_mentions_citing_work ON reference_mentions(citing_work_id);
CREATE INDEX IF NOT EXISTS idx_ref_mentions_parsed_doi ON reference_mentions(parsed_doi);
CREATE INDEX IF NOT EXISTS idx_ref_mentions_parsed_title ON reference_mentions(parsed_title);

-- 8. 实体消歧候选匹配表 (Resolution Candidates - 支持过期标记与重算覆盖)
CREATE TABLE IF NOT EXISTS resolution_candidates (
    candidate_id TEXT PRIMARY KEY,
    mention_id TEXT NOT NULL REFERENCES reference_mentions(mention_id) ON DELETE CASCADE,
    candidate_work_id TEXT NOT NULL REFERENCES works(work_id) ON DELETE RESTRICT,
    match_method TEXT NOT NULL,  -- exact_doi, exact_title, normalized_title_fuzzy, author_year_venue
    confidence_score REAL NOT NULL,  -- 0.0 ~ 1.0 置信度得分
    match_evidence_json TEXT,  -- 匹配依据详情 (JSON 格式)
    is_deleted INTEGER NOT NULL DEFAULT 0,
    expired_at TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_res_candidates_mention ON resolution_candidates(mention_id);
CREATE INDEX IF NOT EXISTS idx_res_candidates_work ON resolution_candidates(candidate_work_id);

-- 9. 实体消歧决策与审计表 (Resolution Decisions - 多版本审计，不可物理覆盖)
CREATE TABLE IF NOT EXISTS resolution_decisions (
    decision_id TEXT PRIMARY KEY,
    mention_id TEXT NOT NULL REFERENCES reference_mentions(mention_id) ON DELETE RESTRICT,
    selected_candidate_id TEXT REFERENCES resolution_candidates(candidate_id) ON DELETE RESTRICT,
    selected_work_id TEXT REFERENCES works(work_id) ON DELETE RESTRICT,
    candidate_snapshot_json TEXT,  -- 裁决时刻冻结的候选快照 (包含当时特征与得分，防止候选被删后证据断裂)
    decision_type TEXT NOT NULL,  -- LINK, NO_MATCH, DEFER, REJECT
    decision_source TEXT NOT NULL,  -- RULE, MODEL, HUMAN
    decided_by TEXT NOT NULL,  -- e.g. 'rule_engine_v1', 'expert_reviewer'
    decided_at TEXT NOT NULL,
    reason TEXT,
    supersedes_decision_id TEXT REFERENCES resolution_decisions(decision_id),
    is_active INTEGER NOT NULL DEFAULT 1,  -- 1: 当前生效决策; 0: 历史已废弃决策
    CHECK (
        (decision_type = 'LINK' AND selected_work_id IS NOT NULL)
        OR
        (decision_type IN ('NO_MATCH', 'DEFER', 'REJECT') AND selected_work_id IS NULL)
    ),
    CHECK (decision_type IN ('LINK', 'NO_MATCH', 'DEFER', 'REJECT')),
    CHECK (decision_source IN ('RULE', 'MODEL', 'HUMAN')),
    CHECK (is_active IN (0, 1)),
    CHECK (candidate_snapshot_json IS NULL OR json_valid(candidate_snapshot_json))
);
CREATE INDEX IF NOT EXISTS idx_res_decisions_mention ON resolution_decisions(mention_id);
-- 约束：每个引用条目至多有一个处于激活状态 (is_active=1) 的决策（未裁决前为 0）
CREATE UNIQUE INDEX IF NOT EXISTS idx_res_decisions_single_active ON resolution_decisions(mention_id) WHERE is_active = 1;

-- 10. 确认引文关系边表 (Citation Edges)
CREATE TABLE IF NOT EXISTS citation_edges (
    edge_id TEXT PRIMARY KEY,
    citing_work_id TEXT NOT NULL REFERENCES works(work_id) ON DELETE RESTRICT,
    cited_work_id TEXT NOT NULL REFERENCES works(work_id) ON DELETE RESTRICT,
    edge_status TEXT NOT NULL DEFAULT 'CONFIRMED',  -- CONFIRMED, CANDIDATE, UNRESOLVED_EXTERNAL, DISPUTED, REVOKED
    first_observed_at TEXT NOT NULL,
    last_confirmed_at TEXT NOT NULL,
    CHECK (citing_work_id != cited_work_id),  -- 严格禁止自环引用 (Work 归并时必须阻断并进入冲突审计)
    UNIQUE(citing_work_id, cited_work_id)
);
CREATE INDEX IF NOT EXISTS idx_citation_edges_citing ON citation_edges(citing_work_id);
CREATE INDEX IF NOT EXISTS idx_citation_edges_cited ON citation_edges(cited_work_id);

-- 11. 引文证据来源断言表 (Citation Assertions - 永久保留的多源可审计凭证)
CREATE TABLE IF NOT EXISTS citation_assertions (
    assertion_id TEXT PRIMARY KEY,
    edge_id TEXT NOT NULL REFERENCES citation_edges(edge_id) ON DELETE RESTRICT,
    source_type TEXT NOT NULL,  -- LOCAL_PDF_PARSED, OPENALEX_API, CROSSREF_API, MANUAL_AUDIT
    source_mention_id TEXT REFERENCES reference_mentions(mention_id) ON DELETE SET NULL,
    external_provenance_id TEXT,  -- 如 OpenAlex Reference ID 或 CrossRef 检索条目 ID
    asserted_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_citation_assertions_edge ON citation_assertions(edge_id);

-- 12. 引文边汇总视图 (动态计算证据总数与馆藏状态，杜绝冗余列漂移与联表放大)
CREATE VIEW IF NOT EXISTS v_citation_edges_summary AS
SELECT e.edge_id, e.citing_work_id, e.cited_work_id, e.edge_status,
       e.first_observed_at, e.last_confirmed_at,
       COUNT(DISTINCT a.assertion_id) AS evidence_count,
       CASE WHEN (w_citing.in_library = 1 AND w_citing.is_deleted = 0 AND w_citing.quarantine_status = 'NORMAL'
                  AND w_cited.in_library = 1 AND w_cited.is_deleted = 0 AND w_cited.quarantine_status = 'NORMAL')
            THEN 1 ELSE 0 END AS in_library_edge
FROM citation_edges e
LEFT JOIN citation_assertions a ON e.edge_id = a.edge_id
JOIN works w_citing ON e.citing_work_id = w_citing.work_id
JOIN works w_cited ON e.cited_work_id = w_cited.work_id
GROUP BY e.edge_id;
"""


def init_graph_schema(conn: sqlite3.Connection) -> None:
    """初始化引文图谱与规范化作品主数据表结构。"""
    conn.executescript(GRAPH_SCHEMA_DDL)
