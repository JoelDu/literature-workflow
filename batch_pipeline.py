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

from datetime import datetime

from dotenv import load_dotenv
load_dotenv()

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn
from tenacity import retry, stop_after_attempt, wait_exponential

from mineru_client import MinerUClient
from llm_router import LLMRouter
from utils import init_dirs, generate_obsidian_note, export_to_excel, extract_key_sections, get_settings, calculate_pdf_hash, log_run_event

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
    """初始化 SQLite 数据库，并自动建立父目录，添加元数据和错误诊断列。"""
    os.makedirs(os.path.dirname(settings.DB_PATH), exist_ok=True)
    conn = sqlite3.connect(settings.DB_PATH)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS papers (
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
    )""")
    
    # 动态检查缺失列进行热升级，防升级卡顿
    c.execute("PRAGMA table_info(papers)")
    existing_cols = {col[1] for col in c.fetchall()}
    for col_name, col_type in [("error_message", "TEXT"), ("processed_at", "TEXT")]:
        if col_name not in existing_cols:
            try:
                c.execute(f"ALTER TABLE papers ADD COLUMN {col_name} {col_type}")
                console.print(f"[yellow]⚠️ 数据库结构升级：已添加 {col_name} 列。")
            except Exception as e:
                console.print(f"[red]数据库结构升级失败: {e}")
                
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
    llm_router = LLMRouter(settings.DEEPSEEK_API_KEY, settings.GEMINI_API_KEY)

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
                continue
            # 优化：如果已经在数据库中存在解析完的 MD 文本，说明之前解析过但提交失败了，
            # 可以直接重用解析结果，不需要重复跑 MinerU
            if mineru_md and status in RESUBMITTABLE_STATUSES:
                truncated = extract_key_sections(mineru_md)
                # 根据语言直接构建大模型请求
                # 统一使用 DeepSeek V3 处理中英文 (Gemini Key 目前已暂停)
                deepseek_requests.append({
                    "custom_id": doc_id,
                    "method": "POST",
                    "url": "/v1/chat/completions",
                    "body": {
                        "model": llm_router.deepseek_model,
                        "messages": [
                            {"role": "system", "content": llm_router.system_prompt},
                            {"role": "user", "content": f"请分析以下论文内容：\n\n{truncated}"},
                        ],
                        "response_format": {"type": "json_object"},
                    },
                })
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
                    
                    docx_path = mineru_res.get("docx_path")
                    if docx_path and os.path.exists(docx_path):
                        docx_dir = os.path.join(os.path.dirname(settings.INPUT_PDF_DIR), "word_exports")
                        os.makedirs(docx_dir, exist_ok=True)
                        shutil.copy(docx_path, os.path.join(docx_dir, f"{title}.docx"))

                    lang = llm_router.detect_language(md_text)
                    truncated = extract_key_sections(md_text)

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

                    # 按语言路由将哈希 doc_id 作为 API 端 custom_id
                    # 统一使用 DeepSeek V3 处理中英文 (Gemini Key 目前已暂停)
                    deepseek_requests.append({
                        "custom_id": doc_id,
                        "method": "POST",
                        "url": "/v1/chat/completions",
                        "body": {
                            "model": llm_router.deepseek_model,
                            "messages": [
                                {"role": "system", "content": llm_router.system_prompt},
                                {"role": "user", "content": f"请分析以下论文内容：\n\n{truncated}"},
                            ],
                            "response_format": {"type": "json_object"},
                        },
                    })

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
            
        truncated = extract_key_sections(mineru_md)
        # 统一使用 DeepSeek V3 处理中英文 (Gemini Key 目前已暂停)
        deepseek_requests.append({
            "custom_id": doc_id,
            "method": "POST",
            "url": "/v1/chat/completions",
            "body": {
                "model": llm_router.deepseek_model,
                "messages": [
                    {"role": "system", "content": llm_router.system_prompt},
                    {"role": "user", "content": f"请分析以下论文内容：\n\n{truncated}"},
                ],
                "response_format": {"type": "json_object"},
            },
        })
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
        sub_cnt, fail_cnt = _submit_deepseek_batch(deduped_deepseek_requests, llm_router, c, conn)
        report["submitted"] += sub_cnt
        report["submit_failed"] += fail_cnt

    # 统一提交 Gemini Batch
    if gemini_requests:
        sub_cnt, fail_cnt = _submit_gemini_batch(gemini_requests, llm_router, c, conn)
        report["submitted"] += sub_cnt
        report["submit_failed"] += fail_cnt

    conn.close()
    return report


def _submit_deepseek_batch(requests: list, llm_router: LLMRouter, c, conn) -> tuple[int, int]:
    console.print(f"[green]提交 {len(requests)} 个任务到 DeepSeek Batch API...")
    ds_file = "deepseek_batch.jsonl"
    with open(ds_file, "w", encoding="utf-8") as f:
        for r in requests:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
            
    try:
        ds_client = llm_router.deepseek_client
        
        @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10), reraise=True)
        def do_submit():
            with open(ds_file, "rb") as fh:
                file_obj = ds_client.files.create(file=fh, purpose="batch")
            
            # Extract file ID - SiliconFlow API wraps file ID in 'data' attribute of FileObject
            file_id = getattr(file_obj, "id", None)
            if not file_id and hasattr(file_obj, "data") and isinstance(file_obj.data, dict):
                file_id = file_obj.data.get("id")
            if not file_id:
                console.print(f"[yellow]无法直接获取 file_obj.id, file_obj: {file_obj}")
                raise ValueError(f"无法从 FileObject 提取 file_id: {file_obj}")
                
            return ds_client.batches.create(
                input_file_id=file_id,
                endpoint="/v1/chat/completions",
                completion_window="24h",
            )
            
        batch_job = do_submit()
        batch_id = getattr(batch_job, "id", None)
        if not batch_id and hasattr(batch_job, "data") and isinstance(batch_job.data, dict):
            batch_id = batch_job.data.get("id")
        if not batch_id:
            console.print(f"[yellow]无法直接获取 batch_job.id, batch_job: {batch_job}")
            raise ValueError(f"无法从 Batch 提取 batch_id: {batch_job}")
        console.print(f"[bold green]DeepSeek Batch 提交成功！ID: {batch_id}")
        
        for req in requests:
            c.execute(
                "UPDATE papers SET status=?, batch_provider='deepseek', batch_job_id=?, error_message=NULL WHERE id=?",
                (STATUS_SUBMITTED, batch_id, req["custom_id"]),
            )
        conn.commit()
        log_run_event(mode="batch", event="batch_submitted", status="success", extra={"batch_id": batch_id, "provider": "deepseek", "count": len(requests)})
        
        # 成功后移除临时 jsonl
        if os.path.exists(ds_file):
            os.remove(ds_file)
        return len(requests), 0
        
    except Exception as e:
        err_msg = f"DeepSeek Batch 提交失败: {str(e)}"
        console.print(f"[bold red]{err_msg}")
        
        # 将本轮队列中的文献在 DB 中全部标为 SUBMIT_FAILED 并保存错误原因以防卡死
        for req in requests:
            c.execute(
                "UPDATE papers SET status=?, error_message=? WHERE id=?",
                (STATUS_SUBMIT_FAILED, err_msg, req["custom_id"]),
            )
        conn.commit()
        log_run_event(mode="batch", event="batch_submitted", status="failed", error=err_msg, extra={"provider": "deepseek", "count": len(requests)})
        
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
    llm_router = LLMRouter(settings.DEEPSEEK_API_KEY, settings.GEMINI_API_KEY)

    c.execute("SELECT DISTINCT batch_job_id, batch_provider FROM papers WHERE status=?", (STATUS_SUBMITTED,))
    jobs = c.fetchall()

    if not jobs:
        console.print("[yellow]当前没有等待拉取结果的 Batch 任务。")
    else:
        report["jobs_checked"] = len(jobs)
        for job_id, provider in jobs:
            console.print(f"检查 {provider} 任务: {job_id}")
            try:
                if provider == "deepseek":
                    comp_cnt, fail_cnt = _fetch_deepseek_results(job_id, llm_router, c, conn)
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


def _fetch_deepseek_results(job_id: str, llm_router: LLMRouter, c, conn) -> tuple[int, int]:
    ds_client = llm_router.deepseek_client
    
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10), reraise=True)
    def do_fetch_status():
        return ds_client.batches.retrieve(job_id)
        
    status = do_fetch_status()
    status_val = getattr(status, "status", None)
    if not status_val and hasattr(status, "data") and isinstance(status.data, dict):
        status_val = status.data.get("status")
        
    console.print(f"  DeepSeek 云端状态: {status_val}")
    
    # 云端成功：正常解析
    if status_val == "completed":
        output_file_id = getattr(status, "output_file_id", None)
        if not output_file_id and hasattr(status, "data") and isinstance(status.data, dict):
            output_file_id = status.data.get("output_file_id")
            
        if not output_file_id:
            raise ValueError(f"无法从 Batch 提取 output_file_id: {status}")
            
        @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10), reraise=True)
        def do_download():
            if output_file_id.startswith("http://") or output_file_id.startswith("https://"):
                import httpx
                resp = httpx.get(output_file_id, timeout=120)
                resp.raise_for_status()
                return resp.text
            else:
                return ds_client.files.content(output_file_id).content.decode("utf-8")
            
        result_content = do_download()
        success_count = 0
        fail_count = 0
        
        for line in result_content.splitlines():
            if not line.strip():
                continue
            try:
                res = json.loads(line)
                doc_id = res["custom_id"]
                content = res["response"]["body"]["choices"][0]["message"]["content"]
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
        conn.commit()
        console.print(f"[green]DeepSeek 结果拉取完毕，成功 {success_count} 篇，失败 {fail_count} 篇。")
        log_run_event(mode="batch", event="batch_fetched", status="success", extra={"batch_id": job_id, "provider": "deepseek", "completed": success_count, "failed": fail_count})
        return success_count, fail_count
        
    # 云端失败：更新整个 Batch 涉及的所有论文为 BATCH_FAILED 状态以闭环
    elif status_val in ("failed", "cancelled", "expired"):
        err_msg = f"DeepSeek 任务在云端夭折，返回状态: {status_val}"
        console.print(f"[bold red]  {err_msg}")
        c.execute("SELECT id FROM papers WHERE batch_job_id=?", (job_id,))
        rows = c.fetchall()
        for r in rows:
            c.execute("UPDATE papers SET status=?, error_message=? WHERE id=?", (STATUS_BATCH_FAILED, err_msg, r[0]))
        conn.commit()
        log_run_event(mode="batch", event="batch_fetched", status="failed", error=err_msg, extra={"batch_id": job_id, "provider": "deepseek", "count": len(rows)})
        return 0, len(rows)
        
    # 其余 pending/processing 状态保持不变
    return 0, 0


def _fetch_gemini_results(job_id: str, llm_router: LLMRouter, c, conn) -> tuple[int, int]:
    gm_client = llm_router.gemini_client
    
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
        f"SELECT id, title, pdf_path, language, images_dir, result_json, batch_provider, batch_job_id FROM papers WHERE status=? LIMIT {settings.BATCH_SIZE_LIMIT}",
        (STATUS_COMPLETED,),
    )
    completed = c.fetchall()
    if not completed:
        console.print("[dim]当前没有新完成的文献需要导出。")
        return 0

    excel_rows = []
    for doc_id, title, pdf_path, lang, images_dir, result_json, provider, job_id in completed:
        try:
            analysis = json.loads(result_json)
        except (json.JSONDecodeError, TypeError):
            analysis = {"tldr": "结果解析 JSON 失败", "background": result_json or ""}

        images = []
        if images_dir and os.path.exists(images_dir):
            for ext in ["*.png", "*.jpg", "*.jpeg"]:
                images.extend(glob.glob(os.path.join(images_dir, "**", ext), recursive=True))
        images = [os.path.abspath(img) for img in images]

        # 导出 Obsidian，携带 doc_id 以确保生成带哈希的安全跨平台相对附件笔记
        note_path = generate_obsidian_note({"title": title, "language": lang}, analysis, images, settings.OBSIDIAN_VAULT_DIR, doc_id)
        note_filename = os.path.basename(note_path)
        obsidian_link_name = os.path.splitext(note_filename)[0]

        # 不清理 MinerU 中间文件（保留公式/表格切片图片及中间 Markdown 结果）
        output_folder = os.path.dirname(images_dir) if images_dir else os.path.join(settings.MINERU_OUTPUT_DIR, f"{title}_{doc_id[:8]}")
        # if os.path.exists(output_folder):
        #     shutil.rmtree(output_folder, ignore_errors=True)
        #     console.print(f"[dim]已清理 MinerU 中间文件: {output_folder}[/dim]")

        # 收集 Excel，携带更加全面的状态、批量任务与文献 ID 字段
        excel_rows.append({
            "文献ID": doc_id,
            "标题": title,
            "作者": analysis.get("authors", ""),
            "年份": analysis.get("year", ""),
            "期刊": analysis.get("journal", ""),
            "文件路径": pdf_path,  # 已经处于归档 processed_pdfs/ 下的真实有效路径
            "Obsidian链接": f"[[{obsidian_link_name}]]",
            "TLDR": analysis.get("tldr", ""),
            "语言": lang,
            "状态": "EXPORTED",
            "服务商": provider,
            "批量任务ID": job_id,
            "解析时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })

        # 移至导出终态
        c.execute("UPDATE papers SET status=? WHERE id=?", (STATUS_EXPORTED, doc_id))
        conn.commit()
        log_run_event(mode="batch", event="paper_exported", title=title, doc_id=doc_id, status="success")
        console.print(f"[green]✅ 成功导出: {title}")

    # 批量合并去重导出 Excel
    if excel_rows:
        export_to_excel(excel_rows, settings.EXCEL_OUTPUT_PATH)
        console.print(f"[bold green]共导出 {len(excel_rows)} 篇文献到 {settings.EXCEL_OUTPUT_PATH}")

        # 导出后自动增量更新综述向量索引与结构化元数据（异常隔离：失败绝不影响导出结果）
        if settings.REVIEW_AUTO_INDEX:
            try:
                from llm_router import make_deepseek_client
                from litreview.indexer import build_index
                client, _ = make_deepseek_client(quiet=True)
                build_index(settings, console, client)
            except Exception as e:
                console.print(f"[yellow]⚠️ 综述索引自动更新失败（不影响导出，可手动运行 review.py index）: {e}")
            try:
                from litreview.enrich import run_enrich
                run_enrich(settings, console, client, use_llm=settings.REVIEW_ENRICH_LLM)
            except Exception as e:
                console.print(f"[yellow]⚠️ 元数据自动提取失败（不影响导出，可手动运行 review.py enrich）: {e}")

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
