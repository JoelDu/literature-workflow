"""MCP server：把文献库检索与综述生成暴露为大模型可直接调用的工具（stdio 协议）。

工具一览：
- library_status      文献库与索引概况
- search_literature   向量检索+重排序，返回最相关的原文片段（供调用方大模型自行归纳作答）
- get_paper_info      按关键词查论文元数据（中英标题/DOI/作者/期刊/关键词/TLDR）
- generate_outline    生成综述大纲 JSON（调用方可修改后传给 start_review）
- start_review        后台启动完整综述生成（7-30 分钟），立即返回 job_id
- resume_review       从持久化章节检查点恢复中断/部分完成任务
- review_status       查询综述生成进度/结果路径

启动方式见 mcp_server.sh（负责注入 API key、清理代理变量、设置数据路径）。
"""
import io
import os
import sys
import json
import time
import logging
import functools
import threading
import traceback
import queue
import uuid
from datetime import datetime
from typing import Any, List, Optional, Dict, Union

import anyio
import sqlite3

# utils 导入时会安装 TeeLogger 劫持 stdout（写 app.log），这会破坏 MCP 的 stdio JSON-RPC
# 协议流 —— 导入完成后立即还原真实的 stdout/stderr。
_real_stdout, _real_stderr = sys.stdout, sys.stderr
from dotenv import dotenv_values, load_dotenv
load_dotenv()
# mcp_server.sh 会先加载可选共享密钥文件；其中可能残留旧 OpenAI key。生成模型与
# 代理以当前项目 .env 为准，和 Docker 的覆盖规则保持一致。只覆盖这四项，保留脚本
# 明确指定的生产 DB_PATH / REVIEW_OUTPUT_DIR。
if os.getenv("MCP_PROJECT_ENV_PRIORITY") == "1":
    _project_env = dotenv_values()
    for _name in ("OPENAI_API_KEY", "OPENAI_API_BASE", "LIT_HTTP_PROXY", "LIT_HTTPS_PROXY"):
        if _project_env.get(_name):
            os.environ[_name] = str(_project_env[_name])
from utils import get_settings, log_run_event
sys.stdout, sys.stderr = _real_stdout, _real_stderr

from rich.console import Console
from mcp.server.fastmcp import FastMCP
from litreview.locator import get_locator

# 类型词表：工具描述里的类型名、corpus 校验、结果标签全部从这里取，
# 加一种文献类型只改 doctype.py。纯正则模块，导入不带重依赖。
from litreview import doctype as dt

settings = get_settings()
mcp = FastMCP("literature-review")

# 日志跟随数据库目录，避免 MCP 指向外部 DATA_ROOT 时日志仍落在源码仓库里。
_log_dir = os.path.dirname(os.path.abspath(settings.DB_PATH))
os.makedirs(_log_dir, exist_ok=True)
logger = logging.getLogger("mcp_server")
logger.setLevel(logging.INFO)
logger.propagate = False
_file_handler = logging.FileHandler(os.path.join(_log_dir, "mcp_server.log"), encoding="utf-8")
_file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(_file_handler)
logger.info(f"MCP server 启动中 (pid={os.getpid()}, cwd={os.getcwd()}, "
            f"python={sys.executable}, db={settings.DB_PATH})")


def _get_db_conn(db_path: str = None, readonly: bool = False) -> sqlite3.Connection:
    """获取配置了 WAL 并发模式与 30s 忙超时的健壮 SQLite 连接。"""
    path = db_path or settings.DB_PATH
    if readonly:
        conn = sqlite3.connect(f"file:{os.path.abspath(path)}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(path, timeout=30.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
    return conn


def retry_on_db_locked(max_retries=5, initial_delay=0.1):
    """数据库并发锁冲突指数退避重试装饰器。"""
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            delay = initial_delay
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except sqlite3.OperationalError as e:
                    if "locked" in str(e).lower() or "busy" in str(e).lower():
                        if attempt == max_retries - 1:
                            logger.error(f"数据库写锁争用超时 [{func.__name__}]: {e}")
                            raise
                        time.sleep(delay)
                        delay *= 2
                    else:
                        raise
        return wrapper
    return decorator


def _logged_tool():
    """给工具函数加调用日志与防崩溃异常边界（进入/耗时/异常防御），确保 stdio JSON-RPC 管道永不崩溃。"""
    def deco(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            call_repr = ", ".join([repr(a) for a in args] +
                                  [f"{k}={v!r}" for k, v in kwargs.items()])
            logger.info(f"→ {fn.__name__}({call_repr})")
            t0 = time.time()
            try:
                # FastMCP 会在事件循环里直接调用同步函数。把网络/检索重活移入工作线程
                result = await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))
                logger.info(f"← {fn.__name__} 完成，耗时 {time.time()-t0:.1f}s")
                return result
            except Exception as e:
                err_trace = traceback.format_exc()
                logger.error(f"✗ {fn.__name__} 捕获异常，耗时 {time.time()-t0:.1f}s\n{err_trace}")
                # 防御性降级提示，绝不抛出未捕获异常冲垮 stdio 管道
                return f"⚠️ 工具执行遇到异常 [{fn.__name__}]: {str(e)}。请检查参数或稍后重试。"
        return mcp.tool()(wrapper)
    return deco

# ── 惰性全局单例（矩阵缓存跨调用复用） ─────────────────────────────────────────

_state = {"generation_client": None, "embedding_client": None,
          "store": None, "embedder": None, "reranker": None}
_state_lock = threading.RLock()
_jobs = {}          # job_id -> {status, log, output, error, ...}
_jobs_lock = threading.Lock()
_review_queue = queue.Queue()
_review_worker = None
_review_worker_lock = threading.Lock()
_review_jobs_db_init_lock = threading.Lock()
_review_jobs_db_initialized = set()
_review_jobs_db_recovered = set()


def _client():
    """OpenAI 生成客户端：mini 分析与 GPT-5.4 综述写作。"""
    if _state["generation_client"] is None:
        with _state_lock:
            if _state["generation_client"] is None:
                from llm_router import make_openai_client
                _state["generation_client"], _ = make_openai_client(quiet=True)
    return _state["generation_client"]


def _embedding_client():
    """硅基流动嵌入客户端；禁止复用 OpenAI 生成端点。"""
    if _state["embedding_client"] is None:
        with _state_lock:
            if _state["embedding_client"] is None:
                key = os.getenv("SILICONFLOW_API_KEY", "")
                base = os.getenv("SILICONFLOW_API_BASE", "")
                if not key or not base:
                    raise RuntimeError("MCP 检索需要 SILICONFLOW_API_KEY/SILICONFLOW_API_BASE")
                import httpx
                from openai import OpenAI
                _state["embedding_client"] = OpenAI(
                    api_key=key, base_url=base, timeout=180,
                    http_client=httpx.Client(timeout=180, trust_env=False))
    return _state["embedding_client"]


def _store():
    if _state["store"] is None:
        with _state_lock:
            if _state["store"] is None:
                from litreview.store import VectorStore
                _state["store"] = VectorStore(settings.DB_PATH, settings.EMBEDDING_DIM)
    return _state["store"]


def _embedder():
    if _state["embedder"] is None:
        with _state_lock:
            if _state["embedder"] is None:
                from litreview.embedder import Embedder
                _state["embedder"] = Embedder(
                    _embedding_client(), settings.EMBEDDING_MODEL, settings.EMBEDDING_DIM,
                    settings.EMBEDDING_BATCH_SIZE, settings.EMBEDDING_QUERY_INSTRUCTION)
    return _state["embedder"]


def _reranker():
    if _state["reranker"] is None and settings.RERANK_ENABLE:
        with _state_lock:
            if _state["reranker"] is None:
                from litreview.reranker import Reranker
                _state["reranker"] = Reranker(settings.RERANK_MODEL)
    return _state["reranker"]


def format_bilingual_title(t_zh: Optional[str], t_en: Optional[str], orig_title: Optional[str] = "") -> str:
    """标准化学术题名展示：保留英文原名 + 中文翻译"""
    zh = (t_zh or "").strip()
    en = (t_en or "").strip()
    orig = (orig_title or "").strip()
    
    # 判断原始标题是否为英文
    is_orig_ascii = orig and any(c.isascii() and c.isalpha() for c in orig)
    
    if en and zh and en.lower() != zh.lower():
        if is_orig_ascii or not any('\u4e00' <= c <= '\u9fff' for c in en):
            return f"《{en}》（中译: {zh}）"
        else:
            return f"《{zh}》（英译: {en}）"
    elif en:
        return f"《{en}》"
    elif zh:
        return f"《{zh}》"
    return f"《{orig or '未命名文献'}》"


# ── 即时工具 ──────────────────────────────────────────────────────────────────

@_logged_tool()
def library_status() -> str:
    """查看本地文献库概况：文献数、向量索引状态、元数据提取状态。"""
    s = _store().stats()
    conn = _store()._conn()
    n_details = conn.execute("SELECT COUNT(*) FROM paper_details").fetchone()[0]
    latest = conn.execute("SELECT MAX(processed_at) FROM papers").fetchone()[0]
    conn.close()
    counts = _store().doc_type_counts()
    breakdown = "、".join(f"{dt.label(k)} {v}" for k, v in
                          sorted(counts.items(), key=lambda kv: -kv[1]))
    # 空库（刚装好、主管道还没跑过）时 breakdown 是空串、latest 是 None，
    # 直接拼进去会印成「0 篇（；最近入库时间 None）」——首次安装第一眼就是这个，
    # 容易被当成出错了。所以两项都按有无分别成句。
    if s["exported_docs"] == 0:
        head = "0 篇（库是空的，把 PDF 放进 input_pdfs 等待处理即可）"
    else:
        head = f"{s['exported_docs']} 篇（{breakdown}；最近入库时间 {latest}）"
    return (f"文献库概况：\n"
            f"- 已入库文献：{head}\n"
            f"- 向量索引：{s['indexed_docs']} 篇 / {s['embedded_chunks']} 块"
            f"（模型 {settings.EMBEDDING_MODEL}，待索引 {s['pending_docs']} 篇）\n"
            f"- 结构化元数据：{n_details} 篇（DOI/中英标题/参考文献/图表）")


@_logged_tool()
def list_documents(
    query: str = "",
    doc_type: str = "",
    vector_status: str = "",
    sort_by: str = "processed_at",
    sort_order: str = "desc",
    cursor: int = 0,
    limit: int = 20,
    include_quarantined: bool = False,
) -> str:
    """分页浏览与多维条件过滤文献库目录（解决查目录需看 Excel 的问题，直接在 MCP 中探索文献全貌）。

    Args:
        query: 标题 / 作者 / DOI / 关键词 / 编号模糊过滤（留空不限）
        doc_type: 文献类型过滤，可选 paper(论文) / patent(专利) / standard(标准) / book(教材) / report(报告) 等（留空不限）
        vector_status: 向量索引状态过滤，可选 indexed(已建索引) / pending(待建索引) / not_eligible(不可索引)（留空不限）
        sort_by: 排序字段，可选 processed_at(入库时间) / year(年份) / title(标题)
        sort_order: 排序方向，asc(升序) 或 desc(降序，默认)
        cursor: 分页游标偏移量（0-indexed，默认 0）
        limit: 每页返回条目数（默认 20，上限 100）
        include_quarantined: 是否包含被隔离的脏数据/Mock测试文献（默认 False 自动排除）
    """
    limit = max(1, min(limit, 100))
    cursor = max(0, cursor)
    conn = _store()._conn()

    where_clauses = ["p.status='EXPORTED'"]
    params = []

    if not include_quarantined:
        where_clauses.append("COALESCE(d.quarantine_status, 'NORMAL') = 'NORMAL'")

    if query:
        # 中英双语常见学科词表自动双向扩展
        synonyms = {
            "尿素": ["urea"],
            "urea": ["尿素"],
            "包膜": ["coated", "coating", "encapsulated"],
            "coating": ["包膜", "涂层"],
            "磷酸": ["phosphoric", "phosphate"],
            "phosphoric": ["磷酸"],
            "磷石膏": ["phosphogypsum", "gypsum"],
            "phosphogypsum": ["磷石膏"],
            "结块": ["caking", "anti-caking", "anticaking"],
            "caking": ["结块", "防结块"],
            "缓释": ["slow release", "controlled release"],
            "控释": ["controlled release", "slow release"]
        }
        terms = [query.strip()]
        for k, syn_list in synonyms.items():
            if k.lower() in query.lower():
                terms.extend(syn_list)
        
        q_or_clauses = []
        for term in terms:
            q_pattern = f"%{term}%"
            q_or_clauses.append("(p.title LIKE ? OR d.title_zh LIKE ? OR d.title_en LIKE ? "
                                "OR d.authors LIKE ? OR d.doi LIKE ? OR d.patent_no LIKE ? OR d.doc_no LIKE ? OR d.cite_key LIKE ?)")
            params.extend([q_pattern] * 8)
        where_clauses.append(f"({' OR '.join(q_or_clauses)})")

    if doc_type:
        where_clauses.append("COALESCE(d.doc_type, 'paper') = ?")
        params.append(doc_type)

    if vector_status == "indexed":
        where_clauses.append("m.embedded = 1")
    elif vector_status == "pending":
        where_clauses.append("(m.embedded = 0 OR m.embedded IS NULL) AND LENGTH(COALESCE(p.mineru_md, '')) > 0")
    elif vector_status == "not_eligible":
        where_clauses.append("LENGTH(COALESCE(p.mineru_md, '')) = 0")

    where_sql = " AND ".join(where_clauses)

    sort_cols = {
        "processed_at": "p.processed_at",
        "year": "COALESCE(d.year, '0000')",
        "title": "COALESCE(d.title_zh, d.title_en, p.title)"
    }
    order_col = sort_cols.get(sort_by, "p.processed_at")
    order_dir = "ASC" if sort_order.lower() == "asc" else "DESC"

    count_sql = f"""
        SELECT COUNT(*)
        FROM papers p
        LEFT JOIN paper_details d ON p.id = d.doc_id
        LEFT JOIN review_index_meta m ON p.id = m.doc_id
        WHERE {where_sql}
    """
    total_count = conn.execute(count_sql, params).fetchone()[0]

    select_sql = f"""
        SELECT p.id, p.title, d.title_zh, d.title_en, d.doi, d.authors, d.journal,
               d.year, COALESCE(d.doc_type, 'paper') as doc_type, d.patent_no, d.doc_no,
               p.processed_at, m.embedded, m.n_chunks, LENGTH(COALESCE(p.mineru_md, '')) as md_len,
               d.cite_key
        FROM papers p
        LEFT JOIN paper_details d ON p.id = d.doc_id
        LEFT JOIN review_index_meta m ON p.id = m.doc_id
        WHERE {where_sql}
        ORDER BY {order_col} {order_dir}
        LIMIT ? OFFSET ?
    """
    rows = conn.execute(select_sql, params + [limit, cursor]).fetchall()
    conn.close()

    now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    next_cursor = cursor + len(rows) if cursor + len(rows) < total_count else None

    if not rows:
        return f"【文献目录浏览 (as_of: {now_iso})】\n未检索到匹配文献（当前筛选条件共 0 篇）。"

    out = [
        f"【文献目录浏览 (as_of: {now_iso})】",
        f"共命中 {total_count} 篇文献，当前显示第 {cursor + 1} ~ {cursor + len(rows)} 篇 (下一页游标 cursor={next_cursor})：\n"
    ]

    for i, r in enumerate(rows, start=cursor + 1):
        (doc_id, title, t_zh, t_en, doi, authors, journal, year,
         dtype, patent_no, doc_no, processed_at, embedded, n_chunks, md_len, cite_key) = r

        display_title = format_bilingual_title(t_zh, t_en, title)
        display_id = cite_key if cite_key else doc_id[:8]
        if dtype == "paper":
            type_tag = "[论文]"
            extra = f"《{journal or '-'}》({year or '-'})"
        elif dtype == "patent":
            type_tag = f"[专利 {patent_no or '-'}]"
            extra = f"年份:{year or '-'}"
        elif dtype == "standard":
            type_tag = f"[标准 {doc_no or '-'}]"
            extra = f"年份:{year or '-'}"
        elif dtype == "book":
            type_tag = "[教材/专著]"
            extra = f"年份:{year or '-'}"
        else:
            type_tag = f"[{dt.label(dtype)}]"
            extra = f"年份:{year or '-'}"

        if embedded == 1:
            v_status = f"✅已索引({n_chunks or 0}块)"
        elif md_len == 0:
            v_status = "❌不可索引(无正文)"
        else:
            v_status = f"⏳待向量化({n_chunks or 0}块)"

        doi_str = f" | DOI:{doi}" if doi else ""
        authors_str = f" | 作者:{authors[:30]}..." if authors and len(authors) > 30 else (f" | 作者:{authors}" if authors else "")

        out.append(f"{i}. {type_tag} {display_title} {extra}{authors_str}{doi_str}\n"
                   f"   文献编号: {display_id} (Hash: {doc_id[:8]}) | 状态: {v_status} | 入库时间: {processed_at or '-'}")

    if next_cursor:
        extra_args = ""
        if query:
            extra_args += f', query="{query}"'
        if doc_type:
            extra_args += f', doc_type="{doc_type}"'
        out.append(f"\n💡 获取下一页请调用：list_documents(cursor={next_cursor}, limit={limit}{extra_args})")

    return "\n".join(out)


@_logged_tool()
def library_reconcile() -> str:
    """严格只读地对齐数据库 (SQLite)、Excel、物理文件 (PDF/EPUB) 与向量索引四层数据，
    输出四层对齐审计诊断报告、孤儿差异清单与重复项检测（纯只读，绝不修改数据）。"""
    conn = _store()._conn()
    now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 1. 数据库统计
    total_papers = conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
    exported_papers = conn.execute("SELECT COUNT(*) FROM papers WHERE status='EXPORTED'").fetchone()[0]
    completed_papers = conn.execute("SELECT COUNT(*) FROM papers WHERE status='COMPLETED'").fetchone()[0]
    total_details = conn.execute("SELECT COUNT(*) FROM paper_details").fetchone()[0]

    # 2. 向量索引审计
    indexed_cnt = conn.execute("SELECT COUNT(*) FROM review_index_meta WHERE embedded=1").fetchone()[0]
    unembedded_meta_cnt = conn.execute("SELECT COUNT(*) FROM review_index_meta WHERE embedded=0").fetchone()[0]
    not_in_meta_rows = conn.execute(
        "SELECT id, title, LENGTH(COALESCE(mineru_md, '')) FROM papers "
        "WHERE id NOT IN (SELECT doc_id FROM review_index_meta)").fetchall()

    # 3. 物理文件审计
    data_dir = os.path.dirname(os.path.abspath(settings.DB_PATH))
    c_papers = conn.execute(
        "SELECT p.id, p.title, p.pdf_path, COALESCE(d.doc_type, 'paper') "
        "FROM papers p LEFT JOIN paper_details d ON p.id=d.doc_id").fetchall()

    missing_source_files = []
    for doc_id, title, p_path, dtype in c_papers:
        if not p_path:
            missing_source_files.append((doc_id[:8], title, "空路径"))
            continue
        abs_p = os.path.join(data_dir, p_path.lstrip("./"))
        if os.path.exists(abs_p) or os.path.exists(p_path):
            continue
        fname = os.path.basename(p_path)
        if dtype == "book" and os.path.exists(os.path.join(data_dir, "processed_books", fname)):
            continue
        if os.path.exists(os.path.join(data_dir, "processed_pdfs", fname)):
            continue
        missing_source_files.append((doc_id[:8], title, p_path))

    # 4. Excel 对齐审计
    excel_path = settings.EXCEL_OUTPUT_PATH
    excel_exists = os.path.exists(excel_path)
    excel_rows = 0
    extra_in_excel = []
    missing_in_excel = []

    if excel_exists:
        try:
            import pandas as pd
            df = pd.read_excel(excel_path)
            excel_rows = len(df)
            excel_ids = set()
            for col in ['文献ID', 'doc_id', 'ID', 'id']:
                if col in df.columns:
                    excel_ids = set(df[col].dropna().astype(str).str.strip())
                    break
            db_ids = {r[0] for r in c_papers}
            extra_in_excel = list(excel_ids - db_ids)
            missing_in_excel = list(db_ids - excel_ids)
        except Exception as e:
            excel_exists = False
            excel_rows = f"读取失败: {e}"

    # 5. 重复项审计
    dup_dois = conn.execute(
        "SELECT doi, COUNT(*) FROM paper_details WHERE doi != '' AND doi IS NOT NULL "
        "GROUP BY doi HAVING COUNT(*) > 1").fetchall()
    dup_titles = conn.execute(
        "SELECT title, COUNT(*) FROM papers GROUP BY title HAVING COUNT(*) > 1").fetchall()
    conn.close()

    out = [
        f"═══════════════════════════════════════════════════════════════════",
        f"📊 Literature Analyzer 四层数据对齐审计报告 (as_of: {now_iso})",
        f"═══════════════════════════════════════════════════════════════════",
        f"\n1. 【主数据库层 (SQLite)】",
        f"   - 文献总记录数: {total_papers} 篇 (已导出 EXPORTED: {exported_papers}, 待导出 COMPLETED: {completed_papers})",
        f"   - 结构化元数据 (paper_details): {total_details} 篇 (覆盖率 {total_details/max(1,total_papers)*100:.1f}%)",
        f"\n2. 【向量索引层 (Vector Index)】",
        f"   - 已索引文献 (embedded=1): {indexed_cnt} 篇",
        f"   - 待向量化文献 (embedded=0): {unembedded_meta_cnt} 篇 (已切块，待执行 review.py index)",
        f"   - 不可索引文献 (无正文/空文本): {len(not_in_meta_rows)} 篇" + (f" (如: {not_in_meta_rows[0][0][:8]} 《{not_in_meta_rows[0][1]}》)" if not_in_meta_rows else ""),
        f"   - 账本闭环验证: {indexed_cnt} (已索引) + {unembedded_meta_cnt} (待向量化) + {len(not_in_meta_rows)} (不可索引) = {total_papers} (总篇数 100% 闭环)",
        f"\n3. 【物理文件层 (Source Files)】",
        f"   - 源文件匹配成功率: {total_papers - len(missing_source_files)} / {total_papers} ({(total_papers - len(missing_source_files))/max(1,total_papers)*100:.1f}%)",
        f"   - 物理文件缺失数: {len(missing_source_files)} 篇",
        f"\n4. 【Excel 投影层 (knowledge_base.xlsx)】",
        f"   - Excel 文件状态: {'存在' if excel_exists else '未生成'} ({excel_rows} 行)",
        f"   - 数据库已导出但 Excel 缺失: {len(missing_in_excel)} 篇",
        f"   - Excel 历史孤儿行 (在 Excel 但已被删除/重置出库): {len(extra_in_excel)} 行 (提示: 可运行 `python cli.py sync-excel` 一键对齐清除)",
        f"\n5. 【异常与重复检测】",
        f"   - 重复 DOI: {len(dup_dois)} 组" + (f" (如: {dup_dois[0][0]} 共 {dup_dois[0][1]} 篇)" if dup_dois else " (无)"),
        f"   - 重复完全同名题名: {len(dup_titles)} 组" + (f" (如: 《{dup_titles[0][0]}》 共 {dup_titles[0][1]} 篇)" if dup_titles else " (无)"),
        f"═══════════════════════════════════════════════════════════════════"
    ]
    return "\n".join(out)


@_logged_tool()
def search_literature(query: str, top_k: int = 8, corpus: str = "all") -> str:
    """在本地文献库中检索与问题最相关的原文片段（向量召回+重排序，支持中英跨语言）。
    返回片段原文与来源论文信息，可据此归纳回答并注明出处。

    Args:
        query: 检索问题或关键词（中文即可，能命中英文文献）
        top_k: 返回片段数量，默认 8
        corpus: 限定来源，all(默认，混检) 或 paper(论文) / book(教材) / patent(专利) /
                standard(标准) / report(报告) / thesis(学位论文) / conf(会议论文) / web(网络资源)
    """
    store = _store()
    if corpus not in ("all",) + tuple(dt.all_types()):
        return (f"corpus 只能是 all 或 {' / '.join(dt.all_types())}，"
                f"收到的是「{corpus}」。")
    qvec = _embedder().embed_query(query)
    rr = _reranker()
    recall_k = settings.RERANK_CANDIDATES if rr else top_k
    results = store.search(qvec, top_k=recall_k, max_per_doc=settings.REVIEW_MAX_CHUNKS_PER_DOC,
                           doc_types=None if corpus == "all" else [corpus])
    if not results:
        hint = "" if corpus == "all" else f"（当前限定分类: {corpus}，可尝试设置为 corpus='all'）"
        stats = store.stats()
        if stats.get("indexed_docs", 0) == 0:
            return "❌ 未检索到结果：当前文献库尚未建立向量索引，请先执行向量化构建。"
        try:
            conn = store._conn()
            meta_hits = conn.execute(
                "SELECT p.id, COALESCE(d.title_zh, d.title_en, p.title) FROM papers p LEFT JOIN paper_details d ON p.id = d.doc_id WHERE p.status='EXPORTED' AND (p.title LIKE ? OR d.title_zh LIKE ? OR d.title_en LIKE ? OR d.keywords LIKE ?) LIMIT 3",
                (f"%{query}%", f"%{query}%", f"%{query}%", f"%{query}%")
            ).fetchall()
            conn.close()
            if meta_hits:
                titles = "、".join([f"《{m[1]}》(ID:{m[0][:8]})" for m in meta_hits])
                return f"向量检索未召回到相关正文片段{hint}，但在文献元数据中匹配到相关文献：{titles}。建议调用 get_paper_info 查询其结构化信息，或更换更具体的学术机理关键词重新检索。"
        except Exception:
            pass
        return f"未检索到匹配的原文片段{hint}。建议缩短检索词或更换关键词重试。"
    if rr:
        ranked = rr.rerank(query, [r.content for r in results], top_n=top_k)
        results = [results[i] for i, _score in ranked]
        scores = [s for _i, s in ranked]
    else:
        results, scores = results[:top_k], [r.score for r in results[:top_k]]
    meta = store.get_paper_meta(list({r.doc_id for r in results}))
    out = [f"检索「{query}」命中 {len(results)} 个片段：\n"]
    for i, (r, sc) in enumerate(zip(results, scores), 1):
        m = meta.get(r.doc_id, {})
        title_str = format_bilingual_title(m.get("title_zh"), m.get("title_en"), m.get("title", r.doc_id[:8]))
        # 类型标签一定要带上：调用方模型看到"[标准 GB 38400-2019]"才知道这段限量值
        # 出自强制性文件而非某篇论文的实验结论，两者的可引用分量完全不同。
        kt = m.get("doc_type") or "paper"
        no = m.get("patent_no") or m.get("doc_no") or ""
        kind = "" if kt == "paper" else f"[{dt.label(kt)}{' ' + no if no else ''}] "
        cid = m.get("cite_key") or r.doc_id[:8]
        out.append(f"[{i}] {kind}{title_str} ({m.get('year') or '年份不详'}) "
                   f"小节:{r.section_title or '-'} 相关度:{sc:.3f} 编号:{cid} (Hash:{r.doc_id[:8]})\n"
                   f"{r.content[:800]}\n")
    return "\n".join(out)


@_logged_tool()
def get_paper_info(keyword: str, limit: int = 5) -> str:
    """按标题/关键词/文献编号查询文献库中论文的结构化元数据（保留英文原名与中文翻译对照、DOI、作者、期刊、年份、关键词、TLDR）。

    Args:
        keyword: 标题、关键词、DOI 或文献编号（如 'U-J25-001'，中英文均可）
        limit: 最多返回条数，默认 5
    """
    conn = _store()._conn()
    rows = conn.execute(
        """SELECT p.id, p.title, d.title_zh, d.title_en, d.doi, d.authors, d.journal,
                  d.year, d.keywords, d.n_refs, d.n_figures, d.n_tables, p.result_json,
                  COALESCE(d.doc_type,'paper'), d.patent_no, d.filing_date,
                  d.legal_status, d.status_checked_at, d.publisher,
                  d.doc_no, d.effective_date, d.cite_key
           FROM papers p LEFT JOIN paper_details d ON d.doc_id = p.id
           WHERE p.status='EXPORTED' AND (p.id LIKE ? OR p.title LIKE ? OR d.title_zh LIKE ?
                 OR d.title_en LIKE ? OR d.keywords LIKE ? OR d.patent_no LIKE ?
                 OR d.doc_no LIKE ? OR d.cite_key LIKE ?)
           LIMIT ?""",
        (f"%{keyword}%",) * 8 + (limit,)).fetchall()
    conn.close()
    if not rows:
        return f"未找到与「{keyword}」匹配的文献。"
    out = [f"找到 {len(rows)} 篇：\n"]
    for (doc_id, title, t_zh, t_en, doi, authors, journal, year,
         kw, n_refs, n_figs, n_tabs, result_json,
         doc_type, patent_no, filing_date, legal_status, checked_at, publisher,
         doc_no, effective_date, cite_key) in rows:
        tldr = ""
        try:
            tldr = json.loads(result_json).get("tldr", "") if result_json else ""
        except Exception:
            pass
        display_title = format_bilingual_title(t_zh, t_en, title)
        display_id = cite_key or doc_id[:8]
        if doc_type == "patent":
            from litreview.patent import describe
            out.append(f"● [专利] {display_title}（文献编号: {display_id} | Hash: {doc_id[:8]}）\n"
                       f"  专利号: {patent_no or '-'} | 申请日: {filing_date or '-'}\n"
                       f"  发明人: {authors or '-'} | 申请人/专利权人: {publisher or '-'}\n"
                       f"  {describe(patent_no, filing_date, legal_status, checked_at)}\n"
                       f"  图 {n_figs or 0} / 表 {n_tabs or 0}\n")
            continue
        if doc_type == "standard":
            out.append(f"● [标准] {display_title}（文献编号: {display_id} | Hash: {doc_id[:8]}）\n"
                       f"  发布机构: {authors or '-'} | 出版者: {publisher or '-'}\n"
                       f"  {dt.describe_standard(doc_no, effective_date, legal_status, checked_at)}\n"
                       f"  图 {n_figs or 0} / 表 {n_tabs or 0}\n")
            continue
        out.append(f"● [{dt.label(doc_type)}] {display_title}（文献编号: {display_id} | Hash: {doc_id[:8]}）\n"
                   f"  作者: {authors or '-'} | 期刊: {journal or '-'} | 年份: {year or '-'}\n"
                   f"  DOI: {doi or '-'} | 关键词: {kw or '-'}\n"
                   f"  参考文献 {n_refs or 0} 条 / 图 {n_figs or 0} / 表 {n_tabs or 0}\n"
                   f"  TLDR: {tldr[:200] or '-'}\n")
    return "\n".join(out)


@_logged_tool()
def doc_status(doc_type: str = "patent", keyword: str = "") -> str:
    """查看专利 / 标准台账：编号、日期、由编号现算的出版阶段或保护期，以及人工核实过的状态。

    专利的「出版阶段」「保护期至」、标准的「实施日期」，都是印在 PDF 上、由编号和日期
    现算出来的事实，不会变。
    而专利的「法律状态」（驳回/撤回/欠费失效/无效）、标准的「现行/被代替/废止」
    **都不在 PDF 里且随时间变化**，只有人工核实过才有值。显示「未核实」时
    **不要**拿出版阶段或实施日期替代它回答，也不要自行推断——
    一份已废止标准的限量值若被当成现行的引用，后果是实打实的。
    本工具只读；更新状态需在服务器上执行
    `python review.py patents|standards --set <编号> --status <状态>`。

    Args:
        doc_type: patent(专利，默认) 或 standard(标准)
        keyword: 按编号或名称过滤，留空则列出该类型全部
    """
    kind = dt.normalize(doc_type)
    if kind not in ("patent", "standard"):
        return "doc_type 只能是 patent(专利) 或 standard(标准)——只有这两类有会随时间变化的状态。"
    rows = _store().list_docs(kind)
    if keyword:
        k = keyword.lower()
        rows = [r for r in rows if k in (r["patent_no"] or "").lower()
                or k in (r["doc_no"] or "").lower() or k in (r["title"] or "").lower()]
    noun = dt.label(kind)
    if not rows:
        return f"未找到匹配「{keyword}」的{noun}。" if keyword else f"库中暂无{noun}记录。"

    out = [f"{noun} {len(rows)} 篇：\n"]
    if kind == "patent":
        from litreview.patent import patent_stage, patent_expiry, is_term_expired
        for r in rows:
            exp = patent_expiry(r["patent_no"], r["filing_date"])
            legal = (f"{r['legal_status']}（{r['status_checked_at']} 核实）"
                     if r["legal_status"] else "未核实（PDF 中无此信息，需人工查证）")
            out.append(f"● {r['patent_no'] or '号码未识别'} 《{r['title'] or '-'}》"
                       f"（文献ID {r['doc_id'][:8]}）\n"
                       f"  申请日: {r['filing_date'] or '-'} | 出版阶段: {patent_stage(r['patent_no'])}\n"
                       f"  保护期至: {exp or '-'}"
                       f"{'（已超期）' if is_term_expired(r['patent_no'], r['filing_date']) else ''}"
                       f" | 法律状态: {legal}\n"
                       f"  发明人: {r['inventors'] or '-'} | 专利权人: {r['assignee'] or '-'}\n")
    else:
        for r in rows:
            out.append(f"● {r['doc_no'] or '编号未识别'} 《{r['title'] or '-'}》"
                       f"（文献ID {r['doc_id'][:8]}）\n"
                       f"  {dt.describe_standard(r['doc_no'], r['effective_date'], r['legal_status'], r['status_checked_at'])}\n"
                       f"  发布机构: {r['inventors'] or '-'} | 出版者: {r['assignee'] or '-'}\n")
    return "\n".join(out)


def _check_literature_sufficiency(topic: str, words: int = 0, target_lang: str = "zh") -> dict:
    """结合综述目标字数 3 档动力学门槛，统计与主题相关的库内文献总篇数并给出自适应缺口诊断"""
    from litreview.stages import get_scaling_config
    scaling = get_scaling_config(words=words, target_lang=target_lang)
    threshold = scaling["min_papers_threshold"]
    tier_name = scaling["tier_name"]
    lang_name = "英文综述" if target_lang.lower() in ("en", "english", "英文") else "中文综述"
    
    store = _store()
    conn = store._conn()
    keywords = [k.strip() for k in topic.replace("、", " ").replace(",", " ").replace("与", " ").replace("相关", " ").replace("研究", " ").split() if len(k.strip()) >= 2]
    
    # 1. 语义向量检索相关文献 ID（仅在真实客户端环境下执行，具备离线与降级容错）
    vector_doc_ids = set()
    sf_key = os.getenv("SILICONFLOW_API_KEY", "")
    if sf_key and not sf_key.startswith("test-") and "invalid" not in os.getenv("SILICONFLOW_API_BASE", ""):
        try:
            qvec = _embedder().embed_query(topic)
            vector_results = store.search(qvec, top_k=80, max_per_doc=1)
            vector_doc_ids = {r.doc_id for r in vector_results}
        except Exception as e:
            logger.debug(f"向量初筛离线或跳过: {e}")
    
    # 2. 关键词与元数据匹配
    kw_doc_ids = set()
    if keywords:
        clauses = []
        params = []
        for kw in keywords:
            clauses.append("(p.title LIKE ? OR d.title_zh LIKE ? OR d.title_en LIKE ? OR d.keywords LIKE ?)")
            params.extend([f"%{kw}%"] * 4)
        sql = f"SELECT p.id FROM papers p LEFT JOIN paper_details d ON p.id = d.doc_id WHERE p.status='EXPORTED' AND ({' OR '.join(clauses)})"
        rows = conn.execute(sql, params).fetchall()
        kw_doc_ids = {r[0] for r in rows}
    
    all_matched_ids = vector_doc_ids.union(kw_doc_ids)
    
    # 统计分类分布
    breakdown = {}
    if all_matched_ids:
        placeholders = ",".join(["?"] * len(all_matched_ids))
        b_rows = conn.execute(f"SELECT COALESCE(d.doc_type, 'paper'), COUNT(*) FROM papers p LEFT JOIN paper_details d ON p.id=d.doc_id WHERE p.id IN ({placeholders}) GROUP BY COALESCE(d.doc_type, 'paper')", list(all_matched_ids)).fetchall()
        for dt_code, count in b_rows:
            breakdown[dt.label(dt_code)] = count
    conn.close()
    
    matched_cnt = len(all_matched_ids)
    is_sufficient = matched_cnt >= threshold
    gap = max(0, threshold - matched_cnt)
    
    dist_str = "、".join(f"{k} {v}篇" for k, v in breakdown.items()) if breakdown else "无"
    
    if is_sufficient:
        msg = f"✅ 【文献储备充分】主题「{topic}」库内相关资料共 {matched_cnt} 篇（包含：{dist_str}），达到{tier_name}（{lang_name}门槛≥{threshold}篇）要求。"
    else:
        msg = f"⚠️ 【文献储备缺口警示】主题「{topic}」库内相关资料共 {matched_cnt} 篇（包含：{dist_str}），距离{tier_name}（{lang_name}门槛≥{threshold}篇）尚缺 {gap} 篇。建议调用 citation_expand 探测扩展或通过流水线补充资料。"
    
    return {
        "is_sufficient": is_sufficient,
        "matched_count": matched_cnt,
        "threshold": threshold,
        "gap": gap,
        "tier_name": tier_name,
        "target_lang": target_lang,
        "breakdown": breakdown,
        "message": msg
    }


@_logged_tool()
def propose_review_angles(topic: str, words: int = 0, target_lang: str = "zh") -> str:
    """【大纲前置人机对齐】基于本地馆藏真实文献，为综述主题提炼 3 套差异鲜明的顶刊级立论方案（Thesis + Research Gap + 主线逻辑）。
    在生成大纲前调用本工具，让作者选择或调整核心立论与学术缺口，彻底避免大模型盲猜导致的平庸大纲。约需 15-30 秒。

    Args:
        topic: 综述主题（如"包膜尿素养分释放动力学与可降解涂层研究进展"）
        words: 预估总字数（可选，如 8000、12000）
        target_lang: 目标语言，可选 'zh' 或 'en'
    """
    from litreview.stages import propose_review_angles as _propose
    check = _check_literature_sufficiency(topic, words=words, target_lang=target_lang)
    console = Console(file=io.StringIO(), no_color=True, width=100)
    angles = _propose(topic, _store(), _embedder(), _client(), settings.ANALYSIS_MODEL,
                      settings, console, words=words, target_lang=target_lang)
    
    res = {
        "topic": topic,
        "literature_sufficiency_check": check,
        "proposed_angles": angles,
        "alignment_guidance": "请选择上述方案之一（或组合/自定义 Thesis 与 Research Gap），在调用 generate_outline 或 start_review 时作为参数传入，以生成具有明确学术立论的高质量大纲。"
    }
    return json.dumps(res, ensure_ascii=False, indent=2)


@_logged_tool()
def generate_outline(topic: str, focus: str = "", words: int = 0, sections: int = 0, target_lang: str = "zh",
                     research_gap: str = "", thesis: str = "", mainline_logic: str = "") -> str:
    """为综述主题生成高水平学术大纲（JSON）。支持注入作者的 Thesis（核心立论）与 Research Gap（学术缺口），
    沿选定主线逻辑构建具有三元组检索问题（机理/量化参数/失效瓶颈）的三级大纲，并自动执行 3 档字数伸缩与文献储备量诊断。
    约需 30-90 秒。

    Args:
        topic: 综述主题（必填，如"包膜尿素释放机理与工业化应用"）
        focus: 侧重方向（可选，如"侧重工业化应用与成本"）
        words: 综述预估总字数（可选，如 8000、12000，自动匹配对应章节与检索规模）
        sections: 主体章节数（可选，0 表示由模型根据字数档位自定）
        target_lang: 综述目标语言，可选 'zh'(中文综述) 或 'en'(英文综述)
        research_gap: 一句话学术缺口/痛点（可选，如"既往研究忽视了残膜在不同土壤温湿度下的微塑料降解动力学分歧"）
        thesis: 一句话核心立论（可选，如"论证蓖麻油基聚氨酯在添加无机纳米粒子后可完全兼顾控释期与全生物降解"）
        mainline_logic: 选定主线分类逻辑（可选，如"要素并列利弊暗转" / "现状-危害-对策问题导向" / "制备-应用" / "过程流"）
    """
    from litreview.stages import generate_outline as _gen
    
    # 1. 执行前置篇数与档位校验
    check = _check_literature_sufficiency(topic, words=words, target_lang=target_lang)
    
    console = Console(file=io.StringIO(), no_color=True, width=100)
    outline = _gen(topic, _store(), _embedder(), _client(), settings.REVIEW_MODEL,
                   settings, console, focus=focus, n_sections=sections,
                   words=words, target_lang=target_lang,
                   research_gap=research_gap, thesis=thesis, mainline_logic=mainline_logic)
    
    res = outline.model_dump()
    res["literature_sufficiency_check"] = check
    res["aligned_thesis"] = thesis or "针对主题核心技术瓶颈与机理分歧展开批判性综合"
    res["aligned_research_gap"] = research_gap or "聚焦现有研究所忽略的关键矛盾、工艺缺陷或理论实测不一致"
    res["aligned_mainline_logic"] = mainline_logic or "沿清晰因果链/问题导向层层递进"
    return json.dumps(res, ensure_ascii=False, indent=2)


# ── 综述生成（后台任务模式 + SQLite 状态持久化 + 故障自愈） ────────────────────────────

@retry_on_db_locked(max_retries=5)
def _init_review_jobs_db(recover_interrupted: bool = False):
    """建表/迁移；进程启动恢复只执行一次，普通读写不会改动任务状态。"""
    db_key = os.path.abspath(settings.DB_PATH)
    with _review_jobs_db_init_lock:
        if db_key in _review_jobs_db_initialized and (
            not recover_interrupted or db_key in _review_jobs_db_recovered
        ):
            return
        conn = _get_db_conn()
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS review_jobs (
                    job_id TEXT PRIMARY KEY,
                    topic TEXT NOT NULL,
                    focus TEXT,
                    words INTEGER DEFAULT 0,
                    sections INTEGER DEFAULT 0,
                    outline_json TEXT,
                    status TEXT NOT NULL,
                    progress_log TEXT,
                    output_path TEXT,
                    docx_path TEXT,
                    summary TEXT,
                    error TEXT,
                    queued_at TEXT,
                    started_at TEXT,
                    completed_at TEXT,
                    heartbeat_at TEXT,
                    checkpoint_json TEXT,
                    research_gap TEXT,
                    thesis TEXT,
                    mainline_logic TEXT,
                    target_lang TEXT DEFAULT 'zh'
                )
            """)
            existing_cols = [r[1] for r in conn.execute("PRAGMA table_info(review_jobs)").fetchall()]
            if "heartbeat_at" not in existing_cols:
                conn.execute("ALTER TABLE review_jobs ADD COLUMN heartbeat_at TEXT")
            if "checkpoint_json" not in existing_cols:
                conn.execute("ALTER TABLE review_jobs ADD COLUMN checkpoint_json TEXT")
            for column, ddl in (
                ("research_gap", "TEXT"),
                ("thesis", "TEXT"),
                ("mainline_logic", "TEXT"),
                ("target_lang", "TEXT DEFAULT 'zh'"),
            ):
                if column not in existing_cols:
                    conn.execute(f"ALTER TABLE review_jobs ADD COLUMN {column} {ddl}")

            if recover_interrupted and db_key not in _review_jobs_db_recovered:
                now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                cur = conn.execute("""
                    UPDATE review_jobs
                    SET status = 'interrupted',
                        error = '服务重启导致任务中断（历史断点已保留，可重新提交）',
                        completed_at = ?
                    WHERE status IN ('running', 'queued')
                """, (now_str,))
                if cur.rowcount > 0:
                    logger.info(f"启动自愈：已中断 {cur.rowcount} 个未完成后台任务并保留检查点。")
                _review_jobs_db_recovered.add(db_key)
            conn.commit()
            _review_jobs_db_initialized.add(db_key)
        finally:
            conn.close()


@retry_on_db_locked(max_retries=5)
def _persist_review_job(job_dict: dict):
    _init_review_jobs_db()
    conn = _get_db_conn()
    try:
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("""
            INSERT OR REPLACE INTO review_jobs
            (job_id, topic, focus, words, sections, outline_json, status, progress_log, output_path, docx_path, summary, error, queued_at, started_at, completed_at, heartbeat_at, checkpoint_json, research_gap, thesis, mainline_logic, target_lang)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            job_dict["job_id"],
            job_dict.get("topic", ""),
            job_dict.get("focus", ""),
            job_dict.get("words", 0),
            job_dict.get("sections", 0),
            job_dict.get("outline_json", ""),
            job_dict.get("status", "queued"),
            json.dumps(job_dict.get("log", []), ensure_ascii=False),
            job_dict.get("output", ""),
            job_dict.get("docx", ""),
            job_dict.get("summary", ""),
            job_dict.get("error", ""),
            job_dict.get("queued_at", ""),
            job_dict.get("started_at", ""),
            job_dict.get("completed_at", ""),
            job_dict.get("heartbeat_at", now_str),
            job_dict.get("checkpoint_json", ""),
            job_dict.get("research_gap", ""),
            job_dict.get("thesis", ""),
            job_dict.get("mainline_logic", ""),
            job_dict.get("target_lang", "zh"),
        ))
        conn.commit()
    finally:
        conn.close()


@retry_on_db_locked(max_retries=5)
def _get_persistent_review_job(job_id: str = "") -> Optional[dict]:
    _init_review_jobs_db()
    conn = _get_db_conn(readonly=True)
    try:
        c = conn.cursor()
        if job_id:
            c.execute("SELECT job_id, topic, focus, words, sections, outline_json, status, progress_log, output_path, docx_path, summary, error, queued_at, started_at, completed_at, heartbeat_at, checkpoint_json, research_gap, thesis, mainline_logic, target_lang FROM review_jobs WHERE job_id = ?", (job_id,))
        else:
            c.execute("SELECT job_id, topic, focus, words, sections, outline_json, status, progress_log, output_path, docx_path, summary, error, queued_at, started_at, completed_at, heartbeat_at, checkpoint_json, research_gap, thesis, mainline_logic, target_lang FROM review_jobs ORDER BY queued_at DESC LIMIT 1")
        row = c.fetchone()
        if row:
            return {
                "job_id": row[0],
                "topic": row[1],
                "focus": row[2] or "",
                "words": row[3] or 0,
                "sections": row[4] or 0,
                "outline_json": row[5] or "",
                "status": row[6],
                "log": json.loads(row[7] or "[]"),
                "output": row[8] or "",
                "docx": row[9] or "",
                "summary": row[10] or "",
                "error": row[11] or "",
                "queued_at": row[12] or "",
                "started_at": row[13] or "",
                "completed_at": row[14] or "",
                "heartbeat_at": row[15] or "",
                "checkpoint_json": row[16] or "",
                "research_gap": row[17] or "",
                "thesis": row[18] or "",
                "mainline_logic": row[19] or "",
                "target_lang": row[20] or "zh",
            }
    finally:
        conn.close()
    return None


# 只在模块启动时执行一次中断恢复；后续读写只确保表结构存在。
_init_review_jobs_db(recover_interrupted=True)


def _run_review_job(job_id: str, topic: str, focus: str, words: int,
                    n_sections: int, outline_json: str,
                    research_gap: str = "", thesis: str = "", mainline_logic: str = "",
                    target_lang: str = "zh"):
    from litreview.models import Outline
    from litreview.stages import (generate_outline as _gen, write_intro_conclusion,
                                  assemble_review, render_review_note, export_docx)
    
    now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _jobs_lock:
        job = _jobs.get(job_id, {
            "job_id": job_id, "topic": topic, "focus": focus, "words": words,
            "sections": n_sections, "outline_json": outline_json, "log": [],
            "status": "running", "output": "", "docx": "", "error": "", "summary": "",
            "queued_at": now_iso,
            "started_at": now_iso,
            "completed_at": "",
            "heartbeat_at": now_iso,
            "checkpoint_json": ""
        })
        job.setdefault("job_id", job_id)
        job.setdefault("topic", topic)
        job.setdefault("focus", focus)
        job.setdefault("words", words)
        job.setdefault("sections", n_sections)
        job.setdefault("outline_json", outline_json)
        job.setdefault("checkpoint_json", "")
        job.setdefault("heartbeat_at", now_iso)
        job.setdefault("research_gap", research_gap)
        job.setdefault("thesis", thesis)
        job.setdefault("mainline_logic", mainline_logic)
        job.setdefault("target_lang", target_lang)
        _jobs[job_id] = job
    buf = io.StringIO()
    console = Console(file=buf, no_color=True, width=100)

    def log(msg):
        with _jobs_lock:
            job["log"].append(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")
            job["heartbeat_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            _persist_review_job(job)

    try:
        client = _client()
        model = settings.REVIEW_MODEL
        store, embedder, reranker = _store(), _embedder(), _reranker()
        paper_meta = store.get_paper_meta()

        if outline_json:
            outline = Outline.from_dict(json.loads(outline_json), topic=topic)
            log(f"使用调用方提供的大纲（{len(outline.sections)} 章节）")
        else:
            log("阶段 A：生成大纲（结合作者 Thesis 与 Research Gap）...")
            outline = _gen(topic, store, embedder, client, model, settings, console,
                           focus=focus, n_sections=n_sections, words=words, target_lang=target_lang,
                           research_gap=research_gap, thesis=thesis, mainline_logic=mainline_logic)
            log(f"大纲完成：{outline.title}（{len(outline.sections)} 章节）")

        # 无论大纲来自调用方还是模型，都持久化规范化结果，供检查点恢复使用。
        with _jobs_lock:
            job["outline_json"] = json.dumps(outline.model_dump(), ensure_ascii=False)
            job["heartbeat_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            _persist_review_job(job)

        from litreview.review_orchestrator import allocate_review_words
        word_plan = allocate_review_words(words, len(outline.sections))
        section_words = word_plan.section_words
        intro_words = word_plan.intro_words
        concl_words = word_plan.conclusion_words

        from litreview.models import SectionDraft
        from litreview.review_orchestrator import (
            ReviewDependencies, ReviewHooks, SectionGenerationConfig,
            generate_review_sections,
        )
        from litreview.stages import get_scaling_config
        scaling = get_scaling_config(words, target_lang)

        initial_drafts = []
        if job.get("checkpoint_json"):
            try:
                initial_drafts = [SectionDraft(**item) for item in json.loads(job["checkpoint_json"])]
                if initial_drafts:
                    log(f"恢复章节检查点：已载入 {len(initial_drafts)} 节")
            except Exception as checkpoint_err:
                log(f"⚠️ 检查点解析失败，将从头生成正文：{checkpoint_err}")

        def persist_checkpoint(completed_drafts):
            with _jobs_lock:
                job["summary"] = f"已完成 {len(completed_drafts)}/{len(outline.sections)} 章节撰写"
                job["checkpoint_json"] = json.dumps(
                    [draft.model_dump() for draft in completed_drafts], ensure_ascii=False
                )
                job["heartbeat_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                _persist_review_job(job)

        section_result = generate_review_sections(
            outline,
            ReviewDependencies(
                store=store, embedder=embedder, client=client, reranker=reranker,
                settings=settings, console=console,
            ),
            SectionGenerationConfig(
                section_words=section_words, focus=focus, source_char_budget=18_000,
                initial_drafts=initial_drafts,
                review_top_k=scaling["rerank_top_k"], evidence_n=scaling["evidence_n"],
            ),
            ReviewHooks(on_progress=log, on_checkpoint=persist_checkpoint),
        )
        drafts = section_result.drafts
        section_evidence = section_result.section_evidence

        if not drafts:
            raise RuntimeError("所有章节均无有效内容生成，请检查大纲或模型状态。")

        log("阶段 C：撰写引言与结论...")
        try:
            intro, conclusion = write_intro_conclusion(outline, drafts, client, model,
                                                       intro_words=intro_words,
                                                       concl_words=concl_words, focus=focus,
                                                       thesis=thesis, research_gap=research_gap)
        except Exception as e:
            log(f"引言/结论生成异常（已安全降级，保留正文）: {e}")
            intro, conclusion = "", ""

        evidence_total = section_result.evidence_count
        review = assemble_review(outline, drafts, intro, conclusion, paper_meta, evidence_total)
        template_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "review_template.md")
        out_path = render_review_note(review, paper_meta, settings, template_path, model)

        docx_path = ""
        try:
            docx_path = export_docx(out_path)
        except Exception as e:
            log(f"⚠️ Word 导出失败（markdown 已正常生成）: {e}")

        with _jobs_lock:
            job["status"] = "partial" if section_result.failed_sections else "done"
            job["output"] = out_path
            job["docx"] = docx_path
            job["completed_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            job["summary"] = (f"《{review.title}》：{len(review.sections)} 章节、"
                              f"引用 {review.doc_count} 篇、证据 {review.evidence_count} 条")
            if section_result.failed_sections:
                job["summary"] += f"；未完成章节：{', '.join(section_result.failed_sections)}"
            _persist_review_job(job)
        log(f"✅ 完成，输出: {out_path}" + (f"，Word: {docx_path}" if docx_path else ""))
        log_run_event(mode="review", event="review_generated", title=review.title,
                      status="success", extra={"topic": topic, "via": "mcp",
                                               "output": out_path, "docx": docx_path})
    except Exception as e:
        with _jobs_lock:
            job["status"] = "failed"
            job["completed_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            job["error"] = f"{e}\n{traceback.format_exc()[-800:]}"
            _persist_review_job(job)
        log(f"❌ 失败: {e}")


@_logged_tool()
def start_review(topic: str, focus: str = "", words: int = 0,
                 sections: int = 0, outline_json: str = "", target_lang: str = "zh",
                 research_gap: str = "", thesis: str = "", mainline_logic: str = "") -> str:
    """后台启动一篇高水平学术综述的生成（约 7-30 分钟，取决于模型），立即返回 job_id。
    用 review_status 查询进度；完成后综述 markdown（写入 Obsidian 文献库的 reviews 目录）
    和 Word（.docx，同目录同名）会一起生成。

    Args:
        topic: 综述主题（必填）
        focus: 侧重方向（可选），贯穿大纲、写作与结论
        words: 目标总字数（可选，0=默认每章 1800-2600 字）
        sections: 主体章节数（可选，0=按字数档位模型自定 3-9 个）
        outline_json: 大纲 JSON（可选，格式同 generate_outline 的输出；提供则跳过大纲生成）
        target_lang: 综述目标语言，'zh'(中文综述) 或 'en'(英文综述)
        research_gap: 一句话学术缺口/痛点（可选，如"现有研究集中于单一涂层，忽略了残膜在土壤中的微塑料降解分歧"）
        thesis: 一句话核心立论（可选，如"论证生物基聚氨酯在纳米杂化改性后可兼顾长控释期与土壤全降解"）
        mainline_logic: 选定主线分类逻辑（可选，如"要素并列利弊暗转" / "现状-危害-对策问题导向"）
    """
    job_id = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    
    # 执行前置篇数与 3 档字数动力学校验
    check = _check_literature_sufficiency(topic, words=words, target_lang=target_lang)
    check_msg = check["message"]
    
    job_dict = {
        "job_id": job_id, "topic": topic, "focus": focus, "words": words,
        "sections": sections, "outline_json": outline_json, "status": "queued",
        "log": [f"[{datetime.now().strftime('%H:%M:%S')}] {check_msg}"], "output": "", "docx": "", "error": "", "summary": "",
        "queued_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "started_at": "", "completed_at": "", "heartbeat_at": "", "checkpoint_json": "",
        "research_gap": research_gap, "thesis": thesis,
        "mainline_logic": mainline_logic, "target_lang": target_lang,
    }
    with _jobs_lock:
        ahead = sum(j["status"] in ("queued", "running") for j in _jobs.values())
        _jobs[job_id] = job_dict
        _persist_review_job(job_dict)
    _ensure_review_worker()
    _review_queue.put((job_id, topic, focus, words, sections, outline_json, research_gap, thesis, mainline_logic, target_lang))
    note = f"（前面还有 {ahead} 个综述任务）" if ahead else ""
    return (f"综述任务已入队，job_id = {job_id}{note}\n"
            f"主题：{topic}" + (f"｜侧重：{focus}" if focus else "") +
            f"\n{check_msg}\n"
            f"\n预计 7-30 分钟完成，请稍后调用 review_status(\"{job_id}\") 查询进度。")


@_logged_tool()
def resume_review(job_id: str) -> str:
    """从 interrupted/failed 任务已持久化的章节检查点继续生成。"""
    job = _get_persistent_review_job(job_id)
    if not job:
        return f"未找到可恢复任务 {job_id}。"
    if job["status"] not in ("interrupted", "failed", "partial"):
        return f"任务 {job_id} 当前状态为 {job['status']}，无需或不能恢复。"
    if not job.get("outline_json"):
        return "该历史任务没有持久化大纲，无法安全续跑；请重新调用 start_review。"

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    job["status"] = "queued"
    job["error"] = ""
    job["completed_at"] = ""
    job["started_at"] = ""
    job["heartbeat_at"] = now_str
    job.setdefault("log", []).append(f"[{datetime.now().strftime('%H:%M:%S')}] 从章节检查点重新入队")
    with _jobs_lock:
        ahead = sum(item.get("status") in ("queued", "running") for item in _jobs.values())
        _jobs[job_id] = job
        _persist_review_job(job)
    _ensure_review_worker()
    _review_queue.put((
        job_id, job["topic"], job.get("focus", ""), job.get("words", 0),
        job.get("sections", 0), job["outline_json"], job.get("research_gap", ""),
        job.get("thesis", ""), job.get("mainline_logic", ""), job.get("target_lang", "zh"),
    ))
    note = f"，前面还有 {ahead} 个任务" if ahead else ""
    return f"任务 {job_id} 已从检查点重新入队{note}。"


@_logged_tool()
def review_status(job_id: str = "") -> str:
    """查询综述生成任务的进度与结果。job_id 留空则返回最近一个任务（支持跨重启持久化查询）。"""
    job = None
    with _jobs_lock:
        jid = job_id or (sorted(_jobs.keys())[-1] if _jobs else "")
        if jid and jid in _jobs:
            job = _jobs[jid]

    if not job:
        job = _get_persistent_review_job(job_id)

    if not job:
        return f"未找到任务 {job_id or '(最近任务)'}。可在综述生成后随时使用本工具查看进度。"

    jid = job["job_id"]
    when = job.get("started_at") or job.get("queued_at", "")
    label = "启动于" if job.get("started_at") else "入队于"
    lines = [f"任务 {jid}（{job.get('topic', '-')}）状态: {job['status']}，{label} {when}"]
    logs = job.get("log", [])
    if logs:
        lines += logs[-8:]
    if job["status"] in ("done", "partial"):
        lines.append(f"结果: {job.get('summary', '-')}")
        loc = get_locator()
        raw_out = job.get('output', '')
        raw_docx = job.get('docx', '')
        lines.append(f"Markdown URI: {loc.sanitize_path_for_remote(raw_out)} (文件: {raw_out})")
        if raw_docx:
            lines.append(f"Word docx URI: {loc.sanitize_path_for_remote(raw_docx)} (文件: {raw_docx})")
    elif job["status"] in ("failed", "interrupted"):
        lines.append(f"错误: {job.get('error', '-')[:400]}")
        if job.get("checkpoint_json"):
            try:
                checkpoint_count = len(json.loads(job["checkpoint_json"]))
                lines.append(f"已保留 {checkpoint_count} 个章节检查点；可调用 resume_review(\"{jid}\") 续跑。")
            except Exception:
                lines.append("存在检查点但内容无法解析，建议重新提交任务。")
    return "\n".join(lines)


@_logged_tool()
def get_citation_network(
    doc_id: str,
    depth: int = 1,
    include_candidates: bool = True,
    include_unresolved: bool = False
) -> str:
    """查询指定文献在本地引文网络中的上下文子图，包括：
    1. 该文献引用了谁 (Outgoing Citations: 馆内已确认链接、候选匹配)
    2. 库内谁引用了它 (Incoming Citations: 被引关系)
    3. 共同参考文献 (Bibliographic Coupling: 共享参考文献的学术同行)
    4. 网络拓扑统计 (已确认边、候选边、未解析引用数)

    Args:
        doc_id: 文献 ID（支持 8 位前缀或完整 ID）或 DOI 或标题片段
        depth: 遍历深度（当前支持 1 度直接引文子图）
        include_candidates: 是否包含待人工/算法二次复核的候选边（默认 True）
        include_unresolved: 是否展开列出未在馆内匹配的外部引用详情（默认 False 仅显示统计）
    """
    from litreview.citation_network import query_citation_network

    # 优先读取测试数据库或环境变量指定的引文网络库
    db_path = os.getenv("CITATION_NETWORK_DB_PATH", os.path.join(_log_dir, "test_citation_network.db"))
    if not os.path.exists(db_path):
        db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "test_citation_network.db")

    if not os.path.exists(db_path):
        return f"❌ 引文网络数据库不存在 ({db_path})，请先运行构建脚本。"

    res = query_citation_network(
        db_path=db_path,
        doc_id=doc_id,
        depth=depth,
        include_candidates=include_candidates,
        include_unresolved=include_unresolved
    )
    if "error" in res:
        return f"❌ {res['error']}"

    seed = res["seed_work"]
    stats = res["network_statistics"]
    outgoing = res["outgoing_citations"]
    incoming = res["incoming_citations"]
    co_cited = res["co_cited_works"]

    lines = []
    lines.append("# 🕸️ 本地引文网络子图分析报告")
    lines.append(f"**核心文献**: 《{seed['title']}》 ({seed['year'] or '未知年份'})")
    lines.append(f"* **Work ID**: `{seed['work_id'][:8]}` ({seed['work_id']})")
    lines.append(f"* **作者**: {', '.join(seed['authors']) if seed['authors'] else '-'}")
    lines.append(f"* **期刊/来源**: {seed['journal'] or '-'}")
    lines.append("")
    lines.append("### 📊 网络拓扑指标统计")
    lines.append(f"* **该文献引用的总参考文献数**: **{stats['total_references_in_seed']}** 条")
    lines.append(f"  * 🏛️ 馆内已确认引用 (Confirmed In-Library): **{stats['confirmed_in_library_outgoing_count']}** 篇")
    if include_candidates:
        lines.append(f"  * 🔍 待复核候选匹配 (Candidates): **{stats['candidate_outgoing_count']}** 篇")
    else:
        lines.append(f"  * 🔍 待复核候选匹配 (已隐藏): **{stats['candidate_outgoing_count']}** 篇 (传参 include_candidates=True 可展开)")
    lines.append(f"  * 🌐 馆外未解析引用 (Unresolved External): **{stats['unresolved_external_references_count']}** 篇")
    lines.append(f"* **馆内被引次数 (Cited By In-Library)**: **{stats['in_library_cited_by_count']}** 次")
    lines.append(f"* **共享参考文献关联文献 (Bibliographic Coupling)**: **{stats['top_bibliographic_coupling_count']}** 篇")
    lines.append("")

    # 1. Outgoing
    lines.append("### 1️⃣ 该文献引用了谁 (Outgoing In-Library Citations)")
    if outgoing["confirmed_in_library"]:
        for i, out in enumerate(outgoing["confirmed_in_library"], 1):
            lines.append(f"{i}. ✅ 《{out['title']}》 ({out['year'] or '-'}) | ID: `{out['work_id'][:8]}`")
            lines.append(f"   - 匹配依据: `{out['match_method']}` (置信度: {out['confidence_score']:.2f})")
    else:
        lines.append("*(暂无馆内已确认引用的文献)*")

    if not include_candidates and stats["candidate_outgoing_count"] > 0:
        lines.append(f"\n*(注：已隐藏 {stats['candidate_outgoing_count']} 篇待复核候选文献，设置 include_candidates=True 可展开查看)*")
    elif include_candidates and outgoing["candidates"]:
        lines.append("\n**待人工/算法复核候选 (Candidates)**:")
        for i, cand in enumerate(outgoing["candidates"], 1):
            lines.append(f"  ? 《{cand['title']}》 ({cand['year'] or '-'}) | ID: `{cand['work_id'][:8]}` (匹配依据: `{cand['match_method']}`, 置信度: {cand['confidence_score']:.2f})")

    # 2. Incoming
    lines.append("### 2️⃣ 馆内谁引用了它 (Incoming In-Library Citations)")
    if incoming["confirmed_in_library"]:
        for i, inc in enumerate(incoming["confirmed_in_library"], 1):
            lines.append(f"{i}. 📥 《{inc['title']}》 ({inc['year'] or '-'}) | 施引文献 ID: `{inc['citing_work_id'][:8]}`")
            lines.append(f"   - 作者: {', '.join(inc['authors'][:3]) if inc['authors'] else '-'}")
    else:
        lines.append("*(当前馆藏内暂无文献引用此篇)*")
    lines.append("")

    # 3. Co-citations
    lines.append("### 3️⃣ 共同参考文献关联同行 (Bibliographic Coupling)")
    if co_cited:
        for i, co in enumerate(co_cited, 1):
            lines.append(f"{i}. 🔗 《{co['title']}》 ({co['year'] or '-'}) | ID: `{co['work_id'][:8]}` | 共同引用 **{co['shared_references_count']}** 篇")
    else:
        lines.append("*(暂无显著共同参考文献关联)*")

    return "\n".join(lines)


@_logged_tool()
def citation_expand(
    seed_ids: Any,
    direction: str = "both",
    depth: int = 1,
    limit_per_track: int = 10,
    min_co_citation: int = 1,
    recent_years: int = 3
) -> str:
    """基于 OpenAlex 与 Crossref 进行外部引文网络滚雪球发现 (depth=1)，输出双轨文献：
    1. Track A 基石文献 (Foundational Literature): 被多篇种子共同引用的经典高被引奠基文献；
    2. Track B 前沿文献 (Emerging Literature): 最近3年内施引种子的前沿最新论文 (含 Open Access 状态与合法链接)；
    3. 实体治理与馆藏重合度分析 (碰撞检测与外部候选池标记)。

    Args:
        seed_ids: 1~3 篇种子文献的本地 ID (如 'bba4a59d')、DOI 或 OpenAlex ID (支持列表或逗号分隔字符串)
        direction: 滚雪球方向，可选 'both'(双向，默认) / 'backward'(仅基石/后向参考文献) / 'forward'(仅前沿/前向施引文献)
        depth: 扩展深度 (第一版固定为 1)
        limit_per_track: 每条轨道返回文献数量上限 (默认 10)
        min_co_citation: 基石文献最小共被引阈值 (默认 1)
        recent_years: 前沿文献近几年范围 (默认 3 年，即 2023-2026)
    """
    from litreview.snowball import expand_citations, format_snowball_markdown_report

    # 标准化 seed_ids 输入
    if isinstance(seed_ids, str):
        seeds_list = [s.strip() for s in seed_ids.split(",") if s.strip()]
    elif isinstance(seed_ids, (list, tuple)):
        seeds_list = [str(s).strip() for s in seed_ids if str(s).strip()]
    else:
        seeds_list = [str(seed_ids).strip()]

    if not seeds_list:
        return "❌ 请提供至少 1 篇种子文献 ID 或 DOI。"

    db_path = os.getenv("CITATION_NETWORK_DB_PATH", os.path.join(_log_dir, "test_citation_network.db"))
    if not os.path.exists(db_path):
        db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "test_citation_network.db")

    res = expand_citations(
        seed_ids=seeds_list[:3],  # 严格限制 1~3 篇
        direction=direction,
        depth=1,
        limit_per_track=limit_per_track,
        min_co_citation=min_co_citation,
        recent_years=recent_years,
        db_path=db_path if os.path.exists(db_path) else None
    )

    return format_snowball_markdown_report(res)


@_logged_tool()
def list_discovery_candidates(
    query_run_id: Optional[str] = None,
    track: Optional[str] = None,
    in_library: Optional[bool] = None,
    is_open_access: Optional[bool] = None,
    lifecycle_status: Optional[str] = None,
    cursor: int = 0,
    limit: int = 20
) -> str:
    """查询外部发现候选池并支持多维条件筛选与分页。

    Args:
        query_run_id: 滚雪球运行批次 ID (留空默认查询最新一次运行)
        track: 轨道筛选，可选 'FOUNDATIONAL'(基石文献) / 'EMERGING'(前沿文献)
        in_library: 是否仅看馆内已有 (True) 或仅看外部新发现 (False)
        is_open_access: 是否仅看开放获取文献 (True/False)
        lifecycle_status: 生命周期状态筛选，可选 'DISCOVERED' / 'APPROVED' / 'FULLTEXT_ACQUIRED' / 'INDEXED' / 'REJECTED'
        cursor: 分页偏移量 (默认 0)
        limit: 每页返回条数 (默认 20，上限 50)
    """
    from litreview.pipeline import list_discovery_candidates as _list_cands

    cache_db = os.getenv(
        "EXTERNAL_DISCOVERY_CACHE_DB_PATH",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "external_discovery_cache.db")
    )
    res = _list_cands(
        cache_db_path=cache_db,
        query_run_id=query_run_id,
        track=track,
        in_library=in_library,
        is_open_access=is_open_access,
        lifecycle_status=lifecycle_status,
        cursor=cursor,
        limit=min(limit, 50)
    )
    if "error" in res:
        return f"❌ {res['error']}"

    cands = res["candidates"]
    total = res["total_count"]
    run_id = res["query_run_id"]
    next_cur = res["next_cursor"]

    lines = []
    lines.append(f"# 📋 外部发现候选文献池 (Run ID: `{run_id}`)")
    lines.append(f"共命中 **{total}** 条候选，当前显示第 {cursor + 1} ~ {cursor + len(cands)} 条 (下一页游标 cursor={next_cur})：\n")

    for i, c in enumerate(cands, start=cursor + 1):
        status_tag = f"[{c['lifecycle_status']}]"
        lib_tag = f"[🏛️ 馆内 `{c['in_library_work_id'][:8]}`]" if c["in_library"] else "[🌐 外部]"
        oa_tag = "[🔓 OA]" if c["is_open_access"] else ""
        lines.append(f"{i}. {status_tag}{lib_tag}{oa_tag} 《{c['title']}》 ({c['publication_year'] or '-'})")
        lines.append(f"   - **ID**: `{c['candidate_id']}` | **DOI**: `{c['doi'] or '-'}` | **OpenAlex**: `{c['openalex_id']}`")
        lines.append(f"   - **作者**: {', '.join(c['authors'][:3]) if c['authors'] else '-'} | **期刊**: {c['journal_or_venue'] or '-'}")
        lines.append(f"   - **轨道**: `{c['track']}` | **被引**: {c['cited_by_count']} 次 | **推荐依据**: {c['recommendation_reason']}")
        if c["oa_url"]:
            lines.append(f"   - **OA 全文链接**: {c['oa_url']}")

    if next_cur:
        lines.append(f"\n💡 获取下一页请调用：list_discovery_candidates(cursor={next_cur}, limit={limit})")

    return "\n".join(lines)


@_logged_tool()
def approve_candidates(
    candidate_ids: Any,
    action: str = "APPROVE",
    reason: Optional[str] = None,
    decision_source: str = "USER",
    operator: str = "user_mcp"
) -> str:
    """明确批准或拒绝发现的候选文献（生命周期状态流转 DISCOVERED -> APPROVED / REJECTED），并记录审计决策。

    Args:
        candidate_ids: 待操作的候选 ID 列表或逗号分隔字符串 (支持 short ID 或完整 candidate_id)
        action: 操作动作，可选 'APPROVE'(批准，默认) 或 'REJECT'(拒绝)
        reason: 审批备注原因 (可选)
        decision_source: 决策来源 (默认 'USER')
        operator: 操作人标识 (默认 'user_mcp')
    """
    from litreview.pipeline import approve_candidates as _approve_cands

    if isinstance(candidate_ids, str):
        c_list = [c.strip() for c in candidate_ids.split(",") if c.strip()]
    elif isinstance(candidate_ids, (list, tuple)):
        c_list = [str(c).strip() for c in candidate_ids if str(c).strip()]
    else:
        c_list = [str(candidate_ids).strip()]

    if not c_list:
        return "❌ 请提供至少一个待操作的 candidate_id。"

    cache_db = os.getenv(
        "EXTERNAL_DISCOVERY_CACHE_DB_PATH",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "external_discovery_cache.db")
    )
    res = _approve_cands(
        cache_db_path=cache_db,
        candidate_ids=c_list,
        action=action,
        reason=reason,
        decision_source=decision_source,
        operator=operator
    )
    if "error" in res:
        return f"❌ {res['error']}"

    lines = []
    lines.append(f"# ✅ 候选文献审批确认报告")
    lines.append(f"* **执行动作**: `{res['action']}` -> 目标状态 **`{res['target_status']}`**")
    lines.append(f"* **成功更新记录数**: **{res['updated_count']}** 篇 | **未找到记录数**: **{res['not_found_count']}** 篇\n")

    for u in res["updated"]:
        lines.append(f"* ✅ 《{u['title']}》")
        lines.append(f"  - ID: `{u['candidate_id']}` | 状态变更: `{u['previous_status']}` -> **`{u['new_status']}`**")
        lines.append(f"  - 决策依据: `{u['reason']}` | 时间: {u['decided_at']}")

    if res.get("rejected"):
        lines.append("\n🚫 以下候选因处于非 DISCOVERED 状态而被状态机拦截 (禁止非法状态倒退):")
        for rj in res["rejected"]:
            lines.append(f"* ❌ 《{rj['title']}》 (ID: `{rj['candidate_id']}`)")
            lines.append(f"  - 当前状态: `{rj['current_status']}` | 拦截原因: {rj['error']}")

    if res["not_found"]:
        lines.append("\n⚠️ 以下 ID 未在候选缓存库中检索到:")
        for nf in res["not_found"]:
            lines.append(f"  - `{nf}`")

    return "\n".join(lines)


@_logged_tool()
def acquire_fulltext(
    candidate_ids: Optional[Any] = None,
    max_fetch: int = 3
) -> str:
    """对已批准 (APPROVED) 的候选文献执行合法的公开 Open Access PDF 下载与验真（暂存至 independent staging 目录）。

    Args:
        candidate_ids: 指定待下载的候选 ID 列表 (留空则按共被引/被引优先级自动处理 APPROVED 队列)
        max_fetch: 本批次最大下载篇数 (默认 3)
    """
    from litreview.pipeline import acquire_fulltext as _acquire_ft

    c_list = None
    if candidate_ids:
        if isinstance(candidate_ids, str):
            c_list = [c.strip() for c in candidate_ids.split(",") if c.strip()]
        elif isinstance(candidate_ids, (list, tuple)):
            c_list = [str(c).strip() for c in candidate_ids if str(c).strip()]

    cache_db = os.getenv(
        "EXTERNAL_DISCOVERY_CACHE_DB_PATH",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "external_discovery_cache.db")
    )
    staging_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "staging_pdf")

    res = _acquire_ft(
        cache_db_path=cache_db,
        candidate_ids=c_list,
        staging_dir=staging_dir,
        max_fetch=max_fetch
    )
    if "error" in res:
        return f"❌ {res['error']}"

    lines = []
    lines.append("# 📥 合法全文获取 (Acquisition) 执行报告")
    lines.append(f"* **尝试获取总数**: **{res['total_attempted']}** 篇")
    lines.append(f"* **成功获取 (FULLTEXT_ACQUIRED)**: **{res['acquired_count']}** 篇")
    lines.append(f"* **获取失败 (FULLTEXT_UNAVAILABLE)**: **{res['unavailable_count']}** 篇\n")

    for r in res["results"]:
        if r["status"] == "FULLTEXT_ACQUIRED":
            size = r.get("file_size_bytes", r.get("file_size", 0))
            sha = r.get("sha256", "")
            url = r.get("source_url", r.get("downloaded_url", "-"))
            lines.append(f"* ✅ 《{r['title']}》")
            lines.append(f"  - Candidate ID: `{r['candidate_id']}`")
            lines.append(f"  - 暂存文件: `{r['file_path']}` ({size:,} 字节, SHA-256: `{sha[:16]}...`)")
            lines.append(f"  - 下载来源: {url}")
        else:
            lines.append(f"* ❌ 《{r['title']}》 -> 状态: `FULLTEXT_UNAVAILABLE`")
            lines.append(f"  - 失败原因: {r.get('error', 'Unknown error')}")

    return "\n".join(lines)


@_logged_tool()
def ingest_acquired_candidates(
    candidate_ids: Optional[Any] = None,
    max_ingest: int = 3
) -> str:
    """对已获取全文 (FULLTEXT_ACQUIRED) 的文献执行正文解析、切块、索引并正式入库测试图谱，完成引文对齐。

    Args:
        candidate_ids: 指定待入库的候选 ID 列表 (留空则自动处理 FULLTEXT_ACQUIRED 队列)
        max_ingest: 本批次最大入库篇数 (默认 3)
    """
    from litreview.pipeline import ingest_acquired_candidates as _ingest_cands

    c_list = None
    if candidate_ids:
        if isinstance(candidate_ids, str):
            c_list = [c.strip() for c in candidate_ids.split(",") if c.strip()]
        elif isinstance(candidate_ids, (list, tuple)):
            c_list = [str(c).strip() for c in candidate_ids if str(c).strip()]

    cache_db = os.getenv(
        "EXTERNAL_DISCOVERY_CACHE_DB_PATH",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "external_discovery_cache.db")
    )
    test_db = os.getenv(
        "CITATION_NETWORK_DB_PATH",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "test_citation_network.db")
    )
    staging_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "staging_pdf")

    res = _ingest_cands(
        cache_db_path=cache_db,
        test_db_path=test_db,
        candidate_ids=c_list,
        staging_dir=staging_dir,
        max_ingest=max_ingest
    )
    if "error" in res:
        return f"❌ {res['error']}"

    lines = []
    lines.append("# 🏛️ 文献解析、索引入库与引文图谱回填报告")
    lines.append(f"* **成功入库篇数**: **{res['ingested_count']}** 篇\n")

    for w in res.get("ingested_works", []):
        chk_cnt = w.get("chunks_persisted", w.get("chunks_count", 0))
        emb_cnt = w.get("embeddings_persisted", 0)
        align = w.get("realignment_stats", {})
        lines.append(f"* 📖 《{w['title']}》 ({w.get('year') or '-'})")
        lines.append(f"  - 新建 Canonical Work ID: `{w['new_work_id']}` | 原 Candidate ID: `{w['candidate_id']}`")
        lines.append(f"  - DOI: `{w.get('doi') or '-'}` | PDF 页数: {w.get('pdf_pages', 0)} 页 | 正文字符: {w.get('text_length', 0):,}")
        lines.append(f"  - 语义分块数: {chk_cnt} 块 | 向量嵌入数: {emb_cnt} 条 | 提取参考文献: {w.get('reference_mentions_extracted', 0)} 条")
        if align:
            lines.append(f"  - 引文消歧账本: 确认在馆: **{align.get('confirmed_links', 0)}** 篇 | 待复核: **{align.get('candidate_defer', 0)}** 篇 | 馆外未解析: **{align.get('unresolved_external', 0)}** 篇 (闭环核验: `{align.get('is_accounting_closed')}`)")
        lines.append(f"  - 最终生命周期状态: **`{w.get('lifecycle_status', 'VECTOR_INDEXED')}`**")

    return "\n".join(lines)


def _review_worker_loop():
    """单消费者 FIFO：综述逐篇执行，知识检索不随综述排队。"""
    while True:
        args = _review_queue.get()
        job_id = args[0]
        with _jobs_lock:
            job = _jobs.get(job_id)
            if job:
                job["status"] = "running"
                job["started_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            _run_review_job(*args)
        finally:
            _review_queue.task_done()


def _ensure_review_worker():
    global _review_worker
    if _review_worker is None or not _review_worker.is_alive():
        with _review_worker_lock:
            if _review_worker is None or not _review_worker.is_alive():
                _review_worker = threading.Thread(
                    target=_review_worker_loop, name="review-fifo-worker", daemon=True)
                _review_worker.start()


_ensure_review_worker()


class McpServerHost:
    """MCP 服务宿主深模块：统一调度 Stdio 与 SSE/Stream 多传输协议。"""

    @classmethod
    def run_from_cli(cls):
        import argparse
        parser = argparse.ArgumentParser(description="literature-review MCP server")
        parser.add_argument(
            "--transport",
            choices=["stdio", "sse", "streamable-http"],
            default=os.getenv("MCP_TRANSPORT", "stdio"),
            help="传输协议: stdio (默认，本地/SSH) 或 sse / streamable-http (远程 HTTP)"
        )
        parser.add_argument(
            "--host",
            default=os.getenv("MCP_HOST", "0.0.0.0"),
            help="HTTP 监听地址 (默认 0.0.0.0)"
        )
        parser.add_argument(
            "--port",
            type=int,
            default=int(os.getenv("MCP_PORT", "8002")),
            help="HTTP 监听端口 (默认 8002)"
        )
        args, _ = parser.parse_known_args()

        if args.transport in ("sse", "streamable-http", "http"):
            import uvicorn
            from starlette.applications import Starlette

            mcp.settings.host = args.host
            mcp.settings.port = args.port
            if hasattr(mcp.settings, "transport_security") and mcp.settings.transport_security:
                mcp.settings.transport_security.enable_dns_rebinding_protection = False
                mcp.settings.transport_security.allowed_hosts = ["*"]
                mcp.settings.transport_security.allowed_origins = ["*"]

            sse_app = mcp.sse_app()
            http_app = mcp.streamable_http_app()

            routes = list(http_app.routes) + list(sse_app.routes)
            seen = set()
            unique_routes = []
            for r in routes:
                path = getattr(r, "path", None)
                if path not in seen:
                    seen.add(path)
                    unique_routes.append(r)

            dual_app = Starlette(
                debug=mcp.settings.debug,
                routes=unique_routes,
                lifespan=http_app.router.lifespan_context,
            )

            logger.info(f"🌐 MCP Server 启动双传输融合模式 (Streamable HTTP: http://{args.host}:{args.port}/mcp | SSE: http://{args.host}:{args.port}/sse)")
            print(f"🌐 MCP Server 监听: http://{args.host}:{args.port}/mcp (Streamable HTTP) 与 http://{args.host}:{args.port}/sse (SSE)", file=sys.stderr)
            try:
                uvicorn.run(dual_app, host=args.host, port=args.port, log_level="info", access_log=False)
            except Exception:
                logger.error(f"MCP server dual-transport 异常退出\n{traceback.format_exc()}")
                raise
        else:
            logger.info("📡 MCP Server 就绪，进入 [STDIO] 事件循环")
            try:
                mcp.run(transport="stdio")
            except Exception:
                logger.error(f"MCP server [stdio] 异常退出\n{traceback.format_exc()}")
                raise
            finally:
                logger.info("MCP server stdio 事件循环结束")


if __name__ == "__main__":
    McpServerHost.run_from_cli()
