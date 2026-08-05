"""`papers` 表的唯一 schema 定义。

这张表是主管道（batch_pipeline）和综述生成器（litreview.store）的交界面：
前者写，后者读。此前 DDL 只写在 `batch_pipeline.init_db()` 里，于是刚装好、
主管道一次都没跑过的机器上，`review.py types|status|patents|standards` 和
MCP 的 `library_status` 全都会抛 `no such table: papers` 的原始 traceback ——
库文件明明建出来了（store 建了自己那 6 张 review_/paper_ 表），偏偏少这一张，
看报错完全猜不到"只是还没初始化"。

所以把 DDL 抽到这里，两边都 import：schema 只有一份，谁先碰数据库谁把表建上，
空库也能正常显示"0 篇"而不是崩掉。

⚠️ 改 papers 的列时只改这里。新增列走 `_HOT_COLS` 热升级，别直接改 `PAPERS_DDL`
里的列清单 —— 老库已经存在，CREATE TABLE IF NOT EXISTS 对它是空操作，
只有 ALTER 才补得上。
"""
import sqlite3

PAPERS_DDL = """CREATE TABLE IF NOT EXISTS papers (
    id TEXT PRIMARY KEY,
    title TEXT,
    pdf_path TEXT,
    language TEXT,
    mineru_md TEXT,
    images_dir TEXT,
    status TEXT,
    batch_provider TEXT,
    batch_job_id TEXT,
    result_json TEXT,
    error_message TEXT,
    processed_at TEXT
)"""

# 老库热升级用：(列名, 类型)。列名会被拼进 SQL，只允许字面量，不接受外部输入。
_HOT_COLS = (
    ("error_message", "TEXT"),
    ("processed_at", "TEXT"),
)


def ensure_papers_table(conn: sqlite3.Connection, on_upgrade=None, on_error=None) -> list:
    """建表 + 按需补列。不 commit，由调用方决定事务边界。

    on_upgrade(col_name) / on_error(col_name, exc)：可选回调，给调用方打日志用
    （batch_pipeline 走 rich console，store 静默）。补列失败不抛，沿用原有行为：
    热升级失败不该拖垮整个管道。

    返回实际补上的列名列表。
    """
    c = conn.cursor()
    c.execute(PAPERS_DDL)
    c.execute("PRAGMA table_info(papers)")
    existing = {row[1] for row in c.fetchall()}
    added = []
    for col_name, col_type in _HOT_COLS:
        if col_name in existing:
            continue
        try:
            c.execute(f"ALTER TABLE papers ADD COLUMN {col_name} {col_type}")
            added.append(col_name)
            if on_upgrade:
                on_upgrade(col_name)
        except Exception as e:
            if on_error:
                on_error(col_name, e)
    return added
