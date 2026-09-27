"""最小可复现的三库种子数据（生产库 / 引文图谱库 / 外部发现缓存库）。

测试原来直接读作者机器上的 /mnt/ripe 生产库、依赖 bba4a59d 之类的真实文献 ID，
别人 clone 下来要么 skip 要么失败，而且 POST 审批会写进真实缓存库。这里用真实的
建库代码（VectorStore / build_test_citation_network_db / init_cache_db）在临时目录
造一套小而完整的数据，数量全部已知，断言可以写成精确值。

文献关系（引用方向 →）：
    B → A (DOI)      B → C (DOI)      B → 外部未解析
    C → A (DOI)      C → 外部未解析
    Q 是被隔离的脏文献（前缀 a93f1740 在建图代码里硬编码为隔离）
另有图谱库中的：
    work_oa_w0000000001    已入库的外部 OA 文献（verified ingested）
    work_oa_w4391143884    孤儿 Work，QUARANTINED_INCOMPLETE，不得出现在任何图里
"""
import json
import os
import sqlite3
from dataclasses import dataclass

EMBED_DIM = 8
NOW = "2026-09-01 10:00:00"


def _hex_id(prefix: str) -> str:
    return (prefix + "0" * 64)[:64]


DOC_A = _hex_id("5eed000a")
DOC_B = _hex_id("5eed000b")
DOC_C = _hex_id("5eed000c")
DOC_Q = _hex_id("a93f1740")  # graph builder quarantines this prefix
OA_WORK = "work_oa_w0000000001"
ORPHAN_WORK = "work_oa_w4391143884"

RUN_ID = "run_seed_000000000001"
CAND_DISCOVERED = "cand_emerg_run_seed_W1000000001"
CAND_DISCOVERED_2 = "cand_found_run_seed_W1000000002"
CAND_INDEXED = "cand_emerg_run_seed_W1000000003"

PAPERS = [
    # id, title, doi, authors, journal, year, refs
    (DOC_A, "Caking of crystalline fertilizers", "10.9999/seed.a", "Chen, Xin; Li, Yu",
     "Powder Technology", "2019", []),
    (DOC_B, "Understanding caking phenomena in industrial salts", "10.9999/seed.b",
     "Zhao, Wei; Sun, Lei", "Chemical Engineering Journal", "2021", [
         "Chen X, Li Y. Caking of crystalline fertilizers. Powder Technology, 2019, 12: 1-10. doi:10.9999/seed.a",
         "Wu H. Coated urea release kinetics in soil. Journal of Fertilizers, 2020. doi:10.9999/seed.c",
         "Smith J. A completely external study on phosphate rock flotation. Minerals Engineering, 2005, 18: 100-110.",
     ]),
    (DOC_C, "Coated urea release kinetics in soil", "10.9999/seed.c", "Wu, Hao",
     "Journal of Fertilizers", "2020", [
         "Chen X, Li Y. Caking of crystalline fertilizers. Powder Technology, 2019. doi:10.9999/seed.a",
         "Brown K. An unrelated external survey of fluorine recovery. Hydrometallurgy, 2011, 7: 20-30.",
     ]),
    (DOC_Q, "Dirty stub document", None, None, None, None, []),
]


@dataclass
class SeedPaths:
    prod_db: str
    citation_db: str
    cache_db: str

    @property
    def root(self) -> str:
        return os.path.dirname(self.prod_db)


def seed_prod_db(path: str) -> None:
    from litreview.store import VectorStore

    VectorStore(path, EMBED_DIM)  # 建 papers / paper_details / paper_references / review_chunks …
    conn = sqlite3.connect(path)
    for doc_id, title, doi, authors, journal, year, refs in PAPERS:
        conn.execute(
            "INSERT INTO papers (id, title, pdf_path, language, status, processed_at) "
            "VALUES (?, ?, ?, 'en', 'EXPORTED', ?)",
            (doc_id, title, f"/seed/processed_pdfs/{doc_id[:8]}.pdf", NOW))
        conn.execute(
            "INSERT INTO paper_details (doc_id, title, title_en, doi, authors, journal, year, n_refs, doc_type) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'paper')",
            (doc_id, title, title, doi, authors, journal, year, len(refs)))
        for i, raw in enumerate(refs, start=1):
            conn.execute("INSERT INTO paper_references (doc_id, ref_index, raw_text) VALUES (?, ?, ?)",
                         (doc_id, i, raw))
    # A 有 2 个带向量的块，B 有 1 个未嵌入的块 → indexed docs = 1, chunks = 3
    blob = bytes(4 * EMBED_DIM)
    chunks = [(DOC_A, 0, blob), (DOC_A, 1, blob), (DOC_B, 0, None)]
    for doc_id, idx, emb in chunks:
        conn.execute(
            "INSERT INTO review_chunks (doc_id, chunk_index, content, char_len, embedding, embedding_model) "
            "VALUES (?, ?, 'seed chunk', 10, ?, 'seed-model')", (doc_id, idx, emb))
    conn.commit()
    conn.close()


def seed_citation_db(path: str, prod_db: str) -> dict:
    from litreview.citation_network import build_test_citation_network_db
    from litreview.pipeline import _ensure_vector_tables_in_test_db

    stats = build_test_citation_network_db(prod_db, path)
    conn = sqlite3.connect(path)
    _ensure_vector_tables_in_test_db(conn)
    conn.executemany(
        "INSERT INTO works (work_id, work_type, canonical_title, publication_year, quarantine_status, "
        "eligible_for_retrieval, eligible_for_citation_graph, in_library, created_at, updated_at) "
        "VALUES (?, 'journal_article', ?, 2024, ?, ?, ?, ?, ?, ?)",
        [
            (OA_WORK, "Open access follow-up on caking", "NORMAL", 1, 1, 1, NOW, NOW),
            (ORPHAN_WORK, "Orphan incomplete OA record", "QUARANTINED_INCOMPLETE", 0, 0, 0, NOW, NOW),
        ])
    # 故意给孤儿挂一条边：图谱接口必须靠过滤把它挡掉，而不是靠数据里恰好没有
    conn.execute(
        "INSERT INTO citation_edges (edge_id, citing_work_id, cited_work_id, edge_status, "
        "first_observed_at, last_confirmed_at) VALUES ('edge_seed_orphan', ?, ?, 'CONFIRMED', ?, ?)",
        (OA_WORK, ORPHAN_WORK, NOW, NOW))
    conn.execute(
        "INSERT INTO citation_edges (edge_id, citing_work_id, cited_work_id, edge_status, "
        "first_observed_at, last_confirmed_at) VALUES ('edge_seed_oa_a', ?, ?, 'CONFIRMED', ?, ?)",
        (OA_WORK, DOC_A, NOW, NOW))
    conn.execute(
        "INSERT INTO document_assets (asset_id, work_id, sha256, file_path, source_status, created_at) "
        "VALUES ('asset_seed_oa', ?, 'deadbeef', '/seed/staging_pdf/oa.pdf', 'ACQUIRED', ?)",
        (OA_WORK, NOW))
    conn.execute(
        "INSERT INTO review_chunks (doc_id, chunk_index, content, char_len) VALUES (?, 0, 'oa chunk', 8)",
        (OA_WORK,))
    conn.commit()
    conn.close()
    return stats


def seed_cache_db(path: str) -> None:
    from litreview.snowball import init_cache_db

    init_cache_db(path)
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO discovery_runs VALUES (?, ?, 'both', 1, '{}', '{}', ?)",
        (RUN_ID, json.dumps([DOC_B[:8]]), NOW))
    rows = [
        (CAND_DISCOVERED, "W1000000001", "10.9999/cand.1", "Emerging study on anti-caking agents",
         "EMERGING", "forward", 1, "DISCOVERED"),
        (CAND_DISCOVERED_2, "W1000000002", "10.9999/cand.2", "Foundational crystal bridge theory",
         "FOUNDATIONAL", "backward", 0, "DISCOVERED"),
        (CAND_INDEXED, "W1000000003", "10.9999/cand.3", "Already vector indexed OA paper",
         "EMERGING", "forward", 1, "VECTOR_INDEXED"),
    ]
    for cid, oa, doi, title, track, direction, is_oa, status in rows:
        conn.execute(
            "INSERT INTO discovered_candidates (candidate_id, query_run_id, openalex_id, doi, title, "
            "publication_year, is_open_access, track, direction, source_seeds_json, co_citation_count, "
            "recommendation_reason, in_library, data_source, response_hash, lifecycle_status, discovered_at) "
            "VALUES (?, ?, ?, ?, ?, 2024, ?, ?, ?, ?, 1, 'seed', 0, 'openalex', 'h', ?, ?)",
            (cid, RUN_ID, oa, doi, title, is_oa, track, direction, json.dumps([DOC_B[:8]]), status, NOW))
    conn.commit()
    conn.close()


def seed_all(root: str) -> SeedPaths:
    os.makedirs(root, exist_ok=True)
    paths = SeedPaths(
        prod_db=os.path.join(root, "batch_tracking.db"),
        citation_db=os.path.join(root, "test_citation_network.db"),
        cache_db=os.path.join(root, "external_discovery_cache.db"),
    )
    seed_prod_db(paths.prod_db)
    seed_citation_db(paths.citation_db, paths.prod_db)
    seed_cache_db(paths.cache_db)
    return paths
