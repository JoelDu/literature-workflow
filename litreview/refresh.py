"""轻量级存量文献 TLDR 与工程借鉴重炼模块 (方案 B)。

直接基于已有结构化解析结果 (methods + results + conclusion + abstract)，
以极小 Token 开销 (~1500 Tokens/篇) 重新生成符合最新四元组与字数规范的深度 TLDR。
"""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Optional

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeElapsedColumn, TimeRemainingColumn

from db_schema import ensure_llm_quota_tables
from litreview.analysis_prompt import build_adaptive_analysis_prompt
from litreview.analysis_record import PaperAnalysisRecord
from litreview.quota import reserve, reconcile, record_llm_call
from llm_router import make_openai_client
from utils import get_settings, sync_excel_with_db

console = Console()


def refresh_single_paper(conn: sqlite3.Connection, doc_id: str, client, model: str, settings) -> dict:
    """对单篇文献进行轻量级结构化重炼。"""
    c = conn.cursor()
    c.execute("""
        SELECT p.id, p.title, p.language, p.result_json, p.mineru_md,
               d.doc_type, d.keywords, d.title_zh, d.title_en, d.cite_key
        FROM papers p
        LEFT JOIN paper_details d ON p.id = d.doc_id
        WHERE p.id = ?
    """, (doc_id,))
    row = c.fetchone()
    if not row:
        return {"status": "error", "error": f"Doc {doc_id} not found"}

    doc_id, title, language, result_json_str, mineru_md, doc_type, keywords, title_zh, title_en, cite_key = row
    doc_type = doc_type or "paper"
    language = language or "zh"

    # 解析已有数据
    old_data = {}
    if result_json_str:
        try:
            old_data = json.loads(result_json_str)
        except Exception:
            pass

    # 优先抽取已有的高价值事实段落以节省 Token
    abstract = old_data.get("abstract", "")
    background = old_data.get("background", "")
    methods = old_data.get("methods", "")
    results = old_data.get("results", "")
    conclusion = old_data.get("conclusion", "")

    # 组装高纯度上下文
    content_chunks = []
    if title: content_chunks.append(f"【文献题名】: {title}")
    if title_zh: content_chunks.append(f"【中文题名】: {title_zh}")
    if abstract: content_chunks.append(f"【原始摘要】:\n{abstract}")
    if methods: content_chunks.append(f"【实验方法与工艺】:\n{methods}")
    if results: content_chunks.append(f"【核心结果与数据】:\n{results}")
    if conclusion: content_chunks.append(f"【结论与启示】:\n{conclusion}")

    context_text = "\n\n".join(content_chunks)
    # 若结构化字段不足，回退截取部分 mineru_md
    if len(context_text) < 300 and mineru_md:
        context_text = mineru_md[:15000]

    # 装配针对该类型的自适应 Prompt
    sys_prompt = build_adaptive_analysis_prompt(context_text, doc_type=doc_type, lang=language)
    user_prompt = f"请根据以下文献已知事实内容，按规范重新提取 tldr、keywords 与 fertilizer_implications：\n\n{context_text}"
    full_prompt = sys_prompt + "\n\n" + user_prompt

    max_tokens = 4000
    reservation = reserve(settings, model, full_prompt, max_tokens)
    t0 = time.time()
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_prompt}
            ],
            response_format={"type": "json_object"},
            temperature=0.3,
            max_completion_tokens=max_tokens
        )
        reconcile(settings, model, reservation, resp.usage)
        cost_sec = time.time() - t0
        content = resp.choices[0].message.content
        record_llm_call(settings, call_type="refresh_tldr", model=model, prompt=full_prompt,
                        response_text=content, stage="lightweight_refresh", usage=resp.usage,
                        cost_seconds=cost_sec, status="SUCCESS", meta={"doc_id": doc_id, "doc_type": doc_type})

        new_data = json.loads(content)
        # 合并更新
        old_data["tldr"] = new_data.get("tldr", old_data.get("tldr", ""))
        old_data["keywords"] = new_data.get("keywords", old_data.get("keywords", ""))
        old_data["fertilizer_implications"] = new_data.get("fertilizer_implications", old_data.get("fertilizer_implications", ""))

        # 写入数据库
        updated_json = json.dumps(old_data, ensure_ascii=False)
        c.execute("UPDATE papers SET result_json=? WHERE id=?", (updated_json, doc_id))
        if new_data.get("keywords"):
            c.execute("UPDATE paper_details SET keywords=? WHERE doc_id=?", (new_data["keywords"], doc_id))
        conn.commit()

        # 同步重新发布 Obsidian 笔记
        try:
            record = PaperAnalysisRecord.from_llm_json(updated_json, doc_id=doc_id, markdown_text=mineru_md or "")
            record.publish_obsidian_note(settings.OBSIDIAN_VAULT_PATH)
        except Exception:
            pass

        return {
            "status": "success",
            "doc_id": doc_id,
            "title": title,
            "tldr": old_data["tldr"],
            "tokens": resp.usage.total_tokens if resp.usage else 0,
            "cost_sec": cost_sec
        }
    except Exception as e:
        cost_sec = time.time() - t0
        record_llm_call(settings, call_type="refresh_tldr", model=model, prompt=full_prompt,
                        response_text="", stage="lightweight_refresh", usage=None,
                        cost_seconds=cost_sec, status="FAILED", error_message=str(e), meta={"doc_id": doc_id})
        return {"status": "error", "doc_id": doc_id, "error": str(e)}


def run_refresh_library(limit: Optional[int] = None, doc_id: Optional[str] = None,
                        topic: Optional[str] = None, force: bool = False) -> None:
    """批量运行存量文献轻量重炼管道。"""
    settings = get_settings()
    client, model = make_openai_client(quiet=True)
    conn = sqlite3.connect(settings.DB_PATH)
    ensure_llm_quota_tables(conn)
    c = conn.cursor()

    query = "SELECT p.id, p.title, d.doc_type FROM papers p LEFT JOIN paper_details d ON p.id = d.doc_id WHERE p.status='EXPORTED'"
    params = []
    if doc_id:
        query += " AND p.id LIKE ?"
        params.append(f"{doc_id}%")
    if topic:
        query += " AND (p.title LIKE ? OR d.title_zh LIKE ? OR d.keywords LIKE ?)"
        params.extend([f"%{topic}%", f"%{topic}%", f"%{topic}%"])
    query += " ORDER BY p.id ASC"
    if limit:
        query += f" LIMIT {int(limit)}"

    c.execute(query, params)
    targets = c.fetchall()

    if not targets:
        console.print("[yellow]未找到符合条件的 EXPORTED 文献。[/yellow]")
        conn.close()
        return

    console.rule(f"[bold cyan]🔄 启动存量文献 TLDR 与工程借鉴轻量重炼 (共 {len(targets)} 篇)")
    total_tokens = 0
    success_count = 0
    fail_count = 0
    t_start = time.time()

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TextColumn("•"),
        TimeElapsedColumn(),
        TextColumn("•"),
        TimeRemainingColumn(),
        console=console
    ) as progress:
        task = progress.add_task("重炼文献中...", total=len(targets))

        for tid, title, dtype in targets:
            progress.update(task, description=f"处理: {title[:20]}...")
            res = refresh_single_paper(conn, tid, client, model, settings)
            if res["status"] == "success":
                success_count += 1
                total_tokens += res.get("tokens", 0)
            else:
                fail_count += 1
                console.print(f"[red]✗ 失败: {title[:20]} - {res.get('error')}[/red]")
            progress.advance(task)

    conn.close()
    cost_total = time.time() - t_start

    # 同步重写 Excel 总表
    console.print("\n[bold cyan]📊 正在按最新重炼数据全量同步 Excel 总表...[/bold cyan]")
    count = sync_excel_with_db(settings.DB_PATH, settings.EXCEL_OUTPUT_PATH)
    console.print(f"[bold green]✔ Excel 同步完成: {count} 篇文献已全部对齐至 {settings.EXCEL_OUTPUT_PATH}[/bold green]")

    console.rule("[bold green]🎉 存量文献重炼完成统计")
    console.print(f"• 成功处理: [bold green]{success_count}[/bold green] 篇")
    if fail_count > 0:
        console.print(f"• 失败篇数: [bold red]{fail_count}[/bold red] 篇")
    console.print(f"• 累计消耗 Tokens: [bold yellow]{total_tokens:,}[/bold yellow] Tokens")
    console.print(f"• 总耗时: [bold cyan]{cost_total:.1f} 秒[/bold cyan] (平均 {cost_total/max(1, len(targets)):.1f}s/篇)")
