"""VectorStore：SQLite BLOB 向量存储 + numpy 内存余弦检索。

与主管道共用 data/batch_tracking.db，表名 review_ 前缀隔离。
"""
import os
import json
import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime

import numpy as np


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
    def __init__(self, db_path: str, dim: int):
        self.db_path = db_path
        self.dim = dim
        self._matrix = None       # np.ndarray (n, dim)
        self._meta = None         # list[dict] 与矩阵行对应
        self._fingerprint = None  # 建矩阵那一刻的库指纹，用于发现别的进程写入的新向量
        self.ensure_schema()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA busy_timeout=30000")  # 与守护进程并发写时等待而非报错
        return conn

    def ensure_schema(self) -> None:
        conn = self._conn()
        c = conn.cursor()
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
        # 书籍/教材、专利需要的字段：增量热升级（paper_details 已存在于老库时补列）。
        # 专利四列全部可空、默认 NULL，对已有的论文/教材行零影响。前两列是印在 PDF 上的事实，
        # 后两列只由人工（review.py patents --set）写入——法律状态不在 PDF 里且会随时间变化，
        # 详见 litreview/patent.py 模块头部说明。
        c.execute("PRAGMA table_info(paper_details)")
        _pd_cols = {row[1] for row in c.fetchall()}
        for _col, _decl in (("doc_type", "TEXT DEFAULT 'paper'"), ("publisher", "TEXT"),
                            ("pub_place", "TEXT"), ("edition", "TEXT"), ("isbn", "TEXT"),
                            ("patent_no", "TEXT"), ("filing_date", "TEXT"),
                            ("legal_status", "TEXT"), ("status_checked_at", "TEXT")):
            if _col not in _pd_cols:
                c.execute(f"ALTER TABLE paper_details ADD COLUMN {_col} {_decl}")
        conn.commit()
        conn.close()

    # ── 索引写入 ──────────────────────────────────────────────────────────

    def docs_needing_index(self) -> list:
        """返回 [(doc_id, mineru_md, doc_type)]：未索引 / 内容变化 / 上次嵌入未完成的 EXPORTED 文档。
        doc_type 供索引时按类型选块大小（书籍块更大）。"""
        conn = self._conn()
        c = conn.cursor()
        c.execute("""
            SELECT p.id, p.mineru_md, m.md_hash, m.embedded, COALESCE(d.doc_type, 'paper')
            FROM papers p
            LEFT JOIN review_index_meta m ON m.doc_id = p.id
            LEFT JOIN paper_details d ON d.doc_id = p.id
            WHERE p.status = 'EXPORTED' AND p.mineru_md IS NOT NULL AND p.mineru_md != ''
        """)
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
        c.execute("""SELECT chunk_id, doc_id, section_title, content, embedding
                     FROM review_chunks WHERE embedding IS NOT NULL""")
        rows = c.fetchall()
        conn.close()
        if not rows:
            self._matrix = np.zeros((0, self.dim), dtype=np.float32)
            self._meta = []
            return self._matrix, self._meta
        vecs, meta = [], []
        for chunk_id, doc_id, section_title, content, blob in rows:
            v = np.frombuffer(blob, dtype=np.float32)
            if v.shape[0] != self.dim:
                continue  # 换过嵌入模型后的旧向量，跳过
            vecs.append(v)
            meta.append({"chunk_id": chunk_id, "doc_id": doc_id,
                         "section_title": section_title, "content": content})
        self._matrix = np.vstack(vecs) if vecs else np.zeros((0, self.dim), dtype=np.float32)
        self._meta = meta
        return self._matrix, self._meta

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
                             d.patent_no, d.filing_date, d.legal_status, d.status_checked_at
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
             d_patent_no, d_filing_date, d_legal_status, d_status_checked) in rows:
            meta = {"title": title or "", "language": language or "zh",
                    "authors": "", "journal": "", "year": "", "tldr": "",
                    "title_zh": d_title_zh or "", "title_en": d_title_en or "",
                    "doi": d_doi or "", "keywords": d_keywords or "",
                    "doc_type": d_doc_type or "paper", "publisher": d_publisher or "",
                    "pub_place": d_pub_place or "", "edition": d_edition or "",
                    "patent_no": d_patent_no or "", "filing_date": d_filing_date or "",
                    "legal_status": d_legal_status or "",
                    "status_checked_at": d_status_checked or ""}
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

    # ── 专利 ──────────────────────────────────────────────────────────────

    def list_patents(self) -> list:
        """返回全部 doc_type='patent' 的著录项目，按申请日倒序。
        只回事实字段 + 人工写入的法律状态；出版阶段/到期日一律由 litreview.patent
        在读取时现算，不落库——落库就会随时间过期且没人知道它过期了。"""
        conn = self._conn()
        rows = conn.execute(
            """SELECT d.doc_id, COALESCE(d.title_zh, d.title, p.title), d.patent_no,
                      d.filing_date, d.authors, d.publisher,
                      d.legal_status, d.status_checked_at
               FROM paper_details d LEFT JOIN papers p ON p.id = d.doc_id
               WHERE d.doc_type = 'patent'
               ORDER BY COALESCE(d.filing_date, '') DESC""").fetchall()
        conn.close()
        keys = ("doc_id", "title", "patent_no", "filing_date", "inventors",
                "assignee", "legal_status", "status_checked_at")
        return [dict(zip(keys, r)) for r in rows]

    def set_patent_status(self, ident: str, status: str, checked_at: str) -> list:
        """人工写入法律状态。ident 可以是专利号（CN110346043A）或 doc_id 前缀。
        返回被更新的 doc_id 列表；空列表表示没匹配上。"""
        conn = self._conn()
        c = conn.cursor()
        key = (ident or "").strip().upper()
        rows = c.execute(
            """SELECT doc_id FROM paper_details
               WHERE doc_type='patent'
                 AND (UPPER(COALESCE(patent_no,'')) = ? OR UPPER(doc_id) LIKE ?)""",
            (key, key + "%")).fetchall()
        hit = [r[0] for r in rows]
        if hit:
            marks = ",".join("?" * len(hit))
            c.execute(f"""UPDATE paper_details SET legal_status=?, status_checked_at=?
                          WHERE doc_id IN ({marks})""", [status, checked_at] + hit)
            conn.commit()
        conn.close()
        return hit

    def mark_as_patent(self, doc_id: str, pat: dict) -> None:
        """只改专利相关列 + doc_type，其余字段（标题/图表/参考文献）原样不动。
        供重扫存量文献用：不重跑 LLM、不重解析 content_list，因此可以随便重复执行。"""
        conn = self._conn()
        c = conn.cursor()
        c.execute("SELECT 1 FROM paper_details WHERE doc_id=?", (doc_id,))
        if c.fetchone():
            c.execute("""UPDATE paper_details
                         SET doc_type='patent', patent_no=?, filing_date=?,
                             authors=COALESCE(NULLIF(?,''), authors),
                             publisher=COALESCE(NULLIF(?,''), publisher),
                             year=COALESCE(NULLIF(?,''), year)
                         WHERE doc_id=?""",
                      (pat.get("patent_no", ""), pat.get("filing_date", ""),
                       pat.get("inventors", ""), pat.get("assignee", ""),
                       (pat.get("filing_date", "") or "")[:4], doc_id))
        else:
            c.execute("""INSERT INTO paper_details
                         (doc_id, doc_type, patent_no, filing_date, authors, publisher, year)
                         VALUES (?,'patent',?,?,?,?,?)""",
                      (doc_id, pat.get("patent_no", ""), pat.get("filing_date", ""),
                       pat.get("inventors", ""), pat.get("assignee", ""),
                       (pat.get("filing_date", "") or "")[:4]))
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
            # INSERT OR REPLACE 会整行重写，若不先取出人工核实过的法律状态，
            # 一次 enrich --force 就把它冲成空。这两列除非本次显式带值，否则一律沿用旧值。
            _old = c.execute(
                "SELECT legal_status, status_checked_at FROM paper_details WHERE doc_id=?",
                (doc_id,)).fetchone() or ("", "")
            _legal = details.get("legal_status") or _old[0] or ""
            _checked = details.get("status_checked_at") or _old[1] or ""
            c.execute("""INSERT OR REPLACE INTO paper_details
                         (doc_id, title, title_zh, title_en, doi, authors, journal, year,
                          keywords, n_figures, n_tables, n_refs, enriched_at, source,
                          doc_type, publisher, pub_place, edition, isbn,
                          patent_no, filing_date, legal_status, status_checked_at)
                         VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
                       _legal, _checked))
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
        c.execute("SELECT COUNT(*) FROM review_index_meta WHERE embedded=1")
        indexed = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM review_chunks")
        chunks = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM review_chunks WHERE embedding IS NOT NULL")
        embedded_chunks = c.fetchone()[0]
        conn.close()
        return {"exported_docs": exported, "indexed_docs": indexed,
                "pending_docs": len(self.docs_needing_index()),
                "chunks": chunks, "embedded_chunks": embedded_chunks}
