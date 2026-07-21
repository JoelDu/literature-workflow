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
        conn.commit()
        conn.close()

    # ── 索引写入 ──────────────────────────────────────────────────────────

    def docs_needing_index(self) -> list:
        """返回 [(doc_id, mineru_md)]：未索引 / 内容变化 / 上次嵌入未完成的 EXPORTED 文档。"""
        conn = self._conn()
        c = conn.cursor()
        c.execute("""
            SELECT p.id, p.mineru_md, m.md_hash, m.embedded
            FROM papers p LEFT JOIN review_index_meta m ON m.doc_id = p.id
            WHERE p.status = 'EXPORTED' AND p.mineru_md IS NOT NULL AND p.mineru_md != ''
        """)
        rows = c.fetchall()
        conn.close()
        out = []
        for doc_id, md, old_hash, embedded in rows:
            if old_hash is None or old_hash != md_hash(md) or not embedded:
                out.append((doc_id, md))
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

    # ── 检索 ──────────────────────────────────────────────────────────────

    def load_matrix(self, force_reload: bool = False):
        if self._matrix is not None and not force_reload:
            return self._matrix, self._meta
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

    def search(self, qvec: np.ndarray, top_k: int = 24, max_per_doc: int = 4) -> list:
        matrix, meta = self.load_matrix()
        if matrix.shape[0] == 0:
            return []
        scores = matrix @ qvec.astype(np.float32)
        order = np.argsort(-scores)
        results, per_doc = [], {}
        for idx in order:
            m = meta[idx]
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
                             d.title_zh, d.title_en, d.doi, d.authors, d.journal, d.year, d.keywords
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
             d_title_zh, d_title_en, d_doi, d_authors, d_journal, d_year, d_keywords) in rows:
            meta = {"title": title or "", "language": language or "zh",
                    "authors": "", "journal": "", "year": "", "tldr": "",
                    "title_zh": d_title_zh or "", "title_en": d_title_en or "",
                    "doi": d_doi or "", "keywords": d_keywords or ""}
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

    def save_enrichment(self, doc_id: str, details: dict, assets: list, refs: list) -> None:
        """单文档单事务写入 paper_details / paper_assets / paper_references。"""
        conn = self._conn()
        c = conn.cursor()
        try:
            c.execute("BEGIN")
            c.execute("""INSERT OR REPLACE INTO paper_details
                         (doc_id, title, title_zh, title_en, doi, authors, journal, year,
                          keywords, n_figures, n_tables, n_refs, enriched_at, source)
                         VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (doc_id, details.get("title", ""), details.get("title_zh", ""),
                       details.get("title_en", ""), details.get("doi", ""),
                       details.get("authors", ""), details.get("journal", ""),
                       details.get("year", ""), details.get("keywords", ""),
                       details.get("n_figures", 0), details.get("n_tables", 0), len(refs),
                       datetime.now().strftime("%Y-%m-%d %H:%M:%S"), details.get("source", "local")))
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
