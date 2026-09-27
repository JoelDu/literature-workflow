import sqlite3
"""web_server.py — 现代轻量 Web UI 后端服务 (Starlette + Uvicorn).

功能一览：
- 静态页面托管与现代化 SPA 交互
- 文献库检索、多维度筛选、元数据详情与级联安全删除
- 拖拽多文件上传 (PDF / EPUB / 教材) 与一键入库流水线
- 语义向量检索 + 重排序在线调试
- AI 综述大纲生成、在线编辑与完整综述撰写 (支持 SSE 实时日志与进度推送)
- 综述在线 Markdown 预览与实时 Word (.docx) / Markdown (.md) 下载
- 系统状态概览、向量索引构建、API Key 连通性测试与在线配置
"""
import os
import re
import sys
import json
import time
import queue
import shutil
import asyncio
import logging
import threading
import traceback
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, List

from dotenv import load_dotenv, dotenv_values, set_key
load_dotenv()

from starlette.applications import Starlette
from starlette.responses import JSONResponse, HTMLResponse, FileResponse, StreamingResponse, Response
from starlette.routing import Route, Mount
from starlette.staticfiles import StaticFiles
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.requests import Request
import uvicorn

from utils import get_settings, log_run_event, calculate_pdf_hash
from litreview import doctype as dt
from db_schema import ensure_papers_table
from litreview.library_store import (
    get_dashboard_stratified_stats,
    list_library_documents,
    get_document_full_detail,
    list_discovery_candidates_paginated
)
from litreview.citation_network import (
    query_citation_network,
    get_full_citation_graph,
    get_seed_subgraph
)
from litreview.guard import RemoteAccessGuardMiddleware, cors_origins
from litreview.safe_paths import safe_basename, resolve_within, is_valid_candidate_id

# ── 日志配置 ──
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("web_server")

# ── 全局状态与任务队列 ──
_jobs_lock = threading.Lock()
_jobs: Dict[str, Dict[str, Any]] = {}
_active_pipelines: Dict[str, Dict[str, Any]] = {}

_state_lock = threading.RLock()
_state = {
    "generation_client": None,
    "embedding_client": None,
    "store": None,
    "embedder": None,
    "reranker": None
}

def get_app_settings():
    """获取强类型配置，缺失时自动提供默认值避免阻断 Web UI 启动与设置页面"""
    import utils
    if utils._settings is not None:
        return utils._settings
    
    # 确保必要字段有占位值，保证初次启动无需预先手动填写 .env 也能访问 Web 端
    if not os.getenv("MINERU_API_KEY"):
        os.environ["MINERU_API_KEY"] = "placeholder_key"
    if not os.getenv("OPENAI_API_KEY"):
        os.environ["OPENAI_API_KEY"] = "placeholder_key"
        
    return utils.get_settings()

def _get_store():
    settings = get_app_settings()
    if _state["store"] is None:
        with _state_lock:
            if _state["store"] is None:
                from litreview.retrieval_store import create_retrieval_store
                _state["store"] = create_retrieval_store(settings, readonly=True)
    return _state["store"]

def _get_client():
    if _state["generation_client"] is None:
        with _state_lock:
            if _state["generation_client"] is None:
                from llm_router import make_openai_client
                _state["generation_client"], _ = make_openai_client(quiet=True)
    return _state["generation_client"]

def _get_embedding_client():
    if _state["embedding_client"] is None:
        with _state_lock:
            if _state["embedding_client"] is None:
                key = os.getenv("SILICONFLOW_API_KEY", "")
                base = os.getenv("SILICONFLOW_API_BASE", "https://api.siliconflow.cn/v1")
                if not key:
                    raise RuntimeError("未配置 SILICONFLOW_API_KEY，无法进行向量嵌入")
                import httpx
                from openai import OpenAI
                _state["embedding_client"] = OpenAI(
                    api_key=key, base_url=base, timeout=180,
                    http_client=httpx.Client(timeout=180, trust_env=False)
                )
    return _state["embedding_client"]

def _get_embedder():
    settings = get_app_settings()
    if _state["embedder"] is None:
        with _state_lock:
            if _state["embedder"] is None:
                from litreview.embedder import Embedder
                _state["embedder"] = Embedder(
                    _get_embedding_client(), settings.EMBEDDING_MODEL, settings.EMBEDDING_DIM,
                    settings.EMBEDDING_BATCH_SIZE, settings.EMBEDDING_QUERY_INSTRUCTION
                )
    return _state["embedder"]

def _get_reranker():
    settings = get_app_settings()
    if _state["reranker"] is None and settings.RERANK_ENABLE:
        with _state_lock:
            if _state["reranker"] is None:
                from litreview.reranker import Reranker
                _state["reranker"] = Reranker(settings.RERANK_MODEL)
    return _state["reranker"]


# ── API 路由实现 ─────────────────────────────────────────────────────────────

async def api_status(request: Request) -> JSONResponse:
    """获取系统状态、文献库统计、向量索引与元数据概况"""
    settings = get_app_settings()
    store = _get_store()
    stats = store.stats()
    
    # 数据库额外统计
    conn = store._conn()
    try:
        n_details = conn.execute("SELECT COUNT(*) FROM paper_details").fetchone()[0]
        n_assets = conn.execute("SELECT COUNT(*) FROM paper_assets").fetchone()[0]
        n_refs = conn.execute("SELECT COUNT(*) FROM paper_references").fetchone()[0]
        latest_row = conn.execute("SELECT MAX(processed_at) FROM papers").fetchone()
        latest = latest_row[0] if latest_row and latest_row[0] else None
    except Exception as e:
        logger.error(f"查询统计异常: {e}")
        n_details, n_assets, n_refs, latest = 0, 0, 0, None
    finally:
        conn.close()

    type_counts = store.doc_type_counts()
    
    # API 配置检查
    has_mineru = bool(os.getenv("MINERU_API_KEY") and os.getenv("MINERU_API_KEY") != "your_mineru_api_key_here")
    has_siliconflow = bool(os.getenv("SILICONFLOW_API_KEY") and os.getenv("SILICONFLOW_API_KEY") != "your_siliconflow_key")
    has_openai = bool(os.getenv("OPENAI_API_KEY") and os.getenv("OPENAI_API_KEY") != "your_openai_api_key_here")
    
    # 待处理文献目录统计
    input_pdf_count = len(list(Path(settings.INPUT_PDF_DIR).glob("*.pdf"))) if os.path.exists(settings.INPUT_PDF_DIR) else 0
    input_book_count = len(list(Path(settings.BOOK_INPUT_DIR).glob("*.*"))) if os.path.exists(settings.BOOK_INPUT_DIR) else 0

    return JSONResponse({
        "success": True,
        "data": {
            "library": {
                "exported_docs": stats.get("exported_docs", 0),
                "indexed_docs": stats.get("indexed_docs", 0),
                "pending_docs": stats.get("pending_docs", 0),
                "chunks": stats.get("chunks", 0),
                "embedded_chunks": stats.get("embedded_chunks", 0),
                "details_count": n_details,
                "assets_count": n_assets,
                "refs_count": n_refs,
                "latest_processed": latest,
                "type_counts": type_counts,
                "doc_types_schema": {k: dt.label(k) for k in dt.all_types()},
                "input_pending": {
                    "pdfs": input_pdf_count,
                    "books": input_book_count
                }
            },
            "config": {
                "run_mode": settings.RUN_MODE,
                "analysis_model": settings.ANALYSIS_MODEL,
                "review_model": settings.REVIEW_MODEL,
                "embedding_model": settings.EMBEDDING_MODEL,
                "rerank_model": settings.RERANK_MODEL,
                "rerank_enabled": settings.RERANK_ENABLE,
                "api_keys": {
                    "mineru_configured": has_mineru,
                    "siliconflow_configured": has_siliconflow,
                    "openai_configured": has_openai
                }
            }
        }
    })


async def api_papers(request: Request) -> JSONResponse:
    """查询文献列表（支持分页、关键词搜索、文献类型过滤、排序）"""
    params = request.query_params
    query = params.get("q", "").strip()
    doc_type = params.get("type", "all").strip()
    page = max(1, int(params.get("page", "1")))
    page_size = min(100, max(1, int(params.get("page_size", "20"))))
    
    store = _get_store()
    conn = store._conn()
    try:
        where_clauses = ["p.status = 'EXPORTED'"]
        args = []
        
        if doc_type and doc_type != "all":
            where_clauses.append("COALESCE(d.doc_type, 'paper') = ?")
            args.append(doc_type)
            
        if query:
            where_clauses.append(
                "(p.title LIKE ? OR d.title_zh LIKE ? OR d.title_en LIKE ? "
                "OR d.authors LIKE ? OR d.journal LIKE ? OR d.keywords LIKE ? "
                "OR d.patent_no LIKE ? OR d.doc_no LIKE ? OR p.id LIKE ?)"
            )
            pattern = f"%{query}%"
            args.extend([pattern] * 9)
            
        where_sql = " AND ".join(where_clauses)
        
        # 统计总数
        count_sql = f"""
            SELECT COUNT(*)
            FROM papers p
            LEFT JOIN paper_details d ON d.doc_id = p.id
            WHERE {where_sql}
        """
        total = conn.execute(count_sql, args).fetchone()[0]
        
        # 分页查询
        offset = (page - 1) * page_size
        select_sql = f"""
            SELECT p.id, p.title, d.title_zh, d.title_en, d.doi, d.authors, d.journal,
                   d.year, d.keywords, d.n_refs, d.n_figures, d.n_tables,
                   COALESCE(d.doc_type, 'paper') as doc_type,
                   d.patent_no, d.filing_date, d.legal_status, d.status_checked_at,
                   d.publisher, d.doc_no, d.effective_date, p.processed_at, p.result_json,
                   m.embedded as is_indexed, m.n_chunks
            FROM papers p
            LEFT JOIN paper_details d ON d.doc_id = p.id
            LEFT JOIN review_index_meta m ON m.doc_id = p.id
            WHERE {where_sql}
            ORDER BY p.processed_at DESC, p.id DESC
            LIMIT ? OFFSET ?
        """
        rows = conn.execute(select_sql, args + [page_size, offset]).fetchall()
        
        items = []
        for r in rows:
            (doc_id, raw_title, t_zh, t_en, doi, authors, journal, year,
             kw, n_refs, n_figs, n_tabs, kind, patent_no, filing_date,
             legal_status, checked_at, publisher, doc_no, effective_date,
             processed_at, result_json, is_indexed, n_chunks) = r
            
            tldr = ""
            if result_json:
                try:
                    tldr = json.loads(result_json).get("tldr", "")
                except Exception:
                    pass
                    
            display_title = t_zh or t_en or raw_title or doc_id[:8]
            
            items.append({
                "id": doc_id,
                "short_id": doc_id[:8],
                "title": display_title,
                "raw_title": raw_title,
                "title_zh": t_zh,
                "title_en": t_en,
                "doi": doi or "",
                "authors": authors or "",
                "journal": journal or "",
                "year": year or "",
                "keywords": kw or "",
                "n_refs": n_refs or 0,
                "n_figures": n_figs or 0,
                "n_tables": n_tabs or 0,
                "doc_type": kind,
                "doc_type_label": dt.label(kind),
                "gb_code": dt.gb_code(kind),
                "patent_no": patent_no or "",
                "filing_date": filing_date or "",
                "legal_status": legal_status or "",
                "status_checked_at": checked_at or "",
                "publisher": publisher or "",
                "doc_no": doc_no or "",
                "effective_date": effective_date or "",
                "processed_at": processed_at or "",
                "is_indexed": bool(is_indexed),
                "chunks_count": n_chunks or 0,
                "tldr": tldr
            })
            
        return JSONResponse({
            "success": True,
            "data": {
                "items": items,
                "total": total,
                "page": page,
                "page_size": page_size,
                "total_pages": (total + page_size - 1) // page_size if total > 0 else 1
            }
        })
    finally:
        conn.close()


async def api_paper_detail(request: Request) -> JSONResponse:
    """获取单篇文献的完整详情 (含结构化字段、图表资产、参考文献列表、分块预览)"""
    doc_id = request.path_params["doc_id"]
    store = _get_store()
    conn = store._conn()
    try:
        paper = conn.execute("SELECT * FROM papers WHERE id = ?", (doc_id,)).fetchone()
        if not paper:
            # 尝试前缀匹配
            paper = conn.execute("SELECT * FROM papers WHERE id LIKE ?", (f"{doc_id}%",)).fetchone()
            if not paper:
                return JSONResponse({"success": False, "error": "文献不存在"}, status_code=404)
            doc_id = paper[0]
            
        detail = conn.execute("SELECT * FROM paper_details WHERE doc_id = ?", (doc_id,)).fetchone()
        assets = conn.execute("SELECT * FROM paper_assets WHERE doc_id = ? ORDER BY id ASC", (doc_id,)).fetchall()
        refs = conn.execute("SELECT * FROM paper_references WHERE doc_id = ? ORDER BY ref_index ASC", (doc_id,)).fetchall()
        chunks = conn.execute("SELECT chunk_index, section_title, content, char_len, (embedding IS NOT NULL) as has_emb FROM review_chunks WHERE doc_id = ? ORDER BY chunk_index ASC", (doc_id,)).fetchall()
        
        meta = store.get_paper_meta([doc_id]).get(doc_id, {})
        
        assets_list = []
        for a in assets:
            assets_list.append({
                "id": a[0],
                "asset_type": a[2],
                "asset_number": a[3],
                "caption": a[4],
                "page_idx": a[5],
                "rel_path": a[6]
            })
            
        refs_list = []
        for rf in refs:
            refs_list.append({
                "ref_index": rf[2],
                "raw_text": rf[3],
                "title": rf[4],
                "authors": rf[5],
                "year": rf[6],
                "doi": rf[7]
            })
            
        chunks_list = []
        for c in chunks:
            chunks_list.append({
                "index": c[0],
                "section": c[1],
                "content": c[2],
                "char_len": c[3],
                "embedded": bool(c[4])
            })
            
        return JSONResponse({
            "success": True,
            "data": {
                "id": doc_id,
                "meta": meta,
                "assets": assets_list,
                "references": refs_list,
                "chunks": chunks_list,
                "chunks_count": len(chunks_list)
            }
        })
    finally:
        conn.close()


async def api_paper_delete(request: Request) -> JSONResponse:
    """安全删除文献（级联清理 6 张表、证据缓存、物理文件与 Excel 记录）"""
    doc_id = request.path_params["doc_id"]
    body = await request.json() if request.headers.get("content-type") == "application/json" else {}
    keep_source = body.get("keep_source", True)
    dry_run = body.get("dry_run", False)
    
    settings = get_app_settings()
    store = _get_store()
    hits = store.resolve_docs(doc_id)
    if not hits:
        return JSONResponse({"success": False, "error": f"未找到匹配文献: {doc_id}"}, status_code=404)
        
    target_id = hits[0]
    res = store.delete_doc(target_id, settings=settings, keep_source=keep_source, dry_run=dry_run)
    
    return JSONResponse({
        "success": True,
        "data": res,
        "message": f"{'预演删除' if dry_run else '已成功删除'}: {res.get('title', target_id[:8])}"
    })


async def api_paper_update_type(request: Request) -> JSONResponse:
    """修改文献类型或相关著录元数据"""
    doc_id = request.path_params["doc_id"]
    data = await request.json()
    new_type = data.get("type", "").strip()
    
    kind = dt.normalize(new_type)
    if not kind:
        return JSONResponse({
            "success": False,
            "error": f"不支持的文献类型: {new_type}，可选: {', '.join(dt.all_types())}"
        }, status_code=400)
        
    store = _get_store()
    hits = store.resolve_docs(doc_id)
    if not hits:
        return JSONResponse({"success": False, "error": f"未找到匹配文献: {doc_id}"}, status_code=404)
        
    target_id = hits[0]
    fields = {k: v for k, v in data.get("fields", {}).items() if v}
    store.mark_doc_type(target_id, kind, fields)
    
    return JSONResponse({
        "success": True,
        "message": f"已成功将 {target_id[:8]} 设为 [{dt.gb_code(kind)}] {dt.label(kind)}"
    })


async def api_upload(request: Request) -> JSONResponse:
    """文件上传接口：支持批量上传 PDF/EPUB 文件并保存到对应输入目录"""
    settings = get_app_settings()
    form = await request.form()
    files = form.getlist("files")
    target_type = form.get("target_type", "pdf")  # pdf 或 book
    
    if not files:
        return JSONResponse({"success": False, "error": "未选择任何文件"}, status_code=400)
        
    dest_dir = settings.BOOK_INPUT_DIR if target_type == "book" else settings.INPUT_PDF_DIR
    os.makedirs(dest_dir, exist_ok=True)
    
    saved = []
    skipped = []
    errors = []
    
    for upload in files:
        raw_name = getattr(upload, "filename", "unnamed.pdf")
        if not raw_name:
            continue
        # 客户端文件名只取基名，防止 "../" 或绝对路径写出输入目录
        filename = safe_basename(raw_name)
        if not filename:
            errors.append(f"{raw_name!r}: 非法文件名")
            continue
        ext = os.path.splitext(filename)[1].lower()
        if target_type == "book" and ext not in (".pdf", ".epub"):
            errors.append(f"{filename}: 书籍仅支持 PDF/EPUB 格式")
            continue
        if target_type == "pdf" and ext != ".pdf":
            errors.append(f"{filename}: 论文仅支持 PDF 格式")
            continue
            
        dest_path = resolve_within(dest_dir, filename)
        if not dest_path:
            errors.append(f"{filename}: 目标路径超出上传目录")
            continue

        # 写入临时文件并计算哈希
        try:
            content = await upload.read()
            with open(dest_path, "wb") as f:
                f.write(content)
            saved.append({
                "filename": filename,
                "size": len(content),
                "path": dest_path
            })
        except Exception as e:
            logger.error(f"保存上传文件失败: {filename}, {e}")
            errors.append(f"{filename}: {str(e)}")
            
    return JSONResponse({
        "success": True,
        "data": {
            "saved": saved,
            "skipped": skipped,
            "errors": errors,
            "total_saved": len(saved)
        },
        "message": f"成功上传 {len(saved)} 个文件至 {dest_dir}"
    })


# ── 后台任务与流水线调度 ─────────────────────────────────────────────────────

def _run_intake_worker(task_id: str, mode: str):
    """后台执行文献入库流水线。

    全程持有 litreview.intake_lock 跨进程锁，避免与 daemon 或另一 Web 任务同时扫描
    同一输入目录、重复处理同一 PDF；锁被占用时任务状态置为 busy 并直接返回。
    """
    from litreview.intake_lock import intake_lock, IntakeBusyError
    settings = get_app_settings()
    task = _active_pipelines[task_id]
    
    def log(msg: str):
        task["logs"].append(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")
        logger.info(f"[{task_id}] {msg}")
        
    try:
        with intake_lock(settings.DB_PATH):
            log(f"🚀 启动文献入库流水线 (模式: {mode})...")
            if mode == "realtime":
                import pipeline
                processed = pipeline.main()
                log(f"✔ 实时扫描完成，成功处理 {processed} 篇文献。")
            elif mode == "batch_prepare":
                import batch_pipeline
                report = batch_pipeline.prepare_batch()
                log(f"✔ Batch 任务提交完成：扫描 {report.get('scanned', 0)} 篇，提交云端 {report.get('submitted', 0)} 篇。")
            elif mode == "batch_fetch":
                import batch_pipeline
                report = batch_pipeline.fetch_batch()
                log(f"✔ Batch 结果拉取完成：检查 {report.get('jobs_checked', 0)} 个批次，成功导出 {report.get('exported', 0)} 篇。")
            elif mode == "books":
                from litreview.bookintake import run_scheduled_intake
                from llm_router import make_openai_client
                client = None
                if settings.REVIEW_ENRICH_LLM:
                    client, _ = make_openai_client(quiet=True)
                from rich.console import Console
                import io
                c = Console(file=io.StringIO())
                rep = run_scheduled_intake(settings, c, client=client, use_llm=settings.REVIEW_ENRICH_LLM)
                log(f"✔ 教材入库完成：扫描 {rep.get('scanned',0)} 本，成功 {rep.get('ok',0)} 本，跳过 {rep.get('skipped',0)} 本。")
            else:
                log(f"未知流水线模式: {mode}")
            
            # 自动执行元数据补齐与向量索引
            if settings.REVIEW_AUTO_INDEX:
                log("🔍 触发增量向量索引构建...")
                from litreview.indexer import build_index
                from rich.console import Console
                import io
                client = None
                if settings.REVIEW_EMBED_BACKEND != "local":
                    from openai import OpenAI
                    client = OpenAI(api_key=os.getenv("SILICONFLOW_API_KEY"),
                                    base_url=os.getenv("SILICONFLOW_API_BASE"), timeout=180)
                rep_idx = build_index(settings, Console(file=io.StringIO()), client)
                log(f"✔ 向量索引增量更新完成：新增索引 {rep_idx.get('indexed', 0)} 篇 / {rep_idx.get('chunks', 0)} 块。")
            
            task["status"] = "done"
            log("🎉 流水线执行完毕！")
    except IntakeBusyError as e:
        task["status"] = "busy"
        task["error"] = str(e)
        log(f"⏸ 已有入库流水线在运行（daemon 或另一 Web 任务），本次未执行: {e}")
    except Exception as e:
        task["status"] = "failed"
        task["error"] = str(e)
        log(f"❌ 流水线执行失败: {e}\n{traceback.format_exc()}")


async def api_pipeline_run(request: Request) -> JSONResponse:
    """触发入库流水线（实时模式 / Batch模式 / 教材入库）"""
    data = await request.json() if request.headers.get("content-type") == "application/json" else {}
    mode = data.get("mode", "realtime")  # realtime, batch_prepare, batch_fetch, books
    
    task_id = f"pipe_{int(time.time())}"
    _active_pipelines[task_id] = {
        "id": task_id,
        "mode": mode,
        "status": "running",
        "logs": [],
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }
    
    t = threading.Thread(target=_run_intake_worker, args=(task_id, mode), daemon=True)
    t.start()
    
    return JSONResponse({
        "success": True,
        "data": {"task_id": task_id, "mode": mode},
        "message": f"流水线任务 {task_id} 已在后台启动"
    })


async def api_pipeline_status(request: Request) -> JSONResponse:
    """查看流水线任务进度与日志"""
    task_id = request.path_params.get("task_id")
    if task_id not in _active_pipelines:
        return JSONResponse({"success": False, "error": "任务不存在"}, status_code=404)
    return JSONResponse({"success": True, "data": _active_pipelines[task_id]})


async def api_index_run(request: Request) -> JSONResponse:
    """手动触发向量索引构建"""
    settings = get_app_settings()
    data = await request.json() if request.headers.get("content-type") == "application/json" else {}
    force = data.get("force", False)
    
    def _run_idx():
        from litreview.indexer import build_index
        from rich.console import Console
        import io
        client = None
        if settings.REVIEW_EMBED_BACKEND != "local":
            from openai import OpenAI
            client = OpenAI(api_key=os.getenv("SILICONFLOW_API_KEY"),
                            base_url=os.getenv("SILICONFLOW_API_BASE"), timeout=180)
        c = Console(file=io.StringIO())
        return build_index(settings, c, client, force=force)
        
    loop = asyncio.get_event_loop()
    try:
        report = await loop.run_in_executor(None, _run_idx)
        return JSONResponse({
            "success": True,
            "data": report,
            "message": f"向量索引更新完成：新增 {report.get('indexed', 0)} 篇，共 {report.get('chunks', 0)} 个文本块"
        })
    except Exception as e:
        logger.error(f"索引构建异常: {e}")
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


async def api_enrich_run(request: Request) -> JSONResponse:
    """手动触发元数据提取与补全 (enrich)"""
    settings = get_app_settings()
    data = await request.json() if request.headers.get("content-type") == "application/json" else {}
    force = data.get("force", False)
    
    def _run_enr():
        from litreview.enrich import run_enrich
        from rich.console import Console
        import io
        client = None
        if settings.REVIEW_ENRICH_LLM:
            client = _get_client()
        c = Console(file=io.StringIO())
        return run_enrich(settings, c, client, force=force, use_llm=settings.REVIEW_ENRICH_LLM)
        
    loop = asyncio.get_event_loop()
    try:
        report = await loop.run_in_executor(None, _run_enr)
        return JSONResponse({
            "success": True,
            "data": report,
            "message": f"元数据提取完成：成功提取 {report.get('enriched', 0)} 篇，抓取图表 {report.get('assets', 0)} 项"
        })
    except Exception as e:
        logger.error(f"元数据提取异常: {e}")
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


async def api_search(request: Request) -> JSONResponse:
    """在线语义检索 + 重排序调试"""
    data = await request.json()
    query = data.get("query", "").strip()
    top_k = min(50, max(1, int(data.get("top_k", 8))))
    corpus = data.get("corpus", "all").strip()
    no_rerank = data.get("no_rerank", False)
    
    if not query:
        return JSONResponse({"success": False, "error": "检索问题或关键词不能为空"}, status_code=400)
        
    store = _get_store()
    embedder = _get_embedder()
    reranker = None if no_rerank else _get_reranker()
    
    settings = get_app_settings()
    recall_k = settings.RERANK_CANDIDATES if reranker else top_k
    doc_types = None if corpus == "all" else [corpus]
    
    def _do_search():
        qvec = embedder.embed_query(query)
        res = store.search(qvec, top_k=recall_k, max_per_doc=settings.REVIEW_MAX_CHUNKS_PER_DOC,
                           doc_types=doc_types)
        if not res:
            return []
            
        if reranker:
            ranked = reranker.rerank(query, [r.content for r in res], top_n=top_k)
            new_res = []
            for idx, score in ranked:
                r = res[idx]
                r.score = score
                new_res.append(r)
            return new_res
        return res[:top_k]
        
    loop = asyncio.get_event_loop()
    try:
        results = await loop.run_in_executor(None, _do_search)
        if not results:
            return JSONResponse({"success": True, "data": {"items": [], "query": query}})
            
        doc_ids = list({r.doc_id for r in results})
        meta = store.get_paper_meta(doc_ids)
        
        items = []
        for idx, r in enumerate(results, 1):
            m = meta.get(r.doc_id, {})
            title = m.get("title_zh") or m.get("title_en") or m.get("title", r.doc_id[:8])
            k = m.get("doc_type") or "paper"
            items.append({
                "rank": idx,
                "doc_id": r.doc_id,
                "short_id": r.doc_id[:8],
                "title": title,
                "doc_type": k,
                "doc_type_label": dt.label(k),
                "gb_code": dt.gb_code(k),
                "section": r.section_title or "-",
                "score": round(float(r.score), 4),
                "year": m.get("year") or "-",
                "authors": m.get("authors") or "-",
                "content": r.content
            })
            
        return JSONResponse({
            "success": True,
            "data": {
                "items": items,
                "query": query,
                "total_hits": len(items),
                "reranked": bool(reranker)
            }
        })
    except Exception as e:
        logger.error(f"语义检索异常: {e}")
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


# ── AI 综述生成与导出 ─────────────────────────────────────────────────────────

_review_jobs_loaded_path = None


def _review_job_store():
    from litreview.review_jobs import ReviewJobStore
    path = os.getenv("WEB_REVIEW_JOBS_DB_PATH") or str(
        Path(get_app_settings().DB_PATH).parent / "web_review_jobs.db")
    return ReviewJobStore(path)


def _load_review_jobs():
    # The Web service runs one process; only a new process recovers abandoned jobs.
    global _review_jobs_loaded_path
    store = _review_job_store()
    with _jobs_lock:
        if _review_jobs_loaded_path == store.path:
            return
        paused = []
        for job in store.load_all():
            if job["status"] in ("running", "queued"):
                job["status"] = "interrupted"
                job["current_stage"] = "INTERRUPTED"
                store.save(job)
            _jobs.setdefault(job["job_id"], job)
            if job["status"] == "paused_quota":
                paused.append((job["job_id"], job.get("resume_after")))
        _review_jobs_loaded_path = store.path
    # 新进程为额度暂停的任务重新布置 UTC 零点后的自动续跑定时器。
    from litreview.review_jobs import schedule_quota_resume
    for job_id, resume_after in paused:
        schedule_quota_resume(job_id, resume_after, _auto_resume_review)


def _resume_review_job(job_id: str) -> tuple[bool, str]:
    """把可恢复任务切回 running 并启动 worker；返回 (是否启动, 错误信息)。"""
    from litreview.review_jobs import RESUMABLE_STATUSES, cancel_quota_resume
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            return False, "任务不存在"
        if job["status"] not in RESUMABLE_STATUSES:
            return False, "当前任务不能恢复"
        cancel_quota_resume(job_id)
        job.update(status="running", error="", current_stage="RESUMING", resume_after="")
        for field in ("baseline_verdict", "baseline_status", "baseline_summary", "baseline_error"):
            job[field] = None
        job.setdefault("log", []).append("从已保存的章节检查点恢复")
        _persist_web_review_job(job)
    _start_review_thread(job)
    return True, ""


def _auto_resume_review(job_id: str) -> None:
    try:
        started, error = _resume_review_job(job_id)
        logger.info(f"额度重置后自动续跑综述任务 {job_id}: {'已启动' if started else error}")
    except Exception as exc:  # 定时器线程内不能抛出
        logger.warning(f"自动续跑综述任务 {job_id} 失败: {exc}")


def _persist_web_review_job(job):
    _review_job_store().save(job)


def _start_review_thread(job):
    threading.Thread(
        target=_review_generator_worker,
        args=(job["job_id"], job["topic"], job.get("focus", ""), job.get("words", 0),
              job.get("sections", 0), job.get("outline"), job.get("model_override", job.get("model"))),
        daemon=True,
    ).start()


async def api_review_resume(request: Request) -> JSONResponse:
    _load_review_jobs()
    job_id = request.path_params["job_id"]
    started, error = _resume_review_job(job_id)
    if not started:
        return JSONResponse({"success": False, "error": error},
                            status_code=404 if error == "任务不存在" else 409)
    return JSONResponse({"success": True, "data": {"job_id": job_id, "status": "running"}})


async def api_review_outline(request: Request) -> JSONResponse:
    """生成综述大纲 JSON (供前端展示与编辑)"""
    data = await request.json()
    topic = data.get("topic", "").strip()
    focus = data.get("focus", "").strip()
    sections = int(data.get("sections", 0))
    model_override = data.get("model", "").strip()
    
    if not topic:
        return JSONResponse({"success": False, "error": "综述主题不能为空"}, status_code=400)
        
    settings = get_app_settings()
    store = _get_store()
    embedder = _get_embedder()
    client = _get_client()
    outline_model = model_override or settings.ANALYSIS_MODEL
    
    def _gen():
        from litreview.stages import generate_outline
        from rich.console import Console
        import io
        return generate_outline(topic, store, embedder, client, outline_model,
                                settings, Console(file=io.StringIO()), focus=focus, n_sections=sections,
                                words=int(data.get("words", 0)), target_lang=data.get("target_lang", "zh"),
                                research_gap=data.get("research_gap", ""), thesis=data.get("thesis", ""),
                                mainline_logic=data.get("mainline_logic", ""))
                                
    loop = asyncio.get_event_loop()
    try:
        outline = await loop.run_in_executor(None, _gen)
        return JSONResponse({
            "success": True,
            "data": outline.model_dump()
        })
    except Exception as e:
        logger.error(f"生成大纲异常: {e}")
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


def _review_generator_worker(job_id: str, topic: str, focus: str, words: int,
                            n_sections: int, outline_json: Optional[str],
                            model_override: Optional[str] = None):
    """执行端到端综述生成的后台 Worker 线程（支持自定义模型名称）"""
    from litreview.models import Outline, SectionDraft
    from litreview.stages import (generate_outline as _gen,
                                  write_intro_conclusion, assemble_review, render_review_note,
                                  export_docx)
    from litreview.review_orchestrator import (ReviewDependencies, ReviewHooks,
        SectionGenerationConfig, allocate_review_words, generate_review_sections,
        quota_pause_info)
    from litreview.quota import QuotaExceededError
    from litreview.stages import get_scaling_config
    import copy
    from rich.console import Console
    import io
    
    settings = copy.copy(get_app_settings())
    job = _jobs[job_id]
    console = Console(file=io.StringIO(), no_color=True, width=100)
    
    def log(msg: str, stage: Optional[str] = None, progress: Optional[int] = None):
        with _jobs_lock:
            job["log"].append(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")
            if stage:
                job["current_stage"] = stage
            if progress is not None:
                job["progress"] = progress
            _persist_web_review_job(job)
        logger.info(f"[ReviewJob {job_id}] {msg}")

    try:
        t0 = time.time()
        client = _get_client()
        model = model_override or settings.REVIEW_MODEL
        analysis_model = model_override or settings.ANALYSIS_MODEL
        settings.REVIEW_MODEL = model
        settings.ANALYSIS_MODEL = analysis_model
        store = _get_store()
        embedder = _get_embedder()
        reranker = _get_reranker()
        paper_meta = store.get_paper_meta()
        
        log(f"🤖 综述撰写引擎已加载模型: {model}")
        
        # 1. 大纲生成或加载
        if outline_json:
            outline_dict = json.loads(outline_json) if isinstance(outline_json, str) else outline_json
            outline = Outline.from_dict(outline_dict, topic=topic)
            log(f"📋 使用用户确认的大纲（共 {len(outline.sections)} 章节）", stage="A_OUTLINE", progress=10)
        else:
            log("📋 阶段 A：根据文献库内容生成综述结构大纲...", stage="A_OUTLINE", progress=5)
            outline = _gen(topic, store, embedder, client, analysis_model, settings, console,
                           focus=focus, n_sections=n_sections, words=words,
                           target_lang=job.get("target_lang", "zh"),
                           research_gap=job.get("research_gap", ""), thesis=job.get("thesis", ""),
                           mainline_logic=job.get("mainline_logic", ""))
            log(f"✔ 大纲完成：《{outline.title}》（共 {len(outline.sections)} 章节）", stage="A_OUTLINE", progress=15)
        # 显式参数优先，否则沿用大纲 JSON 中的立论，保证每章写作都拿到同一套 thesis。
        outline.apply_alignment(job.get("thesis", ""), job.get("research_gap", ""),
                                job.get("mainline_logic", ""))

        with _jobs_lock:
            job["thesis"], job["research_gap"], job["mainline_logic"] = (
                outline.thesis, outline.research_gap, outline.mainline_logic)
            job["outline"] = outline.model_dump()

            _persist_web_review_job(job)

        word_plan = allocate_review_words(words, len(outline.sections))
        section_words, intro_words, concl_words = (
            word_plan.section_words, word_plan.intro_words, word_plan.conclusion_words)
        scaling = get_scaling_config(words, job.get("target_lang", "zh"))

        def checkpoint(drafts):
            with _jobs_lock:
                job["checkpoint"] = [draft.model_dump() for draft in drafts]
                job["progress"] = 15 + int(70 * len(drafts) / max(1, len(outline.sections)))
                job["current_stage"] = "C_WRITING"
                _persist_web_review_job(job)

        section_result = generate_review_sections(
            outline,
            ReviewDependencies(store, embedder, client, reranker, settings, console),
            SectionGenerationConfig(
                section_words=section_words, focus=focus,
                initial_drafts=[SectionDraft(**item) for item in job.get("checkpoint", [])],
                review_top_k=scaling["rerank_top_k"], evidence_n=scaling["evidence_n"],
                thesis=outline.thesis, research_gap=outline.research_gap,
                mainline_logic=outline.mainline_logic),
            ReviewHooks(on_progress=log, on_checkpoint=checkpoint),
        )
        if isinstance(section_result.paused_quota, dict):
            _pause_web_review_job(job, section_result.paused_quota, log)
            return
        drafts = section_result.drafts
        # A resumed missing chapter belongs in its original outline position.
        order = {section.heading: i for i, section in enumerate(outline.sections)}
        drafts.sort(key=lambda draft: order.get(draft.heading, len(order)))
        incomplete = bool(section_result.failed_sections)
        job["failed_sections"] = section_result.failed_sections

        if not drafts:
            raise RuntimeError("所有章节均无有效证据支持，请检查主题与文献库是否匹配！")

        # 4. 引言与结论撰写
        log("✍️ 阶段 C：撰写引言背景与结论展望...", stage="C_WRITING", progress=88)
        try:
            intro, conclusion = write_intro_conclusion(outline, drafts, client, model,
                                                       intro_words=intro_words,
                                                       concl_words=concl_words, focus=focus,
                                                       thesis=job.get("thesis", ""),
                                                       research_gap=job.get("research_gap", ""))
        except QuotaExceededError:
            raise
        except Exception as e:
            log(f"⚠️ 引言/结论撰写异常（正文已保留）: {e}")
            intro, conclusion = "", ""
            incomplete = True

        # 5. 装配与渲染导出 (Stage D)
        log("📦 阶段 D：组装规范参考文献、生成 Markdown 与 Word 文档...", stage="D_ASSEMBLING", progress=92)
        evidence_total = section_result.evidence_count
        review = assemble_review(outline, drafts, intro, conclusion, paper_meta, evidence_total,
                                 evidence_by_heading=section_result.evidence_by_heading)
        
        template_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "review_template.md")
        out_path = render_review_note(review, paper_meta, settings, template_path, model)
        log(f"✔ Markdown 综述生成完毕: {os.path.basename(out_path)}")

        docx_path = ""
        try:
            docx_path = export_docx(out_path)
            log(f"✔ Word (.docx) 转换成功: {os.path.basename(docx_path)}")
        except Exception as e:
            log(f"⚠️ Word 转换失败 (可能是 pandoc 尚未安装，Markdown 文件已正常保存): {e}")

        manifest_path = ""
        manifest_err = None
        try:
            from litreview.manifest import (
                write_review_manifest, ManifestInput, RunContext, PromptSnapshot, RetrievalSnapshot
            )
            from litreview import prompts
            evidence_by_heading = section_result.evidence_by_heading
            run_ctx = RunContext(
                topic=topic,
                run_id=job_id,
                trigger_source="web",
                model_name=model,
                analysis_model=analysis_model,
                reranker_model=settings.RERANK_MODEL,
                target_words=words,
                target_lang=job.get("target_lang", "zh"),
                focus=focus,
                thesis=job.get("thesis", ""),
                research_gap=job.get("research_gap", ""),
                mainline_logic=job.get("mainline_logic", ""),
                prompts=PromptSnapshot(
                    outline_prompt_version=prompts.PROMPT_VERSION,
                    section_prompt_version=prompts.PROMPT_VERSION,
                    tldr_prompt_version=prompts.PROMPT_VERSION,
                ),
                retrieval=RetrievalSnapshot(
                    review_top_k=getattr(settings, "REVIEW_TOP_K", 0),
                    evidence_n=getattr(settings, "REVIEW_EVIDENCE_N", 0),
                    target_words=words,
                    section_words_min=section_words[0],
                    section_words_max=section_words[1],
                    reranker_model=settings.RERANK_MODEL,
                ),
                duration_seconds=int(time.time() - t0),
                corpus_fingerprint=store.get_corpus_fingerprint(),
            )
            manifest_path = write_review_manifest(ManifestInput(
                output_md_path=out_path,
                review=review,
                outline=outline,
                evidence_by_heading=evidence_by_heading,
                paper_meta=paper_meta,
                run_context=run_ctx,
                docx_path=docx_path or None,
            ))
            log(f"✔ Manifest 运行清单生成完毕: {os.path.basename(manifest_path)}")
        except Exception as exc:
            manifest_err = str(exc)
            log(f"⚠️ Manifest 运行清单生成失败 (Markdown 文件已正常保存): {exc}")

        baseline_verdict = None
        baseline_status = None
        baseline_summary = None
        baseline_error = None
        if manifest_path and os.path.exists(manifest_path):
            try:
                from litreview.run_baseline import check_review_candidate
                chk_report = check_review_candidate(
                    candidate_manifest_path=manifest_path,
                    registry_path=settings.REVIEW_BASELINE_REGISTRY_PATH,
                    output_root=settings.REVIEW_OUTPUT_DIR,
                )
                baseline_verdict = chk_report.verdict
                baseline_status = chk_report.status
                if chk_report.status == "no_baseline":
                    baseline_summary = "无已注册基线（首次生成）"
                elif chk_report.verdict == "PASS":
                    baseline_summary = "基线比对通过"
                elif chk_report.verdict == "WARN":
                    baseline_summary = "基线比对存在演进差异"
                elif chk_report.status == "candidate_invalid":
                    baseline_summary = "候选清单校验未通过"
                elif chk_report.status == "baseline_invalid":
                    baseline_summary = "基线注册表或基线文件损坏"
                else:
                    baseline_summary = "检测到基线质量退化"
                log(f"🛡️ 自动基线检查完成: {chk_report.verdict} ({chk_report.status}) - {baseline_summary}")
            except Exception as b_exc:
                from litreview.run_validation import _sanitize_path_in_str
                baseline_status = "check_error"
                baseline_summary = "基线检查异常"
                baseline_error = _sanitize_path_in_str(str(b_exc))
                log(f"⚠️ 自动基线检查异常: {baseline_error}")

        elapsed = int(time.time() - t0)
        with _jobs_lock:
            job["status"] = "partial" if manifest_err or incomplete else "done"
            job["current_stage"] = "PARTIAL_COMPLETED" if manifest_err or incomplete else "COMPLETED"
            job["progress"] = 100
            job["output_md"] = out_path
            job["output_docx"] = docx_path
            job["output_manifest"] = manifest_path
            job["filename_md"] = os.path.basename(out_path) if out_path else ""
            job["filename_docx"] = os.path.basename(docx_path) if docx_path else ""
            job["filename_manifest"] = os.path.basename(manifest_path) if manifest_path else ""
            job["manifest_error"] = manifest_err
            job["baseline_verdict"] = baseline_verdict
            job["baseline_status"] = baseline_status
            job["baseline_summary"] = baseline_summary
            job["baseline_error"] = baseline_error
            job["doc_count"] = review.doc_count
            job["evidence_count"] = review.evidence_count
            job["elapsed_seconds"] = elapsed
            
        if incomplete:
            log(f"⚠️ 部分正文或引言/结论尚未完成，可从检查点重试：{section_result.failed_sections}")
        elif manifest_err:
            log(f"⚠️ 综述正文与 Word 已生成，但运行清单生成失败: {manifest_err}")
        elif baseline_verdict == "FAIL":
            log(f"⚠️ 综述生成完成，但基线检查发现质量退化或基线损坏: {baseline_summary}")
        elif baseline_status == "check_error":
            log(f"⚠️ 综述生成完成，但自动基线检查异常: {baseline_error}")
        else:
            log(f"🎉 综述生成完毕！使用模型 {model}，引用文献 {review.doc_count} 篇，引用证据 {review.evidence_count} 条，总耗时 {elapsed} 秒。")
        status_tag = "partial" if manifest_err or incomplete else "success"
        log_run_event(mode="review", event="review_generated", title=review.title, status=status_tag,
                      extra={"topic": topic, "sections": len(review.sections),
                             "cited_docs": review.doc_count, "elapsed_s": elapsed,
                             "output": out_path, "docx": docx_path, "manifest": manifest_path,
                             "manifest_error": manifest_err, "model": model,
                             "baseline_verdict": baseline_verdict,
                             "baseline_status": baseline_status,
                             "baseline_summary": baseline_summary,
                             "baseline_error": baseline_error})
    except QuotaExceededError as e:
        _pause_web_review_job(job, quota_pause_info(e), log)
    except Exception as e:
        logger.error(f"综述生成失败: {e}\n{traceback.format_exc()}")
        with _jobs_lock:
            job["status"] = "failed"
            job["current_stage"] = "FAILED"
            job["error"] = str(e)
            job["log"].append(f"[{datetime.now().strftime('%H:%M:%S')}] ❌ 任务失败: {e}")
            _persist_web_review_job(job)


def _pause_web_review_job(job: dict, info: dict, log) -> None:
    """额度耗尽 → paused_quota（非失败）：保留检查点、记录 UTC 重置时刻并布置自动续跑。"""
    from litreview.review_jobs import PAUSED_QUOTA, schedule_quota_resume
    with _jobs_lock:
        job["status"] = PAUSED_QUOTA
        job["current_stage"] = "PAUSED_QUOTA"
        job["resume_after"] = info.get("resume_at", "")
        job["error"] = info.get("message", "")
        _persist_web_review_job(job)
    delay = schedule_quota_resume(job["job_id"], job["resume_after"], _auto_resume_review)
    suffix = "届时自动续跑" if delay is not None else "届时请手动恢复"
    log(f"⏸ 额度耗尽，任务已暂停（非失败）；下一次 UTC 额度重置 {job['resume_after']}，{suffix}",
        stage="PAUSED_QUOTA")


async def api_review_generate(request: Request) -> JSONResponse:
    """启动综述生成任务"""
    _load_review_jobs()
    data = await request.json()
    topic = data.get("topic", "").strip()
    focus = data.get("focus", "").strip()
    words = int(data.get("words", 0))
    sections = int(data.get("sections", 0))
    model_override = data.get("model", "").strip() or None
    outline_json = data.get("outline", None)
    
    if not topic:
        return JSONResponse({"success": False, "error": "综述主题不能为空"}, status_code=400)
        
    job_id = f"job_{int(time.time())}_{os.urandom(3).hex()}"
    with _jobs_lock:
        _jobs[job_id] = {
            "job_id": job_id,
            "topic": topic,
            "focus": focus,
            "words": words,
            "sections": sections,
            "outline": outline_json,
            "checkpoint": [],
            "target_lang": data.get("target_lang", "zh"),
            "thesis": data.get("thesis", ""),
            "research_gap": data.get("research_gap", ""),
            "mainline_logic": data.get("mainline_logic", ""),
            "model": model_override or get_app_settings().REVIEW_MODEL,
            "model_override": model_override,
            "status": "running",
            "current_stage": "PENDING",
            "progress": 0,
            "log": [f"[{datetime.now().strftime('%H:%M:%S')}] 综述任务创建: 《{topic}》"],
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "output_md": "",
            "output_docx": "",
            "output_manifest": "",
            "filename_md": "",
            "filename_docx": "",
            "filename_manifest": "",
            "manifest_error": None,
            "baseline_verdict": None,
            "baseline_status": None,
            "baseline_summary": None,
            "baseline_error": None,
            "error": ""
        }
        
    _persist_web_review_job(_jobs[job_id])
    _start_review_thread(_jobs[job_id])

    return JSONResponse({
        "success": True,
        "data": {
            "job_id": job_id,
            "topic": topic,
            "status": "running",
            "model": model_override or get_app_settings().REVIEW_MODEL
        },
        "message": "综述生成任务已启动"
    })


async def api_review_jobs(request: Request) -> JSONResponse:
    """获取所有综述任务列表"""
    _load_review_jobs()
    with _jobs_lock:
        job_list = list(_jobs.values())
    job_list.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return JSONResponse({"success": True, "data": job_list})


async def api_review_job_detail(request: Request) -> JSONResponse:
    """获取单个综述任务详情"""
    _load_review_jobs()
    job_id = request.path_params["job_id"]
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        return JSONResponse({"success": False, "error": "任务不存在"}, status_code=404)
    return JSONResponse({"success": True, "data": job})


async def api_review_job_stream(request: Request) -> StreamingResponse:
    """SSE 流式传输综述生成日志与进度"""
    _load_review_jobs()
    job_id = request.path_params["job_id"]
    
    async def event_generator():
        last_log_idx = 0
        while True:
            with _jobs_lock:
                job = _jobs.get(job_id)
            if not job:
                yield f"data: {json.dumps({'error': 'Job not found'})}\n\n"
                break
                
            logs = job.get("log", [])
            new_logs = logs[last_log_idx:]
            last_log_idx = len(logs)
            
            payload = {
                "job_id": job_id,
                "status": job.get("status"),
                "current_stage": job.get("current_stage"),
                "progress": job.get("progress", 0),
                "new_logs": new_logs,
                "output_md": job.get("output_md"),
                "output_docx": job.get("output_docx"),
                "output_manifest": job.get("output_manifest"),
                "filename_md": job.get("filename_md"),
                "filename_docx": job.get("filename_docx"),
                "filename_manifest": job.get("filename_manifest"),
                "baseline_verdict": job.get("baseline_verdict"),
                "baseline_status": job.get("baseline_status"),
                "baseline_summary": job.get("baseline_summary"),
                "baseline_error": job.get("baseline_error"),
                "resume_after": job.get("resume_after"),
                "error": job.get("error")
            }
            
            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
            
            if job.get("status") in ("done", "failed", "partial", "interrupted", "paused_quota"):
                break
                
            await asyncio.sleep(1.0)
            
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )


async def api_review_files(request: Request) -> JSONResponse:
    """列出已生成的所有综述文件 (MD / DOCX)"""
    settings = get_app_settings()
    out_dir = Path(settings.REVIEW_OUTPUT_DIR)
    if not out_dir.exists():
        return JSONResponse({"success": True, "data": []})
        
    md_files = list(out_dir.glob("*.md"))
    results = []
    
    for f in sorted(md_files, key=lambda x: x.stat().st_mtime, reverse=True):
        stem = f.stem
        docx_file = f.with_suffix(".docx")
        has_docx = docx_file.exists()
        
        stat = f.stat()
        mtime = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        
        # 尝试提取标题
        title = stem
        try:
            with open(f, "r", encoding="utf-8") as fp:
                first_lines = [fp.readline() for _ in range(5)]
                for line in first_lines:
                    if line.startswith("# "):
                        title = line[2:].strip()
                        break
        except Exception:
            pass
            
        results.append({
            "title": title,
            "filename_md": f.name,
            "filename_docx": docx_file.name if has_docx else None,
            "has_docx": has_docx,
            "size_kb": round(stat.st_size / 1024, 1),
            "updated_at": mtime
        })
        
    return JSONResponse({"success": True, "data": results})


async def api_review_preview(request: Request) -> JSONResponse:
    """读取指定综述 Markdown 的内容用于在线预览"""
    filename = request.query_params.get("filename", "")
    if not filename or ".." in filename or safe_basename(filename) != filename:
        return JSONResponse({"success": False, "error": "非法文件名"}, status_code=400)

    settings = get_app_settings()
    resolved = resolve_within(settings.REVIEW_OUTPUT_DIR, filename)
    if not resolved:
        return JSONResponse({"success": False, "error": "非法文件名"}, status_code=400)
    target = Path(resolved)
    if not target.exists() or not target.is_file():
        return JSONResponse({"success": False, "error": "文件不存在"}, status_code=404)
        
    try:
        with open(target, "r", encoding="utf-8") as f:
            content = f.read()
        return JSONResponse({
            "success": True,
            "data": {
                "filename": filename,
                "content": content
            }
        })
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


async def api_review_download(request: Request) -> Response:
    """下载综述文件 (.docx 或 .md)"""
    filename = request.query_params.get("filename", "")
    if not filename or ".." in filename or safe_basename(filename) != filename:
        return JSONResponse({"success": False, "error": "非法文件名"}, status_code=400)

    settings = get_app_settings()
    resolved = resolve_within(settings.REVIEW_OUTPUT_DIR, filename)
    if not resolved:
        return JSONResponse({"success": False, "error": "非法文件名"}, status_code=400)
    target = Path(resolved)
    if not target.exists() or not target.is_file():
        return JSONResponse({"success": False, "error": "文件不存在"}, status_code=404)
        
    # URL 编码文件名支持中文下载
    import urllib.parse
    encoded_filename = urllib.parse.quote(filename)
    
    media_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document" if filename.endswith(".docx") else "text/markdown; charset=utf-8"
    
    return FileResponse(
        path=str(target),
        media_type=media_type,
        filename=filename,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}"
        }
    )


# ── 系统设置与 API 测试 ───────────────────────────────────────────────────────

async def api_test_keys(request: Request) -> JSONResponse:
    """全面测试三大 API 连通性 (MinerU, SiliconFlow, OpenAI)"""
    results = {}
    
    # 1. 测试 MinerU
    mineru_key = os.getenv("MINERU_API_KEY", "")
    if not mineru_key or mineru_key == "your_mineru_api_key_here":
        results["mineru"] = {"ok": False, "msg": "未配置 MINERU_API_KEY"}
    else:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get("https://mineru.net/api/v4/user-info", headers={"Authorization": f"Bearer {mineru_key}"})
                if r.status_code == 200:
                    results["mineru"] = {"ok": True, "msg": "连接成功 (MinerU 接口正常)"}
                else:
                    results["mineru"] = {"ok": False, "msg": f"状态码 {r.status_code}: {r.text[:100]}"}
        except Exception as e:
            results["mineru"] = {"ok": False, "msg": f"请求失败: {str(e)}"}
            
    # 2. 测试 SiliconFlow
    sf_key = os.getenv("SILICONFLOW_API_KEY", "")
    sf_base = os.getenv("SILICONFLOW_API_BASE", "https://api.siliconflow.cn/v1")
    if not sf_key or sf_key == "your_siliconflow_key":
        results["siliconflow"] = {"ok": False, "msg": "未配置 SILICONFLOW_API_KEY"}
    else:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(f"{sf_base}/models", headers={"Authorization": f"Bearer {sf_key}"})
                if r.status_code == 200:
                    results["siliconflow"] = {"ok": True, "msg": "连接成功 (硅基流动向量与重排模型可用)"}
                else:
                    results["siliconflow"] = {"ok": False, "msg": f"状态码 {r.status_code}: {r.text[:100]}"}
        except Exception as e:
            results["siliconflow"] = {"ok": False, "msg": f"请求失败: {str(e)}"}
            
    # 3. 测试 OpenAI
    oa_key = os.getenv("OPENAI_API_KEY", "")
    oa_base = os.getenv("OPENAI_API_BASE", "https://api.openai.com/v1")
    if not oa_key or oa_key == "your_openai_api_key_here":
        results["openai"] = {"ok": False, "msg": "未配置 OPENAI_API_KEY"}
    else:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(f"{oa_base}/models", headers={"Authorization": f"Bearer {oa_key}"})
                if r.status_code == 200:
                    results["openai"] = {"ok": True, "msg": f"连接成功 (模型端点正常: {oa_base})"}
                else:
                    results["openai"] = {"ok": False, "msg": f"状态码 {r.status_code}: {r.text[:100]}"}
        except Exception as e:
            results["openai"] = {"ok": False, "msg": f"请求失败: {str(e)}"}
            
    return JSONResponse({"success": True, "data": results})


async def api_save_env(request: Request) -> JSONResponse:
    """在线更新 .env 配置文件"""
    data = await request.json()
    env_path = Path(".env")
    if not env_path.exists():
        if Path(".env.example").exists():
            shutil.copy(".env.example", ".env")
        else:
            env_path.touch()
            
    allowed_keys = [
        "MINERU_API_KEY", "SILICONFLOW_API_KEY", "SILICONFLOW_API_BASE",
        "OPENAI_API_KEY", "OPENAI_API_BASE", "ANALYSIS_MODEL", "REVIEW_MODEL",
        "RUN_MODE", "WEBHOOK_URL"
    ]
    
    updated = []
    for k, v in data.items():
        if k in allowed_keys and v is not None:
            val_str = str(v).strip()
            set_key(str(env_path), k, val_str)
            os.environ[k] = val_str
            updated.append(k)
            
    # 重置单例状态
    global _state
    with _state_lock:
        _state = {
            "generation_client": None,
            "embedding_client": None,
            "store": None,
            "embedder": None,
            "reranker": None
        }
        
    return JSONResponse({
        "success": True,
        "message": f"已成功更新配置项: {', '.join(updated)}",
        "updated": updated
    })



# ── 引文网络、滚雪球发现与端到端入库 API ────────────────────────────────────────────────────────

def _get_prod_db_path():
    return get_app_settings().DB_PATH

def _get_cache_db_path():
    return os.getenv(
        "EXTERNAL_DISCOVERY_CACHE_DB_PATH",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "external_discovery_cache.db")
    )

def _get_test_citation_db_path():
    from litreview.retrieval_store import discovery_db_path
    return discovery_db_path()

def _get_staging_pdf_dir():
    return os.getenv(
        "STAGING_PDF_DIR",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "staging_pdf")
    )

_bg_jobs_lock = threading.Lock()
_bg_jobs: Dict[str, Dict[str, Any]] = {}

def _create_bg_job(job_type: str, title: str, meta: Dict[str, Any] = None) -> str:
    job_id = f"job_{job_type[:4]}_{int(time.time())}_{os.urandom(3).hex()}"
    with _bg_jobs_lock:
        _bg_jobs[job_id] = {
            "job_id": job_id,
            "type": job_type,
            "title": title,
            "status": "pending",
            "progress": 0,
            "message": "任务已排队...",
            "logs": [f"[{datetime.now().strftime('%H:%M:%S')}] 任务创建: {title}"],
            "result": None,
            "error": None,
            "meta": meta or {},
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
    return job_id

def _update_bg_job(job_id: str, status: str = None, progress: int = None, message: str = None, log_entry: str = None, result: Any = None, error: str = None):
    with _bg_jobs_lock:
        if job_id not in _bg_jobs:
            return
        job = _bg_jobs[job_id]
        if status:
            job["status"] = status
        if progress is not None:
            job["progress"] = max(0, min(100, progress))
        if message:
            job["message"] = message
        if log_entry:
            job["logs"].append(f"[{datetime.now().strftime('%H:%M:%S')}] {log_entry}")
        if result is not None:
            job["result"] = result
        if error is not None:
            job["error"] = str(error)
        job["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def _get_bg_job(job_id: str) -> Optional[Dict[str, Any]]:
    with _bg_jobs_lock:
        return _bg_jobs.get(job_id)

def _run_discovery_worker(job_id: str, seed_ids: List[str], direction: str, depth: int, limit_per_track: int, recent_years: int, min_co_citation: int = 1):
    _update_bg_job(job_id, status="running", progress=10, message="正在初始化滚雪球引擎...", log_entry=f"启动 OpenAlex 发现: 种子={seed_ids}, 方向={direction}, 共被引阈值={min_co_citation}, 上限={limit_per_track}/轨")
    try:
        cache_db = _get_cache_db_path()
        prod_db = _get_prod_db_path()
        _update_bg_job(job_id, progress=30, message="正在查询 OpenAlex 引文网络与元数据...", log_entry="调用 OpenAlex API 检索出向与入向引文...")
        from litreview.snowball import expand_citations
        res = expand_citations(
            seed_ids=seed_ids,
            direction=direction,
            depth=depth,
            limit_per_track=limit_per_track,
            min_co_citation=min_co_citation,
            recent_years=recent_years,
            db_path=prod_db,
            cache_db_path=cache_db
        )
        if "error" in res:
            logger.warning(f"Discovery job {job_id} failed: {res['error']}")
            _update_bg_job(
                job_id,
                status="failed",
                progress=100,
                message=f"发现失败: {res['error']}",
                log_entry=f"失败: {res['error']}",
                error=res["error"]
            )
            return

        _update_bg_job(
            job_id,
            status="completed",
            progress=100,
            message=f"滚雪球发现完成：发现 {res.get('materialized_candidates_count', 0)} 篇候选 (基石: {res.get('foundational_count', 0)}, 前沿: {res.get('emerging_count', 0)})",
            log_entry=f"发现完成: run_id={res.get('query_run_id')}, 新外部候选={res.get('new_external_candidates_count', 0)}",
            result=res
        )
    except Exception as e:
        logger.error(f"Discovery job {job_id} error: {e}", exc_info=True)
        _update_bg_job(job_id, status="failed", progress=100, message=f"发现失败: {e}", log_entry=f"异常: {traceback.format_exc()}", error=str(e))

def _run_acquire_worker(job_id: str, candidate_ids: List[str]):
    _update_bg_job(job_id, status="running", progress=10, message="准备获取合法公开 OA 全文...", log_entry=f"待获取候选数量: {len(candidate_ids)}")
    try:
        cache_db = _get_cache_db_path()
        staging_dir = _get_staging_pdf_dir()
        _update_bg_job(job_id, progress=40, message="正在合法下载 PDF 并进行魔数与哈希验真...", log_entry="执行合法 OA 下载与 PDF 校验...")
        from litreview.pipeline import acquire_fulltext
        res = acquire_fulltext(
            cache_db_path=cache_db,
            staging_dir=staging_dir,
            candidate_ids=candidate_ids,
            max_fetch=len(candidate_ids)
        )
        _update_bg_job(
            job_id,
            status="completed",
            progress=100,
            message=f"全文获取完成：成功 {res.get('acquired_count', 0)} 篇，失败 {res.get('failed_count', 0)} 篇",
            log_entry=f"获取结果: 成功={res.get('acquired_count', 0)}, 失败={res.get('failed_count', 0)}",
            result=res
        )
    except Exception as e:
        logger.error(f"Acquire job {job_id} error: {e}", exc_info=True)
        _update_bg_job(job_id, status="failed", progress=100, message=f"获取失败: {e}", log_entry=f"异常: {traceback.format_exc()}", error=str(e))

def _run_ingest_worker(job_id: str, candidate_ids: List[str]):
    _update_bg_job(job_id, status="running", progress=10, message="启动文献解析、语义切块与向量入库...", log_entry=f"待入库候选: {candidate_ids}")
    try:
        cache_db = _get_cache_db_path()
        test_db = _get_test_citation_db_path()
        staging_dir = _get_staging_pdf_dir()
        _update_bg_job(job_id, progress=30, message="正在提取正文、切块并调用 Qwen3 生成 4096 维向量...", log_entry="解析 PDF 并生成密集语义向量...")
        from litreview.pipeline import ingest_acquired_candidates
        res = ingest_acquired_candidates(
            cache_db_path=cache_db,
            test_db_path=test_db,
            staging_dir=staging_dir,
            candidate_ids=candidate_ids,
            max_ingest=len(candidate_ids)
        )
        if res.get("error"):
            raise RuntimeError(res["error"])
        indexed = res.get("indexed_count", 0)
        failed = res.get("failed_count", 0)
        status = "partial" if failed and indexed else "failed" if failed else "completed"
        _update_bg_job(
            job_id,
            status=status,
            progress=100,
            message=f"语义索引完成 {indexed} 篇；未完成、可重试 {failed} 篇",
            log_entry=f"解析/图谱入库 {res.get('ingested_count', 0)} 篇，真实向量索引 {indexed} 篇，待重试 {failed} 篇",
            result=res,
            error="部分文献语义索引未完成，请重试" if failed else None,
        )
    except Exception as e:
        logger.error(f"Ingest job {job_id} error: {e}", exc_info=True)
        _update_bg_job(job_id, status="failed", progress=100, message=f"入库失败: {e}", log_entry=f"异常: {traceback.format_exc()}", error=str(e))


async def api_dashboard_stats(request: Request) -> JSONResponse:
    """获取全局系统概况看板数据 (委托至 LibraryStore 深模块)"""
    try:
        stats = get_dashboard_stratified_stats(
            _get_prod_db_path(),
            _get_test_citation_db_path(),
            _get_cache_db_path()
        )
        return JSONResponse({"success": True, "data": stats})
    except Exception as e:
        logger.error(f"Dashboard stats error: {e}", exc_info=True)
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


async def api_library_documents(request: Request) -> JSONResponse:
    """分页查询馆藏文献目录 (委托至 LibraryStore 深模块)"""
    try:
        page = int(request.query_params.get("page", "1"))
        page_size = int(request.query_params.get("page_size", "20"))
        q = request.query_params.get("q", "")
        doc_type = request.query_params.get("type", "all")
        indexed = request.query_params.get("indexed", "all")
        
        res = list_library_documents(
            _get_prod_db_path(),
            _get_test_citation_db_path(),
            page=page,
            page_size=page_size,
            query=q,
            doc_type=doc_type,
            indexed=indexed
        )
        tot = res["total"]
        tot_pages = res.get("total_pages") or ((tot + page_size - 1) // page_size if page_size > 0 else 1)
        return JSONResponse({
            "success": True,
            "data": {
                "items": res["items"],
                "total": tot,
                "page": res["page"],
                "page_size": res["page_size"],
                "total_pages": tot_pages
            },
            "total": tot
        })
    except Exception as e:
        logger.error(f"List library documents error: {e}", exc_info=True)
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


async def api_library_document_detail(request: Request) -> JSONResponse:
    """获取单篇文献的详细信息、PDF 资产状态、参考文献消歧明细和引文子图 (委托至 LibraryStore 深模块)"""
    doc_id = request.path_params.get("doc_id", "").strip()
    try:
        data = get_document_full_detail(doc_id, _get_prod_db_path(), _get_test_citation_db_path())
        return JSONResponse({"success": True, "data": data})
    except Exception as e:
        logger.error(f"Get document detail error: {e}", exc_info=True)
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)



async def api_library_document_enrich(request: Request) -> JSONResponse:
    """通过 OpenAlex / CrossRef 联网权威对齐并深度补全单篇文献元数据 (委托至 LibraryStore 深模块)"""
    doc_id = request.path_params.get("doc_id", "").strip()
    try:
        res = enrich_document_metadata(
            doc_id,
            _get_prod_db_path(),
            _get_test_citation_db_path(),
            _get_cache_db_path()
        )
        return JSONResponse(res)
    except Exception as e:
        logger.error(f"Enrich document error: {e}", exc_info=True)
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)

async def api_citations_graph(request: Request) -> JSONResponse:
    """获取指定文献的局部引文力导向子图数据 (委托至 CitationNetwork 深模块)"""
    doc_id = request.path_params.get("doc_id", "").strip()
    test_db = _get_test_citation_db_path()
    if not os.path.exists(test_db):
        return JSONResponse({"success": False, "error": "引文网络数据库不存在"}, status_code=404)
        
    try:
        data = get_seed_subgraph(test_db, doc_id)
        if not data.get("nodes"):
            return JSONResponse({"success": False, "error": f"未找到文献 {doc_id} 的图谱节点"}, status_code=404)
        return JSONResponse({"success": True, "data": data})
    except Exception as e:
        logger.error(f"Generate citation graph error: {e}", exc_info=True)
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


async def api_citations_graph_full(request: Request) -> JSONResponse:
    """获取引文网络图谱数据 (委托至 CitationNetwork 深模块)"""
    test_db = _get_test_citation_db_path()
    if not os.path.exists(test_db):
        return JSONResponse({"success": False, "error": "引文网络数据库不存在"}, status_code=404)
        
    include_unresolved = request.query_params.get("include_unresolved", "false").lower() in ("true", "1")
    limit = int(request.query_params.get("limit", "10000"))
    
    try:
        data = get_full_citation_graph(test_db, include_unresolved=include_unresolved, limit=limit)
        return JSONResponse({"success": True, "data": data})
    except Exception as e:
        logger.error(f"Generate full citation graph error: {e}", exc_info=True)
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


async def api_discovery_expand(request: Request) -> JSONResponse:
    """启动滚雪球外部引文发现任务 (异步后台执行)"""
    data = await request.json() if request.headers.get("content-type") == "application/json" else {}
    seed_ids = data.get("seed_ids", ["bba4a59d", "6a3736de"])
    direction = data.get("direction", "both")
    depth = int(data.get("depth", 1))
    limit_per_track = int(data.get("limit_per_track", 20))
    recent_years = int(data.get("recent_years", 3))
    min_co_citation = int(data.get("min_co_citation", 1))
    
    if not seed_ids:
        return JSONResponse({"success": False, "error": "请提供至少一个种子文献 ID"}, status_code=400)
        
    title = f"OpenAlex 滚雪球发现 (种子={len(seed_ids)}篇, 深度={depth})"
    job_id = _create_bg_job("discovery", title, meta=data)
    
    t = threading.Thread(
        target=_run_discovery_worker,
        args=(job_id, seed_ids, direction, depth, limit_per_track, recent_years, min_co_citation),
        daemon=True
    )
    t.start()
    
    return JSONResponse({
        "success": True,
        "data": {"job_id": job_id},
        "message": f"发现任务已在后台启动 (Job ID: {job_id})"
    })


async def api_discovery_runs(request: Request) -> JSONResponse:
    """获取所有历史发现批次记录"""
    cache_db = _get_cache_db_path()
    if not os.path.exists(cache_db):
        return JSONResponse({"success": True, "data": []})
        
    conn = sqlite3.connect(cache_db)
    conn.row_factory = sqlite3.Row
    try:
        runs = [dict(r) for r in conn.execute("SELECT * FROM discovery_runs ORDER BY created_at DESC").fetchall()]
        return JSONResponse({"success": True, "data": runs})
    finally:
        conn.close()


async def api_discovery_candidates(request: Request) -> JSONResponse:
    """分页查询外部发现候选池文献 (委托至 LibraryStore 深模块)"""
    cache_db = _get_cache_db_path()
    if not os.path.exists(cache_db):
        return JSONResponse({"success": False, "error": "候选池数据库不存在"}, status_code=404)
        
    try:
        page = int(request.query_params.get("page", "1"))
        page_size = int(request.query_params.get("page_size", "20"))
        q = request.query_params.get("q", "")
        track = request.query_params.get("track", "ALL")
        is_oa_str = request.query_params.get("is_oa")
        is_oa = is_oa_str.lower() == "true" if is_oa_str is not None else None
        status = request.query_params.get("status", "ALL")
        
        res = list_discovery_candidates_paginated(
            cache_db,
            page=page,
            page_size=page_size,
            query=q,
            track=track,
            is_oa=is_oa,
            status=status
        )
        tot = res["total"]
        tot_pages = res.get("total_pages") or ((tot + page_size - 1) // page_size if page_size > 0 else 1)
        return JSONResponse({
            "success": True,
            "data": {
                "items": res["items"],
                "total": tot,
                "page": res["page"],
                "page_size": res["page_size"],
                "total_pages": tot_pages
            },
            "total": tot
        })
    except Exception as e:
        logger.error(f"Query discovery candidates error: {e}", exc_info=True)
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


async def api_discovery_candidate_decision(request: Request) -> JSONResponse:
    """对候选文献执行审批决策 (APPROVE / REJECT，硬约束拦截非法状态流转)"""
    data = await request.json() if request.headers.get("content-type") == "application/json" else {}
    candidate_ids = data.get("candidate_ids", [])
    action = data.get("action", "APPROVE").upper()
    reason = data.get("reason", "WEB_UI_USER_CONFIRMATION")
    decision_source = data.get("decision_source", "USER")
    operator = data.get("operator", "web_ui_user")
    
    if not candidate_ids:
        return JSONResponse({"success": False, "error": "请提供至少一个待审批的 candidate_id"}, status_code=400)
        
    cache_db = _get_cache_db_path()
    from litreview.pipeline import approve_candidates
    res = approve_candidates(
        cache_db_path=cache_db,
        candidate_ids=candidate_ids,
        action=action,
        reason=reason,
        decision_source=decision_source,
        operator=operator
    )
    
    if "error" in res:
        return JSONResponse({"success": False, "error": res["error"]}, status_code=400)
        
    return JSONResponse({
        "success": True,
        "data": res,
        "message": f"审批完成：成功流转 {res['updated_count']} 篇，拦截拒绝 {res['rejected_count']} 篇"
    })


async def api_pipeline_acquire(request: Request) -> JSONResponse:
    """启动已批准候选文献的合法 OA 全文获取任务 (异步后台执行)"""
    data = await request.json() if request.headers.get("content-type") == "application/json" else {}
    candidate_ids = data.get("candidate_ids", [])
    
    title = f"合法 OA PDF 获取任务 ({len(candidate_ids)} 篇)"
    job_id = _create_bg_job("acquire", title, meta=data)
    
    t = threading.Thread(
        target=_run_acquire_worker,
        args=(job_id, candidate_ids),
        daemon=True
    )
    t.start()
    
    return JSONResponse({
        "success": True,
        "data": {"job_id": job_id},
        "message": f"获取任务已启动 (Job ID: {job_id})"
    })


async def api_pipeline_ingest(request: Request) -> JSONResponse:
    """启动已获取文献的解析、真实 Qwen3 向量索引与图谱回填任务 (异步后台执行)"""
    data = await request.json() if request.headers.get("content-type") == "application/json" else {}
    candidate_ids = data.get("candidate_ids", [])
    
    title = f"文献解析、Qwen3 向量化与图谱回填 ({len(candidate_ids)} 篇)"
    job_id = _create_bg_job("ingest", title, meta=data)
    
    t = threading.Thread(
        target=_run_ingest_worker,
        args=(job_id, candidate_ids),
        daemon=True
    )
    t.start()
    
    return JSONResponse({
        "success": True,
        "data": {"job_id": job_id},
        "message": f"入库任务已启动 (Job ID: {job_id})"
    })


async def api_pipeline_job_status(request: Request) -> JSONResponse:
    """查询后台异步任务状态、实时进度、阶段日志与最终结果"""
    job_id = request.path_params.get("job_id", "").strip()
    job = _get_bg_job(job_id)
    if not job:
        return JSONResponse({"success": False, "error": "任务不存在"}, status_code=404)
    return JSONResponse({"success": True, "data": job})


async def api_audit_events(request: Request) -> JSONResponse:
    """获取候选决策审计记录、PDF 资产元数据记录与流水线执行历史"""
    cache_db = _get_cache_db_path()
    test_db = _get_test_citation_db_path()
    
    decisions = []
    assets = []
    
    if os.path.exists(cache_db):
        try:
            conn = sqlite3.connect(cache_db)
            conn.row_factory = sqlite3.Row
            dec_rows = conn.execute("""
                SELECT d.*, c.canonical_title as title
                FROM candidate_decisions d
                LEFT JOIN discovered_candidates c ON d.candidate_id = c.candidate_id
                ORDER BY d.decided_at DESC LIMIT 50
            """).fetchall()
            decisions = [dict(r) for r in dec_rows]
            conn.close()
        except Exception as e:
            logger.warning(f"Query candidate_decisions error: {e}")
            
    if os.path.exists(test_db):
        try:
            conn = sqlite3.connect(test_db)
            conn.row_factory = sqlite3.Row
            asset_rows = conn.execute("""
                SELECT a.*, w.canonical_title as title
                FROM document_assets a
                LEFT JOIN works w ON a.work_id = w.work_id
                ORDER BY a.created_at DESC LIMIT 50
            """).fetchall()
            assets = [dict(r) for r in asset_rows]
            conn.close()
        except Exception as e:
            logger.warning(f"Query document_assets error: {e}")
            
    return JSONResponse({
        "success": True,
        "data": {
            "decisions": decisions,
            "assets": assets
        }
    })


async def api_pipeline_overview(request: Request) -> JSONResponse:
    """获取入库流水线的三级分栏数据：待入库、失败需人工干预、已成功入库"""
    cache_db = _get_cache_db_path()
    pending = []
    failed = []
    success = []
    
    if os.path.exists(cache_db):
        conn = sqlite3.connect(cache_db)
        conn.row_factory = sqlite3.Row
        try:
            # 已解析但尚无真实向量的文献仍须可见、可重试。
            pending_rows = conn.execute("""
                SELECT * FROM discovered_candidates
                WHERE lifecycle_status IN ('APPROVED', 'FULLTEXT_ACQUIRED', 'PARSED', 'GRAPH_INDEXED', 'VECTOR_INDEXED_TEST', 'INDEXED')
                ORDER BY discovered_at DESC
            """).fetchall()
            pending = [dict(r) for r in pending_rows]
            
            # 2. 失败需人工干预 (FULLTEXT_UNAVAILABLE)
            failed_rows = conn.execute("""
                SELECT * FROM discovered_candidates
                WHERE lifecycle_status = 'FULLTEXT_UNAVAILABLE'
                ORDER BY discovered_at DESC
            """).fetchall()
            failed = [dict(r) for r in failed_rows]
            
            # 只有真实语义索引完成才属于成功。
            success_rows = conn.execute("""
                SELECT * FROM discovered_candidates
                WHERE lifecycle_status = 'VECTOR_INDEXED'
                ORDER BY discovered_at DESC
            """).fetchall()
            success = [dict(r) for r in success_rows]
        except Exception as e:
            logger.warning(f"Query pipeline overview error: {e}")
        finally:
            conn.close()
            
    return JSONResponse({
        "success": True,
        "data": {
            "pending": pending,
            "failed": failed,
            "success": success
        }
    })


async def api_pipeline_upload_pdf(request: Request) -> JSONResponse:
    """上传本地 PDF 补录文件，自动验真并变更为 FULLTEXT_ACQUIRED 状态"""
    try:
        form = await request.form()
        candidate_id = form.get("candidate_id")
        file_obj = form.get("file")
        
        if not candidate_id or not file_obj:
            return JSONResponse({"success": False, "error": "必须提供 candidate_id 和 PDF 文件"}, status_code=400)
        if not is_valid_candidate_id(candidate_id):
            return JSONResponse({"success": False, "error": "非法 candidate_id"}, status_code=400)

        # 候选必须真实存在于发现库，才允许落盘到 staging
        cache_db = _get_cache_db_path()
        if not os.path.exists(cache_db):
            return JSONResponse({"success": False, "error": f"未找到候选: {candidate_id}"}, status_code=404)
        conn = sqlite3.connect(cache_db)
        try:
            exists = conn.execute("SELECT 1 FROM discovered_candidates WHERE candidate_id = ?", (candidate_id,)).fetchone()
        finally:
            conn.close()
        if not exists:
            return JSONResponse({"success": False, "error": f"未找到候选: {candidate_id}"}, status_code=404)

        pdf_bytes = await file_obj.read()
        if len(pdf_bytes) < 100 or pdf_bytes[:4] != b"%PDF":
            return JSONResponse({"success": False, "error": "上传的文件不是有效的 PDF 格式 (缺少 %PDF 文件头)"}, status_code=400)

        staging_dir = _get_staging_pdf_dir()
        os.makedirs(staging_dir, exist_ok=True)
        dest_path = resolve_within(staging_dir, f"{candidate_id}.pdf")
        if not dest_path:
            return JSONResponse({"success": False, "error": "非法 candidate_id"}, status_code=400)

        with open(dest_path, "wb") as f:
            f.write(pdf_bytes)

        if os.path.exists(cache_db):
            conn = sqlite3.connect(cache_db)
            conn.execute("UPDATE discovered_candidates SET lifecycle_status = 'FULLTEXT_ACQUIRED' WHERE candidate_id = ?", (candidate_id,))
            conn.commit()
            conn.close()
            
        return JSONResponse({
            "success": True,
            "message": f"PDF 文件上传成功，大小: {len(pdf_bytes)} 字节，状态已转为 FULLTEXT_ACQUIRED",
            "data": {
                "candidate_id": candidate_id,
                "file_size": len(pdf_bytes),
                "file_path": dest_path
            }
        })
    except Exception as e:
        logger.error(f"Upload PDF error: {e}", exc_info=True)
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


# ── 路由与静态资源装配 ─────────────────────────────────────────────────────────

static_dir = Path(__file__).parent / "web_static"
static_dir.mkdir(exist_ok=True)

async def serve_index(request: Request) -> HTMLResponse:
    index_html = static_dir / "index.html"
    if index_html.exists():
        with open(index_html, "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    return HTMLResponse("<h1>Literature Analyzer Web UI 正在初始化...</h1>")

routes = [
    # ── 引文网络与滚雪球发现端到端 API ──
    Route("/api/dashboard/stats", endpoint=api_dashboard_stats, methods=["GET"]),
    Route("/api/library/documents", endpoint=api_library_documents, methods=["GET"]),
    Route("/api/library/documents/{doc_id}", endpoint=api_library_document_detail, methods=["GET"]),
    Route("/api/library/documents/{doc_id}/enrich", endpoint=api_library_document_enrich, methods=["POST"]),
    Route("/api/citations/graph/full", endpoint=api_citations_graph_full, methods=["GET"]),
    Route("/api/citations/{doc_id}", endpoint=api_citations_graph, methods=["GET"]),
    Route("/api/discovery/expand", endpoint=api_discovery_expand, methods=["POST"]),
    Route("/api/discovery/runs", endpoint=api_discovery_runs, methods=["GET"]),
    Route("/api/discovery/candidates", endpoint=api_discovery_candidates, methods=["GET"]),
    Route("/api/discovery/candidates/decision", endpoint=api_discovery_candidate_decision, methods=["POST"]),
    Route("/api/pipeline/overview", endpoint=api_pipeline_overview, methods=["GET"]),
    Route("/api/pipeline/upload", endpoint=api_pipeline_upload_pdf, methods=["POST"]),
    Route("/api/pipeline/acquire", endpoint=api_pipeline_acquire, methods=["POST"]),
    Route("/api/pipeline/ingest", endpoint=api_pipeline_ingest, methods=["POST"]),
    Route("/api/pipeline/jobs/{job_id}", endpoint=api_pipeline_job_status, methods=["GET"]),
    Route("/api/audit/events", endpoint=api_audit_events, methods=["GET"]),

    Route("/", endpoint=serve_index, methods=["GET"]),
    
    # 概况与状态
    Route("/api/status", endpoint=api_status, methods=["GET"]),
    Route("/api/test_keys", endpoint=api_test_keys, methods=["POST"]),
    Route("/api/config/save", endpoint=api_save_env, methods=["POST"]),
    
    # 文献库管理
    Route("/api/papers", endpoint=api_papers, methods=["GET"]),
    Route("/api/papers/{doc_id}", endpoint=api_paper_detail, methods=["GET"]),
    Route("/api/papers/{doc_id}", endpoint=api_paper_delete, methods=["DELETE"]),
    Route("/api/papers/{doc_id}/type", endpoint=api_paper_update_type, methods=["POST"]),
    
    # 上传与流水线
    Route("/api/upload", endpoint=api_upload, methods=["POST"]),
    Route("/api/pipeline/run", endpoint=api_pipeline_run, methods=["POST"]),
    Route("/api/pipeline/status/{task_id}", endpoint=api_pipeline_status, methods=["GET"]),
    Route("/api/index/run", endpoint=api_index_run, methods=["POST"]),
    Route("/api/enrich/run", endpoint=api_enrich_run, methods=["POST"]),
    
    # 语义检索
    Route("/api/search", endpoint=api_search, methods=["POST"]),
    
    # 综述生成与管理
    Route("/api/review/outline", endpoint=api_review_outline, methods=["POST"]),
    Route("/api/review/generate", endpoint=api_review_generate, methods=["POST"]),
    Route("/api/review/jobs", endpoint=api_review_jobs, methods=["GET"]),
    Route("/api/review/jobs/{job_id}", endpoint=api_review_job_detail, methods=["GET"]),
    Route("/api/review/jobs/{job_id}/resume", endpoint=api_review_resume, methods=["POST"]),
    Route("/api/review/jobs/{job_id}/stream", endpoint=api_review_job_stream, methods=["GET"]),
    Route("/api/review/files", endpoint=api_review_files, methods=["GET"]),
    Route("/api/review/preview", endpoint=api_review_preview, methods=["GET"]),
    Route("/api/review/download", endpoint=api_review_download, methods=["GET"]),
    
    # 静态资源挂载
    Mount("/static", app=StaticFiles(directory=str(static_dir)), name="static"),
]

def _cors_origins() -> List[str]:
    """WEB_CORS_ORIGINS 白名单；解析逻辑在 litreview.guard.cors_origins，与 CSRF 校验共用。"""
    return cors_origins()


def build_middleware(cors_origins: Optional[List[str]] = None) -> List[Middleware]:
    origins = _cors_origins() if cors_origins is None else cors_origins
    stack = [Middleware(GZipMiddleware, minimum_size=1000)]
    if origins:
        stack.append(Middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_methods=["*"],
            allow_headers=["*"],
        ))
    stack.append(Middleware(RemoteAccessGuardMiddleware))
    return stack


middleware = build_middleware()

app = Starlette(routes=routes, middleware=middleware)


def find_available_port(host: str, start_port: int) -> int:
    import socket
    port = start_port
    while port < start_port + 100:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind((host, port))
                return port
            except OSError:
                port += 1
    return start_port


def run():
    """CLI 启动主入口"""
    req_port = int(os.getenv("WEB_PORT", "8001"))
    host = os.getenv("WEB_HOST", "0.0.0.0")
    port = find_available_port(host, req_port)
    
    print(f"\n=======================================================")
    print(f" 📚 Literature Analyzer Commercial Web Server 启动中...")
    print(f" 🌐 本地访问地址: http://127.0.0.1:{port}")
    print(f" 🌐 局域网访问地址: http://0.0.0.0:{port}")
    if port != req_port:
        print(f" ⚠️ 端口 {req_port} 已被占用，已自动切换至空闲端口 {port}")
    print(f" 💡 按 Ctrl+C 可停止服务")
    print(f"=======================================================\n")
    uvicorn.run(app, host=host, port=port, log_level="info", proxy_headers=False)


if __name__ == "__main__":
    run()
