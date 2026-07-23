"""VectorStore 单元测试：合成向量，不走网络。"""
import os
import sys
import sqlite3
import tempfile

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from litreview.chunker import Chunk
from litreview.store import VectorStore, md_hash

DIM = 8


def _make_store(tmpdir):
    db_path = os.path.join(tmpdir, "test.db")
    # 建 papers 表模拟主管道
    conn = sqlite3.connect(db_path)
    conn.execute("""CREATE TABLE papers (
        id TEXT PRIMARY KEY, title TEXT, pdf_path TEXT, language TEXT,
        mineru_md TEXT, images_dir TEXT, status TEXT, batch_provider TEXT,
        batch_job_id TEXT, result_json TEXT, error_message TEXT, processed_at TEXT)""")
    conn.commit()
    conn.close()
    return VectorStore(db_path, DIM), db_path


def _unit(v):
    v = np.array(v, dtype=np.float32)
    return v / np.linalg.norm(v)


def test_schema_idempotent(tmp_path=None):
    with tempfile.TemporaryDirectory() as td:
        store, _ = _make_store(td)
        store.ensure_schema()  # 二次调用不报错


def test_chunk_roundtrip_and_search():
    with tempfile.TemporaryDirectory() as td:
        store, db_path = _make_store(td)
        # 三个文档，向量方向可控
        docs = {
            "a" * 64: _unit([1, 0, 0, 0, 0, 0, 0, 0]),
            "b" * 64: _unit([0, 1, 0, 0, 0, 0, 0, 0]),
            "c" * 64: _unit([0.9, 0.1, 0, 0, 0, 0, 0, 0]),
        }
        for doc_id, vec in docs.items():
            chunks = [Chunk(doc_id=doc_id, chunk_index=0, section_title="s", content="x" * 100)]
            ids = store.replace_doc_chunks(doc_id, "h", chunks)
            store.save_embeddings(ids, vec.reshape(1, -1), "test-model")
            store.mark_embedded(doc_id)

        q = _unit([1, 0, 0, 0, 0, 0, 0, 0])
        results = store.search(q, top_k=3, max_per_doc=4)
        assert len(results) == 3
        # 排序：a (cos=1) > c (cos≈0.994) > b (cos=0)
        assert results[0].doc_id == "a" * 64
        assert results[1].doc_id == "c" * 64
        assert results[0].score > results[1].score > results[2].score


def test_per_doc_cap():
    with tempfile.TemporaryDirectory() as td:
        store, _ = _make_store(td)
        doc_id = "a" * 64
        chunks = [Chunk(doc_id=doc_id, chunk_index=i, section_title="s", content=f"content-{i}" * 20)
                  for i in range(6)]
        ids = store.replace_doc_chunks(doc_id, "h", chunks)
        vecs = np.tile(_unit([1, 0, 0, 0, 0, 0, 0, 0]), (6, 1))
        store.save_embeddings(ids, vecs, "test-model")
        results = store.search(_unit([1, 0, 0, 0, 0, 0, 0, 0]), top_k=10, max_per_doc=2)
        assert len(results) == 2  # 单文档上限生效


def test_replace_doc_chunks_replaces():
    with tempfile.TemporaryDirectory() as td:
        store, db_path = _make_store(td)
        doc_id = "a" * 64
        store.replace_doc_chunks(doc_id, "h1",
                                 [Chunk(doc_id=doc_id, chunk_index=i, section_title="", content="y" * 60)
                                  for i in range(5)])
        store.replace_doc_chunks(doc_id, "h2",
                                 [Chunk(doc_id=doc_id, chunk_index=0, section_title="", content="z" * 60)])
        conn = sqlite3.connect(db_path)
        n = conn.execute("SELECT COUNT(*) FROM review_chunks WHERE doc_id=?", (doc_id,)).fetchone()[0]
        h = conn.execute("SELECT md_hash FROM review_index_meta WHERE doc_id=?", (doc_id,)).fetchone()[0]
        conn.close()
        assert n == 1
        assert h == "h2"


def test_docs_needing_index_diff():
    with tempfile.TemporaryDirectory() as td:
        store, db_path = _make_store(td)
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO papers (id, title, mineru_md, status) VALUES (?,?,?,?)",
                     ("a" * 64, "t1", "全文内容一", "EXPORTED"))
        conn.execute("INSERT INTO papers (id, title, mineru_md, status) VALUES (?,?,?,?)",
                     ("b" * 64, "t2", "全文内容二", "EXPORTED"))
        conn.execute("INSERT INTO papers (id, title, mineru_md, status) VALUES (?,?,?,?)",
                     ("c" * 64, "t3", "未导出", "BATCH_SUBMITTED"))
        conn.commit()
        conn.close()

        todo = store.docs_needing_index()
        assert {d for d, _, _ in todo} == {"a" * 64, "b" * 64}  # 未索引的 EXPORTED

        # 索引 a 之后，只剩 b
        md = {d: m for d, m, _ in todo}["a" * 64]
        ids = store.replace_doc_chunks("a" * 64, md_hash(md),
                                       [Chunk(doc_id="a" * 64, chunk_index=0, section_title="", content="x" * 60)])
        store.save_embeddings(ids, np.zeros((1, DIM), dtype=np.float32), "m")
        store.mark_embedded("a" * 64)
        todo2 = store.docs_needing_index()
        assert {d for d, _, _ in todo2} == {"b" * 64}

        # 修改 a 的内容 → 重新出现在待索引
        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE papers SET mineru_md=? WHERE id=?", ("改过的内容", "a" * 64))
        conn.commit()
        conn.close()
        todo3 = store.docs_needing_index()
        assert "a" * 64 in {d for d, _, _ in todo3}


def test_dim_mismatch_skipped():
    """换嵌入模型后旧维度向量应被跳过而不是崩溃。"""
    with tempfile.TemporaryDirectory() as td:
        store, db_path = _make_store(td)
        doc_id = "a" * 64
        ids = store.replace_doc_chunks(doc_id, "h",
                                       [Chunk(doc_id=doc_id, chunk_index=0, section_title="", content="x" * 60)])
        wrong = np.zeros((1, DIM + 4), dtype=np.float32)
        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE review_chunks SET embedding=?, embedding_model='old' WHERE chunk_id=?",
                     (wrong[0].tobytes(), ids[0]))
        conn.commit()
        conn.close()
        matrix, meta = store.load_matrix(force_reload=True)
        assert matrix.shape[0] == 0
