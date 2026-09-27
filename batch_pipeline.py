import os
import sys
import glob
import json
import time
import sqlite3
import shutil

# 强制劫持 shutil.move 以兼容 Docker 挂载跨设备移动
def _safe_move_hijack(src, dst, **kwargs):
    import os, shutil as s
    if not os.path.exists(src):
        print(f"[Warning] shutil.move 源文件已不存在，跳过（可能是重复处理导致）: {src}", file=sys.stderr)
        return
    os.makedirs(os.path.dirname(dst) if os.path.dirname(dst) else '.', exist_ok=True)
    s.copy2(src, dst)
    try:
        os.remove(src)
    except Exception as e:
        print(f"[Warning] 已复制到 {dst}，但删除源文件失败，可能导致重复残留: {src} ({e})", file=sys.stderr)
import shutil
shutil.move = _safe_move_hijack

from datetime import datetime, timezone

from dotenv import load_dotenv
load_dotenv()

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn
from tenacity import retry, stop_after_attempt, wait_exponential

from mineru_client import MinerUClient
from llm_router import LLMRouter
from db_schema import ensure_papers_table, ensure_llm_quota_tables
from litreview.token_budget import request_reserved_tokens, usage_totals
from utils import init_dirs, build_note_metadata, generate_obsidian_note, export_to_excel, extract_key_sections, get_settings, calculate_pdf_hash, log_run_event, MIN_MARKDOWN_CHARS

# ── 配置获取 ──────────────────────────────────────────────────────────────────
settings = get_settings()

console = Console()

# ── 状态机扩充常量 (P0) ───────────────────────────────────────────────────────
STATUS_PARSED = "PARSED"
STATUS_SUBMIT_FAILED = "SUBMIT_FAILED"             # 提交 API 失败
STATUS_SUBMITTED = "BATCH_SUBMITTED"
STATUS_BATCH_FAILED = "BATCH_FAILED"               # 云端 Batch 本身失败（expired/failed/cancelled）
STATUS_RESULT_PARSE_FAILED = "RESULT_PARSE_FAILED" # 云端成功但下载或解析结果失败
STATUS_COMPLETED = "COMPLETED"
STATUS_EXPORTED = "EXPORTED"

# 自动重试重提交补偿状态集
RESUBMITTABLE_STATUSES = (STATUS_PARSED, STATUS_SUBMIT_FAILED, STATUS_BATCH_FAILED, STATUS_RESULT_PARSE_FAILED)
# 已在处理中/已完成，必须跳过的状态集
SKIP_STATUSES = (STATUS_SUBMITTED, STATUS_COMPLETED, STATUS_EXPORTED)


# ── 数据库 ──────────────────────────────────────────────────────────────────

def init_db() -> sqlite3.Connection:
    """初始化 SQLite 数据库，并自动建立父目录，添加元数据和错误诊断列。

    papers 的 DDL 在 db_schema.py —— 那是它唯一的定义处，litreview.store 也 import
    同一份，好让主管道没跑过的新库里 review.py / MCP 不至于撞 no such table。
    """
    os.makedirs(os.path.dirname(settings.DB_PATH), exist_ok=True)
    conn = sqlite3.connect(settings.DB_PATH)
    ensure_papers_table(
        conn,
        on_upgrade=lambda col: console.print(f"[yellow]⚠️ 数据库结构升级：已添加 {col} 列。"),
        on_error=lambda col, e: console.print(f"[red]数据库结构升级失败: {e}"),
    )
    ensure_llm_quota_tables(conn)
    conn.commit()
    return conn


# ── 阶段 1：解析 PDF 并提交 Batch 任务 ───────────────────────────────────────

def prepare_batch() -> dict:
    """
    扫描、解析 PDF 并提交 Batch 任务给 LLM 云端。
    返回本次准备任务的运行报告，便于 Daemon 实现静默推送通知。
    """
    console.rule("[bold blue]阶段 1: 解析与提交 Batch 任务 (享受 50% 折扣)")
    
    report = {
        "scanned": 0,
        "pdf_parsed_failed": 0,
        "submitted": 0,
        "submit_failed": 0
    }
    
    conn = init_db()
    c = conn.cursor()
    
    # 自动创建所需要的临时及输出文件夹
    init_dirs(
        settings.INPUT_PDF_DIR,
        settings.MINERU_OUTPUT_DIR,
        settings.OBSIDIAN_VAULT_DIR,
        settings.PROCESSED_PDF_DIR,
        settings.FAILED_PDF_DIR
    )

    mineru_client = MinerUClient(settings.MINERU_API_KEY)
    llm_router = LLMRouter()

    deepseek_requests = []
    gemini_requests = []

    # CAJ 预转换：在扫描 PDF 之前把 input_pdfs 下的 .caj 转成 .pdf，
    # 转换成功的文件会被下面的 glob("*.pdf") 自动捡起进入正常流程
    caj_files = glob.glob(os.path.join(settings.INPUT_PDF_DIR, "*.caj"))
    if caj_files:
        console.print(f"[cyan]🔄 检测到 {len(caj_files)} 个 CAJ 文件，开始转换为 PDF...")
        try:
            from caj_converter import convert_caj_files
            convert_caj_files()
        except Exception as e:
            console.print(f"[red]CAJ 转换模块运行异常，本轮跳过: {e}")

    pdf_files = glob.glob(os.path.join(settings.INPUT_PDF_DIR, "*.pdf"))
    
    # 扫描非 PDF 文件以进行 CAJ/其它不支持格式的感知
    all_files = glob.glob(os.path.join(settings.INPUT_PDF_DIR, "*"))
    unsupported_files = [f for f in all_files if os.path.isfile(f) and not f.lower().endswith(".pdf")]
    
    if unsupported_files:
        exts = sorted(list(set(os.path.splitext(f)[1].lower() for f in unsupported_files)))
        console.print(f"[yellow]⚠️ 发现 {len(unsupported_files)} 个不支持的文件格式 ({', '.join(exts)})，仅支持 PDF 格式。")

    if not pdf_files:
        console.print("[yellow]没有找到待处理的 PDF 文件，仅执行数据库残留记录检查。")

    # 扫描待处理文献：排除 SKIP_STATUSES 状态，包含 RESUBMITTABLE_STATUSES 自动捡起重试
    unprocessed_pdfs = []
    for pdf_path in pdf_files:
        doc_id = calculate_pdf_hash(pdf_path)
        c.execute("SELECT status, mineru_md, language FROM papers WHERE id=?", (doc_id,))
        row = c.fetchone()
        if row:
            status, mineru_md, lang = row
            if status in SKIP_STATUSES:
                # 若已入库（COMPLETED/EXPORTED）但仍留在输入目录，自动移动到归档目录防止堆积
                new_pdf_path = os.path.join(settings.PROCESSED_PDF_DIR, os.path.basename(pdf_path))
                if os.path.exists(pdf_path) and os.path.abspath(pdf_path) != os.path.abspath(new_pdf_path):
                    shutil.move(pdf_path, new_pdf_path)
                    c.execute("UPDATE papers SET pdf_path=? WHERE id=?", (new_pdf_path, doc_id))
                    conn.commit()
                continue
            # 优化：如果已经在数据库中存在解析完的 MD 文本，说明之前解析过但提交失败了，
            # 可以直接重用解析结果，不需要重复跑 MinerU
            if mineru_md and status in RESUBMITTABLE_STATUSES:
                deepseek_requests.append(llm_router.build_request(doc_id, mineru_md))
                # 将 PDF 移动到归档，防重复扫描
                new_pdf_path = os.path.join(settings.PROCESSED_PDF_DIR, os.path.basename(pdf_path))
                if os.path.exists(pdf_path):
                    shutil.move(pdf_path, new_pdf_path)
                c.execute("UPDATE papers SET pdf_path=? WHERE id=?", (new_pdf_path, doc_id))
                conn.commit()
                continue
                
        unprocessed_pdfs.append(pdf_path)

    # 每次运行最多处理 BATCH_SIZE_LIMIT 篇，控制速率
    new_to_parse = unprocessed_pdfs[:settings.BATCH_SIZE_LIMIT]
    report["scanned"] = len(new_to_parse)

    if new_to_parse:
        with Progress(
            SpinnerColumn(), TextColumn("[progress.description]{task.description}"), BarColumn(), console=console
        ) as progress:
            task = progress.add_task("[cyan]解析 PDF 并准备 Batch 数据...", total=len(new_to_parse))

            for pdf_path in new_to_parse:
                filename = os.path.basename(pdf_path)
                title = os.path.splitext(filename)[0]
                doc_id = calculate_pdf_hash(pdf_path)

                progress.update(task, description=f"[cyan]MinerU 解析: {filename}")
                try:
                    # 文件夹带上 8 位哈希以防同名冲突
                    output_folder = os.path.join(settings.MINERU_OUTPUT_DIR, f"{title}_{doc_id[:8]}")
                    mineru_res = mineru_client.process_pdf(pdf_path, output_folder)
                    md_text = mineru_res["markdown"]

                    # 空正文直接判永久失败：MinerU 返回 success 不代表抽出了字。
                    # 必须赶在下面 shutil.move 到 processed_pdfs 之前拦，否则这份 PDF 会被
                    # 当成"已处理"归档，人再也不会去看它，而库里多了一条纯属编造的分析。
                    if len(md_text.strip()) < MIN_MARKDOWN_CHARS:
                        err_msg = f"MinerU 解析出的正文只有 {len(md_text.strip())} 字符（阈值 {MIN_MARKDOWN_CHARS}），大概率是扫描件或纯图版 PDF"
                        console.print(f"[red]正文为空，跳过 {filename}: {err_msg}")
                        shutil.move(pdf_path, os.path.join(settings.FAILED_PDF_DIR, filename))
                        report["pdf_parsed_failed"] += 1
                        log_run_event(mode="batch", event="paper_parsed", title=title, doc_id=doc_id,
                                      status="failed", error=err_msg, extra={"type": "empty_markdown"})
                        progress.advance(task)
                        continue

                    docx_path = mineru_res.get("docx_path")
                    if docx_path and os.path.exists(docx_path):
                        docx_dir = os.path.join(os.path.dirname(settings.INPUT_PDF_DIR), "word_exports")
                        os.makedirs(docx_dir, exist_ok=True)
                        shutil.copy(docx_path, os.path.join(docx_dir, f"{title}.docx"))

                    lang = llm_router.detect_language(md_text)

                    # 提交前直接移动到归档路径，确保 DB 中的路径绝对不会挂空
                    new_pdf_path = os.path.join(settings.PROCESSED_PDF_DIR, filename)
                    shutil.move(pdf_path, new_pdf_path)

                    c.execute(
                        """INSERT OR REPLACE INTO papers
                           (id, title, pdf_path, language, mineru_md, images_dir, status, processed_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (doc_id, title, new_pdf_path, lang, md_text, mineru_res["images_dir"], STATUS_PARSED, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                    )
                    conn.commit()
                    log_run_event(mode="batch", event="paper_parsed", title=title, doc_id=doc_id, status="success")

                    deepseek_requests.append(llm_router.build_request(doc_id, md_text))

                except Exception as e:
                    import requests
                    err_msg = str(e).lower()
                    is_transient = (
                        any(kw in err_msg for kw in ["parsing failed", "try again later", "timeout"])
                        or isinstance(e, (TimeoutError, ConnectionError, requests.RequestException))
                    )
                    if is_transient:
                        console.print(f"[yellow]⚠️ 解析临时失败 (待重试) {filename}: {e}")
                        log_run_event(mode="batch", event="paper_parsed", title=title, doc_id=doc_id, status="failed", error=f"解析临时失败: {e}", extra={"type": "transient"})
                    else:
                        console.print(f"[red]解析永久失败 {filename}: {e}")
                        shutil.move(pdf_path, os.path.join(settings.FAILED_PDF_DIR, filename))
                        report["pdf_parsed_failed"] += 1
                        log_run_event(mode="batch", event="paper_parsed", title=title, doc_id=doc_id, status="failed", error=f"解析永久失败: {e}", extra={"type": "permanent"})

                progress.advance(task)

    # 自动拾起数据库中残留的已解析 (PARSED) 或提交失败 (SUBMIT_FAILED, BATCH_FAILED, RESULT_PARSE_FAILED) 记录进行补充提交
    # 这对守护进程中途崩溃/重启后的自动错峰重试至关重要，且对空扫描时的数据恢复极其重要！
    c.execute(
        "SELECT id, title, language, mineru_md FROM papers WHERE status IN (?, ?, ?, ?)",
        (STATUS_PARSED, STATUS_SUBMIT_FAILED, STATUS_BATCH_FAILED, STATUS_RESULT_PARSE_FAILED)
    )
    db_records = c.fetchall()
    
    # 过滤出当前尚未加入请求队列的记录
    existing_custom_ids = set(r["custom_id"] for r in deepseek_requests) | set(r["id"] for r in gemini_requests)
    
    added_from_db = 0
    for doc_id, title, lang, mineru_md in db_records:
        if doc_id in existing_custom_ids:
            continue
        if not mineru_md:
            continue
            
        deepseek_requests.append(llm_router.build_request(doc_id, mineru_md))
        added_from_db += 1
        
    if added_from_db > 0:
        console.print(f"[green]从数据库恢复并补充加载了 {added_from_db} 篇已解析但未成功提交的文献。")


    # 统一提交 DeepSeek Batch (在提交前确保 custom_id 严格去重，以防 API 校验失败)
    if deepseek_requests:
        seen_custom_ids = set()
        deduped_deepseek_requests = []
        for req in deepseek_requests:
            cid = req["custom_id"]
            if cid not in seen_custom_ids:
                seen_custom_ids.add(cid)
                deduped_deepseek_requests.append(req)
        sub_cnt, fail_cnt = _submit_openai_batches(deduped_deepseek_requests, llm_router, c, conn)
        report["submitted"] += sub_cnt
        report["submit_failed"] += fail_cnt

    # 统一提交 Gemini Batch
    if gemini_requests:
        sub_cnt, fail_cnt = _submit_gemini_batch(gemini_requests, llm_router, c, conn)
        report["submitted"] += sub_cnt
        report["submit_failed"] += fail_cnt

    conn.close()
    return report


def _utc_date() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _quota_available(c) -> int:
    today = _utc_date()
    now = datetime.now(timezone.utc).isoformat()
    # 与同步的元数据/证据调用共用同一模型账本。否则两条路径各自跑满 200 万，
    # 实际会超过 gpt-5.4-mini 的项目日上限。
    c.execute("INSERT OR IGNORE INTO llm_usage_model_daily (utc_date, model, updated_at) VALUES (?, ?, ?)",
              (today, settings.ANALYSIS_MODEL, now))
    c.execute("SELECT reserved_tokens, actual_total_tokens FROM llm_usage_model_daily WHERE utc_date=? AND model=?",
              (today, settings.ANALYSIS_MODEL))
    reserved, actual = c.fetchone()
    return max(0, settings.LLM_DAILY_TOKEN_CAP - reserved - actual)


def _split_for_quota(requests: list, available: int) -> list[list]:
    """按 600k/当日剩余额度切组；一篇超出单批预算时不提交。"""
    batches, current, current_tokens = [], [], 0
    limit = min(settings.LLM_BATCH_TOKEN_CAP, available)
    for request in requests:
        reserved = request_reserved_tokens(request)
        if reserved > settings.LLM_BATCH_TOKEN_CAP:
            console.print(f"[red]跳过 {request['custom_id'][:8]}：单篇预估 {reserved} token 超过单批上限。")
            continue
        if current and current_tokens + reserved > limit:
            batches.append(current)
            available -= current_tokens
            current, current_tokens, limit = [], 0, min(settings.LLM_BATCH_TOKEN_CAP, available)
        if reserved <= limit:
            current.append(request)
            current_tokens += reserved
    if current:
        batches.append(current)
    return batches


def _submit_openai_batches(requests: list, llm_router: LLMRouter, c, conn) -> tuple[int, int]:
    available = _quota_available(c)
    batches = _split_for_quota(requests, available)
    if not batches:
        console.print(f"[yellow]UTC {_utc_date()} 可用生成额度不足；等待下一次 UTC 00:00 重置。")
        return 0, 0
    # 每次只提交一个批次：必须等取回真实 usage 后才允许递推下一批。
    return _submit_openai_batch(batches[0], llm_router, c, conn)


def _submit_openai_batch(requests: list, llm_router: LLMRouter, c, conn) -> tuple[int, int]:
    reserved_tokens = sum(request_reserved_tokens(r) for r in requests)
    console.print(f"[green]提交 {len(requests)} 个任务到 OpenAI Batch（预留 {reserved_tokens} token）...")
    ds_file = "openai_batch.jsonl"
    with open(ds_file, "w", encoding="utf-8") as f:
        for r in requests:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
            
    try:
        ds_client = llm_router.openai_client
        
        @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10), reraise=True)
        def do_submit():
            with open(ds_file, "rb") as fh:
                file_obj = ds_client.files.create(file=fh, purpose="batch")
            
            file_id = getattr(file_obj, "id", None)
            if not file_id:
                raise ValueError(f"无法从 FileObject 提取 file_id: {file_obj}")
            
            # 🛡️ 针对 OpenAI 分布式存储最终一致性的就绪等待与轮询校验 (防 Cannot find file 异步竞态)
            for attempt in range(8):
                try:
                    finfo = ds_client.files.retrieve(file_id)
                    fstatus = getattr(finfo, "status", None)
                    if fstatus == "processed":
                        break
                except Exception:
                    pass
                time.sleep(1.5)
                
            return ds_client.batches.create(
                input_file_id=file_id,
                endpoint="/v1/chat/completions",
                completion_window="24h",
            )
            
        batch_job = do_submit()
        batch_id = getattr(batch_job, "id", None)
        if not batch_id:
            raise ValueError(f"无法从 Batch 提取 batch_id: {batch_job}")
        now = datetime.now(timezone.utc).isoformat()
        utc_date = _utc_date()
        c.execute("INSERT INTO llm_batches (batch_id, model, utc_date, reserved_tokens, status, created_at, updated_at) VALUES (?, ?, ?, ?, 'submitted', ?, ?)",
                  (batch_id, llm_router.openai_model, utc_date, reserved_tokens, now, now))
        c.execute("INSERT OR IGNORE INTO llm_usage_daily (utc_date, updated_at) VALUES (?, ?)", (utc_date, now))
        c.execute("INSERT OR IGNORE INTO llm_usage_model_daily (utc_date, model, updated_at) VALUES (?, ?, ?)",
                  (utc_date, llm_router.openai_model, now))
        c.execute("UPDATE llm_usage_daily SET reserved_tokens=reserved_tokens+?, submitted_batches=submitted_batches+1, updated_at=? WHERE utc_date=?",
                  (reserved_tokens, now, utc_date))
        c.execute("UPDATE llm_usage_model_daily SET reserved_tokens=reserved_tokens+?, updated_at=? WHERE utc_date=? AND model=?",
                  (reserved_tokens, now, utc_date, llm_router.openai_model))
        console.print(f"[bold green]OpenAI Batch 提交成功！ID: {batch_id}")
        
        for req in requests:
            c.execute(
                "UPDATE papers SET status=?, batch_provider='openai', batch_job_id=?, error_message=NULL WHERE id=?",
                (STATUS_SUBMITTED, batch_id, req["custom_id"]),
            )
        conn.commit()
        log_run_event(mode="batch", event="batch_submitted", status="success", extra={"batch_id": batch_id, "provider": "openai", "model": llm_router.openai_model, "count": len(requests), "reserved_tokens": reserved_tokens})
        
        # 成功后移除临时 jsonl
        if os.path.exists(ds_file):
            os.remove(ds_file)
        return len(requests), 0
        
    except Exception as e:
        err_msg = f"OpenAI Batch 提交失败: {str(e)}"
        console.print(f"[bold red]{err_msg}")
        
        # 将本轮队列中的文献在 DB 中全部标为 SUBMIT_FAILED 并保存错误原因以防卡死
        for req in requests:
            c.execute(
                "UPDATE papers SET status=?, error_message=? WHERE id=?",
                (STATUS_SUBMIT_FAILED, err_msg, req["custom_id"]),
            )
        conn.commit()
        log_run_event(mode="batch", event="batch_submitted", status="failed", error=err_msg, extra={"provider": "openai", "count": len(requests)})
        
        if os.path.exists(ds_file):
            os.remove(ds_file)
        return 0, len(requests)


def _submit_gemini_batch(requests: list, llm_router: LLMRouter, c, conn) -> tuple[int, int]:
    console.print(f"[green]提交 {len(requests)} 个任务到 Gemini Batch API...")
    gm_file = "gemini_batch.jsonl"
    with open(gm_file, "w", encoding="utf-8") as f:
        for r in requests:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
            
    try:
        gm_client = llm_router.gemini_client
        if gm_client is None:
            raise ValueError("拉取或提交历史 Gemini Batch 任务需要配置 GEMINI_API_KEY")
        
        @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10), reraise=True)
        def do_submit():
            uploaded_file = gm_client.files.upload(file=gm_file, config={"mimeType": "text/plain"})
            return gm_client.batches.create(model="gemini-2.5-pro", src=uploaded_file.name)
            
        batch_job = do_submit()
        batch_name = batch_job.name
        console.print(f"[bold green]Gemini Batch 提交成功！Name: {batch_name}")
        
        for req in requests:
            c.execute(
                "UPDATE papers SET status=?, batch_provider='gemini', batch_job_id=?, error_message=NULL WHERE id=?",
                (STATUS_SUBMITTED, batch_name, req["id"]),
            )
        conn.commit()
        
        if os.path.exists(gm_file):
            os.remove(gm_file)
        return len(requests), 0
        
    except Exception as e:
        err_msg = f"Gemini Batch 提交失败: {str(e)}"
        console.print(f"[bold red]{err_msg}")
        
        # 记录提交异常，转为提交失败状态
        for req in requests:
            c.execute(
                "UPDATE papers SET status=?, error_message=? WHERE id=?",
                (STATUS_SUBMIT_FAILED, err_msg, req["id"]),
            )
        conn.commit()
        
        if os.path.exists(gm_file):
            os.remove(gm_file)
        return 0, len(requests)


# ── 阶段 2：获取 Batch 结果并导出 ────────────────────────────────────────────

def fetch_batch() -> dict:
    """
    检查云端 Batch 任务进度，下载完成的 AI 响应结果并导出至知识库。
    返回本次拉取任务的运行报告，便于 Daemon 实现静默推送。
    """
    console.rule("[bold blue]阶段 2: 检查 Batch 结果并生成输出")
    
    report = {
        "jobs_checked": 0,
        "completed": 0,
        "failed": 0,
        "exported": 0
    }
    
    conn = init_db()
    c = conn.cursor()
    llm_router = LLMRouter()

    c.execute("SELECT DISTINCT batch_job_id, batch_provider FROM papers WHERE status=?", (STATUS_SUBMITTED,))
    jobs = c.fetchall()

    if not jobs:
        console.print("[yellow]当前没有等待拉取结果的 Batch 任务。")
    else:
        report["jobs_checked"] = len(jobs)
        for job_id, provider in jobs:
            console.print(f"检查 {provider} 任务: {job_id}")
            try:
                if provider == "openai":
                    comp_cnt, fail_cnt = _fetch_openai_results(job_id, llm_router, c, conn)
                elif provider == "deepseek":  # 仅拉取迁移前已提交任务，不会创建新任务
                    from llm_router import make_legacy_siliconflow_client
                    comp_cnt, fail_cnt = _fetch_deepseek_results(job_id, make_legacy_siliconflow_client(), c, conn)
                elif provider == "gemini":
                    comp_cnt, fail_cnt = _fetch_gemini_results(job_id, llm_router, c, conn)
                else:
                    comp_cnt, fail_cnt = 0, 0
                report["completed"] += comp_cnt
                report["failed"] += fail_cnt
            except Exception as e:
                console.print(f"[red]检查云端任务 {job_id} 时发生未知崩溃: {e}")

    # 导出 COMPLETED 文献
    report["exported"] = _export_completed(c, conn)
    
    conn.close()
    return report


def _mark_batch_failed(job_id: str, err_msg: str, c, conn) -> int:
    """把整个 Batch 涉及的论文标成 BATCH_FAILED 并闭环，返回受影响篇数。

    BATCH_FAILED 在下一轮阶段 1 的「自动拾起」名单里，所以标记之后这些论文会被
    重新提交 —— 这正是它跟「卡在 BATCH_SUBMITTED」的本质区别：前者能自愈，后者是死局。
    """
    c.execute("SELECT id FROM papers WHERE batch_job_id=?", (job_id,))
    rows = c.fetchall()
    for r in rows:
        c.execute("UPDATE papers SET status=?, error_message=? WHERE id=?", (STATUS_BATCH_FAILED, err_msg, r[0]))
    conn.commit()
    log_run_event(mode="batch", event="batch_fetched", status="failed", error=err_msg,
                  extra={"batch_id": job_id, "provider": "deepseek", "count": len(rows)})
    return len(rows)


def _record_batch_usage(job_id: str, usage, c, conn, status: str) -> None:
    """幂等落真实 usage；total_tokens 含输出/推理消耗时以它为唯一额度依据。"""
    ensure_llm_quota_tables(conn)
    c.execute("SELECT utc_date, model, reserved_tokens, usage_recorded FROM llm_batches WHERE batch_id=?", (job_id,))
    batch = c.fetchone()
    if not batch or batch[3]:
        return
    utc_date, model, reserved, _ = batch
    inp, out, reasoning, total = usage_totals(usage)
    now = datetime.now(timezone.utc).isoformat()
    # 预留只用于防止并发超额。真实 usage 到账后释放预留并累计实际 total。
    c.execute("UPDATE llm_usage_daily SET reserved_tokens=MAX(0, reserved_tokens-?), actual_input_tokens=actual_input_tokens+?, actual_output_tokens=actual_output_tokens+?, actual_reasoning_tokens=actual_reasoning_tokens+?, actual_total_tokens=actual_total_tokens+?, completed_batches=completed_batches+?, failed_batches=failed_batches+?, updated_at=? WHERE utc_date=?",
              (reserved, inp, out, reasoning, total, int(status != 'failed'), int(status == 'failed'), now, utc_date))
    c.execute("UPDATE llm_usage_model_daily SET reserved_tokens=MAX(0, reserved_tokens-?), actual_input_tokens=actual_input_tokens+?, actual_output_tokens=actual_output_tokens+?, actual_reasoning_tokens=actual_reasoning_tokens+?, actual_total_tokens=actual_total_tokens+?, updated_at=? WHERE utc_date=? AND model=?",
              (reserved, inp, out, reasoning, total, now, utc_date, model))
    c.execute("UPDATE llm_batches SET actual_tokens=?, status=?, usage_recorded=1, updated_at=? WHERE batch_id=?",
              (total, status, now, job_id))
    conn.commit()


def _record_batch_unknown_failure(job_id: str, c, conn) -> None:
    """云端失败但拿不到 usage：保留预留，宁可少跑也绝不超出当日额度。"""
    ensure_llm_quota_tables(conn)
    now = datetime.now(timezone.utc).isoformat()
    c.execute("UPDATE llm_batches SET status='failed_unknown_usage', updated_at=? WHERE batch_id=? AND usage_recorded=0", (now, job_id))
    c.execute("UPDATE llm_usage_daily SET failed_batches=failed_batches+1, updated_at=? WHERE utc_date=(SELECT utc_date FROM llm_batches WHERE batch_id=?)", (now, job_id))
    conn.commit()


def _read_batch_file(file_ref: str, client) -> str:
    """读取 Batch 的输出/错误文件。

    硅基流动在 output_file_id / error_file_id 里放的是**完整 OSS URL**而不是 file id，
    照着 OpenAI 的约定去调 /v1/files/{id}/content 只会拿到 404。两种形式都得认。
    """
    if file_ref.startswith("http://") or file_ref.startswith("https://"):
        import httpx
        resp = httpx.get(file_ref, timeout=120)
        resp.raise_for_status()
        return resp.text
    return client.files.content(file_ref).content.decode("utf-8")


def _batch_request_count(status, name: str):
    """读取 SDK 对象或原始 dict 中的 Batch request_counts 字段。"""
    counts = getattr(status, "request_counts", None)
    if counts is None and hasattr(status, "data") and isinstance(status.data, dict):
        counts = status.data.get("request_counts")
    value = getattr(counts, name, None)
    if value is None and isinstance(counts, dict):
        value = counts.get(name)
    return value


def _mark_failed_batch_lines(job_id: str, error_content: str, c, conn,
                             fallback_count: int = 0) -> int:
    """把错误文件中的单条请求闭环，返回本次标记的篇数。

    SiliconFlow 的 Batch 允许一个任务里同时存在成功和失败请求。错误文件格式在
    不同兼容层上略有差异，但都会保留 custom_id；如果错误文件无法提供 custom_id，
    用该 Batch 尚未完成的记录作为兜底，避免留下永久的 BATCH_SUBMITTED。
    """
    failed = {}
    for line in error_content.splitlines():
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        custom_id = raw.get("custom_id")
        if not custom_id:
            continue
        detail = raw.get("error") or raw.get("response") or raw
        failed[custom_id] = f"Batch 单条请求失败: {str(detail)[:500]}"

    if failed:
        rows = [(doc_id, msg) for doc_id, msg in failed.items()]
    elif fallback_count:
        c.execute("SELECT id FROM papers WHERE batch_job_id=? AND status=?",
                  (job_id, STATUS_SUBMITTED))
        rows = [(row[0], "Batch 单条请求失败（错误文件未返回 custom_id）")
                for row in c.fetchall()[:fallback_count]]
    else:
        rows = []

    updated = 0
    for doc_id, msg in rows:
        cursor = c.execute("UPDATE papers SET status=?, error_message=? "
                           "WHERE id=? AND status=?",
                           (STATUS_BATCH_FAILED, msg, doc_id, STATUS_SUBMITTED))
        updated += cursor.rowcount
    conn.commit()
    return updated


def _fetch_openai_results(job_id: str, llm_router: LLMRouter, c, conn) -> tuple[int, int]:
    ds_client = getattr(llm_router, "openai_client", None) or llm_router.deepseek_client

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10), reraise=True)
    def do_fetch_status():
        return ds_client.batches.retrieve(job_id)
        
    status = do_fetch_status()
    status_val = getattr(status, "status", None)
    if not status_val and hasattr(status, "data") and isinstance(status.data, dict):
        status_val = status.data.get("status")
        
    console.print(f"  OpenAI 云端状态: {status_val}")
    
    # 云端成功：正常解析
    if status_val == "completed":
        output_file_id = getattr(status, "output_file_id", None)
        if not output_file_id and hasattr(status, "data") and isinstance(status.data, dict):
            output_file_id = status.data.get("output_file_id")
            
        if not output_file_id:
            # 云端说 completed 却没有输出文件，只有一种情形：这批里没有任何一条请求跑成功，
            # 结果全在 error_file 里（典型触发条件是模型本身不可用，如 2026-07 的 DeepSeek-V3）。
            #
            # 此前这里直接 raise，异常被外层的「未知崩溃」兜底吞掉，论文就永远停在
            # BATCH_SUBMITTED —— 而 BATCH_SUBMITTED 不在阶段 1 的自动拾起名单里，等于永久卡死，
            # 且每一轮拉取都要再查一遍这个死批次。改成走 BATCH_FAILED 才能被重新提交。
            done = _batch_request_count(status, "completed")
            if done:
                # 有成功条目却拿不到输出文件，那是另一回事，照旧抛出交给上层记录
                raise ValueError(f"无法从 Batch 提取 output_file_id: {status}")

            error_file_id = getattr(status, "error_file_id", None)
            if not error_file_id and hasattr(status, "data") and isinstance(status.data, dict):
                error_file_id = status.data.get("error_file_id")
            detail = ""
            if error_file_id:
                try:
                    detail = _read_batch_file(error_file_id, ds_client).strip()[:500]
                except Exception as e:
                    detail = f"(错误文件拉取失败: {e})"
            err_msg = f"OpenAI Batch 全部请求失败，云端无输出文件。错误详情: {detail}"
            console.print(f"[bold red]  {err_msg}")
            _record_batch_unknown_failure(job_id, c, conn)
            return 0, _mark_batch_failed(job_id, err_msg, c, conn)

        @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10), reraise=True)
        def do_download():
            return _read_batch_file(output_file_id, ds_client)

        result_content = do_download()
        success_count = 0
        fail_count = 0
        
        for line in result_content.splitlines():
            if not line.strip():
                continue
            try:
                res = json.loads(line)
                doc_id = res["custom_id"]
                body = res["response"]["body"]
                content = body["choices"][0]["message"]["content"]
                c.execute("UPDATE papers SET status=?, result_json=?, error_message=NULL WHERE id=?", (STATUS_COMPLETED, content, doc_id))
                success_count += 1
            except Exception as e:
                # 某一行的解析/下载出错，设为 RESULT_PARSE_FAILED 防止整批卡住
                try:
                    res_raw = json.loads(line)
                    doc_id = res_raw.get("custom_id")
                    if doc_id:
                        c.execute("UPDATE papers SET status=?, error_message=? WHERE id=?", (STATUS_RESULT_PARSE_FAILED, f"解析行失败: {str(e)}", doc_id))
                except Exception:
                    pass
                console.print(f"[yellow]解析结果行失败: {e}")
                fail_count += 1

        # 有 output_file 并不代表整批成功：失败请求会单独进入 error_file。
        # 若不处理这里，混合批次中的失败文献会永久停在 BATCH_SUBMITTED。
        error_file_id = getattr(status, "error_file_id", None)
        if not error_file_id and hasattr(status, "data") and isinstance(status.data, dict):
            error_file_id = status.data.get("error_file_id")
        if error_file_id:
            try:
                failed_content = _read_batch_file(error_file_id, ds_client)
                failed_count = _mark_failed_batch_lines(
                    job_id, failed_content, c, conn,
                    fallback_count=_batch_request_count(status, "failed") or 0,
                )
                fail_count += failed_count
            except Exception as e:
                console.print(f"[yellow]读取 Batch 错误文件失败: {e}")
        # Batch 输出 JSONL 每条都有 usage；汇总后一次性记入当天账本。
        usages = []
        for line in result_content.splitlines():
            try:
                raw = json.loads(line)
                usages.append(raw.get("response", {}).get("body", {}).get("usage"))
            except json.JSONDecodeError:
                pass
        input_tokens = output_tokens = reasoning_tokens = total_tokens = 0
        for usage in usages:
            i, o, r, t = usage_totals(usage)
            input_tokens += i; output_tokens += o; reasoning_tokens += r; total_tokens += t
        _record_batch_usage(job_id, {"prompt_tokens": input_tokens, "completion_tokens": output_tokens,
                                     "total_tokens": total_tokens,
                                     "completion_tokens_details": {"reasoning_tokens": reasoning_tokens}}, c, conn, "completed")
        conn.commit()
        console.print(f"[green]OpenAI 结果拉取完毕，成功 {success_count} 篇，失败 {fail_count} 篇，实际 {total_tokens} token。")
        log_run_event(mode="batch", event="batch_fetched", status="success", extra={"batch_id": job_id, "provider": "openai", "completed": success_count, "failed": fail_count, "actual_tokens": total_tokens})
        return success_count, fail_count
        
    # 云端失败：提取错误明细，并更新整批论文状态以便守护进程下一轮自动恢复重试
    elif status_val in ("failed", "cancelled", "expired"):
        err_details = []
        if hasattr(status, "errors") and getattr(status.errors, "data", None):
            for ed in status.errors.data:
                code_txt = getattr(ed, "code", "")
                msg_txt = getattr(ed, "message", "")
                err_details.append(f"[{code_txt}] {msg_txt}")
        detail_str = "; ".join(err_details) if err_details else ""
        err_msg = f"OpenAI 任务在云端夭折，返回状态: {status_val}" + (f" (原因: {detail_str})" if detail_str else "")
        console.print(f"[bold red]  {err_msg}")
        _record_batch_unknown_failure(job_id, c, conn)
        return 0, _mark_batch_failed(job_id, err_msg, c, conn)
        
    # 其余 pending/processing 状态保持不变
    return 0, 0


def _fetch_deepseek_results(job_id: str, client, c, conn) -> tuple[int, int]:
    """迁移前的 SiliconFlow Batch 兼容拉取入口；不计入 GPT 配额账本。"""
    class _LegacyRouter:
        deepseek_client = getattr(client, "deepseek_client", client)
    return _fetch_openai_results(job_id, _LegacyRouter(), c, conn)


def _fetch_gemini_results(job_id: str, llm_router: LLMRouter, c, conn) -> tuple[int, int]:
    gm_client = llm_router.gemini_client
    if gm_client is None:
        raise ValueError("拉取历史 Gemini Batch 任务需要配置 GEMINI_API_KEY")
    
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10), reraise=True)
    def do_fetch_status():
        return gm_client.batches.get(name=job_id)
        
    status = do_fetch_status()
    state_name = status.state.name if hasattr(status.state, "name") else str(status.state)
    console.print(f"  Gemini 云端状态: {state_name}")

    if "SUCCEEDED" in state_name:
        success_count = 0
        fail_count = 0
        try:
            dest = getattr(status, "dest", None) or getattr(status, "output_resource", None)
            file_name = getattr(dest, "file_name", None) or getattr(dest, "name", None)

            if file_name:
                import httpx
                @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10), reraise=True)
                def do_download():
                    result_file = gm_client.files.get(name=file_name)
                    resp = httpx.get(result_file.uri, timeout=120)
                    resp.raise_for_status()
                    return resp
                
                resp = do_download()
                for line in resp.text.splitlines():
                    if not line.strip():
                        continue
                    try:
                        res = json.loads(line)
                        doc_id = res.get("id") or res.get("custom_id")
                        content = res["response"]["candidates"][0]["content"]["parts"][0]["text"]
                        c.execute(
                            "UPDATE papers SET status=?, result_json=?, error_message=NULL WHERE id=?",
                            (STATUS_COMPLETED, content, doc_id),
                        )
                        success_count += 1
                    except Exception as e:
                        try:
                            res_raw = json.loads(line)
                            doc_id = res_raw.get("id") or res_raw.get("custom_id")
                            if doc_id:
                                c.execute("UPDATE papers SET status=?, error_message=? WHERE id=?", (STATUS_RESULT_PARSE_FAILED, f"解析行失败: {str(e)}", doc_id))
                        except Exception:
                            pass
                        console.print(f"[yellow]解析 Gemini 结果行失败: {e}")
                        fail_count += 1
                conn.commit()
                console.print(f"[green]Gemini 结果拉取完毕，成功 {success_count} 篇，失败 {fail_count} 篇。")
                log_run_event(mode="batch", event="batch_fetched", status="success", extra={"batch_id": job_id, "provider": "gemini", "completed": success_count, "failed": fail_count})
                return success_count, fail_count
            else:
                err_msg = f"Gemini 云端完成，但 SDK 未返回文件名。请手动下载，output_uri: {getattr(status, 'output_uri', '未知')}"
                console.print(f"[yellow]  {err_msg}")
                # 这种情况也转入解析失败，避免卡住
                c.execute("SELECT id FROM papers WHERE batch_job_id=?", (job_id,))
                rows = c.fetchall()
                for r in rows:
                    c.execute("UPDATE papers SET status=?, error_message=? WHERE id=?", (STATUS_RESULT_PARSE_FAILED, err_msg, r[0]))
                conn.commit()
                log_run_event(mode="batch", event="batch_fetched", status="failed", error=err_msg, extra={"batch_id": job_id, "provider": "gemini", "count": len(rows)})
                return 0, len(rows)

        except Exception as e:
            err_msg = f"自动下载并解析 Gemini 结果出现故障: {str(e)}"
            console.print(f"[yellow]  {err_msg}")
            c.execute("SELECT id FROM papers WHERE batch_job_id=?", (job_id,))
            rows = c.fetchall()
            for r in rows:
                c.execute("UPDATE papers SET status=?, error_message=? WHERE id=?", (STATUS_RESULT_PARSE_FAILED, err_msg, r[0]))
            conn.commit()
            log_run_event(mode="batch", event="batch_fetched", status="failed", error=err_msg, extra={"batch_id": job_id, "provider": "gemini", "count": len(rows)})
            return 0, len(rows)

    elif state_name in ("FAILED", "CANCELED", "CANCELLED"):
        err_msg = f"Gemini 任务在云端失效，返回状态: {state_name}"
        console.print(f"[bold red]  {err_msg}")
        c.execute("SELECT id FROM papers WHERE batch_job_id=?", (job_id,))
        rows = c.fetchall()
        for r in rows:
            c.execute("UPDATE papers SET status=?, error_message=? WHERE id=?", (STATUS_BATCH_FAILED, err_msg, r[0]))
        conn.commit()
        log_run_event(mode="batch", event="batch_fetched", status="failed", error=err_msg, extra={"batch_id": job_id, "provider": "gemini", "count": len(rows)})
        return 0, len(rows)

    return 0, 0


def _export_completed(c, conn) -> int:
    """将所有 COMPLETED 状态的论文批量导出为 Obsidian 笔记和 Excel 并归档。
    # 限制单次导出最多 BATCH_SIZE_LIMIT 篇以平抑速率，按文献唯一ID进行 Excel 导出及防重写。
    """
    c.execute(
        f"SELECT id, title, pdf_path, language, images_dir, result_json, batch_provider, batch_job_id, mineru_md FROM papers WHERE status=? LIMIT {settings.BATCH_SIZE_LIMIT}",
        (STATUS_COMPLETED,),
    )
    completed = c.fetchall()
    if not completed:
        console.print("[dim]当前没有新完成的文献需要导出。")
        return 0

    from litreview.analysis_record import PaperAnalysisRecord

    excel_rows = []
    exported = []  # (doc_id, title)：本轮笔记已生成、待 Excel 落盘后统一标 EXPORTED
    for doc_id, title, pdf_path, lang, images_dir, result_json, provider, job_id, md_text in completed:
        # 单篇隔离：任何一篇出错只把它自己转入失败态，绝不拖累前后文献。
        # 失败态用 RESULT_PARSE_FAILED —— 它在 RESUBMITTABLE_STATUSES 里，下一轮
        # prepare_batch 会自动重新提交；若仍停在 COMPLETED，无 ORDER BY 的 LIMIT
        # 查询会反复先取到它，后面的文献永远轮不到。
        try:
            note_meta = build_note_metadata(title, md_text or "", lang)
            analysis = PaperAnalysisRecord.from_llm_json(
                result_json,
                markdown_text=md_text or "",
                paper_meta=note_meta,
            )
            # 与实时管道一致：LLM 返回的 JSON 解析失败时绝不把垃圾内容导出为终态
            if analysis.error:
                raise ValueError(f"分析结果解析失败: {analysis.error}")

            images = []
            if images_dir and os.path.exists(images_dir):
                for ext in ["*.png", "*.jpg", "*.jpeg"]:
                    images.extend(glob.glob(os.path.join(images_dir, "**", ext), recursive=True))
            images = [os.path.abspath(img) for img in images]

            # 导出 Obsidian，携带 doc_id 以确保生成带哈希的安全跨平台相对附件笔记
            note_path = generate_obsidian_note(note_meta, analysis, images, settings.OBSIDIAN_VAULT_DIR, doc_id)
            note_filename = os.path.basename(note_path)
            obsidian_link_name = os.path.splitext(note_filename)[0]

            # 收集 Excel，通过深模块自动格式化与列对齐
            excel_rows.append(analysis.to_excel_row(
                doc_id=doc_id,
                title=title,
                pdf_path=pdf_path,
                note_name=obsidian_link_name,
                status="EXPORTED",
                provider=provider,
                job_id=job_id,
                processed_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ))
            exported.append((doc_id, title))
        except Exception as e:
            err_msg = f"导出失败，已转入重提交队列: {str(e)[:500]}"
            console.print(f"[yellow]⚠️ {title}: {err_msg}")
            c.execute("UPDATE papers SET status=?, error_message=? WHERE id=?",
                      (STATUS_RESULT_PARSE_FAILED, err_msg, doc_id))
            conn.commit()
            log_run_event(mode="batch", event="paper_exported", title=title, doc_id=doc_id, status="failed", error=err_msg)

    if not excel_rows:
        return 0

    # 先落 Excel，再统一标 EXPORTED：Excel 写失败时这些文献仍停在 COMPLETED，
    # 下一轮会原样重导（笔记按 doc_id 覆盖、Excel 按文献ID去重，均幂等），不会出现
    # 「数据库已 EXPORTED 但 Excel 里没有」的孤儿。
    try:
        export_to_excel(excel_rows, settings.EXCEL_OUTPUT_PATH)
    except Exception as e:
        console.print(f"[red]Excel 写入失败，{len(excel_rows)} 篇文献保留 COMPLETED 待下轮重导: {e}")
        log_run_event(mode="batch", event="excel_export", status="failed", error=str(e), extra={"count": len(excel_rows)})
        return 0

    # 单事务移至导出终态
    with conn:
        c.executemany("UPDATE papers SET status=?, error_message=NULL WHERE id=?",
                      [(STATUS_EXPORTED, doc_id) for doc_id, _ in exported])
    for doc_id, title in exported:
        log_run_event(mode="batch", event="paper_exported", title=title, doc_id=doc_id, status="success")
        console.print(f"[green]✅ 成功导出: {title}")
    console.print(f"[bold green]共导出 {len(excel_rows)} 篇文献到 {settings.EXCEL_OUTPUT_PATH}")

    # 导出后自动立即增量更新综述 4096 维 Qwen3 向量索引与结构化元数据（即时处理，无需夜间等待）
    if settings.REVIEW_AUTO_INDEX:
        try:
            from litreview.indexer import build_index
            exported_ids = [r["文献ID"] for r in excel_rows]
            build_index(settings=settings, console=console, doc_ids=exported_ids)
        except Exception as e:
            console.print(f"[yellow]⚠️ 综述向量索引自动更新失败（不影响导出，可手动重试）: {e}")
        try:
            from llm_router import make_openai_client
            from litreview.enrich import run_enrich
            llm_client, _ = make_openai_client(quiet=True)
            run_enrich(settings, console, llm_client, use_llm=settings.REVIEW_ENRICH_LLM)
        except Exception as e:
            console.print(f"[yellow]⚠️ 元数据自动提取失败（不影响导出，可手动重试）: {e}")

    return len(excel_rows)


# ── 入口 ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "check":
        fetch_batch()
    else:
        prepare_batch()
        console.print(
            "\n[bold cyan]💡 提示：Batch 任务通常需要一段时间（最多 24 小时）。"
            "\n请稍后运行 `python batch_pipeline.py check` 获取结果并生成笔记。"
        )
