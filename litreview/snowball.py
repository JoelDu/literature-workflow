"""OpenAlex & Crossref External Citation Snowball Engine (Phase 3 MVP).

Performs depth-1 forward (emerging) and backward (foundational) citation expansion
from 1~3 seed works using OpenAlex and Crossref APIs, persisting candidate records to
an independent external discovery cache database without modifying production data.
"""
import os
import re
import json
import time
import uuid
import hashlib
import urllib.request
import urllib.parse
import urllib.error
import sqlite3
from typing import Dict, List, Any, Optional, Tuple, Set
from dataclasses import dataclass, field, asdict
from datetime import datetime

from litreview.ref_parser import normalize_title, extract_first_author_surname
from litreview.sqlite_runtime import connect_sqlite


@dataclass
class DiscoveredCandidate:
    candidate_id: str
    query_run_id: str
    openalex_id: Optional[str] = None
    doi: Optional[str] = None
    title: str = ""
    authors: List[str] = field(default_factory=list)
    journal_or_venue: Optional[str] = None
    publication_year: Optional[int] = None
    cited_by_count: int = 0
    is_open_access: bool = False
    oa_url: Optional[str] = None
    landing_page_url: Optional[str] = None
    track: str = "FOUNDATIONAL"  # FOUNDATIONAL or EMERGING
    direction: str = "backward"  # backward or forward
    source_seeds: List[str] = field(default_factory=list)
    co_citation_count: int = 0
    recommendation_reason: str = ""
    in_library: bool = False
    in_library_work_id: Optional[str] = None
    match_method: Optional[str] = None
    match_evidence: Optional[Dict[str, Any]] = None
    data_source: str = "openalex"
    response_hash: str = ""
    lifecycle_status: str = "DISCOVERED"
    discovered_at: str = ""


CACHE_DB_DDL = """
CREATE TABLE IF NOT EXISTS discovery_runs (
    query_run_id TEXT PRIMARY KEY,
    seeds_json TEXT NOT NULL,
    direction TEXT NOT NULL,
    depth INTEGER NOT NULL,
    parameters_json TEXT NOT NULL,
    statistics_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS discovered_candidates (
    candidate_id TEXT PRIMARY KEY,
    query_run_id TEXT NOT NULL,
    openalex_id TEXT,
    doi TEXT,
    title TEXT NOT NULL,
    authors_json TEXT,
    journal_or_venue TEXT,
    publication_year INTEGER,
    cited_by_count INTEGER,
    is_open_access INTEGER,
    oa_url TEXT,
    landing_page_url TEXT,
    track TEXT NOT NULL,
    direction TEXT NOT NULL,
    source_seeds_json TEXT NOT NULL,
    co_citation_count INTEGER NOT NULL,
    recommendation_reason TEXT NOT NULL,
    in_library INTEGER NOT NULL,
    in_library_work_id TEXT,
    match_method TEXT,
    match_evidence_json TEXT,
    data_source TEXT NOT NULL,
    response_hash TEXT NOT NULL,
    lifecycle_status TEXT NOT NULL DEFAULT 'DISCOVERED',
    discovered_at TEXT NOT NULL,
    FOREIGN KEY (query_run_id) REFERENCES discovery_runs(query_run_id)
);

CREATE TABLE IF NOT EXISTS candidate_decisions (
    decision_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL,
    previous_status TEXT NOT NULL,
    new_status TEXT NOT NULL,
    action TEXT NOT NULL,
    decision_source TEXT NOT NULL,
    reason TEXT,
    candidate_snapshot_json TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    operator TEXT NOT NULL,
    FOREIGN KEY (candidate_id) REFERENCES discovered_candidates(candidate_id)
);

CREATE INDEX IF NOT EXISTS idx_discovered_run ON discovered_candidates(query_run_id);
CREATE INDEX IF NOT EXISTS idx_discovered_doi ON discovered_candidates(doi);
CREATE INDEX IF NOT EXISTS idx_discovered_openalex ON discovered_candidates(openalex_id);
CREATE INDEX IF NOT EXISTS idx_discovered_status_time
    ON discovered_candidates(lifecycle_status, discovered_at DESC);
CREATE INDEX IF NOT EXISTS idx_discovered_openalex_status_time
    ON discovered_candidates(openalex_id, lifecycle_status, discovered_at DESC);
CREATE INDEX IF NOT EXISTS idx_discovered_run_track_status
    ON discovered_candidates(query_run_id, track, lifecycle_status);
CREATE INDEX IF NOT EXISTS idx_decisions_cand ON candidate_decisions(candidate_id);

CREATE VIEW IF NOT EXISTS v_canonical_candidates AS
SELECT * FROM (
    SELECT c.*,
           CASE
             WHEN trim(COALESCE(c.openalex_id, '')) != ''
               THEN 'openalex:' || lower(trim(c.openalex_id))
             WHEN trim(COALESCE(c.doi, '')) != ''
               THEN 'doi:' || lower(trim(c.doi))
             ELSE 'title_year:' || lower(trim(c.title)) || ':' || COALESCE(c.publication_year, '')
           END AS canonical_key,
           ROW_NUMBER() OVER (
             PARTITION BY CASE
               WHEN trim(COALESCE(c.openalex_id, '')) != ''
                 THEN 'openalex:' || lower(trim(c.openalex_id))
               WHEN trim(COALESCE(c.doi, '')) != ''
                 THEN 'doi:' || lower(trim(c.doi))
               ELSE 'title_year:' || lower(trim(c.title)) || ':' || COALESCE(c.publication_year, '')
             END
             ORDER BY c.in_library DESC,
               CASE c.lifecycle_status
                 WHEN 'QUARANTINED_INCOMPLETE' THEN 120
                 WHEN 'VECTOR_INDEXED' THEN 110
                 WHEN 'VECTOR_INDEXED_TEST' THEN 105
                 WHEN 'GRAPH_INDEXED' THEN 100
                 WHEN 'PARSED' THEN 90
                 WHEN 'FULLTEXT_ACQUIRED' THEN 80
                 WHEN 'APPROVED' THEN 60
                 WHEN 'REJECTED' THEN 50
                 WHEN 'FULLTEXT_UNAVAILABLE' THEN 40
                 WHEN 'DISCOVERED' THEN 10
                 ELSE 0 END DESC,
               c.discovered_at DESC,
               c.candidate_id DESC
           ) AS canonical_rank
    FROM discovered_candidates c
) ranked
WHERE canonical_rank = 1;

CREATE TRIGGER IF NOT EXISTS trg_prevent_update_candidate_decisions
BEFORE UPDATE ON candidate_decisions
BEGIN
    SELECT RAISE(ABORT, 'candidate_decisions table is strictly append-only; UPDATE is prohibited');
END;

CREATE TRIGGER IF NOT EXISTS trg_prevent_delete_candidate_decisions
BEFORE DELETE ON candidate_decisions
BEGIN
    SELECT RAISE(ABORT, 'candidate_decisions table is strictly append-only; DELETE is prohibited');
END;
"""


def init_cache_db(cache_db_path: str) -> None:
    """初始化独立外部发现缓存数据库。"""
    os.makedirs(os.path.dirname(os.path.abspath(cache_db_path)), exist_ok=True)
    conn = connect_sqlite(cache_db_path, foreign_keys=True)
    conn.executescript(CACHE_DB_DDL)
    conn.commit()
    conn.close()


class OpenAlexClient:
    """安全的 OpenAlex API 客户端，按官方协议传递 API Key 与联系人参数。"""

    def __init__(self, email: Optional[str] = None, api_key: Optional[str] = None):
        self.email = email or os.getenv("OPENALEX_EMAIL", "")
        self.api_key = api_key or os.getenv("OPENALEX_API_KEY", "")
        
        ua_contact = f" (mailto:{self.email})" if self.email else ""
        self.user_agent = f"literature_analyzer/1.0{ua_contact}"
        self.api_requests_count = 0
        self.api_failures_count = 0

    def _build_url(self, base_url: str, params: Optional[Dict[str, Any]] = None) -> str:
        p = params.copy() if params else {}
        if self.api_key and "api_key" not in p:
            p["api_key"] = self.api_key
        if self.email and "mailto" not in p:
            p["mailto"] = self.email
        
        if p:
            qs = urllib.parse.urlencode(p)
            delimiter = "&" if "?" in base_url else "?"
            return f"{base_url}{delimiter}{qs}"
        return base_url

    def _request(self, url: str, timeout: int = 15) -> Optional[Dict[str, Any]]:
        self.api_requests_count += 1
        headers = {"User-Agent": self.user_agent}
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status == 200:
                    return json.loads(resp.read().decode("utf-8"))
        except Exception:
            self.api_failures_count += 1
            return None
        self.api_failures_count += 1
        return None

    def get_work_by_doi(self, doi: str) -> Optional[Dict[str, Any]]:
        clean_doi = doi.strip().lower()
        if clean_doi.startswith("http"):
            clean_doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", clean_doi)
        raw_url = f"https://api.openalex.org/works/https://doi.org/{urllib.parse.quote(clean_doi)}"
        url = self._build_url(raw_url)
        return self._request(url)

    def get_work_by_openalex_id(self, openalex_id: str) -> Optional[Dict[str, Any]]:
        short_id = openalex_id.split("/")[-1]
        raw_url = f"https://api.openalex.org/works/{short_id}"
        url = self._build_url(raw_url)
        return self._request(url)

    def search_work_by_title(self, title: str) -> Optional[Dict[str, Any]]:
        norm_t = normalize_title(title)
        raw_url = "https://api.openalex.org/works"
        
        # 1. 尝试标题精准搜索
        params = {"filter": f"title.search:{norm_t}", "per-page": 1}
        url = self._build_url(raw_url, params)
        res = self._request(url)
        if res and res.get("results"):
            return res["results"][0]
            
        # 2. 尝试通用全文/标题模糊检索
        params_fuzzy = {"search": title[:200], "per-page": 1}
        url_fuzzy = self._build_url(raw_url, params_fuzzy)
        res_fuzzy = self._request(url_fuzzy)
        if res_fuzzy and res_fuzzy.get("results"):
            return res_fuzzy["results"][0]

        return None

    def batch_get_works(self, openalex_ids: List[str], chunk_size: int = 50) -> List[Dict[str, Any]]:
        results = []
        short_ids = [w.split("/")[-1] for w in openalex_ids if w]
        for i in range(0, len(short_ids), chunk_size):
            chunk = short_ids[i:i + chunk_size]
            pipe_filter = "|".join(chunk)
            raw_url = "https://api.openalex.org/works"
            params = {"filter": f"openalex:{pipe_filter}", "per-page": len(chunk)}
            url = self._build_url(raw_url, params)
            res = self._request(url)
            if res and "results" in res:
                results.extend(res["results"])
            time.sleep(0.05)
        return results

    def get_citing_works(
        self,
        seed_openalex_ids: List[str],
        from_year: int = 2023,
        max_records: int = 150,
        per_page: int = 50
    ) -> Tuple[int, List[Dict[str, Any]]]:
        """获取近几年的施引文献（支持分页与真实 meta.count 读取）。"""
        short_ids = [w.split("/")[-1] for w in seed_openalex_ids if w]
        pipe_filter = "|".join(short_ids)
        raw_url = "https://api.openalex.org/works"
        
        all_results = []
        total_available = 0
        page = 1

        while len(all_results) < max_records:
            params = {
                "filter": f"cites:{pipe_filter},from_publication_date:{from_year}-01-01",
                "sort": "cited_by_count:desc,publication_year:desc",
                "per-page": per_page,
                "page": page
            }
            url = self._build_url(raw_url, params)
            res = self._request(url)
            if not res or "results" not in res:
                break

            total_available = res.get("meta", {}).get("count", 0)
            page_results = res.get("results", [])
            if not page_results:
                break

            all_results.extend(page_results)
            if len(all_results) >= total_available or len(page_results) < per_page:
                break

            page += 1
            time.sleep(0.05)

        return total_available, all_results[:max_records]


class CrossrefClient:
    """用于书目元数据补充与校验的 Crossref 客户端。"""

    def __init__(self, email: Optional[str] = None):
        self.email = email or os.getenv("CROSSREF_MAILTO") or os.getenv("OPENALEX_EMAIL", "")
        ua_contact = f" (mailto:{self.email})" if self.email else ""
        self.user_agent = f"literature_analyzer/1.0{ua_contact}"
        self.api_requests_count = 0
        self.api_failures_count = 0

    def get_work_by_doi(self, doi: str, timeout: int = 10) -> Optional[Dict[str, Any]]:
        clean_doi = doi.strip().lower()
        if clean_doi.startswith("http"):
            clean_doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", clean_doi)
        url = f"https://api.crossref.org/works/{urllib.parse.quote(clean_doi)}"
        if self.email:
            url += f"?mailto={urllib.parse.quote(self.email)}"

        self.api_requests_count += 1
        headers = {"User-Agent": self.user_agent}
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    return data.get("message")
        except Exception:
            self.api_failures_count += 1
            return None
        self.api_failures_count += 1
        return None


def _resolve_seed_work(
    seed_input: str,
    client: OpenAlexClient,
    db_path: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """将输入的 doc_id / DOI / 题名解析为带有本馆元数据的 OpenAlex Work。"""
    seed_str = seed_input.strip()
    db_meta = None

    # 收集可能存在的本地数据库路径（测试库与生产库）
    dbs_to_check = []
    if db_path and os.path.exists(db_path):
        dbs_to_check.append(db_path)
    prod_env_db = os.getenv("DB_PATH", "./data/batch_tracking.db")
    if os.path.exists(prod_env_db) and prod_env_db not in dbs_to_check:
        dbs_to_check.append(prod_env_db)
    for default_path in ["data/test_citation_network.db", "data/batch_tracking.db"]:
        if os.path.exists(default_path) and default_path not in dbs_to_check:
            dbs_to_check.append(default_path)

    for db_p in dbs_to_check:
        try:
            conn = sqlite3.connect(f"file:{db_p}?mode=ro", uri=True)
            c = conn.cursor()
            
            # 1. 检查 works + work_identifiers 表 (Phase 3 规范结构)
            try:
                row = c.execute("""
                    SELECT w.work_id, w.canonical_title, w.publication_year, w.journal_or_venue,
                           i.id_value as doi
                    FROM works w
                    LEFT JOIN work_identifiers i ON w.work_id = i.work_id AND i.id_type = 'doi'
                    WHERE w.work_id = ? OR w.work_id LIKE ? OR i.id_value = ?
                    LIMIT 1
                """, (seed_str, f"{seed_str}%", seed_str.lower())).fetchone()
                if row:
                    db_meta = {
                        "doc_id": row[0],
                        "title": row[1],
                        "year": row[2],
                        "journal": row[3],
                        "doi": row[4]
                    }
                    conn.close()
                    break
            except Exception:
                pass

            # 2. 检查 paper_details 表 (生产馆藏 344 篇结构)
            try:
                row = c.execute("""
                    SELECT doc_id, COALESCE(NULLIF(title_en, ''), NULLIF(title, ''), title_zh), doi
                    FROM paper_details
                    WHERE doc_id = ? OR doc_id LIKE ? OR doi = ? OR title_en = ? OR title_zh = ?
                    LIMIT 1
                """, (seed_str, f"{seed_str}%", seed_str.lower(), seed_str, seed_str)).fetchone()
                if row:
                    db_meta = {
                        "doc_id": row[0],
                        "title": row[1],
                        "year": None,
                        "journal": None,
                        "doi": row[2]
                    }
                    conn.close()
                    break
            except Exception:
                pass

            # 3. 检查 papers 表
            try:
                row = c.execute("""
                    SELECT id, title FROM papers WHERE id = ? OR id LIKE ? OR title = ? LIMIT 1
                """, (seed_str, f"{seed_str}%", seed_str)).fetchone()
                if row:
                    db_meta = {
                        "doc_id": row[0],
                        "title": row[1],
                        "year": None,
                        "journal": None,
                        "doi": None
                    }
                    conn.close()
                    break
            except Exception:
                pass

            conn.close()
        except Exception:
            pass

    oa_work = None
    target_doi = db_meta["doi"] if db_meta and db_meta.get("doi") else (seed_str if "/" in seed_str else None)

    # 1. 优先通过 DOI 精准匹配 OpenAlex Work
    if target_doi:
        oa_work = client.get_work_by_doi(target_doi)

    # 2. 尝试 OpenAlex ID 匹配
    if not oa_work and (seed_str.startswith("W") or seed_str.startswith("https://openalex.org/W")):
        oa_work = client.get_work_by_openalex_id(seed_str)

    # 3. 尝试通过本地题名（英文/中文标准化题名）在 OpenAlex 中检索
    if not oa_work and db_meta and db_meta.get("title"):
        oa_work = client.search_work_by_title(db_meta["title"])

    # 4. 尝试直接以输入内容作为题名搜索
    if not oa_work and not target_doi:
        oa_work = client.search_work_by_title(seed_str)

    if oa_work:
        oa_work["local_doc_id"] = db_meta["doc_id"] if db_meta else None
        return oa_work

    return None


def _load_in_library_index(db_path: Optional[str]) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    """加载全部馆藏（344篇）的 DOI 与标准化题名索引（含年份与实体快照）。"""
    doi_index = {}
    title_index = {}

    dbs_to_check = []
    if db_path and os.path.exists(db_path):
        dbs_to_check.append(db_path)
    prod_env_db = os.getenv("DB_PATH", "./data/batch_tracking.db")
    if os.path.exists(prod_env_db) and prod_env_db not in dbs_to_check:
        dbs_to_check.append(prod_env_db)
    for default_path in ["data/test_citation_network.db", "data/batch_tracking.db"]:
        if os.path.exists(default_path) and default_path not in dbs_to_check:
            dbs_to_check.append(default_path)

    for db_p in dbs_to_check:
        try:
            conn = sqlite3.connect(f"file:{db_p}?mode=ro", uri=True)
            c = conn.cursor()
            
            # 1. 加载 works 表
            try:
                rows = c.execute("""
                    SELECT w.work_id, w.canonical_title, w.publication_year, i.id_value as doi
                    FROM works w
                    LEFT JOIN work_identifiers i ON w.work_id = i.work_id AND i.id_type = 'doi'
                    WHERE w.in_library = 1 AND w.quarantine_status = 'NORMAL'
                """).fetchall()
                for wid, title, year, doi in rows:
                    record = {"work_id": wid, "title": title, "year": year, "doi": doi}
                    if doi:
                        clean_doi = doi.strip().lower()
                        doi_index[clean_doi] = record
                    if title:
                        norm_t = normalize_title(title)
                        if len(norm_t) >= 5:
                            title_index[norm_t] = record
            except Exception:
                pass

            # 2. 加载 paper_details 表 (生产 344 篇)
            try:
                rows_pd = c.execute("""
                    SELECT doc_id, COALESCE(NULLIF(title_en, ''), NULLIF(title, ''), title_zh), doi
                    FROM paper_details
                """).fetchall()
                for did, title, doi in rows_pd:
                    record = {"work_id": did, "title": title, "year": None, "doi": doi}
                    if doi:
                        clean_doi = doi.strip().lower()
                        if clean_doi not in doi_index:
                            doi_index[clean_doi] = record
                    if title:
                        norm_t = normalize_title(title)
                        if len(norm_t) >= 5 and norm_t not in title_index:
                            title_index[norm_t] = record
            except Exception:
                pass

            conn.close()
        except Exception:
            pass

    return doi_index, title_index


def _check_in_library_match(
    doi: Optional[str],
    title: str,
    year: Optional[int],
    doi_index: Dict[str, Dict[str, Any]],
    title_index: Dict[str, Dict[str, Any]]
) -> Tuple[bool, Optional[str], Optional[str], Optional[Dict[str, Any]]]:
    """严格碰撞检测：1. DOI 完全一致；或 2. 标准化题名一致且年份不冲突。"""
    if doi:
        clean_doi = doi.strip().lower()
        if clean_doi in doi_index:
            matched = doi_index[clean_doi]
            return True, matched["work_id"], "exact_doi", {
                "matched_field": "doi",
                "matched_value": clean_doi,
                "local_title": matched["title"],
                "local_year": matched["year"]
            }

    if title:
        norm_t = normalize_title(title)
        if len(norm_t) >= 5 and norm_t in title_index:
            matched = title_index[norm_t]
            # 年份冲突校验
            if year and matched["year"] and abs(year - matched["year"]) > 1:
                return False, None, None, None
            return True, matched["work_id"], "exact_title", {
                "matched_field": "canonical_title",
                "matched_value": norm_t,
                "local_title": matched["title"],
                "local_year": matched["year"]
            }

    return False, None, None, None


def expand_citations(
    seed_ids: List[str],
    direction: str = "both",
    depth: int = 1,
    limit_per_track: int = 10,
    min_co_citation: int = 1,
    recent_years: int = 3,
    max_forward_fetch: int = 150,
    db_path: Optional[str] = None,
    cache_db_path: Optional[str] = None
) -> Dict[str, Any]:
    """执行深度=1的 OpenAlex 引文网络外部滚雪球扩展并记录完整统计账本。"""
    query_run_id = f"run_{uuid.uuid4().hex[:12]}"
    now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    current_year = datetime.now().year
    from_year = current_year - recent_years

    oa_client = OpenAlexClient()
    crossref_client = CrossrefClient()

    doi_index, title_index = _load_in_library_index(db_path)

    # 1. 解析所有种子文献
    resolved_seeds = []
    seed_works = {}
    for s_input in seed_ids:
        s_work = _resolve_seed_work(s_input, oa_client, db_path)
        if s_work:
            oa_short_id = s_work["id"].split("/")[-1]
            seed_key = s_work.get("local_doc_id") if s_work.get("local_doc_id") and not s_work.get("local_doc_id").startswith("10.") else oa_short_id
            resolved_seeds.append({
                "input": s_input,
                "doc_id": seed_key,
                "openalex_id": s_work["id"],
                "doi": s_work.get("doi"),
                "title": s_work.get("title"),
                "publication_year": s_work.get("publication_year"),
                "cited_by_count": s_work.get("cited_by_count", 0),
                "referenced_works_count": len(s_work.get("referenced_works", []))
            })
            seed_works[seed_key] = s_work

    if not resolved_seeds:
        return {"error": "未能解析任何有效的种子文献，请检查输入的 doc_id / DOI。"}

    # 2. 统计账本原始计数
    backward_raw_mentions = sum(len(w.get("referenced_works", [])) for w in seed_works.values())
    
    # 收集全部唯一的后向 OpenAlex ID
    ref_to_seeds: Dict[str, Set[str]] = {}
    for s_key, s_work in seed_works.items():
        for r_url in s_work.get("referenced_works", []):
            r_id = r_url.split("/")[-1]
            if r_id not in ref_to_seeds:
                ref_to_seeds[r_id] = set()
            ref_to_seeds[r_id].add(s_key)

    backward_unique_ids = list(ref_to_seeds.keys())
    backward_unique_ids_before_limit = len(backward_unique_ids)
    backward_metadata_requested = 0
    backward_metadata_returned = 0

    foundational_candidates: List[DiscoveredCandidate] = []
    emerging_candidates: List[DiscoveredCandidate] = []

    # 3. Track A: Foundational Literature (全量分批获取，不提前截断)
    if direction in ("backward", "both"):
        backward_metadata_requested = len(backward_unique_ids)
        ref_works = oa_client.batch_get_works(backward_unique_ids, chunk_size=50)
        backward_metadata_returned = len(ref_works)

        for rw in ref_works:
            r_id = rw["id"].split("/")[-1]
            citing_seeds = sorted(list(ref_to_seeds.get(r_id, [])))
            co_cite_cnt = len(citing_seeds)

            if co_cite_cnt < min_co_citation:
                continue

            doi = rw.get("doi")
            clean_doi = doi.lower().replace("https://doi.org/", "") if doi else None
            title = rw.get("title") or "Unknown Title"
            year = rw.get("publication_year")

            authors = [
                a.get("author", {}).get("display_name")
                for a in rw.get("authorships", [])
                if a.get("author", {}).get("display_name")
            ]

            venue = rw.get("primary_location", {}).get("source", {}).get("display_name") if rw.get("primary_location") and rw.get("primary_location", {}).get("source") else None
            oa_info = rw.get("open_access", {})
            is_oa = bool(oa_info.get("is_oa"))
            oa_url = oa_info.get("oa_url")
            landing_url = rw.get("doi") or rw.get("id")

            # 碰撞检测
            in_lib, lib_wid, m_method, m_evidence = _check_in_library_match(
                doi=clean_doi, title=title, year=year,
                doi_index=doi_index, title_index=title_index
            )

            if co_cite_cnt > 1:
                reason = f"被 {', '.join(citing_seeds)} 共 {co_cite_cnt} 篇种子共同引用作为机理基础"
            else:
                reason = f"被种子文献 {citing_seeds[0]} 引用的基础高被引文献"

            cand_id = f"cand_found_{query_run_id}_{r_id}"
            resp_hash = hashlib.sha256(json.dumps(rw, sort_keys=True).encode("utf-8")).hexdigest()

            foundational_candidates.append(DiscoveredCandidate(
                candidate_id=cand_id,
                query_run_id=query_run_id,
                openalex_id=rw.get("id"),
                doi=clean_doi,
                title=title,
                authors=authors,
                journal_or_venue=venue,
                publication_year=year,
                cited_by_count=rw.get("cited_by_count", 0),
                is_open_access=is_oa,
                oa_url=oa_url,
                landing_page_url=landing_url,
                track="FOUNDATIONAL",
                direction="backward",
                source_seeds=citing_seeds,
                co_citation_count=co_cite_cnt,
                recommendation_reason=reason,
                in_library=in_lib,
                in_library_work_id=lib_wid,
                match_method=m_method,
                match_evidence=m_evidence,
                data_source="openalex",
                response_hash=resp_hash,
                lifecycle_status="DISCOVERED",
                discovered_at=now_iso
            ))

        foundational_candidates.sort(
            key=lambda x: (x.co_citation_count, x.cited_by_count, x.publication_year or 0),
            reverse=True
        )

    # 4. Track B: Emerging Literature (前向施引文献，支持分页与截断标记)
    forward_total_available = 0
    forward_records_retrieved = 0
    forward_records_truncated = False

    if direction in ("forward", "both"):
        seed_oa_ids = [w["id"] for w in seed_works.values()]
        total_avail, citing_results = oa_client.get_citing_works(
            seed_oa_ids, from_year=from_year, max_records=max_forward_fetch, per_page=50
        )
        forward_total_available = total_avail
        forward_records_retrieved = len(citing_results)
        forward_records_truncated = (forward_total_available > forward_records_retrieved)

        for cw in citing_results:
            doi = cw.get("doi")
            clean_doi = doi.lower().replace("https://doi.org/", "") if doi else None
            title = cw.get("title") or "Unknown Title"
            year = cw.get("publication_year")

            authors = [
                a.get("author", {}).get("display_name")
                for a in cw.get("authorships", [])
                if a.get("author", {}).get("display_name")
            ]

            venue = cw.get("primary_location", {}).get("source", {}).get("display_name") if cw.get("primary_location") and cw.get("primary_location", {}).get("source") else None
            oa_info = cw.get("open_access", {})
            is_oa = bool(oa_info.get("is_oa"))
            oa_url = oa_info.get("oa_url")
            landing_url = cw.get("doi") or cw.get("id")

            # 严格依据 referenced_works 判定施引了哪些种子
            referenced = [r.split("/")[-1] for r in cw.get("referenced_works", [])]
            cited_seeds = []
            for s_key, s_work in seed_works.items():
                s_short_id = s_work["id"].split("/")[-1]
                if s_short_id in referenced:
                    cited_seeds.append(s_key)

            if not cited_seeds:
                cited_seeds = ["SOURCE_SEED_UNKNOWN"]
                reason = "近3年最新施引文献 (具体施引种子待细粒度校验)"
            elif len(cited_seeds) > 1:
                reason = f"近3年最新施引文献 (同时引用了种子 {', '.join(cited_seeds)})"
            else:
                reason = f"近3年最新施引文献 (引用了种子 {cited_seeds[0]})"

            in_lib, lib_wid, m_method, m_evidence = _check_in_library_match(
                doi=clean_doi, title=title, year=year,
                doi_index=doi_index, title_index=title_index
            )

            cw_id = cw["id"].split("/")[-1]
            cand_id = f"cand_emerg_{query_run_id}_{cw_id}"
            resp_hash = hashlib.sha256(json.dumps(cw, sort_keys=True).encode("utf-8")).hexdigest()

            emerging_candidates.append(DiscoveredCandidate(
                candidate_id=cand_id,
                query_run_id=query_run_id,
                openalex_id=cw.get("id"),
                doi=clean_doi,
                title=title,
                authors=authors,
                journal_or_venue=venue,
                publication_year=year,
                cited_by_count=cw.get("cited_by_count", 0),
                is_open_access=is_oa,
                oa_url=oa_url,
                landing_page_url=landing_url,
                track="EMERGING",
                direction="forward",
                source_seeds=cited_seeds,
                co_citation_count=len(cited_seeds) if "SOURCE_SEED_UNKNOWN" not in cited_seeds else 0,
                recommendation_reason=reason,
                in_library=in_lib,
                in_library_work_id=lib_wid,
                match_method=m_method,
                match_evidence=m_evidence,
                data_source="openalex",
                response_hash=resp_hash,
                lifecycle_status="DISCOVERED",
                discovered_at=now_iso
            ))

        emerging_candidates.sort(
            key=lambda x: (x.co_citation_count, x.cited_by_count, x.publication_year or 0),
            reverse=True
        )

    # 5. 去重与聚合统计
    all_materialized = foundational_candidates + emerging_candidates
    dedup_seen = set()
    unique_candidates = []

    for c in all_materialized:
        dedup_key = c.openalex_id or c.doi or f"{normalize_title(c.title)}|{c.publication_year}"
        if dedup_key not in dedup_seen:
            dedup_seen.add(dedup_key)
            unique_candidates.append(c)

    top_foundational = foundational_candidates[:limit_per_track]
    top_emerging = emerging_candidates[:limit_per_track]

    # 三篇种子共同引用的交集文献
    shared_by_all_seeds = [
        c for c in foundational_candidates
        if len(c.source_seeds) >= len(seed_works) and len(seed_works) >= 2
    ]

    in_lib_matches = [c for c in unique_candidates if c.in_library]
    new_ext_candidates = [c for c in unique_candidates if not c.in_library]

    total_api_requests = oa_client.api_requests_count + crossref_client.api_requests_count
    total_api_failures = oa_client.api_failures_count + crossref_client.api_failures_count
    api_success_rate = round((total_api_requests - total_api_failures) / max(1, total_api_requests), 3)
    metadata_coverage = round(backward_metadata_returned / max(1, backward_unique_ids_before_limit), 3) if backward_unique_ids_before_limit > 0 else 1.0

    statistics = {
        "query_run_id": query_run_id,
        "seeds_count": len(resolved_seeds),
        "backward_raw_mentions": backward_raw_mentions,
        "backward_unique_ids_before_limit": backward_unique_ids_before_limit,
        "backward_metadata_requested": backward_metadata_requested,
        "backward_metadata_returned": backward_metadata_returned,
        "forward_total_available": forward_total_available,
        "forward_records_retrieved": forward_records_retrieved,
        "forward_records_truncated": forward_records_truncated,
        "materialized_candidates_before_dedup": len(all_materialized),
        "unique_candidates_after_dedup": len(unique_candidates),
        "foundational_candidates_count": len(foundational_candidates),
        "emerging_candidates_count": len(emerging_candidates),
        "in_library_matches_count": len(in_lib_matches),
        "new_external_candidates_count": len(new_ext_candidates),
        "api_requests_count": total_api_requests,
        "api_failures_count": total_api_failures,
        "api_success_rate": api_success_rate,
        "metadata_coverage": metadata_coverage
    }

    # 6. 持久化至独立外部发现缓存数据库
    if not cache_db_path:
        cache_db_path = os.getenv(
            "EXTERNAL_DISCOVERY_CACHE_DB_PATH",
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "external_discovery_cache.db")
        )

    try:
        init_cache_db(cache_db_path)
        conn = connect_sqlite(cache_db_path, foreign_keys=True)
        conn.execute("""
            INSERT INTO discovery_runs (
                query_run_id, seeds_json, direction, depth,
                parameters_json, statistics_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            query_run_id,
            json.dumps(resolved_seeds, ensure_ascii=False),
            direction,
            1,
            json.dumps({
                "limit_per_track": limit_per_track,
                "min_co_citation": min_co_citation,
                "recent_years_cutoff": from_year,
                "max_forward_fetch": max_forward_fetch
            }),
            json.dumps(statistics, ensure_ascii=False),
            now_iso
        ))

        for cand in unique_candidates:
            conn.execute("""
                INSERT OR REPLACE INTO discovered_candidates (
                    candidate_id, query_run_id, openalex_id, doi, title,
                    authors_json, journal_or_venue, publication_year,
                    cited_by_count, is_open_access, oa_url, landing_page_url,
                    track, direction, source_seeds_json, co_citation_count,
                    recommendation_reason, in_library, in_library_work_id,
                    match_method, match_evidence_json, data_source,
                    response_hash, lifecycle_status, discovered_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'DISCOVERED', ?)
            """, (
                cand.candidate_id,
                cand.query_run_id,
                cand.openalex_id,
                cand.doi,
                cand.title,
                json.dumps(cand.authors, ensure_ascii=False),
                cand.journal_or_venue,
                cand.publication_year,
                cand.cited_by_count,
                1 if cand.is_open_access else 0,
                cand.oa_url,
                cand.landing_page_url,
                cand.track,
                cand.direction,
                json.dumps(cand.source_seeds, ensure_ascii=False),
                cand.co_citation_count,
                cand.recommendation_reason,
                1 if cand.in_library else 0,
                cand.in_library_work_id,
                cand.match_method,
                json.dumps(cand.match_evidence, ensure_ascii=False) if cand.match_evidence else None,
                cand.data_source,
                cand.response_hash,
                cand.discovered_at
            ))

        conn.commit()
        conn.close()
    except Exception as e:
        statistics["cache_db_save_error"] = str(e)

    return {
        "query_run_id": query_run_id,
        "seeds": resolved_seeds,
        "seeds_count": len(resolved_seeds),
        "materialized_candidates_count": len(unique_candidates),
        "foundational_count": len(foundational_candidates),
        "emerging_count": len(emerging_candidates),
        "new_external_candidates_count": len(new_ext_candidates),
        "in_library_matches_count": len(in_lib_matches),
        "parameters": {
            "direction": direction,
            "depth": 1,
            "limit_per_track": limit_per_track,
            "min_co_citation": min_co_citation,
            "recent_years_cutoff": from_year,
            "max_forward_fetch": max_forward_fetch
        },
        "statistics": statistics,
        "top_foundational_literature": [asdict(c) for c in top_foundational],
        "top_emerging_literature": [asdict(c) for c in top_emerging],
        "shared_by_all_seeds": [asdict(c) for c in shared_by_all_seeds],
        "in_library_matches": [asdict(c) for c in in_lib_matches],
        "new_external_candidates_sample": [asdict(c) for c in new_ext_candidates[:20]]
    }


def format_snowball_markdown_report(result: Dict[str, Any]) -> str:
    """将滚雪球分析结果渲染为严格直接绑定 query_run_id 的规范 Markdown 报告。"""
    if "error" in result:
        return f"❌ {result['error']}"

    run_id = result["query_run_id"]
    seeds = result["seeds"]
    stats = result["statistics"]
    foundational = result["top_foundational_literature"]
    emerging = result["top_emerging_literature"]
    shared_all = result["shared_by_all_seeds"]
    in_lib = result["in_library_matches"]

    lines = []
    lines.append(f"# ❄️ OpenAlex 外部引文网络滚雪球发现报告 (Run ID: `{run_id}`)")
    lines.append("")

    # 1. 种子文献
    lines.append("### 🎯 种子文献 (Seed Works)")
    for i, s in enumerate(seeds, 1):
        lines.append(f"{i}. 《{s['title']}》 ({s['publication_year'] or '-'}) | 本地/标识ID: `{s['doc_id']}`")
        lines.append(f"   - DOI: `{s['doi'] or '-'}` | OpenAlex ID: `{s['openalex_id']}`")
        lines.append(f"   - 参考文献 (后向): **{s['referenced_works_count']}** 篇 | 被引次数 (前向): **{s['cited_by_count']}** 次")
    lines.append("")

    # 2. 统计账本
    lines.append("### 📊 完整执行统计账本 (Execution Ledger)")
    lines.append(f"* **后向参考文献 (Backward)**: 原始引用提及 **{stats['backward_raw_mentions']}** 条 | 唯一 OpenAlex ID **{stats['backward_unique_ids_before_limit']}** 篇 | 批请求元数据 **{stats['backward_metadata_requested']}** 篇 | 实际返回 **{stats['backward_metadata_returned']}** 篇 (覆盖率: **{stats['metadata_coverage']*100:.1f}%**)")
    trunc_str = f"已按安全上限截断至 {stats['forward_records_retrieved']} 篇" if stats['forward_records_truncated'] else "全量获取"
    lines.append(f"* **前向施引文献 (Forward 近3年)**: 全局可用施引 **{stats['forward_total_available']}** 篇 | 实际获取 **{stats['forward_records_retrieved']}** 篇 ({trunc_str})")
    lines.append(f"* **候选实体物化与去重**: 跨轨物化候选 **{stats['materialized_candidates_before_dedup']}** 篇 | 去重后唯一候选 **{stats['unique_candidates_after_dedup']}** 篇")
    lines.append(f"  * 🏛️ 基石文献候选 (Foundational): **{stats['foundational_candidates_count']}** 篇")
    lines.append(f"  * 🚀 前沿文献候选 (Emerging): **{stats['emerging_candidates_count']}** 篇")
    lines.append(f"* **本地馆藏碰撞对照**: 命中本地 344 馆藏 **{stats['in_library_matches_count']}** 篇 | 外部新发现候选 **{stats['new_external_candidates_count']}** 篇 (已存入缓存库，状态 DISCOVERED)")
    lines.append(f"* **API 审计**: 请求 **{stats['api_requests_count']}** 次 | 失败 **{stats['api_failures_count']}** 次 | API 请求成功率 **{stats['api_success_rate']*100:.1f}%**")
    lines.append("")

    # 3. Track A: Foundational
    lines.append("### 🏛️ Track A: Top 10 基石文献 (Foundational Literature)")
    if foundational:
        for i, f in enumerate(foundational, 1):
            m_method_str = f" ({f.get('match_method')})" if f.get("match_method") else ""
            lib_badge = f" [🏛️ 匹配馆藏: `{f.get('in_library_work_id', '')[:8]}`{m_method_str}]" if f.get("in_library") else " [🌐 外部新发现]"
            oa_badge = " [🔓 OA]" if f.get("is_open_access") else ""
            lines.append(f"{i}. 《{f.get('title', 'Unknown')}》 ({f.get('publication_year') or '-'}){lib_badge}{oa_badge}")
            lines.append(f"   - **作者**: {', '.join(f.get('authors', [])[:3]) if f.get('authors') else '-'} | **期刊/来源**: {f.get('journal_or_venue') or '-'}")
            lines.append(f"   - **推荐依据**: {f.get('recommendation_reason', '-')} (全局被引: **{f.get('cited_by_count', 0)}** 次)")
            lines.append(f"   - **标识/链接**: DOI: `{f.get('doi') or '-'}` | 链接: {f.get('landing_page_url') or f.get('openalex_id') or '-'}")
    else:
        lines.append("*(未检索到符合条件的基石文献)*")
    lines.append("")

    # 4. Track B: Emerging
    lines.append("### 🚀 Track B: Top 10 前沿文献 (Emerging Literature, 最近3年)")
    if emerging:
        for i, e in enumerate(emerging, 1):
            m_method_str = f" ({e.get('match_method')})" if e.get("match_method") else ""
            lib_badge = f" [🏛️ 匹配馆藏: `{e.get('in_library_work_id', '')[:8]}`{m_method_str}]" if e.get("in_library") else " [🌐 外部新发现]"
            oa_badge = f" [🔓 OA 链接]({e.get('oa_url')})" if e.get("is_open_access") and e.get("oa_url") else (" [🔓 OA]" if e.get("is_open_access") else "")
            lines.append(f"{i}. 《{e.get('title', 'Unknown')}》 ({e.get('publication_year') or '-'}){lib_badge}{oa_badge}")
            lines.append(f"   - **作者**: {', '.join(e.get('authors', [])[:3]) if e.get('authors') else '-'} | **期刊/来源**: {e.get('journal_or_venue') or '-'}")
            lines.append(f"   - **推荐理由**: {e.get('recommendation_reason', '-')} (全局被引: **{e.get('cited_by_count', 0)}** 次)")
            lines.append(f"   - **合法链接**: {e.get('landing_page_url') or e.get('openalex_id') or '-'}")
    else:
        lines.append("*(未检索到符合条件的前沿文献)*")
    lines.append("")

    # 5. Shared by all seeds
    lines.append("### 🔗 三篇种子共同引用的核心交叉文献 (Cross-Seed Intersection)")
    if shared_all:
        for i, s in enumerate(shared_all, 1):
            lines.append(f"{i}. 《{s['title']}》 ({s['publication_year'] or '-'}) | 全局被引: {s['cited_by_count']} 次")
            lines.append(f"   - 共同引用来源: `{', '.join(s['source_seeds'])}` | DOI: `{s['doi'] or '-'}`")
    else:
        lines.append("*(三篇种子间暂无被全部种子共同引用的单一参考文献)*")
    lines.append("")

    # 6. In-library matches
    lines.append("### 📚 发现文献与本地 344 篇馆藏重合对照")
    if in_lib:
        for i, m in enumerate(in_lib, 1):
            m_method = m.get("match_method") or "in_library_match"
            wid = m.get("in_library_work_id", "")[:8]
            lines.append(f"{i}. 《{m.get('title', 'Unknown')}》 ({m.get('publication_year') or '-'}) -> 命中馆藏 Work: `{wid}` (匹配方式: `{m_method}`, 来源轨道: {m.get('track', '-')})")
    else:
        lines.append("*(本次滚雪球发现的候选文献均为外部未入库文献)*")

    return "\n".join(lines)
