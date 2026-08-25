"""VectorStore：SQLite BLOB 向量存储 + numpy 内存余弦检索。

与主管道共用 data/batch_tracking.db，表名 review_ 前缀隔离。
"""
import os
import re
import json
import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime

import numpy as np

from db_schema import ensure_papers_table
from litreview.sqlite_runtime import connect_sqlite


@dataclass
class ScoredChunk:
    chunk_id: int
    doc_id: str
    section_title: str
    content: str
    score: float


def md_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class VectorStore:
    def __init__(self, db_path: str, dim: int, readonly: bool = False):
        self.db_path = db_path
        self.dim = dim
        self.readonly = readonly
        self._matrix = None       # np.ndarray (n, dim)
        self._meta = None         # list[dict] 与矩阵行对应
        self._fingerprint = None  # 建矩阵那一刻的库指纹，用于发现别的进程写入的新向量
        if not self.readonly:
            self.ensure_schema()

    def _conn(self) -> sqlite3.Connection:
        return connect_sqlite(self.db_path, readonly=self.readonly)

    def ensure_schema(self) -> None:
        conn = self._conn()
        c = conn.cursor()
        # papers 归主管道所有，这里只是确保它存在：本模块几乎每个查询都要 JOIN 它，
        # 而刚装好、batch_pipeline 一次都没跑过的机器上它还不存在，
        # 结果是 review.py / MCP 一上来就抛 no such table 的原始 traceback。
        # DDL 只有 db_schema.py 一份，两边共用，不会各写各的。
        ensure_papers_table(conn)
        c.execute("""CREATE TABLE IF NOT EXISTS review_chunks (
            chunk_id        INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_id          TEXT NOT NULL,
            chunk_index     INTEGER NOT NULL,
            section_title   TEXT NOT NULL DEFAULT '',
            content         TEXT NOT NULL,
            char_len        INTEGER NOT NULL,
            embedding       BLOB,
            embedding_model TEXT,
            UNIQUE(doc_id, chunk_index)
        )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_review_chunks_doc ON review_chunks(doc_id)")
        c.execute("""CREATE TABLE IF NOT EXISTS review_index_meta (
            doc_id     TEXT PRIMARY KEY,
            md_hash    TEXT NOT NULL,
            n_chunks   INTEGER NOT NULL,
            embedded   INTEGER NOT NULL DEFAULT 0,
            indexed_at TEXT
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS review_evidence_cache (
            cache_key  TEXT PRIMARY KEY,
            chunk_id   INTEGER NOT NULL,
            query      TEXT NOT NULL,
            score      REAL,
            summary    TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        )""")
        c.execute("PRAGMA table_info(review_evidence_cache)")
        _cache_cols = {row[1] for row in c.fetchall()}
        for _col, _decl in (("content_hash", "TEXT"), ("prompt_version", "TEXT"),
                            ("model_name", "TEXT"), ("schema_version", "TEXT")):
            if _col not in _cache_cols:
                c.execute(f"ALTER TABLE review_evidence_cache ADD COLUMN {_col} {_decl}")
        c.execute("CREATE INDEX IF NOT EXISTS idx_review_evidence_chunk ON review_evidence_cache(chunk_id)")
        # ── 结构化元数据（enrich 管道产出） ──
        c.execute("""CREATE TABLE IF NOT EXISTS paper_details (
            doc_id      TEXT PRIMARY KEY,
            title       TEXT,
            title_zh    TEXT,
            title_en    TEXT,
            doi         TEXT,
            authors     TEXT,
            journal     TEXT,
            year        TEXT,
            keywords    TEXT,
            n_figures   INTEGER DEFAULT 0,
            n_tables    INTEGER DEFAULT 0,
            n_refs      INTEGER DEFAULT 0,
            enriched_at TEXT,
            source      TEXT
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS paper_assets (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_id     TEXT NOT NULL,
            asset_type TEXT NOT NULL,
            img_path   TEXT,
            caption    TEXT,
            page_idx   INTEGER
        )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_paper_assets_doc ON paper_assets(doc_id)")
        c.execute("""CREATE TABLE IF NOT EXISTS paper_references (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_id    TEXT NOT NULL,
            ref_index INTEGER NOT NULL,
            raw_text  TEXT NOT NULL
        )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_paper_refs_doc ON paper_references(doc_id)")
        # 各文献类型（教材/专利/标准/报告/网页）要用到的字段：增量热升级——
        # paper_details 已存在于老库时按需补列，全部可空、默认 NULL，对已有行零影响。
        # 其中 legal_status / status_checked_at 这两列**只由人工写入**
        #（review.py patents|standards --set）：专利的法律状态、标准的现行与否都不在 PDF 上，
        # 且会随时间变化，机器无从推断。详见 litreview/patent.py 与 doctype.py 的模块头部说明。
        # 其余列（patent_no、doc_no、filing_date=申请日、effective_date=标准实施日…）
        # 都是印在封面上的事实，由正则抽取，不会变。
        c.execute("PRAGMA table_info(paper_details)")
        _pd_cols = {row[1] for row in c.fetchall()}
        for _col, _decl in (("doc_type", "TEXT DEFAULT 'paper'"), ("publisher", "TEXT"),
                            ("pub_place", "TEXT"), ("edition", "TEXT"), ("isbn", "TEXT"),
                            ("patent_no", "TEXT"), ("filing_date", "TEXT"),
                            ("legal_status", "TEXT"), ("status_checked_at", "TEXT"),
                            ("doc_no", "TEXT"), ("url", "TEXT"),
                            ("effective_date", "TEXT"),
                            ("quarantine_status", "TEXT DEFAULT 'NORMAL'"),
                            ("quarantine_reason", "TEXT"),
                            ("cite_key", "TEXT")):
            if _col not in _pd_cols:
                c.execute(f"ALTER TABLE paper_details ADD COLUMN {_col} {_decl}")
        conn.commit()
        conn.close()

    # ── 索引写入 ──────────────────────────────────────────────────────────

    def docs_needing_index(self, doc_ids: list = None) -> list:
        """返回 [(doc_id, mineru_md, doc_type)]：未索引 / 内容变化 / 上次嵌入未完成的 EXPORTED 文档。
        若传入 doc_ids 则仅检查指定文档。
        doc_type 供索引时按类型选块大小（书籍块更大）。"""
        conn = self._conn()
        c = conn.cursor()
        where_clause = "p.status = 'EXPORTED' AND p.mineru_md IS NOT NULL AND p.mineru_md != ''"
        params = []
        if doc_ids:
            marks = ",".join("?" * len(doc_ids))
            where_clause += f" AND p.id IN ({marks})"
            params = list(doc_ids)

        c.execute(f"""
            SELECT p.id, p.mineru_md, m.md_hash, m.embedded, COALESCE(d.doc_type, 'paper')
            FROM papers p
            LEFT JOIN review_index_meta m ON m.doc_id = p.id
            LEFT JOIN paper_details d ON d.doc_id = p.id
            WHERE {where_clause}
        """, params)
        rows = c.fetchall()
        conn.close()
        out = []
        for doc_id, md, old_hash, embedded, doc_type in rows:
            if old_hash is None or old_hash != md_hash(md) or not embedded:
                out.append((doc_id, md, doc_type))
        return out

    def replace_doc_chunks(self, doc_id: str, content_hash: str, chunks: list) -> list:
        """单文档单事务：删旧块、插新块、写 meta(embedded=0)。返回新 chunk_id 列表。"""
        conn = self._conn()
        c = conn.cursor()
        try:
            c.execute("BEGIN")
            c.execute("DELETE FROM review_chunks WHERE doc_id=?", (doc_id,))
            ids = []
            for ch in chunks:
                c.execute(
                    """INSERT INTO review_chunks (doc_id, chunk_index, section_title, content, char_len)
                       VALUES (?, ?, ?, ?, ?)""",
                    (doc_id, ch.chunk_index, ch.section_title, ch.content, len(ch.content)),
                )
                ids.append(c.lastrowid)
            c.execute(
                """INSERT OR REPLACE INTO review_index_meta (doc_id, md_hash, n_chunks, embedded, indexed_at)
                   VALUES (?, ?, ?, 0, ?)""",
                (doc_id, content_hash, len(chunks), datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            )
            conn.commit()
            return ids
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def save_embeddings(self, chunk_ids: list, vecs: np.ndarray, model: str) -> None:
        conn = self._conn()
        c = conn.cursor()
        for cid, vec in zip(chunk_ids, vecs):
            c.execute(
                "UPDATE review_chunks SET embedding=?, embedding_model=? WHERE chunk_id=?",
                (vec.astype(np.float32).tobytes(), model, cid),
            )
        conn.commit()
        conn.close()
        self._matrix = None  # 失效缓存

    def mark_embedded(self, doc_id: str) -> None:
        conn = self._conn()
        conn.execute("UPDATE review_index_meta SET embedded=1 WHERE doc_id=?", (doc_id,))
        conn.commit()
        conn.close()

    def doc_index_state(self, doc_id: str):
        """返回 (md_hash, n_chunks, embedded)；从未索引过则返回 None。

        供夜间断点续跑判断：md_hash 未变说明分块结果仍然有效，可跳过
        replace_doc_chunks（它会 DELETE 掉上一晚已算好的向量）。
        """
        conn = self._conn()
        row = conn.execute(
            "SELECT md_hash, n_chunks, embedded FROM review_index_meta WHERE doc_id=?",
            (doc_id,),
        ).fetchone()
        conn.close()
        return row

    def chunks_needing_embedding(self, doc_id: str) -> list:
        """返回该文档尚未嵌入的 [(chunk_id, content), ...]，按 chunk_index 排序。"""
        conn = self._conn()
        rows = conn.execute(
            """SELECT chunk_id, content FROM review_chunks
               WHERE doc_id=? AND embedding IS NULL ORDER BY chunk_index""",
            (doc_id,),
        ).fetchall()
        conn.close()
        return rows

    # ── 检索 ──────────────────────────────────────────────────────────────

    def _embed_fingerprint(self) -> tuple:
        """"已嵌入内容"的轻量指纹：(已嵌入文档数, 最新一次索引时间)。

        嵌入现在由**另一个进程**（夜间的 nightly_index.py）写入，本进程的
        `save_embeddings` 失效缓存管不着它。MCP server 是长驻进程，一旦缓存过矩阵，
        夜里新嵌的书就永远检索不到，得手动重启——而 library_status 又照常报出新数目，
        表现为"统计说有、检索说没有"的静默分裂。

        指纹只查 review_index_meta（一篇一行，几百行）。不查
        `COUNT(*) FROM review_chunks WHERE embedding IS NOT NULL`——那是几十万行、
        每行 16KB BLOB 的表，每次检索都全表扫一遍代价太大。
        """
        conn = self._conn()
        row = conn.execute(
            "SELECT COUNT(*), COALESCE(MAX(indexed_at), '') FROM review_index_meta "
            "WHERE embedded=1").fetchone()
        conn.close()
        return row

    def load_matrix(self, force_reload: bool = False):
        fingerprint = self._embed_fingerprint()
        if self._matrix is not None and not force_reload and fingerprint == self._fingerprint:
            return self._matrix, self._meta
        self._fingerprint = fingerprint
        conn = self._conn()
        c = conn.cursor()
        where_sql = """FROM review_chunks c
                     LEFT JOIN paper_details d ON c.doc_id = d.doc_id
                     WHERE c.embedding IS NOT NULL
                       AND COALESCE(d.quarantine_status, 'NORMAL') = 'NORMAL'"""
        row_count = c.execute(f"SELECT COUNT(*) {where_sql}").fetchone()[0]
        c.execute(f"""SELECT c.chunk_id, c.doc_id, c.section_title, c.content, c.embedding
                     {where_sql}""")
        if not row_count:
            conn.close()
            self._matrix = np.zeros((0, self.dim), dtype=np.float32)
            self._meta = []
            return self._matrix, self._meta
        matrix = np.empty((row_count, self.dim), dtype=np.float32)
        meta = []
        valid = 0
        for chunk_id, doc_id, section_title, content, blob in c:
            v = np.frombuffer(blob, dtype=np.float32)
            if v.shape[0] != self.dim:
                continue  # 换过嵌入模型后的旧向量，跳过
            matrix[valid] = v
            meta.append({"chunk_id": chunk_id, "doc_id": doc_id,
                         "section_title": section_title, "content": content})
            valid += 1
        conn.close()
        self._matrix = matrix[:valid] if valid else np.zeros((0, self.dim), dtype=np.float32)
        self._meta = meta
        return self._matrix, self._meta

    @staticmethod
    def evidence_cache_key(content: str, query: str, prompt_version: str,
                           model_name: str, schema_version: str) -> tuple[str, str]:
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        normalized_query = " ".join(query.split()).casefold()
        raw = "\x1f".join((content_hash, normalized_query, prompt_version,
                            model_name, schema_version))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest(), content_hash

    def get_evidence_cache(self, chunk_id: int, content: str, query: str,
                           prompt_version: str, model_name: str,
                           schema_version: str) -> dict | None:
        key, _ = self.evidence_cache_key(content, query, prompt_version,
                                         model_name, schema_version)
        conn = self._conn()
        row = conn.execute("""SELECT score, summary FROM review_evidence_cache
                              WHERE cache_key=?""", (key,)).fetchone()
        conn.close()
        if not row or row[0] is None or not str(row[1] or "").strip():
            return None
        return {"score": int(row[0]), "summary": str(row[1])}

    def put_evidence_cache(self, chunk_id: int, content: str, query: str,
                           prompt_version: str, model_name: str,
                           schema_version: str, score: int, summary: str) -> None:
        if not str(summary or "").strip():
            return
        key, content_hash = self.evidence_cache_key(
            content, query, prompt_version, model_name, schema_version)
        conn = self._conn()
        conn.execute("""INSERT OR REPLACE INTO review_evidence_cache
            (cache_key, chunk_id, query, score, summary, content_hash,
             prompt_version, model_name, schema_version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (key, chunk_id, query, int(score), summary, content_hash,
             prompt_version, model_name, schema_version))
        conn.commit()
        conn.close()

    def _doc_type_map(self) -> dict:
        """doc_id → doc_type（缺省 'paper'），供来源过滤。"""
        conn = self._conn()
        rows = conn.execute("SELECT doc_id, COALESCE(doc_type,'paper') FROM paper_details").fetchall()
        conn.close()
        return {d: t for d, t in rows}

    def search(self, qvec: np.ndarray, top_k: int = 24, max_per_doc: int = 4,
               doc_types: list = None) -> list:
        matrix, meta = self.load_matrix()
        if matrix.shape[0] == 0:
            return []
        allow = set(doc_types) if doc_types else None
        dtm = self._doc_type_map() if allow is not None else {}
        scores = matrix @ qvec.astype(np.float32)
        order = np.argsort(-scores)
        results, per_doc = [], {}
        for idx in order:
            m = meta[idx]
            if allow is not None and dtm.get(m["doc_id"], "paper") not in allow:
                continue
            if per_doc.get(m["doc_id"], 0) >= max_per_doc:
                continue
            per_doc[m["doc_id"]] = per_doc.get(m["doc_id"], 0) + 1
            results.append(ScoredChunk(
                chunk_id=m["chunk_id"], doc_id=m["doc_id"],
                section_title=m["section_title"], content=m["content"],
                score=float(scores[idx]),
            ))
            if len(results) >= top_k:
                break
        return results

    def doc_mean_vectors(self) -> dict:
        """doc_id → 该文档全部 chunk 向量均值（再归一化），用于大纲阶段主题预筛。"""
        matrix, meta = self.load_matrix()
        by_doc = {}
        for i, m in enumerate(meta):
            by_doc.setdefault(m["doc_id"], []).append(i)
        out = {}
        for doc_id, idxs in by_doc.items():
            v = matrix[idxs].mean(axis=0)
            n = np.linalg.norm(v)
            out[doc_id] = v / n if n > 0 else v
        return out

    # ── 论文元数据 ────────────────────────────────────────────────────────

    def get_paper_meta(self, doc_ids: list = None) -> dict:
        """doc_id → 元数据 dict。paper_details 的结构化字段（enrich 产出）优先于 result_json。"""
        conn = self._conn()
        c = conn.cursor()
        base_sql = """SELECT p.id, p.title, p.language, p.result_json,
                             d.title_zh, d.title_en, d.doi, d.authors, d.journal, d.year, d.keywords,
                             d.doc_type, d.publisher, d.pub_place, d.edition,
                             d.patent_no, d.filing_date, d.legal_status, d.status_checked_at,
                             d.doc_no, d.url, d.effective_date, d.cite_key
                      FROM papers p LEFT JOIN paper_details d ON d.doc_id = p.id"""
        if doc_ids:
            marks = ",".join("?" * len(doc_ids))
            c.execute(f"{base_sql} WHERE p.id IN ({marks})", doc_ids)
        else:
            c.execute(f"{base_sql} WHERE p.status='EXPORTED'")
        rows = c.fetchall()
        conn.close()
        out = {}
        for (doc_id, title, language, result_json,
             d_title_zh, d_title_en, d_doi, d_authors, d_journal, d_year, d_keywords,
             d_doc_type, d_publisher, d_pub_place, d_edition,
             d_patent_no, d_filing_date, d_legal_status, d_status_checked,
             d_doc_no, d_url, d_effective, d_cite_key) in rows:
            meta = {"title": title or "", "language": language or "zh",
                    "authors": "", "journal": "", "year": "", "tldr": "",
                    "title_zh": d_title_zh or "", "title_en": d_title_en or "",
                    "doi": d_doi or "", "keywords": d_keywords or "",
                    "doc_type": d_doc_type or "paper", "publisher": d_publisher or "",
                    "pub_place": d_pub_place or "", "edition": d_edition or "",
                    "patent_no": d_patent_no or "", "filing_date": d_filing_date or "",
                    "legal_status": d_legal_status or "",
                    "status_checked_at": d_status_checked or "",
                    "doc_no": d_doc_no or "", "url": d_url or "",
                    "effective_date": d_effective or "",
                    "cite_key": d_cite_key or ""}
            if result_json:
                try:
                    analysis = json.loads(result_json)
                    for k in ("authors", "journal", "year", "tldr"):
                        meta[k] = str(analysis.get(k, "") or "")
                except Exception:
                    pass
            # 结构化字段优先覆盖（非空才覆盖）
            for k, v in (("authors", d_authors), ("journal", d_journal), ("year", d_year)):
                if v:
                    meta[k] = str(v)
            out[doc_id] = meta
        return out

    # ── 规范性文件（专利 / 标准）──────────────────────────────────────────
    # 这两类共用一套读写：都有"印在封面上的编号"和"不印在封面上、会随时间变化、
    # 只能人工核实的状态"，机制完全同构，没必要写两遍。

    # mark_doc_type 允许写入的列。白名单是必需的：列名要直接拼进 SQL，
    # 不设限就等于把 UPDATE 的列名开放给调用方拼字符串。
    _MARKABLE_COLS = ("patent_no", "doc_no", "filing_date", "effective_date",
                      "authors", "publisher", "pub_place", "year",
                      "title", "title_zh", "title_en",
                      "keywords", "journal", "isbn", "edition", "url")

    def list_docs(self, doc_type: str) -> list:
        """返回某一类型的全部著录项目，按编号/日期倒序。

        只回事实字段 + 人工写入的状态；出版阶段、保护期届满日这类**推导值一律不落库**，
        由 litreview.patent 在读取时现算——落库就会随时间过期，而且没人知道它过期了。
        """
        conn = self._conn()
        rows = conn.execute(
            """SELECT d.doc_id, COALESCE(NULLIF(d.title_zh,''), NULLIF(d.title,''), p.title),
                      COALESCE(d.patent_no,''), COALESCE(d.doc_no,''),
                      COALESCE(d.filing_date,''), COALESCE(d.authors,''),
                      COALESCE(d.publisher,''), COALESCE(d.year,''),
                      COALESCE(d.legal_status,''), COALESCE(d.status_checked_at,''),
                      COALESCE(d.url,''), COALESCE(d.effective_date,'')
               FROM paper_details d LEFT JOIN papers p ON p.id = d.doc_id
               WHERE d.doc_type = ?
               ORDER BY COALESCE(NULLIF(d.filing_date,''), d.year, '') DESC""",
            (doc_type,)).fetchall()
        conn.close()
        keys = ("doc_id", "title", "patent_no", "doc_no", "filing_date", "inventors",
                "assignee", "year", "legal_status", "status_checked_at", "url",
                "effective_date")
        return [dict(zip(keys, r)) for r in rows]

    def resolve_docs(self, ident: str, doc_type: str = "") -> list:
        """把用户随手给的一个标识符解析成 doc_id 列表。

        依次尝试：doc_id 前缀 → 专利号 → 标准号（忽略空格差异）→ 标题片段。
        命中多篇就全部返回，由调用方决定是报错还是批量处理——这里不替它做主。
        """
        key = (ident or "").strip()
        if not key:
            return []
        flat = re.sub(r"[\s/]", "", key).upper()
        cond = "(UPPER(p.id) LIKE ? OR UPPER(COALESCE(d.patent_no,'')) = ? " \
               "OR REPLACE(REPLACE(UPPER(COALESCE(d.doc_no,'')),' ',''),'/','') = ? " \
               "OR COALESCE(d.title_zh,'') LIKE ? OR COALESCE(p.title,'') LIKE ?)"
        args = [key.upper() + "%", key.upper(), flat, f"%{key}%", f"%{key}%"]
        if doc_type:
            cond += " AND COALESCE(d.doc_type, 'paper') = ?"
            args.append(doc_type)
        conn = self._conn()
        rows = conn.execute(
            f"""SELECT p.id FROM papers p
                LEFT JOIN paper_details d ON d.doc_id = p.id WHERE {cond}""", args).fetchall()
        conn.close()
        return [r[0] for r in rows]

    def set_doc_status(self, ident: str, status: str, checked_at: str,
                       doc_type: str = "") -> list:
        """人工写入状态（专利的法律状态 / 标准的现行与否）。

        必须同时写核实日期：没有日期的状态过两年就无从判断还准不准，等于假数据。
        返回被更新的 doc_id 列表；空列表表示没匹配上。
        """
        hit = self.resolve_docs(ident, doc_type)
        if not hit:
            return []
        conn = self._conn()
        marks = ",".join("?" * len(hit))
        conn.execute(f"""UPDATE paper_details SET legal_status=?, status_checked_at=?
                         WHERE doc_id IN ({marks})""", [status, checked_at] + hit)
        conn.commit()
        conn.close()
        return hit

    def mark_doc_type(self, doc_id: str, doc_type: str, fields: dict = None) -> None:
        """把一篇文献归到指定类型，并写入随类型而来的著录项目。

        只动 doc_type 和 fields 里给出的列，其余字段（图表、参考文献、DOI…）原样不动，
        所以重扫存量文献时既不用重跑 LLM、也不用重解析 content_list，可以反复执行。
        每一列都走 COALESCE(NULLIF(?,''), 列)：**新值非空才覆盖，空值一律沿用旧值**。
        这条规则是防呆用的——某次识别退化没抽出编号时，不会把上次抽对的编号冲成空。
        """
        cols = {k: str(v or "") for k, v in (fields or {}).items()
                if k in self._MARKABLE_COLS}
        conn = self._conn()
        c = conn.cursor()
        c.execute("SELECT 1 FROM paper_details WHERE doc_id=?", (doc_id,))
        if c.fetchone():
            sets = ["doc_type=?"] + [f"{k}=COALESCE(NULLIF(?,''), {k})" for k in cols]
            c.execute(f"UPDATE paper_details SET {', '.join(sets)} WHERE doc_id=?",
                      [doc_type] + list(cols.values()) + [doc_id])
        else:
            names = ["doc_id", "doc_type"] + list(cols)
            c.execute(f"INSERT INTO paper_details ({', '.join(names)}) "
                      f"VALUES ({','.join('?' * len(names))})",
                      [doc_id, doc_type] + list(cols.values()))
        conn.commit()
        conn.close()

    def doc_type_counts(self) -> dict:
        """doc_type → 已入库篇数（只统计 EXPORTED）。"""
        conn = self._conn()
        rows = conn.execute(
            """SELECT COALESCE(d.doc_type,'paper'), COUNT(*)
               FROM papers p LEFT JOIN paper_details d ON d.doc_id = p.id
               WHERE p.status='EXPORTED' GROUP BY 1""").fetchall()
        conn.close()
        return {r[0]: r[1] for r in rows}

    # ── enrich 管道读写 ───────────────────────────────────────────────────

    def docs_needing_enrich(self, force: bool = False) -> list:
        """返回 [(doc_id, title, images_dir, mineru_md头部)] 待提取元数据的 EXPORTED 文档。"""
        conn = self._conn()
        c = conn.cursor()
        if force:
            c.execute("""SELECT p.id, p.title, p.images_dir, substr(p.mineru_md, 1, 3000)
                         FROM papers p WHERE p.status='EXPORTED'""")
        else:
            c.execute("""SELECT p.id, p.title, p.images_dir, substr(p.mineru_md, 1, 3000)
                         FROM papers p LEFT JOIN paper_details d ON d.doc_id = p.id
                         WHERE p.status='EXPORTED' AND d.enriched_at IS NULL""")
        rows = c.fetchall()
        conn.close()
        return rows

    def get_assets_for_docs(self, doc_ids: list, captioned_only: bool = True) -> list:
        """返回指定文献的图表资产 [{doc_id, asset_type, img_path, caption, page_idx}]，供综述插图选图。"""
        if not doc_ids:
            return []
        conn = self._conn()
        c = conn.cursor()
        placeholders = ",".join("?" * len(doc_ids))
        sql = f"""SELECT doc_id, asset_type, img_path, caption, page_idx
                  FROM paper_assets WHERE doc_id IN ({placeholders})"""
        if captioned_only:
            sql += " AND caption != ''"
        c.execute(sql, list(doc_ids))
        rows = [{"doc_id": r[0], "asset_type": r[1], "img_path": r[2],
                 "caption": r[3], "page_idx": r[4]} for r in c.fetchall()]
        conn.close()
        return rows

    def save_enrichment(self, doc_id: str, details: dict, assets: list, refs: list) -> None:
        """单文档单事务写入 paper_details / paper_assets / paper_references。"""
        conn = self._conn()
        c = conn.cursor()
        try:
            c.execute("BEGIN")
            # INSERT OR REPLACE 会整行重写，若不先取出人工填过的那几列，
            # 一次 enrich --force 就把它们冲成空。这几列除非本次显式带值，否则一律沿用旧值：
            # 状态与核实日期是人工核实结果，doc_no/url 是重扫或人工 set-type 写进去的。
            _CARRY = ("legal_status", "status_checked_at", "doc_no", "url", "effective_date")
            _old = c.execute(
                f"SELECT {', '.join(_CARRY)} FROM paper_details WHERE doc_id=?",
                (doc_id,)).fetchone() or ("",) * len(_CARRY)
            _kept = {k: (details.get(k) or _old[i] or "") for i, k in enumerate(_CARRY)}
            c.execute("""INSERT OR REPLACE INTO paper_details
                         (doc_id, title, title_zh, title_en, doi, authors, journal, year,
                          keywords, n_figures, n_tables, n_refs, enriched_at, source,
                          doc_type, publisher, pub_place, edition, isbn,
                          patent_no, filing_date, legal_status, status_checked_at,
                          doc_no, url, effective_date)
                         VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (doc_id, details.get("title", ""), details.get("title_zh", ""),
                       details.get("title_en", ""), details.get("doi", ""),
                       details.get("authors", ""), details.get("journal", ""),
                       details.get("year", ""), details.get("keywords", ""),
                       details.get("n_figures", 0), details.get("n_tables", 0), len(refs),
                       datetime.now().strftime("%Y-%m-%d %H:%M:%S"), details.get("source", "local"),
                       details.get("doc_type", "paper"), details.get("publisher", ""),
                       details.get("pub_place", ""), details.get("edition", ""),
                       details.get("isbn", ""),
                       details.get("patent_no", ""), details.get("filing_date", ""),
                       _kept["legal_status"], _kept["status_checked_at"],
                       _kept["doc_no"], _kept["url"], _kept["effective_date"]))
            c.execute("DELETE FROM paper_assets WHERE doc_id=?", (doc_id,))
            for a in assets:
                c.execute("""INSERT INTO paper_assets (doc_id, asset_type, img_path, caption, page_idx)
                             VALUES (?,?,?,?,?)""",
                          (doc_id, a["asset_type"], a.get("img_path", ""),
                           a.get("caption", ""), a.get("page_idx")))
            c.execute("DELETE FROM paper_references WHERE doc_id=?", (doc_id,))
            for i, r in enumerate(refs, 1):
                c.execute("INSERT INTO paper_references (doc_id, ref_index, raw_text) VALUES (?,?,?)",
                          (doc_id, i, r))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def stats(self) -> dict:
        conn = self._conn()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM papers WHERE status='EXPORTED'")
        exported = c.fetchone()[0]
        c.execute("SELECT COUNT(DISTINCT doc_id) FROM review_chunks WHERE embedding IS NOT NULL")
        indexed = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM review_chunks")
        chunks = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM review_chunks WHERE embedding IS NOT NULL")
        embedded_chunks = c.fetchone()[0]
        conn.close()
        return {"exported_docs": exported, "indexed_docs": indexed,
                "pending_docs": len(self.docs_needing_index()),
                "chunks": chunks, "embedded_chunks": embedded_chunks}

    def clean_orphans(self) -> dict:
        """清理无对应 review_chunks 的孤儿 review_index_meta 记录。"""
        conn = self._conn()
        c = conn.cursor()
        c.execute("""
            DELETE FROM review_index_meta
            WHERE doc_id NOT IN (SELECT DISTINCT doc_id FROM review_chunks WHERE embedding IS NOT NULL)
        """)
        removed = c.rowcount
        conn.commit()
        conn.close()
        return {"orphans_removed": removed}

    def delete_doc(self, doc_id: str, settings=None, keep_source: bool = True,
                   dry_run: bool = False) -> dict:
        """安全级联删除一篇文献：
        1. 解出 chunk_id 删 review_evidence_cache；
        2. 删 6 张表：review_chunks, review_index_meta, paper_references, paper_assets, paper_details, papers;
        3. 删 3 处物理文件：Obsidian 笔记 (*_<doc_id[:8]>.md), attachments/<doc_id>, mineru_output/book_output 中间产物目录;
        4. 删 Excel 记录行;
        5. (可选) keep_source=False 时同时删除 processed_pdfs 中的源文件。

        返回包含被删除项数量和文件路径的 dict。
        """
        import glob
        import shutil
        from utils import remove_from_excel

        report = {
            "doc_id": doc_id,
            "title": "",
            "chunks_deleted": 0,
            "evidence_cache_deleted": 0,
            "meta_deleted": 0,
            "assets_deleted": 0,
            "refs_deleted": 0,
            "details_deleted": 0,
            "papers_deleted": 0,
            "files_removed": [],
            "excel_rows_removed": 0,
            "dry_run": dry_run,
        }

        conn = self._conn()
        row = conn.execute("SELECT title, pdf_path FROM papers WHERE id=?", (doc_id,)).fetchone()
        title = row[0] if row else ""
        pdf_path = row[1] if row else ""
        report["title"] = title

        chunk_rows = conn.execute("SELECT chunk_id FROM review_chunks WHERE doc_id=?", (doc_id,)).fetchall()
        chunk_ids = [r[0] for r in chunk_rows]
        report["chunks_deleted"] = len(chunk_ids)

        if chunk_ids:
            marks = ",".join("?" * len(chunk_ids))
            ev_count = conn.execute(f"SELECT COUNT(*) FROM review_evidence_cache WHERE chunk_id IN ({marks})", chunk_ids).fetchone()[0]
            report["evidence_cache_deleted"] = ev_count

        meta_count = conn.execute("SELECT COUNT(*) FROM review_index_meta WHERE doc_id=?", (doc_id,)).fetchone()[0]
        report["meta_deleted"] = meta_count
        assets_count = conn.execute("SELECT COUNT(*) FROM paper_assets WHERE doc_id=?", (doc_id,)).fetchone()[0]
        report["assets_deleted"] = assets_count
        refs_count = conn.execute("SELECT COUNT(*) FROM paper_references WHERE doc_id=?", (doc_id,)).fetchone()[0]
        report["refs_deleted"] = refs_count
        details_count = conn.execute("SELECT COUNT(*) FROM paper_details WHERE doc_id=?", (doc_id,)).fetchone()[0]
        report["details_deleted"] = details_count
        report["papers_deleted"] = 1 if row else 0

        files_to_remove = []
        dirs_to_remove = []
        if settings:
            # 1. Obsidian notes & attachments
            vault = getattr(settings, "OBSIDIAN_VAULT_DIR", "")
            if vault and os.path.isdir(vault):
                for f in glob.glob(os.path.join(vault, f"*_{doc_id[:8]}.md")):
                    files_to_remove.append(f)
                att_dir = os.path.join(vault, "attachments", doc_id)
                if os.path.isdir(att_dir):
                    dirs_to_remove.append(att_dir)

            # 2. mineru_output
            mineru_dir = getattr(settings, "MINERU_OUTPUT_DIR", "")
            if mineru_dir and os.path.isdir(mineru_dir):
                for d in glob.glob(os.path.join(mineru_dir, f"*_{doc_id[:8]}*")):
                    if os.path.isdir(d):
                        dirs_to_remove.append(d)

            # 3. book_output
            book_dir = getattr(settings, "BOOK_OUTPUT_DIR", "")
            if book_dir and os.path.isdir(book_dir):
                for d in glob.glob(os.path.join(book_dir, f"*_{doc_id[:8]}*")):
                    if os.path.isdir(d):
                        dirs_to_remove.append(d)

            # 4. 源文件
            if not keep_source and pdf_path and os.path.isfile(pdf_path):
                files_to_remove.append(pdf_path)

        report["files_removed"] = files_to_remove + dirs_to_remove

        if not dry_run:
            if chunk_ids:
                marks = ",".join("?" * len(chunk_ids))
                conn.execute(f"DELETE FROM review_evidence_cache WHERE chunk_id IN ({marks})", chunk_ids)
            conn.execute("DELETE FROM review_chunks WHERE doc_id=?", (doc_id,))
            conn.execute("DELETE FROM review_index_meta WHERE doc_id=?", (doc_id,))
            conn.execute("DELETE FROM paper_references WHERE doc_id=?", (doc_id,))
            conn.execute("DELETE FROM paper_assets WHERE doc_id=?", (doc_id,))
            conn.execute("DELETE FROM paper_details WHERE doc_id=?", (doc_id,))
            conn.execute("DELETE FROM papers WHERE id=?", (doc_id,))
            conn.commit()

            for f in files_to_remove:
                try:
                    if os.path.isfile(f):
                        os.remove(f)
                except Exception:
                    pass
            for d in dirs_to_remove:
                try:
                    if os.path.isdir(d):
                        shutil.rmtree(d, ignore_errors=True)
                except Exception:
                    pass

            if settings and getattr(settings, "EXCEL_OUTPUT_PATH", ""):
                try:
                    report["excel_rows_removed"] = remove_from_excel([doc_id], settings.EXCEL_OUTPUT_PATH)
                except Exception:
                    pass

            # 清除内存矩阵缓存
            self._matrix = None
            self._meta = None
            self._fingerprint = None

        conn.close()
        return report
