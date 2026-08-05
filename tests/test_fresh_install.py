"""全新安装（空库）路径的回归测试。

背景：papers 表原本只在 `batch_pipeline.init_db()` 里建。刚 clone 下来、主管道
一次都没跑过的机器上，`review.py types|status|patents|standards` 和 MCP 的
`library_status` 会直接抛 `sqlite3.OperationalError: no such table: papers`。

而 tests/test_store.py 的 `_make_store()` 自己手工建了 papers（注释写着"模拟主管道"），
于是整套测试都跑在"表已存在"的前提上，结构上永远碰不到这条路径 —— 126 个用例全绿，
真装一台新机器却第一条命令就崩。

所以这里的每个用例都**刻意不预建任何表**，只给一个空目录。
"""
import os
import sys
import sqlite3
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from litreview.store import VectorStore
from db_schema import ensure_papers_table, PAPERS_DDL

DIM = 8


def _blank_db(td):
    """一个从未被任何代码碰过的库路径 —— 文件都还不存在。"""
    return os.path.join(td, "brand_new.db")


def test_vectorstore_on_blank_db_creates_papers():
    """VectorStore 初始化后 papers 必须存在：它几乎每个查询都要 JOIN 这张表。"""
    with tempfile.TemporaryDirectory() as td:
        db_path = _blank_db(td)
        VectorStore(db_path, DIM)
        conn = sqlite3.connect(db_path)
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        assert "papers" in names, f"空库初始化后仍没有 papers 表，实际只有 {sorted(names)}"


def test_stats_on_blank_db_returns_zeros_not_crash():
    """这就是 MCP library_status 走的那条路 —— 空库要报 0，不是抛异常。"""
    with tempfile.TemporaryDirectory() as td:
        store = VectorStore(_blank_db(td), DIM)
        s = store.stats()
        assert s["exported_docs"] == 0
        assert s["indexed_docs"] == 0
        assert s["chunks"] == 0


def test_doc_type_counts_on_blank_db():
    """review.py types 走的那条路。"""
    with tempfile.TemporaryDirectory() as td:
        store = VectorStore(_blank_db(td), DIM)
        assert store.doc_type_counts() == {} or \
            all(v == 0 for v in store.doc_type_counts().values())


def test_docs_needing_index_on_blank_db():
    """夜间嵌入任务在空库上应当报"无待办"，而不是崩在 JOIN papers 上。"""
    with tempfile.TemporaryDirectory() as td:
        store = VectorStore(_blank_db(td), DIM)
        assert store.docs_needing_index() == []


def test_ensure_papers_table_is_idempotent():
    """重复调用不报错：主管道和 store 都会调它，谁先跑不确定。"""
    with tempfile.TemporaryDirectory() as td:
        conn = sqlite3.connect(_blank_db(td))
        assert ensure_papers_table(conn) == []      # 新表，无需补列
        assert ensure_papers_table(conn) == []      # 二次调用
        conn.commit()
        conn.close()


def test_hot_upgrade_adds_missing_columns_to_old_db():
    """老库（缺 error_message / processed_at）应被热升级补上，且不动已有数据。

    这两列是后加的，线上库里存在只有前十列的老表。CREATE TABLE IF NOT EXISTS
    对已存在的表是空操作，只有 ALTER 补得上 —— 所以 DDL 改列清单是没用的。
    """
    with tempfile.TemporaryDirectory() as td:
        db_path = _blank_db(td)
        conn = sqlite3.connect(db_path)
        conn.execute("""CREATE TABLE papers (
            id TEXT PRIMARY KEY, title TEXT, pdf_path TEXT, language TEXT,
            mineru_md TEXT, images_dir TEXT, status TEXT, batch_provider TEXT,
            batch_job_id TEXT, result_json TEXT)""")
        conn.execute("INSERT INTO papers (id, title) VALUES ('old1', '旧数据')")
        conn.commit()

        added = ensure_papers_table(conn)
        conn.commit()
        assert set(added) == {"error_message", "processed_at"}

        cols = {r[1] for r in conn.execute("PRAGMA table_info(papers)")}
        assert {"error_message", "processed_at"} <= cols
        # 已有行不能被动到
        assert conn.execute("SELECT title FROM papers WHERE id='old1'").fetchone()[0] == "旧数据"
        conn.close()


def test_ddl_and_hot_cols_agree():
    """PAPERS_DDL 里已经写了的列，不该再出现在热升级清单里造成无谓 ALTER。"""
    import db_schema
    with tempfile.TemporaryDirectory() as td:
        conn = sqlite3.connect(_blank_db(td))
        conn.execute(PAPERS_DDL)
        ddl_cols = {r[1] for r in conn.execute("PRAGMA table_info(papers)")}
        conn.close()
    for col, _ in db_schema._HOT_COLS:
        assert col in ddl_cols, f"{col} 在热升级清单里但 PAPERS_DDL 没有，新库会缺这列"
