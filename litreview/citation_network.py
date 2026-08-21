"""Citation Network Engine (Phase 2B Local Citation Network MVP).

Manages local citation network construction, candidate resolution,
and graph traversal query APIs in the decoupled Graph Schema.
"""
import os
import json
import sqlite3
import hashlib
from datetime import datetime
from typing import Dict, List, Any, Optional, Tuple
from dataclasses import dataclass

from litreview.graph_schema import GRAPH_SCHEMA_DDL, init_graph_schema
from litreview.ref_parser import (
    parse_reference_mention,
    normalize_title,
    extract_first_author_surname
)


def build_test_citation_network_db(
    prod_db_path: str,
    test_db_path: str
) -> Dict[str, Any]:
    """从生产库只读构建独立的测试引文图谱数据库。"""
    if os.path.exists(test_db_path):
        os.remove(test_db_path)
    os.makedirs(os.path.dirname(os.path.abspath(test_db_path)), exist_ok=True)

    test_conn = sqlite3.connect(test_db_path)
    init_graph_schema(test_conn)

    prod_conn = sqlite3.connect(f"file:{prod_db_path}?mode=ro", uri=True)
    prod_c = prod_conn.cursor()

    now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 1. 导入 344 篇主库馆藏为 Canonical Works
    papers = prod_c.execute("""
        SELECT p.id, p.title, p.pdf_path, p.language,
               d.title_zh, d.title_en, d.doi, d.authors, d.journal, d.year,
               COALESCE(d.doc_type, 'paper') as doc_type, d.patent_no, d.doc_no,
               p.processed_at
        FROM papers p
        LEFT JOIN paper_details d ON p.id = d.doc_id
    """).fetchall()

    quarantined_prefixes = ('a93f1740', 'a5620039', '8d73f880')

    in_library_works = []
    doi_index = {}
    title_index = {}
    author_year_index = {}
    token_index = {}

    for row in papers:
        (doc_id, title, pdf_path, language,
         title_zh, title_en, doi, authors, journal, year,
         doc_type, patent_no, doc_no, processed_at) = row

        is_quarantined = any(doc_id.startswith(q) for q in quarantined_prefixes)
        q_status = 'QUARANTINED' if is_quarantined else 'NORMAL'
        q_reason = 'ISOLATED_DIRTY_OR_STUB_PDF' if is_quarantined else None

        canon_title = title_zh or title_en or title or f"Document {doc_id[:8]}"
        w_year = int(year) if year and str(year).isdigit() else None
        first_auth = extract_first_author_surname(authors)

        authors_list = [a.strip() for a in (authors or '').split(';') if a.strip()] or ([authors.strip()] if authors else [])

        test_conn.execute("""
            INSERT INTO works (
                work_id, work_type, canonical_title, publication_year,
                journal_or_venue, authors_json, quarantine_status,
                quarantine_reason, eligible_for_retrieval,
                eligible_for_citation_graph, in_library, is_deleted,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0, ?, ?)
        """, (
            doc_id, doc_type, canon_title, w_year,
            journal, json.dumps(authors_list, ensure_ascii=False),
            q_status, q_reason,
            0 if is_quarantined else 1,
            0 if is_quarantined else 1,
            processed_at or now_iso, now_iso
        ))

        # Work Identifiers
        if doi:
            clean_doi = doi.strip().lower()
            test_conn.execute("""
                INSERT OR IGNORE INTO work_identifiers (
                    work_id, id_type, id_value, is_primary, created_at
                ) VALUES (?, 'doi', ?, 1, ?)
            """, (doc_id, clean_doi, now_iso))
            if not is_quarantined:
                doi_index[clean_doi] = {
                    'work_id': doc_id,
                    'title': canon_title,
                    'year': w_year,
                    'first_auth': first_auth
                }

        # Work Titles
        for t, t_type in [(canon_title, 'original'), (title_zh, 'zh_translation'), (title_en, 'en_translation')]:
            if t:
                test_conn.execute("""
                    INSERT INTO work_titles (work_id, title, title_type, language, created_at)
                    VALUES (?, ?, ?, ?, ?)
                """, (doc_id, t, t_type, 'zh' if t_type == 'zh_translation' else 'en', now_iso))
                if not is_quarantined:
                    norm_t = normalize_title(t)
                    if len(norm_t) >= 5:
                        title_index[norm_t] = {
                            'work_id': doc_id,
                            'title': canon_title,
                            'year': w_year,
                            'first_auth': first_auth
                        }
                        for tk in norm_t.split():
                            if len(tk) >= 4:
                                if tk not in token_index:
                                    token_index[tk] = set()
                                token_index[tk].add(doc_id)

        # Document Assets
        if pdf_path:
            asset_id = f"asset_{doc_id[:8]}"
            test_conn.execute("""
                INSERT INTO document_assets (
                    asset_id, work_id, sha256, file_path, source_status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
            """, (
                asset_id, doc_id, hashlib.sha256(pdf_path.encode('utf-8')).hexdigest(),
                pdf_path, 'PARSE_FAILED_SUSPECTED_STUB' if doc_id.startswith('8d73f880') else 'ACQUIRED',
                now_iso
            ))

        if first_auth and w_year and not is_quarantined:
            for y_off in (0, -1, 1):
                k = (first_auth.lower(), w_year + y_off)
                if k not in author_year_index:
                    author_year_index[k] = []
                author_year_index[k].append({
                    'work_id': doc_id,
                    'title': canon_title,
                    'year': w_year,
                    'first_auth': first_auth
                })

    test_conn.commit()

    # 2. 读取全部 8,682 条原始引用并在独立测试库处理
    refs = prod_c.execute("""
        SELECT doc_id, ref_index, raw_text
        FROM paper_references
        ORDER BY doc_id, ref_index
    """).fetchall()
    prod_conn.close()

    total_refs = len(refs)
    confirmed_links = 0
    candidate_matches = 0
    unresolved_external = 0
    short_suspects = 0

    seen_edges = set()  # (citing, cited)

    for doc_id, r_idx, raw_t in refs:
        mention_id = f"mention_{doc_id[:8]}_{r_idx}"
        p = parse_reference_mention(raw_t)

        if p.quality_flag in ('SHORT_SUSPECT', 'BLANK', 'OCR_CORRUPTED') or len(raw_t.strip()) <= 10:
            short_suspects += 1

        # 写入 reference_mentions 表
        test_conn.execute("""
            INSERT INTO reference_mentions (
                mention_id, citing_work_id, source_run_id, ref_index,
                raw_text, raw_length, quality_flag, parsed_doi,
                parsed_title, parsed_authors, parsed_year, parsed_venue,
                parse_status, created_at
            ) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            mention_id, doc_id, r_idx, raw_t, len(raw_t),
            p.quality_flag, p.parsed_doi, p.parsed_title,
            p.parsed_authors, p.parsed_year, p.parsed_venue,
            p.parse_status, now_iso
        ))

        # 实体消歧匹配决策
        matched = None
        # A. 严格 DOI 匹配
        if p.parsed_doi:
            doi_k = p.parsed_doi.strip().lower()
            if doi_k in doi_index:
                tgt = doi_index[doi_k]
                if tgt['work_id'] != doc_id:
                    matched = ('EXACT_DOI', tgt, 1.0)

        # B. 标准化题名完全一致且年份无冲突
        if not matched and p.parsed_title:
            norm_t = normalize_title(p.parsed_title)
            if norm_t in title_index:
                tgt = title_index[norm_t]
                if tgt['work_id'] != doc_id:
                    if p.parsed_year and tgt['year'] and abs(p.parsed_year - tgt['year']) > 1:
                        # 年份冲突转为候选
                        matched = ('CANDIDATE_YEAR_CONFLICT', tgt, 0.75)
                    else:
                        matched = ('EXACT_TITLE', tgt, 0.98)

        # C. 模糊/作者年份候选 (仅作为 CANDIDATE，禁止自动链接与 Work 合并)
        if not matched:
            ref_auth = extract_first_author_surname(p.parsed_authors)
            if ref_auth and p.parsed_year:
                cands = author_year_index.get((ref_auth.lower(), p.parsed_year), [])
                for tgt in cands:
                    if tgt['work_id'] != doc_id:
                        matched = ('CANDIDATE_AUTHOR_YEAR', tgt, 0.80)
                        break

        if matched and matched[0] in ('EXACT_DOI', 'EXACT_TITLE'):
            # 确认在馆链接
            m_type, tgt, conf = matched
            confirmed_links += 1
            cand_id = f"cand_{mention_id}_inlib"
            dec_id = f"dec_{mention_id}"
            edge_id = f"edge_{doc_id[:8]}_{tgt['work_id'][:8]}"

            test_conn.execute("""
                INSERT INTO resolution_candidates (
                    candidate_id, mention_id, candidate_work_id,
                    match_method, confidence_score, match_evidence_json,
                    is_deleted, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 0, ?)
            """, (
                cand_id, mention_id, tgt['work_id'],
                m_type.lower(), conf,
                json.dumps({'matched_target_title': tgt['title'], 'method': m_type}, ensure_ascii=False),
                now_iso
            ))

            test_conn.execute("""
                INSERT INTO resolution_decisions (
                    decision_id, mention_id, selected_candidate_id,
                    selected_work_id, candidate_snapshot_json,
                    decision_type, decision_source, decided_by,
                    decided_at, reason, is_active
                ) VALUES (?, ?, ?, ?, ?, 'LINK', 'RULE', 'local_citation_engine_v1', ?, 'EXACT_BIBLIOGRAPHIC_MATCH', 1)
            """, (
                dec_id, mention_id, cand_id, tgt['work_id'],
                json.dumps({'target_work_id': tgt['work_id'], 'confidence': conf}),
                now_iso
            ))

            # 写入 citation_edges (去重)
            if (doc_id, tgt['work_id']) not in seen_edges:
                test_conn.execute("""
                    INSERT OR IGNORE INTO citation_edges (
                        edge_id, citing_work_id, cited_work_id,
                        edge_status, first_observed_at, last_confirmed_at
                    ) VALUES (?, ?, ?, 'CONFIRMED', ?, ?)
                """, (edge_id, doc_id, tgt['work_id'], now_iso, now_iso))
                seen_edges.add((doc_id, tgt['work_id']))

            # 写入 citation_assertions
            assert_id = f"assert_{edge_id}_{r_idx}"
            test_conn.execute("""
                INSERT OR IGNORE INTO citation_assertions (
                    assertion_id, edge_id, source_type,
                    source_mention_id, external_provenance_id, asserted_at
                ) VALUES (?, ?, 'LOCAL_PDF_PARSED', ?, NULL, ?)
            """, (assert_id, edge_id, mention_id, now_iso))

        else:
            # 未确认在馆链接 -> 创建临时外部 Work 节点 (状态 UNRESOLVED)
            ext_work_id = f"ext_work_{mention_id}"
            ext_title = p.parsed_title or p.clean_text[:100]

            test_conn.execute("""
                INSERT INTO works (
                    work_id, work_type, canonical_title, publication_year,
                    journal_or_venue, authors_json, quarantine_status,
                    quarantine_reason, eligible_for_retrieval,
                    eligible_for_citation_graph, in_library, is_deleted,
                    created_at, updated_at
                ) VALUES (?, 'journal_article', ?, ?, ?, ?, 'UNRESOLVED', 'EXTERNAL_UNRESOLVED_REFERENCE', 0, 1, 0, 0, ?, ?)
            """, (
                ext_work_id, ext_title, p.parsed_year,
                p.parsed_venue, json.dumps([p.parsed_authors] if p.parsed_authors else [], ensure_ascii=False),
                now_iso, now_iso
            ))

            dec_id = f"dec_{mention_id}"

            if matched and matched[0].startswith('CANDIDATE'):
                candidate_matches += 1
                m_type, tgt, conf = matched
                cand_id = f"cand_{mention_id}_cand"

                test_conn.execute("""
                    INSERT INTO resolution_candidates (
                        candidate_id, mention_id, candidate_work_id,
                        match_method, confidence_score, match_evidence_json,
                        is_deleted, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 0, ?)
                """, (
                    cand_id, mention_id, tgt['work_id'],
                    m_type.lower(), conf,
                    json.dumps({'candidate_target_title': tgt['title'], 'method': m_type}, ensure_ascii=False),
                    now_iso
                ))

                test_conn.execute("""
                    INSERT INTO resolution_decisions (
                        decision_id, mention_id, selected_candidate_id,
                        selected_work_id, candidate_snapshot_json,
                        decision_type, decision_source, decided_by,
                        decided_at, reason, is_active
                    ) VALUES (?, ?, ?, NULL, ?, 'DEFER', 'RULE', 'local_citation_engine_v1', ?, 'AMBIGUOUS_CANDIDATE_REQUIRES_REVIEW', 1)
                """, (
                    dec_id, mention_id, cand_id,
                    json.dumps({'candidate_work_id': tgt['work_id'], 'confidence': conf}),
                    now_iso
                ))

                edge_id = f"edge_{doc_id[:8]}_{ext_work_id}"
                test_conn.execute("""
                    INSERT OR IGNORE INTO citation_edges (
                        edge_id, citing_work_id, cited_work_id,
                        edge_status, first_observed_at, last_confirmed_at
                    ) VALUES (?, ?, ?, 'CANDIDATE', ?, ?)
                """, (edge_id, doc_id, ext_work_id, now_iso, now_iso))
            else:
                unresolved_external += 1
                test_conn.execute("""
                    INSERT INTO resolution_decisions (
                        decision_id, mention_id, selected_candidate_id,
                        selected_work_id, candidate_snapshot_json,
                        decision_type, decision_source, decided_by,
                        decided_at, reason, is_active
                    ) VALUES (?, ?, NULL, NULL, NULL, 'NO_MATCH', 'RULE', 'local_citation_engine_v1', ?, 'NO_MATCH_IN_LOCAL_LIBRARY', 1)
                """, (dec_id, mention_id, now_iso))

                edge_id = f"edge_{doc_id[:8]}_{ext_work_id}"
                test_conn.execute("""
                    INSERT OR IGNORE INTO citation_edges (
                        edge_id, citing_work_id, cited_work_id,
                        edge_status, first_observed_at, last_confirmed_at
                    ) VALUES (?, ?, ?, 'UNRESOLVED_EXTERNAL', ?, ?)
                """, (edge_id, doc_id, ext_work_id, now_iso, now_iso))

            assert_id = f"assert_{edge_id}_{r_idx}"
            test_conn.execute("""
                INSERT OR IGNORE INTO citation_assertions (
                    assertion_id, edge_id, source_type,
                    source_mention_id, external_provenance_id, asserted_at
                ) VALUES (?, ?, 'LOCAL_PDF_PARSED', ?, NULL, ?)
            """, (assert_id, edge_id, mention_id, now_iso))

    test_conn.commit()

    stats = {
        'total_canonical_in_library_works': len(papers),
        'total_references_processed': total_refs,
        'valid_references_count': total_refs - short_suspects,
        'short_suspects_count': short_suspects,
        'confirmed_in_library_links': confirmed_links,
        'candidate_matches_count': candidate_matches,
        'unresolved_external_references': unresolved_external,
        'total_citation_edges': test_conn.execute("SELECT COUNT(*) FROM citation_edges").fetchone()[0],
        'total_citation_assertions': test_conn.execute("SELECT COUNT(*) FROM citation_assertions").fetchone()[0],
        'total_works_in_db': test_conn.execute("SELECT COUNT(*) FROM works").fetchone()[0]
    }
    test_conn.close()
    return stats


def query_citation_network(
    db_path: str,
    doc_id: str,
    depth: int = 1,
    include_candidates: bool = True,
    include_unresolved: bool = False
) -> Dict[str, Any]:
    """查询指定文献的局部引文子图与共被引网络。"""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    c = conn.cursor()

    # 1. 定位 Seed Work
    seed_row = c.execute("""
        SELECT work_id, canonical_title, publication_year, journal_or_venue, authors_json, in_library, quarantine_status
        FROM works
        WHERE work_id = ? OR work_id LIKE ?
        LIMIT 1
    """, (doc_id, f"{doc_id}%")).fetchone()

    if not seed_row:
        # 尝试从候选池缓存库匹配 (external_discovery_cache.db)
        cache_db = os.getenv("EXTERNAL_DISCOVERY_CACHE_DB_PATH", os.path.join(os.path.dirname(db_path), "external_discovery_cache.db"))
        if os.path.exists(cache_db):
            try:
                c_conn = sqlite3.connect(f"file:{cache_db}?mode=ro", uri=True)
                c_row = c_conn.execute("""
                    SELECT candidate_id, title, publication_year, journal_or_venue, authors_json, in_library, lifecycle_status
                    FROM discovered_candidates
                    WHERE candidate_id = ? OR candidate_id LIKE ? OR openalex_id LIKE ?
                    LIMIT 1
                """, (doc_id, f"{doc_id}%", f"%{doc_id}%")).fetchone()
                c_conn.close()
                if c_row:
                    seed_row = c_row
            except Exception:
                pass

        if not seed_row:
            conn.close()
            return {'error': f"Work not found for doc_id: {doc_id}"}

    seed_id, seed_title, seed_year, seed_journal, seed_authors_json, in_lib, q_status = seed_row
    seed_authors = json.loads(seed_authors_json) if seed_authors_json else []

    # 2. 该文献引用了谁 (Outgoing Citations)
    outgoing_rows = c.execute("""
        SELECT e.edge_id, e.cited_work_id, e.edge_status,
               w.canonical_title, w.publication_year, w.journal_or_venue, w.authors_json, w.in_library,
               c.match_method, c.confidence_score
        FROM citation_edges e
        JOIN works w ON e.cited_work_id = w.work_id
        LEFT JOIN reference_mentions m ON m.citing_work_id = e.citing_work_id
        LEFT JOIN resolution_candidates c ON c.mention_id = m.mention_id AND c.candidate_work_id = w.work_id
        WHERE e.citing_work_id = ?
        GROUP BY e.cited_work_id
    """, (seed_id,)).fetchall()

    outgoing_confirmed = []
    outgoing_candidates = []
    outgoing_unresolved_count = 0

    for r in outgoing_rows:
        (edge_id, cited_id, edge_status,
         t_title, t_year, t_venue, t_authors_json, t_in_lib,
         match_method, conf) = r

        t_authors = json.loads(t_authors_json) if t_authors_json else []

        item = {
            'work_id': cited_id,
            'title': t_title,
            'year': t_year,
            'journal': t_venue,
            'authors': t_authors,
            'in_library': bool(t_in_lib),
            'edge_status': edge_status,
            'match_method': match_method or ('in_library_confirmed' if t_in_lib else 'unresolved'),
            'confidence_score': conf or (1.0 if t_in_lib else 0.0)
        }

        if t_in_lib and edge_status == 'CONFIRMED':
            outgoing_confirmed.append(item)
        elif edge_status == 'CANDIDATE':
            if include_candidates:
                outgoing_candidates.append(item)
        else:
            outgoing_unresolved_count += 1

    # 3. 库内谁引用了它 (Incoming Citations)
    incoming_rows = c.execute("""
        SELECT e.edge_id, e.citing_work_id, e.edge_status,
               w.canonical_title, w.publication_year, w.journal_or_venue, w.authors_json,
               c.match_method, c.confidence_score
        FROM citation_edges e
        JOIN works w ON e.citing_work_id = w.work_id
        LEFT JOIN reference_mentions m ON m.citing_work_id = e.citing_work_id
        LEFT JOIN resolution_candidates c ON c.mention_id = m.mention_id AND c.candidate_work_id = e.cited_work_id
        WHERE e.cited_work_id = ?
        GROUP BY e.citing_work_id
    """, (seed_id,)).fetchall()

    incoming_confirmed = []
    incoming_candidates = []

    for r in incoming_rows:
        (edge_id, citing_id, edge_status,
         c_title, c_year, c_venue, c_authors_json,
         match_method, conf) = r

        c_authors = json.loads(c_authors_json) if c_authors_json else []
        item = {
            'citing_work_id': citing_id,
            'title': c_title,
            'year': c_year,
            'journal': c_venue,
            'authors': c_authors,
            'edge_status': edge_status,
            'match_method': match_method or 'in_library_citation',
            'confidence_score': conf or 1.0
        }
        if edge_status == 'CONFIRMED':
            incoming_confirmed.append(item)
        elif edge_status == 'CANDIDATE' and include_candidates:
            incoming_candidates.append(item)

    # 4. 共同参考文献 (Bibliographic Coupling / Co-cited in-library references)
    # 查找与 seed_id 共同引用了相同外部/内部文献的其他馆藏文献
    co_citation_rows = c.execute("""
        SELECT e2.citing_work_id, w2.canonical_title, w2.publication_year, COUNT(DISTINCT e2.cited_work_id) as shared_count
        FROM citation_edges e1
        JOIN citation_edges e2 ON e1.cited_work_id = e2.cited_work_id AND e1.citing_work_id != e2.citing_work_id
        JOIN works w2 ON e2.citing_work_id = w2.work_id AND w2.in_library = 1 AND w2.quarantine_status = 'NORMAL'
        WHERE e1.citing_work_id = ?
        GROUP BY e2.citing_work_id
        ORDER BY shared_count DESC
        LIMIT 5
    """, (seed_id,)).fetchall()

    co_citations = []
    for (co_id, co_title, co_year, shared_cnt) in co_citation_rows:
        co_citations.append({
            'work_id': co_id,
            'title': co_title,
            'year': co_year,
            'shared_references_count': shared_cnt
        })

    # 总引用数
    total_seed_refs = c.execute("""
        SELECT COUNT(*) FROM reference_mentions WHERE citing_work_id = ?
    """, (seed_id,)).fetchone()[0]

    conn.close()

    return {
        'seed_work': {
            'work_id': seed_id,
            'title': seed_title,
            'year': seed_year,
            'journal': seed_journal,
            'authors': seed_authors,
            'in_library': bool(in_lib),
            'quarantine_status': q_status
        },
        'network_statistics': {
            'total_references_in_seed': total_seed_refs,
            'confirmed_in_library_outgoing_count': len(outgoing_confirmed),
            'candidate_outgoing_count': len(outgoing_candidates),
            'unresolved_external_references_count': outgoing_unresolved_count,
            'in_library_cited_by_count': len(incoming_confirmed),
            'candidate_incoming_count': len(incoming_candidates),
            'top_bibliographic_coupling_count': len(co_citations)
        },
        'outgoing_citations': {
            'confirmed_in_library': outgoing_confirmed,
            'candidates': outgoing_candidates
        },
        'incoming_citations': {
            'confirmed_in_library': incoming_confirmed,
            'candidates': incoming_candidates
        },
        'co_cited_works': co_citations
    }


def get_full_citation_graph(
    test_db_path: str,
    include_unresolved: bool = False,
    limit: int = 10000
) -> Dict[str, Any]:
    """获取引文网络图谱数据 (默认骨干模式以保证 60 FPS 流畅交互，支持按需加载全量 9110 节点)。"""
    if not os.path.exists(test_db_path):
        return {"nodes": [], "links": [], "categories": []}

    conn = sqlite3.connect(test_db_path)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    if include_unresolved:
        works = [dict(r) for r in c.execute(
            "SELECT * FROM works WHERE is_deleted = 0 AND eligible_for_citation_graph != 0 LIMIT ?", (limit,)
        ).fetchall()]
        edges = [dict(r) for r in c.execute("SELECT * FROM citation_edges").fetchall()]
    else:
        works = [dict(r) for r in c.execute("""
            SELECT * FROM works
            WHERE is_deleted = 0 AND eligible_for_citation_graph != 0
            AND (
                in_library = 1
                OR work_id IN (SELECT citing_work_id FROM citation_edges WHERE edge_status IN ('CONFIRMED', 'CANDIDATE'))
                OR work_id IN (SELECT cited_work_id FROM citation_edges WHERE edge_status IN ('CONFIRMED', 'CANDIDATE'))
            )
            LIMIT ?
        """, (limit,)).fetchall()]
        edges = [dict(r) for r in c.execute(
            "SELECT * FROM citation_edges WHERE edge_status IN ('CONFIRMED', 'CANDIDATE')"
        ).fetchall()]

    conn.close()

    node_ids = {w["work_id"] for w in works}

    valid_edges = [
        e for e in edges
        if e["citing_work_id"] in node_ids and e["cited_work_id"] in node_ids
    ]

    degree = {}
    for e in valid_edges:
        src = e["citing_work_id"]
        tgt = e["cited_work_id"]
        degree[src] = degree.get(src, 0) + 1
        degree[tgt] = degree.get(tgt, 0) + 1

    nodes = []
    categories = [
        {"name": "馆内核心馆藏"},
        {"name": "外部入库文献 (OA)"},
        {"name": "外部未解析节点"},
        {"name": "待复核候选"}
    ]

    for w in works:
        wid = w["work_id"]
        in_lib = bool(w["in_library"])
        is_oa = wid.startswith("work_oa_")

        cat_idx = 1 if is_oa else (0 if in_lib else (3 if "cand_" in wid else 2))
        color = "#10b981" if is_oa else ("#2563eb" if in_lib else ("#f59e0b" if "cand_" in wid else "#64748b"))

        deg = degree.get(wid, 0)
        size = min(45, max(12, 12 + deg * 2.5))

        nodes.append({
            "id": wid,
            "name": w["canonical_title"] or wid,
            "category": cat_idx,
            "symbolSize": size,
            "value": deg,
            "itemStyle": {"color": color},
            "work_id": wid,
            "year": w.get("publication_year"),
            "in_library": in_lib
        })

    links = []
    for e in valid_edges:
        st = e["edge_status"]
        color = "#2563eb" if st == "CONFIRMED" else ("#f59e0b" if st == "CANDIDATE" else "#94a3b8")
        line_type = "solid" if st == "CONFIRMED" else "dashed"
        links.append({
            "source": e["citing_work_id"],
            "target": e["cited_work_id"],
            "value": st,
            "lineStyle": {
                "color": color,
                "type": line_type,
                "width": 1.5 if st == "CONFIRMED" else 1
            }
        })

    return {
        "nodes": nodes,
        "links": links,
        "categories": categories,
        "is_backbone": not include_unresolved,
        "total_nodes_count": len(nodes),
        "total_links_count": len(links)
    }


def get_seed_subgraph(
    test_db_path: str,
    seed_id: str,
    depth: int = 1
) -> Dict[str, Any]:
    """获取指定种子文献的聚焦局部子图。"""
    if not os.path.exists(test_db_path):
        return {"nodes": [], "links": [], "categories": []}

    conn = sqlite3.connect(test_db_path)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # 允许短前缀 ID 查询 (如 "bba4a59d")
    real_row = c.execute("SELECT work_id FROM works WHERE work_id = ? OR work_id LIKE ? LIMIT 1", (seed_id, f"{seed_id}%")).fetchone()
    if real_row:
        seed_id = real_row[0]

    # 查询直连的一阶出入向边
    edges = [dict(r) for r in c.execute("""
        SELECT * FROM citation_edges 
        WHERE citing_work_id = ? OR cited_work_id = ?
    """, (seed_id, seed_id)).fetchall()]

    sub_node_ids = {seed_id}
    for e in edges:
        sub_node_ids.add(e["citing_work_id"])
        sub_node_ids.add(e["cited_work_id"])

    placeholders = ",".join(["?"] * len(sub_node_ids))
    works = [dict(r) for r in c.execute(
        f"SELECT * FROM works WHERE work_id IN ({placeholders}) AND is_deleted = 0 AND eligible_for_citation_graph != 0",
        list(sub_node_ids)
    ).fetchall()]
    conn.close()

    valid_node_ids = {w["work_id"] for w in works}
    valid_edges = [
        e for e in edges
        if e["citing_work_id"] in valid_node_ids and e["cited_work_id"] in valid_node_ids
    ]

    degree = {}
    for e in valid_edges:
        degree[e["citing_work_id"]] = degree.get(e["citing_work_id"], 0) + 1
        degree[e["cited_work_id"]] = degree.get(e["cited_work_id"], 0) + 1

    nodes = []
    categories = [
        {"name": "聚焦核心文献"},
        {"name": "关联馆藏文献"},
        {"name": "外部引用文献"}
    ]

    for w in works:
        wid = w["work_id"]
        is_seed = (wid == seed_id)
        in_lib = bool(w["in_library"])
        cat_idx = 0 if is_seed else (1 if in_lib else 2)
        color = "#ef4444" if is_seed else ("#2563eb" if in_lib else "#64748b")
        deg = degree.get(wid, 0)
        size = 35 if is_seed else min(30, max(14, 14 + deg * 3))

        nodes.append({
            "id": wid,
            "name": w["canonical_title"] or wid,
            "category": cat_idx,
            "symbolSize": size,
            "value": deg,
            "itemStyle": {"color": color},
            "work_id": wid,
            "year": w.get("publication_year"),
            "in_library": in_lib
        })

    links = []
    for e in valid_edges:
        st = e["edge_status"]
        color = "#2563eb" if st == "CONFIRMED" else ("#f59e0b" if st == "CANDIDATE" else "#94a3b8")
        links.append({
            "source": e["citing_work_id"],
            "target": e["cited_work_id"],
            "value": st,
            "lineStyle": {"color": color, "type": "solid" if st == "CONFIRMED" else "dashed", "width": 1.5}
        })

    return {
        "nodes": nodes,
        "links": links,
        "categories": categories,
        "seed_id": seed_id
    }
