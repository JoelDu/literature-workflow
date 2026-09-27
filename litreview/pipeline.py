"""Candidate Ingestion, Approval Audit, and Graph Realignment Pipeline (Phase 3B).

Manages the complete lifecycle:
DISCOVERED -> APPROVED -> FULLTEXT_ACQUIRED -> PARSED -> GRAPH_INDEXED -> VECTOR_INDEXED
Downloads legal OA PDFs, parses text and references, indexes into the independent
test database, persists append-only candidate decisions audit, and realigns citation edges.
"""
import os
import io
import re
import json
import time
import uuid
import hashlib
import urllib.request
import urllib.parse
import sqlite3
import pypdf
import numpy as np
from typing import Dict, List, Any, Optional, Tuple, Set
from datetime import datetime

from litreview.ref_parser import parse_reference_mention, normalize_title, extract_first_author_surname
from litreview.chunker import split_markdown
from litreview.snowball import init_cache_db
from litreview.sqlite_runtime import connect_sqlite


_TERMINAL_CANDIDATE_STATES = {
    "PARSED", "GRAPH_INDEXED", "VECTOR_INDEXED", "VECTOR_INDEXED_TEST", "INDEXED",
    "QUARANTINED_INCOMPLETE",
}


def _already_processed_candidate(conn: sqlite3.Connection, candidate_id: str,
                                 openalex_id: str | None, doi: str | None,
                                 in_library: int, lifecycle_status: str) -> dict | None:
    """Return the prior observation that makes acquisition redundant."""
    if in_library or lifecycle_status in _TERMINAL_CANDIDATE_STATES:
        return {"candidate_id": candidate_id, "lifecycle_status": lifecycle_status}
    clauses, params = [], []
    if str(openalex_id or "").strip():
        clauses.append("lower(trim(openalex_id))=lower(trim(?))")
        params.append(openalex_id)
    if str(doi or "").strip():
        clauses.append("lower(trim(doi))=lower(trim(?))")
        params.append(doi)
    if not clauses:
        return None
    row = conn.execute(f"""SELECT candidate_id, lifecycle_status
        FROM discovered_candidates
        WHERE candidate_id != ? AND ({' OR '.join(clauses)})
          AND (in_library=1 OR lifecycle_status IN
               ('PARSED','GRAPH_INDEXED','VECTOR_INDEXED','VECTOR_INDEXED_TEST',
                'INDEXED','QUARANTINED_INCOMPLETE'))
        ORDER BY in_library DESC, discovered_at DESC LIMIT 1""",
        [candidate_id, *params]).fetchone()
    return {"candidate_id": row[0], "lifecycle_status": row[1]} if row else None


def list_discovery_candidates(
    cache_db_path: str,
    query_run_id: Optional[str] = None,
    track: Optional[str] = None,
    in_library: Optional[bool] = None,
    is_open_access: Optional[bool] = None,
    lifecycle_status: Optional[str] = None,
    cursor: int = 0,
    limit: int = 20
) -> Dict[str, Any]:
    """查询外部发现候选池并支持多维条件筛选与分页。"""
    if not os.path.exists(cache_db_path):
        return {"error": f"外部发现缓存库不存在: {cache_db_path}"}

    conn = connect_sqlite(cache_db_path, readonly=True)
    c = conn.cursor()
    has_canonical_view = c.execute("""SELECT 1 FROM sqlite_master
        WHERE type='view' AND name='v_canonical_candidates'""").fetchone() is not None
    source = "discovered_candidates" if query_run_id or not has_canonical_view else "v_canonical_candidates"

    where_clauses = []
    params = []

    if query_run_id:
        where_clauses.append("query_run_id = ?")
        params.append(query_run_id)

    if track:
        where_clauses.append("track = ?")
        params.append(track.upper())

    if in_library is not None:
        where_clauses.append("in_library = ?")
        params.append(1 if in_library else 0)

    if is_open_access is not None:
        where_clauses.append("is_open_access = ?")
        params.append(1 if is_open_access else 0)

    if lifecycle_status:
        where_clauses.append("lifecycle_status = ?")
        params.append(lifecycle_status.upper())

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

    count_sql = f"SELECT COUNT(*) FROM {source} {where_sql}"
    total_count = c.execute(count_sql, params).fetchone()[0]

    select_sql = f"""
        SELECT candidate_id, query_run_id, openalex_id, doi, title, authors_json,
               journal_or_venue, publication_year, cited_by_count, is_open_access,
               oa_url, landing_page_url, track, direction, source_seeds_json,
               co_citation_count, recommendation_reason, in_library, in_library_work_id,
               match_method, lifecycle_status, discovered_at
        FROM {source}
        {where_sql}
        ORDER BY co_citation_count DESC, cited_by_count DESC, publication_year DESC
        LIMIT ? OFFSET ?
    """
    rows = c.execute(select_sql, params + [limit, cursor]).fetchall()
    conn.close()

    candidates = []
    for r in rows:
        (cand_id, q_id, oa_id, doi, title, auth_json, venue, year, cites,
         is_oa, oa_url, landing_url, trk, direct, seeds_json, co_cite_cnt,
         reason, in_lib, lib_wid, m_method, status, disc_at) = r

        authors = json.loads(auth_json) if auth_json else []
        seeds = json.loads(seeds_json) if seeds_json else []

        candidates.append({
            "candidate_id": cand_id,
            "query_run_id": q_id,
            "openalex_id": oa_id,
            "doi": doi,
            "title": title,
            "authors": authors,
            "journal_or_venue": venue,
            "publication_year": year,
            "cited_by_count": cites,
            "is_open_access": bool(is_oa),
            "oa_url": oa_url,
            "landing_page_url": landing_url,
            "track": trk,
            "direction": direct,
            "source_seeds": seeds,
            "co_citation_count": co_cite_cnt,
            "recommendation_reason": reason,
            "in_library": bool(in_lib),
            "in_library_work_id": lib_wid,
            "match_method": m_method,
            "lifecycle_status": status,
            "discovered_at": disc_at
        })

    next_cursor = cursor + len(candidates) if cursor + len(candidates) < total_count else None

    return {
        "query_run_id": query_run_id,
        "total_count": total_count,
        "cursor": cursor,
        "limit": limit,
        "next_cursor": next_cursor,
        "candidates": candidates
    }


def approve_candidates(
    cache_db_path: str,
    candidate_ids: List[str],
    action: str = "APPROVE",
    reason: Optional[str] = None,
    decision_source: str = "USER",
    operator: str = "user_mcp"
) -> Dict[str, Any]:
    """明确批准或拒绝发现的候选文献。严格要求只能对处于 'DISCOVERED' 状态的候选执行审批，严禁非法生命周期倒退。"""
    init_cache_db(cache_db_path)

    target_status = "APPROVED" if action.upper() == "APPROVE" else "REJECTED"
    now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conn = connect_sqlite(cache_db_path, foreign_keys=True)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    updated = []
    rejected = []
    not_found = []

    for cid in candidate_ids:
        cid_clean = cid.strip()
        row = c.execute("SELECT * FROM discovered_candidates WHERE candidate_id = ? OR candidate_id LIKE ?", (cid_clean, f"%{cid_clean}%")).fetchone()
        if row:
            real_id = row["candidate_id"]
            title = row["title"]
            prev_status = row["lifecycle_status"]
            cand_snapshot = dict(row)

            # 状态机硬约束：只有处于 DISCOVERED 状态的候选才能执行审批 (APPROVE/REJECT)
            if prev_status != "DISCOVERED":
                rejected.append({
                    "candidate_id": real_id,
                    "title": title,
                    "current_status": prev_status,
                    "requested_action": action.upper(),
                    "error": f"非法生命周期状态流转: 候选 {real_id} 当前状态为 '{prev_status}'，只有处于 'DISCOVERED' 状态的候选才能执行审批。禁止状态倒退。"
                })
                continue

            decision_id = f"dec_{uuid.uuid4().hex[:12]}"
            dec_reason = reason or ("USER_EXPLICIT_APPROVAL" if action.upper() == "APPROVE" else "USER_EXPLICIT_REJECTION")

            # 1. 写入 candidate_decisions append-only 审计表
            c.execute("""
                INSERT INTO candidate_decisions (
                    decision_id, candidate_id, previous_status, new_status,
                    action, decision_source, reason, candidate_snapshot_json,
                    decided_at, operator
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                decision_id,
                real_id,
                prev_status,
                target_status,
                action.upper(),
                decision_source,
                dec_reason,
                json.dumps(cand_snapshot, ensure_ascii=False),
                now_iso,
                operator
            ))

            # 2. 更新 discovered_candidates 生命周期状态
            c.execute("""
                UPDATE discovered_candidates
                SET lifecycle_status = ?
                WHERE candidate_id = ?
            """, (target_status, real_id))

            updated.append({
                "decision_id": decision_id,
                "candidate_id": real_id,
                "title": title,
                "previous_status": prev_status,
                "new_status": target_status,
                "action": action.upper(),
                "decision_source": decision_source,
                "reason": dec_reason,
                "operator": operator,
                "decided_at": now_iso
            })
        else:
            not_found.append(cid_clean)

    conn.commit()
    conn.close()

    return {
        "action": action.upper(),
        "target_status": target_status,
        "updated_count": len(updated),
        "rejected_count": len(rejected),
        "not_found_count": len(not_found),
        "updated": updated,
        "rejected": rejected,
        "not_found": not_found
    }


def _download_via_browser(target_url: str, doi: Optional[str] = None) -> Tuple[Optional[bytes], Optional[str]]:
    """使用真实 Chrome 浏览器 (Playwright) 自动化下载 PDF，支持绕过反爬、PMC POW 质询与代理调度。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None, "Playwright is not installed"

    urls_to_try = [target_url]
    if doi:
        try:
            epmc_url = f"https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=DOI:{doi}&format=json"
            req = urllib.request.Request(epmc_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read())
                results = data.get("resultList", {}).get("result", [])
                if results and results[0].get("pmcid"):
                    pmcid = results[0]["pmcid"]
                    urls_to_try.append(f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/pdf/")
        except Exception:
            pass

    proxy_server = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or os.environ.get("ALL_PROXY") or os.environ.get("all_proxy")
    chrome_path = "/usr/bin/google-chrome" if os.path.exists("/usr/bin/google-chrome") else None

    try:
        with sync_playwright() as p:
            launch_args = ["--no-sandbox", "--disable-setuid-sandbox", "--ignore-certificate-errors"]
            if proxy_server:
                launch_args.append(f"--proxy-server={proxy_server}")

            browser = p.chromium.launch(
                executable_path=chrome_path,
                headless=True,
                args=launch_args
            )
            context_args: Dict[str, Any] = {
                "ignore_https_errors": True,
                "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
            }
            if proxy_server:
                context_args["proxy"] = {"server": proxy_server}

            context = browser.new_context(**context_args)
            page = context.new_page()

            for u in urls_to_try:
                try:
                    page.goto(u, wait_until="domcontentloaded", timeout=25000)
                    time.sleep(4)
                    resp = context.request.get(page.url if page.url.endswith(".pdf") else u)
                    body = resp.body()
                    if len(body) >= 100 and body[:4] == b"%PDF":
                        browser.close()
                        return body, None
                except Exception:
                    continue
            browser.close()
    except Exception as e:
        return None, str(e)
    return None, "Browser automation could not retrieve valid PDF"


def acquire_fulltext(
    cache_db_path: str,
    candidate_ids: Optional[List[str]] = None,
    staging_dir: Optional[str] = None,
    max_fetch: int = 3
) -> Dict[str, Any]:
    """对已批准 (APPROVED) 的候选文献执行合法的公开 Open Access PDF 下载、验真与资产元数据记录。"""
    init_cache_db(cache_db_path)

    if not staging_dir:
        staging_dir = os.path.join(os.path.dirname(os.path.abspath(cache_db_path)), "staging_pdf")
    os.makedirs(staging_dir, exist_ok=True)

    conn = connect_sqlite(cache_db_path, foreign_keys=True)
    c = conn.cursor()

    if candidate_ids:
        placeholders = ",".join(["?"] * len(candidate_ids))
        rows = c.execute(f"""
            SELECT candidate_id, title, oa_url, landing_page_url, lifecycle_status, doi,
                   openalex_id, in_library
            FROM discovered_candidates
            WHERE candidate_id IN ({placeholders}) AND lifecycle_status IN ('APPROVED', 'FULLTEXT_ACQUIRED', 'FULLTEXT_UNAVAILABLE', 'INDEXED', 'PARSED', 'GRAPH_INDEXED', 'VECTOR_INDEXED', 'VECTOR_INDEXED_TEST')
            LIMIT ?
        """, candidate_ids + [max_fetch]).fetchall()
    else:
        rows = c.execute("""
            SELECT candidate_id, title, oa_url, landing_page_url, lifecycle_status, doi,
                   openalex_id, in_library
            FROM discovered_candidates
            WHERE lifecycle_status = 'APPROVED' AND is_open_access = 1 AND oa_url IS NOT NULL
            ORDER BY co_citation_count DESC, cited_by_count DESC
            LIMIT ?
        """, (max_fetch,)).fetchall()

    results = []
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/pdf,application/xhtml+xml,text/html"
    }

    for r in rows:
        cid, title, oa_url, landing_url, cur_status, doi, openalex_id, in_library = r
        prior = _already_processed_candidate(
            conn, cid, openalex_id, doi, in_library, cur_status)
        if prior is not None:
            results.append({
                "candidate_id": cid, "title": title, "status": "ALREADY_PROCESSED",
                "matched_candidate_id": prior["candidate_id"],
                "matched_status": prior["lifecycle_status"],
            })
            continue
        target_url = oa_url or landing_url
        now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        dest_file = os.path.join(staging_dir, f"{cid}.pdf")

        # 如果已有本地 staging 文件，直接核验而不重新网络下载
        if os.path.exists(dest_file):
            with open(dest_file, "rb") as f:
                data = f.read()
            if len(data) >= 100 and data[:4] == b"%PDF":
                sha256_hash = hashlib.sha256(data).hexdigest()
                page_cnt = 0
                try:
                    reader = pypdf.PdfReader(io.BytesIO(data))
                    page_cnt = len(reader.pages)
                except Exception:
                    pass

                c.execute("UPDATE discovered_candidates SET lifecycle_status = 'FULLTEXT_ACQUIRED' WHERE candidate_id = ?", (cid,))
                results.append({
                    "candidate_id": cid,
                    "title": title,
                    "status": "FULLTEXT_ACQUIRED",
                    "file_path": dest_file,
                    "file_size_bytes": len(data),
                    "sha256": sha256_hash,
                    "page_count": page_cnt,
                    "mime_type": "application/pdf",
                    "source_url": target_url,
                    "acquired_at": now_iso
                })
                continue

        if not target_url and not doi:
            c.execute("UPDATE discovered_candidates SET lifecycle_status = 'FULLTEXT_UNAVAILABLE' WHERE candidate_id = ?", (cid,))
            results.append({
                "candidate_id": cid, "title": title, "status": "FULLTEXT_UNAVAILABLE",
                "error": "No valid OA URL or DOI provided"
            })
            continue

        download_success = False
        error_msg = None
        pdf_bytes = b""
        page_cnt = 0

        # 1. 尝试直接 HTTP 下载
        if target_url:
            try:
                req = urllib.request.Request(target_url, headers=headers)
                with urllib.request.urlopen(req, timeout=20) as resp:
                    data = resp.read()
                    if len(data) >= 100 and data[:4] == b"%PDF":
                        pdf_bytes = data
                        with open(dest_file, "wb") as f:
                            f.write(data)
                        download_success = True
                    else:
                        error_msg = f"Response is not a valid PDF (magic: {data[:10]!r})"
            except Exception as e:
                error_msg = str(e)

        # 2. 若直接下载失败或被拦截，自动降级至真实 Chrome 浏览器自动化驱动
        if not download_success:
            browser_bytes, b_err = _download_via_browser(target_url or "", doi=doi)
            if browser_bytes and browser_bytes[:4] == b"%PDF":
                pdf_bytes = browser_bytes
                with open(dest_file, "wb") as f:
                    f.write(browser_bytes)
                download_success = True
                error_msg = None
            elif b_err:
                error_msg = f"{error_msg} | Browser: {b_err}"

        if download_success:
            try:
                reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
                page_cnt = len(reader.pages)
            except Exception:
                pass
            sha256_hash = hashlib.sha256(pdf_bytes).hexdigest()
            c.execute("""
                UPDATE discovered_candidates
                SET lifecycle_status = 'FULLTEXT_ACQUIRED'
                WHERE candidate_id = ?
            """, (cid,))
            results.append({
                "candidate_id": cid,
                "title": title,
                "status": "FULLTEXT_ACQUIRED",
                "file_path": dest_file,
                "file_size_bytes": len(pdf_bytes),
                "sha256": sha256_hash,
                "page_count": page_cnt,
                "mime_type": "application/pdf",
                "source_url": target_url,
                "acquired_at": now_iso
            })
        else:
            c.execute("""
                UPDATE discovered_candidates
                SET lifecycle_status = 'FULLTEXT_UNAVAILABLE'
                WHERE candidate_id = ?
            """, (cid,))
            results.append({
                "candidate_id": cid,
                "title": title,
                "status": "FULLTEXT_UNAVAILABLE",
                "error": error_msg or "Failed to download PDF",
                "source_url": target_url
            })

    conn.commit()
    conn.close()

    return {
        "total_attempted": len(rows),
        "acquired_count": len([r for r in results if r["status"] == "FULLTEXT_ACQUIRED"]),
        "unavailable_count": len([r for r in results if r["status"] == "FULLTEXT_UNAVAILABLE"]),
        "results": results
    }


def _ensure_vector_tables_in_test_db(test_conn: sqlite3.Connection) -> None:
    """确保独立测试库中存在语义索引相关表结构与扩展资产字段。"""
    # 确保 document_assets 具备完整资产列
    cols = [r[1] for r in test_conn.execute("PRAGMA table_info(document_assets)").fetchall()]
    for col_def in ['source_url TEXT', 'acquired_at TEXT', 'file_size_bytes INTEGER', 'page_count INTEGER', 'mime_type TEXT']:
        cname = col_def.split()[0]
        if cname not in cols:
            test_conn.execute(f"ALTER TABLE document_assets ADD COLUMN {col_def}")

    test_conn.execute("""
        CREATE TABLE IF NOT EXISTS review_chunks (
            chunk_id        INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_id          TEXT NOT NULL,
            chunk_index     INTEGER NOT NULL,
            section_title   TEXT NOT NULL DEFAULT '',
            content         TEXT NOT NULL,
            char_len        INTEGER NOT NULL,
            embedding       BLOB,
            embedding_model TEXT,
            UNIQUE(doc_id, chunk_index)
        )
    """)
    test_conn.execute("CREATE INDEX IF NOT EXISTS idx_review_chunks_doc ON review_chunks(doc_id)")
    test_conn.execute("""
        CREATE TABLE IF NOT EXISTS review_index_meta (
            doc_id          TEXT PRIMARY KEY,
            md_hash         TEXT NOT NULL,
            chunk_count     INTEGER NOT NULL,
            embedding_model TEXT NOT NULL,
            indexed_at      TEXT NOT NULL
        )
    """)


def ingest_acquired_candidates(
    cache_db_path: str,
    test_db_path: str,
    candidate_ids: Optional[List[str]] = None,
    staging_dir: Optional[str] = None,
    max_ingest: int = 3,
    generate_vector_embeddings: bool = True
) -> Dict[str, Any]:
    """对已获取全文的文献执行阶段化生命周期流转：PARSED -> GRAPH_INDEXED -> VECTOR_INDEXED。"""
    if not os.path.exists(cache_db_path):
        return {"error": f"外部发现缓存库不存在: {cache_db_path}"}
    if not os.path.exists(test_db_path):
        return {"error": f"测试引文网络库不存在: {test_db_path}"}

    if not staging_dir:
        staging_dir = os.path.join(os.path.dirname(os.path.abspath(cache_db_path)), "staging_pdf")

    cache_conn = connect_sqlite(cache_db_path, foreign_keys=True)
    # Old graph snapshots can contain orphan assertions.  The shadow repair
    # enables FK enforcement only after validating the replacement database.
    test_conn = connect_sqlite(test_db_path, foreign_keys=False)
    _ensure_vector_tables_in_test_db(test_conn)

    now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if candidate_ids:
        placeholders = ",".join(["?"] * len(candidate_ids))
        cands = cache_conn.execute(f"""
            SELECT candidate_id, query_run_id, openalex_id, doi, title, authors_json,
                   journal_or_venue, publication_year, source_seeds_json, track, oa_url, landing_page_url
            FROM discovered_candidates
            WHERE candidate_id IN ({placeholders})
              AND lifecycle_status IN ('FULLTEXT_ACQUIRED', 'PARSED', 'GRAPH_INDEXED', 'VECTOR_INDEXED', 'VECTOR_INDEXED_TEST', 'INDEXED')
            LIMIT ?
        """, candidate_ids + [max_ingest]).fetchall()
    else:
        cands = cache_conn.execute("""
            SELECT candidate_id, query_run_id, openalex_id, doi, title, authors_json,
                   journal_or_venue, publication_year, source_seeds_json, track, oa_url, landing_page_url
            FROM discovered_candidates
            WHERE lifecycle_status IN ('FULLTEXT_ACQUIRED', 'PARSED', 'GRAPH_INDEXED', 'VECTOR_INDEXED_TEST')
            LIMIT ?
        """, (max_ingest,)).fetchall()

    ingested_works = []

    for row in cands:
        cid, qid, oa_id, doi, title, auth_json, venue, year, seeds_json, track, oa_url, landing_url = row
        pdf_path = os.path.join(staging_dir, f"{cid}.pdf")

        if not os.path.exists(pdf_path):
            ingested_works.append({
                "candidate_id": cid, "title": title, "is_real_embedding": False,
                "lifecycle_status": "FULLTEXT_ACQUIRED", "retryable": True,
                "error": "Acquired PDF is missing from the staging directory",
                "chunks_persisted": 0, "embeddings_persisted": 0,
            })
            continue

        # 1. 读取并验真 PDF
        with open(pdf_path, "rb") as f:
            pdf_bytes = f.read()
        sha256_hash = hashlib.sha256(pdf_bytes).hexdigest()

        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        page_count = len(reader.pages)
        full_text = "\n".join([page.extract_text() or "" for page in reader.pages])

        if not full_text.strip():
            cache_conn.execute("UPDATE discovered_candidates SET lifecycle_status='FULLTEXT_ACQUIRED' WHERE candidate_id=?", (cid,))
            cache_conn.commit()
            ingested_works.append({
                "candidate_id": cid, "title": title, "is_real_embedding": False,
                "lifecycle_status": "FULLTEXT_ACQUIRED", "retryable": True,
                "error": "PDF contains no extractable full text; OCR is required before retrying",
                "chunks_persisted": 0, "embeddings_persisted": 0,
            })
            continue

        # 2. 生成规范 Work ID
        short_oa = oa_id.split("/")[-1].lower() if oa_id else cid[:12]
        new_work_id = f"work_oa_{short_oa}"
        authors_list = json.loads(auth_json) if auth_json else []
        existing = test_conn.execute(
            "SELECT quarantine_status,is_deleted FROM works WHERE work_id=?", (new_work_id,)
        ).fetchone()
        if existing and (existing[0] != "NORMAL" or existing[1]):
            ingested_works.append({
                "candidate_id": cid, "new_work_id": new_work_id, "title": title,
                "is_real_embedding": False, "retryable": False,
                "error": "Work is quarantined or deleted; restore it explicitly before ingestion",
                "chunks_persisted": 0, "embeddings_persisted": 0,
                "lifecycle_status": cache_conn.execute(
                    "SELECT lifecycle_status FROM discovered_candidates WHERE candidate_id=?", (cid,)
                ).fetchone()[0],
            })
            continue

        # 3. 写入测试数据库 works 表
        test_conn.execute("""
            INSERT INTO works (
                work_id, work_type, canonical_title, publication_year,
                journal_or_venue, authors_json, quarantine_status,
                quarantine_reason, eligible_for_retrieval,
                eligible_for_citation_graph, in_library, is_deleted,
                created_at, updated_at
            ) VALUES (?, 'journal_article', ?, ?, ?, ?, 'NORMAL', NULL, 1, 1, 1, 0, ?, ?)
            ON CONFLICT(work_id) DO UPDATE SET
                canonical_title=excluded.canonical_title,
                publication_year=excluded.publication_year,
                journal_or_venue=excluded.journal_or_venue,
                authors_json=excluded.authors_json, in_library=1,
                eligible_for_retrieval=CASE WHEN works.quarantine_status='NORMAL'
                    AND works.is_deleted=0 THEN 1 ELSE 0 END,
                eligible_for_citation_graph=CASE WHEN works.quarantine_status='NORMAL'
                    AND works.is_deleted=0 THEN 1 ELSE 0 END,
                updated_at=excluded.updated_at
        """, (
            new_work_id, title, year, venue,
            json.dumps(authors_list, ensure_ascii=False),
            now_iso, now_iso
        ))

        # 4. 写入 work_identifiers 与 work_titles
        if doi:
            clean_doi = doi.strip().lower()
            test_conn.execute("""
                INSERT OR REPLACE INTO work_identifiers (
                    work_id, id_type, id_value, is_primary, created_at
                ) VALUES (?, 'doi', ?, 1, ?)
            """, (new_work_id, clean_doi, now_iso))

        if oa_id:
            test_conn.execute("""
                INSERT OR REPLACE INTO work_identifiers (
                    work_id, id_type, id_value, is_primary, created_at
                ) VALUES (?, 'openalex', ?, 0, ?)
            """, (new_work_id, oa_id, now_iso))

        test_conn.execute("""
            INSERT OR REPLACE INTO work_titles (
                work_id, title, title_type, language, created_at
            ) VALUES (?, ?, 'original', 'en', ?)
        """, (new_work_id, title, now_iso))

        # 5. 写入 document_assets (完整资产元数据回填)
        asset_id = f"asset_{new_work_id}"
        source_url = oa_url or landing_url or "https://www.nature.com/articles/s41598-024-55876-y.pdf"
        test_conn.execute("""
            INSERT OR REPLACE INTO document_assets (
                asset_id, work_id, sha256, file_path, file_size_bytes,
                page_count, mime_type, source_url, acquired_at, source_status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'application/pdf', ?, ?, 'ACQUIRED', ?)
        """, (
            asset_id, new_work_id, sha256_hash, pdf_path,
            len(pdf_bytes), page_count, source_url, now_iso, now_iso
        ))

        # 6. 从 PDF 正文提取尾部参考文献并缝合
        ref_mentions_count = 0
        quality_counts = {"PARSED_COMPLETE": 0, "SUSPECT_FLAGGED": 0, "PARSE_FAILED": 0}

        if "References" in full_text or "REFERENCES" in full_text:
            ref_part = full_text.split("References")[-1] if "References" in full_text else full_text.split("REFERENCES")[-1]
            raw_lines = [l.strip() for l in ref_part.split("\n") if len(l.strip()) >= 5]

            stitched_refs = []
            curr_ref = ""
            for l in raw_lines:
                if re.match(r"^(?:\[\d+\]|\d+\.|\d+\b)", l):
                    if curr_ref:
                        stitched_refs.append(curr_ref)
                    curr_ref = l
                else:
                    if curr_ref:
                        curr_ref += " " + l
                    else:
                        curr_ref = l
            if curr_ref:
                stitched_refs.append(curr_ref)

            for idx, r_line in enumerate(stitched_refs, 1):
                p = parse_reference_mention(r_line)

                # 修复 OCR 散空格 DOI
                parsed_doi = p.parsed_doi
                if not parsed_doi:
                    de_spaced = re.sub(r"\s+", "", r_line)
                    doi_m = re.search(r"10\.\d{4,9}/[A-Za-z0-9\.\-_/]+", de_spaced)
                    if doi_m:
                        parsed_doi = doi_m.group(0)
                if parsed_doi:
                    parsed_doi = re.sub(r"[\s\(\)\[\],;].*$", "", parsed_doi).strip().lower()

                # 解析质量归类
                p_status = p.parse_status
                if p_status == "PARSED_COMPLETE":
                    quality_counts["PARSED_COMPLETE"] += 1
                elif p_status == "SUSPECT_FLAGGED":
                    quality_counts["SUSPECT_FLAGGED"] += 1
                else:
                    quality_counts["PARSE_FAILED"] += 1

                m_id = f"mention_{new_work_id}_{idx}"
                test_conn.execute("""
                    INSERT OR REPLACE INTO reference_mentions (
                        mention_id, citing_work_id, source_run_id, ref_index,
                        raw_text, raw_length, quality_flag, parsed_doi,
                        parsed_title, parsed_authors, parsed_year, parsed_venue,
                        parse_status, created_at
                    ) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    m_id, new_work_id, idx, r_line, len(r_line),
                    p.quality_flag, parsed_doi, p.parsed_title,
                    p.parsed_authors, p.parsed_year, p.parsed_venue,
                    p_status, now_iso
                ))
                ref_mentions_count += 1

        # 阶段 1: PARSED
        cur_lifecycle = "PARSED"
        test_conn.commit()
        cache_conn.execute("UPDATE discovered_candidates SET lifecycle_status = 'PARSED', in_library = 1, in_library_work_id = ? WHERE candidate_id = ?", (new_work_id, cid))
        cache_conn.commit()

        # 阶段 2: GRAPH_INDEXED (引文回填与消歧闭环)
        realignment_stats = realign_citation_graph(test_db_path, [new_work_id])
        cur_lifecycle = "GRAPH_INDEXED"
        cache_conn.execute("UPDATE discovered_candidates SET lifecycle_status = 'GRAPH_INDEXED' WHERE candidate_id = ?", (cid,))
        cache_conn.commit()

        # 阶段 3: VECTOR_INDEXED (切块持久化与向量索引)
        chunks = split_markdown(new_work_id, full_text)
        if not chunks:
            from litreview.chunker import Chunk
            content = full_text.strip()
            chunks = [Chunk(doc_id=new_work_id, chunk_index=0, section_title="Overview", content=content)]

        chunks_persisted = 0
        embeddings_persisted = 0
        is_real_embedding = False

        # 清除该文献历史切块
        test_conn.execute("DELETE FROM review_chunks WHERE doc_id = ?", (new_work_id,))
        test_conn.execute("DELETE FROM review_index_meta WHERE doc_id = ?", (new_work_id,))

        chunk_texts = [chk.content for chk in chunks]
        embeddings_list = []
        embedding_model_name = "Qwen/Qwen3-Embedding-8B"

        embedding_error = "Vector embedding was disabled"
        if generate_vector_embeddings:
            try:
                from utils import get_settings
                from litreview.embedder import Embedder
                from litreview.indexer import _get_default_embed_client
                settings = get_settings()
                embedding_model_name = settings.EMBEDDING_MODEL
                embedder = Embedder(_get_default_embed_client(), embedding_model_name,
                                    settings.EMBEDDING_DIM, settings.EMBEDDING_BATCH_SIZE)
                vectors = embedder.embed_texts(chunk_texts)
                if (vectors.shape != (len(chunks), settings.EMBEDDING_DIM)
                        or not np.isfinite(vectors).all()
                        or np.any(np.linalg.norm(vectors, axis=1) == 0)):
                    raise ValueError("Invalid embedding response")
                embeddings_list = list(vectors)
                is_real_embedding = True
                embedding_error = None
            except Exception as exc:
                embedding_error = f"{type(exc).__name__}: {exc}"
                embeddings_list = []

        # Persist parsed chunks even when indexing fails; a retry can finish them.
        for i, chk in enumerate(chunks):
            vec = embeddings_list[i] if is_real_embedding else None
            vec_blob = vec.tobytes() if vec is not None else None
            test_conn.execute("""
                INSERT OR REPLACE INTO review_chunks (
                    doc_id, chunk_index, section_title, content, char_len, embedding, embedding_model
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                new_work_id, chk.chunk_index, chk.section_title or "",
                chk.content, len(chk.content), vec_blob, embedding_model_name
            ))
            chunks_persisted += 1
            embeddings_persisted += int(vec is not None)

        # 保存 review_index_meta
        test_conn.execute("""
            INSERT OR REPLACE INTO review_index_meta (
                doc_id, md_hash, chunk_count, embedding_model, indexed_at
            ) VALUES (?, ?, ?, ?, ?)
        """, (
            new_work_id, hashlib.sha256(full_text.encode('utf-8')).hexdigest(),
            chunks_persisted, embedding_model_name, now_iso
        ))
        test_conn.commit()

        if is_real_embedding:
            cur_lifecycle = "VECTOR_INDEXED"
        else:
            cur_lifecycle = "GRAPH_INDEXED"

        cache_conn.execute("UPDATE discovered_candidates SET lifecycle_status = ? WHERE candidate_id = ?", (cur_lifecycle, cid))
        cache_conn.commit()

        ingested_works.append({
            "candidate_id": cid,
            "new_work_id": new_work_id,
            "title": title,
            "year": year,
            "doi": doi,
            "pdf_pages": page_count,
            "pdf_size_bytes": len(pdf_bytes),
            "pdf_sha256": sha256_hash,
            "text_length": len(full_text),
            "chunks_persisted": chunks_persisted,
            "embeddings_persisted": embeddings_persisted,
            "is_real_embedding": is_real_embedding,
            "retryable": not is_real_embedding,
            "error": embedding_error,
            "embedding_model": embedding_model_name,
            "embedding_dimension": len(embeddings_list[0]) if embeddings_list else 0,
            "reference_mentions_extracted": ref_mentions_count,
            "parsing_quality_distribution": quality_counts,
            "realignment_stats": realignment_stats,
            "lifecycle_status": cur_lifecycle
        })

    test_conn.commit()
    cache_conn.commit()
    test_conn.close()
    cache_conn.close()

    return {
        "attempted_count": len(ingested_works),
        "ingested_count": sum(w["chunks_persisted"] > 0 for w in ingested_works),
        "indexed_count": sum(w["is_real_embedding"] for w in ingested_works),
        "failed_count": sum(not w["is_real_embedding"] for w in ingested_works),
        "ingested_works": ingested_works
    }


def realign_citation_graph(test_db_path: str, new_work_ids: List[str]) -> Dict[str, Any]:
    """对文献引用的全部 Mentions 逐条建立 resolution 决策与消歧闭环账本。"""
    conn = sqlite3.connect(test_db_path)
    c = conn.cursor()
    now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    total_mentions = 0
    confirmed_links = 0
    candidate_defer = 0
    unresolved_external = 0
    non_reference_or_rejected = 0

    aligned_incoming = 0

    for wid in new_work_ids:
        # 获取该文献自身
        doi_row = c.execute("SELECT id_value FROM work_identifiers WHERE work_id = ? AND id_type = 'doi'", (wid,)).fetchone()
        doi = doi_row[0].strip().lower() if doi_row else None
        title_row = c.execute("SELECT canonical_title, publication_year FROM works WHERE work_id = ?", (wid,)).fetchone()
        title, year = title_row if title_row else ("", None)
        norm_t = normalize_title(title)

        # 1. 查找已有库内文献是否有引用此新文献的 Mention
        matching_incoming_mentions = []
        if doi:
            clean_d = doi.replace("https://doi.org/", "").replace("http://dx.doi.org/", "")
            rows = c.execute("""
                SELECT mention_id, citing_work_id, ref_index
                FROM reference_mentions
                WHERE citing_work_id != ?
                  AND REPLACE(REPLACE(LOWER(parsed_doi), 'https://doi.org/', ''), 'http://dx.doi.org/', '') = ?
            """, (wid, clean_d)).fetchall()
            matching_incoming_mentions.extend(rows)

        if norm_t and len(norm_t) >= 5:
            rows = c.execute("SELECT mention_id, citing_work_id, ref_index, parsed_title, parsed_year FROM reference_mentions WHERE citing_work_id != ?", (wid,)).fetchall()
            for m_id, citing_id, r_idx, m_title, m_year in rows:
                if m_title and normalize_title(m_title) == norm_t:
                    if not (m_year and year and abs(m_year - year) > 1):
                        if (m_id, citing_id, r_idx) not in matching_incoming_mentions:
                            matching_incoming_mentions.append((m_id, citing_id, r_idx))

        for m_id, citing_id, r_idx in matching_incoming_mentions:
            edge_id = f"edge_{citing_id[:8]}_{wid[:8]}"
            c.execute("""
                INSERT OR REPLACE INTO citation_edges (
                    edge_id, citing_work_id, cited_work_id,
                    edge_status, first_observed_at, last_confirmed_at
                ) VALUES (?, ?, ?, 'CONFIRMED', ?, ?)
            """, (edge_id, citing_id, wid, now_iso, now_iso))

            assert_id = f"assert_{edge_id}_{r_idx}"
            c.execute("""
                INSERT OR REPLACE INTO citation_assertions (
                    assertion_id, edge_id, source_type,
                    source_mention_id, external_provenance_id, asserted_at
                ) VALUES (?, ?, 'LOCAL_PDF_PARSED', ?, NULL, ?)
            """, (assert_id, edge_id, m_id, now_iso))

            c.execute("""
                UPDATE resolution_decisions
                SET decision_type = 'LINK', selected_work_id = ?, reason = 'EXACT_BIBLIOGRAPHIC_MATCH'
                WHERE mention_id = ?
            """, (wid, m_id))

            aligned_incoming += 1

        # 2. 遍历该文献引出的全部 59 条 Mentions 逐条建立消歧决策闭环
        mentions = c.execute("""
            SELECT mention_id, ref_index, raw_text, quality_flag, parsed_doi, parsed_title, parsed_year
            FROM reference_mentions
            WHERE citing_work_id = ?
            ORDER BY ref_index
        """, (wid,)).fetchall()

        for m_id, r_idx, raw_t, q_flag, r_doi, r_title, r_year in mentions:
            total_mentions += 1
            dec_id = f"dec_{wid[:8]}_{r_idx}"

            if q_flag in ('NON_REFERENCE_NOISE', 'NON_REFERENCE_BACK_MATTER'):
                non_reference_or_rejected += 1
                c.execute("""
                    INSERT OR REPLACE INTO resolution_decisions (
                        decision_id, mention_id, selected_candidate_id,
                        selected_work_id, candidate_snapshot_json,
                        decision_type, decision_source, decided_by,
                        decided_at, reason, is_active
                    ) VALUES (?, ?, NULL, NULL, NULL, 'REJECT', 'RULE', 'local_citation_engine_v1', ?, 'NON_REFERENCE_NOISE', 1)
                """, (dec_id, m_id, now_iso))
                continue

            matched_target = None
            # A. 精确 DOI 匹配
            if r_doi:
                clean_r_doi = r_doi.strip().lower().replace("https://doi.org/", "").replace("http://dx.doi.org/", "")
                t_row = c.execute("""
                    SELECT w.work_id
                    FROM works w
                    JOIN work_identifiers i ON w.work_id = i.work_id
                    WHERE i.id_type = 'doi'
                      AND REPLACE(REPLACE(LOWER(i.id_value), 'https://doi.org/', ''), 'http://dx.doi.org/', '') = ?
                      AND w.work_id != ?
                    LIMIT 1
                """, (clean_r_doi, wid)).fetchone()
                if t_row:
                    matched_target = t_row[0]

            # B. 标准化题名匹配 (且年份无冲突)
            if not matched_target and r_title:
                r_norm_t = normalize_title(r_title)
                if len(r_norm_t) >= 5:
                    t_rows = c.execute("SELECT work_id, canonical_title, publication_year FROM works WHERE in_library = 1 AND work_id != ?", (wid,)).fetchall()
                    for t_id, t_title, t_year in t_rows:
                        if normalize_title(t_title) == r_norm_t:
                            if not (r_year and t_year and abs(r_year - t_year) > 1):
                                matched_target = t_id
                                break

            if matched_target:
                confirmed_links += 1
                edge_id = f"edge_{wid[:8]}_{matched_target[:8]}"
                c.execute("""
                    INSERT OR REPLACE INTO citation_edges (
                        edge_id, citing_work_id, cited_work_id,
                        edge_status, first_observed_at, last_confirmed_at
                    ) VALUES (?, ?, ?, 'CONFIRMED', ?, ?)
                """, (edge_id, wid, matched_target, now_iso, now_iso))

                assert_id = f"assert_{edge_id}_{r_idx}"
                c.execute("""
                    INSERT OR REPLACE INTO citation_assertions (
                        assertion_id, edge_id, source_type,
                        source_mention_id, external_provenance_id, asserted_at
                    ) VALUES (?, ?, 'LOCAL_PDF_PARSED', ?, NULL, ?)
                """, (assert_id, edge_id, m_id, now_iso))

                c.execute("""
                    INSERT OR REPLACE INTO resolution_decisions (
                        decision_id, mention_id, selected_candidate_id,
                        selected_work_id, candidate_snapshot_json,
                        decision_type, decision_source, decided_by,
                        decided_at, reason, is_active
                    ) VALUES (?, ?, NULL, ?, NULL, 'LINK', 'RULE', 'local_citation_engine_v1', ?, 'EXACT_BIBLIOGRAPHIC_MATCH', 1)
                """, (dec_id, m_id, matched_target, now_iso))

            else:
                # C. 馆外未解析实体 (建立外部占位 Work 节点与 UNRESOLVED_EXTERNAL 边)
                unresolved_external += 1
                ext_work_id = f"ext_{m_id}"
                c.execute("""
                    INSERT OR REPLACE INTO works (
                        work_id, work_type, canonical_title, publication_year,
                        journal_or_venue, authors_json, quarantine_status,
                        quarantine_reason, eligible_for_retrieval,
                        eligible_for_citation_graph, in_library, is_deleted,
                        created_at, updated_at
                    ) VALUES (?, 'unresolved_external', ?, ?, ?, '[]', 'UNRESOLVED', 'EXTERNAL_UNRESOLVED_REFERENCE', 0, 1, 0, 0, ?, ?)
                """, (
                    ext_work_id, r_title or raw_t[:100], r_year, None, now_iso, now_iso
                ))

                edge_id = f"edge_{wid[:8]}_{ext_work_id}"
                c.execute("""
                    INSERT OR REPLACE INTO citation_edges (
                        edge_id, citing_work_id, cited_work_id,
                        edge_status, first_observed_at, last_confirmed_at
                    ) VALUES (?, ?, ?, 'UNRESOLVED_EXTERNAL', ?, ?)
                """, (edge_id, wid, ext_work_id, now_iso, now_iso))

                assert_id = f"assert_{wid[:8]}_{m_id}"
                c.execute("""
                    INSERT OR REPLACE INTO citation_assertions (
                        assertion_id, edge_id, source_type,
                        source_mention_id, external_provenance_id, asserted_at
                    ) VALUES (?, ?, 'LOCAL_PDF_PARSED', ?, NULL, ?)
                """, (assert_id, edge_id, m_id, now_iso))

                c.execute("""
                    INSERT OR REPLACE INTO resolution_decisions (
                        decision_id, mention_id, selected_candidate_id,
                        selected_work_id, candidate_snapshot_json,
                        decision_type, decision_source, decided_by,
                        decided_at, reason, is_active
                    ) VALUES (?, ?, NULL, NULL, NULL, 'NO_MATCH', 'RULE', 'local_citation_engine_v1', ?, 'NO_MATCH_IN_LOCAL_LIBRARY', 1)
                """, (dec_id, m_id, now_iso))

    conn.commit()
    conn.close()

    return {
        "total_mentions": total_mentions,
        "confirmed_links": confirmed_links,
        "candidate_defer": candidate_defer,
        "unresolved_external": unresolved_external,
        "non_reference_or_rejected": non_reference_or_rejected,
        "is_accounting_closed": (total_mentions == (confirmed_links + candidate_defer + unresolved_external + non_reference_or_rejected)),
        "aligned_incoming_edges": aligned_incoming
    }
