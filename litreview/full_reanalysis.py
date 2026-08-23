"""方案 A：全量全文重新分析模块。

直接读取完整 Markdown 正文 (mineru_md)，调用 gpt-5.4-mini 全文分析，
重构全部 9 个结构化字段 (TLDR, keywords, fertilizer_implications, abstract,
background, methods, results, conclusion, metadata)。
支持按主题过滤、单日 Token 熔断上限与分批滚动排期。
"""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Optional, List

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeElapsedColumn, TimeRemainingColumn

from db_schema import ensure_llm_quota_tables
from litreview.analysis_record import PaperAnalysisRecord
from litreview.quota import reserve, reconcile, record_llm_call
from llm_router import LLMRouter, make_openai_client
from utils import get_settings, sync_excel_with_db

console = Console()


def reanalyze_single_paper(conn: sqlite3.Connection, doc_id: str, router: LLMRouter, settings) -> dict:
    """对单篇文献进行全量全文重新解析与结构化持久化。"""
    c = conn.cursor()
    c.execute("""
        SELECT p.id, p.title, p.language, p.mineru_md,
               d.doc_type, d.keywords, d.title_zh, d.title_en, d.cite_key
        FROM papers p
        LEFT JOIN paper_details d ON p.id = d.doc_id
        WHERE p.id = ?
    """, (doc_id,))
    row = c.fetchone()
    if not row:
        return {"status": "error", "error": f"Doc {doc_id} not found"}

    doc_id, title, language, mineru_md, doc_type, keywords, title_zh, title_en, cite_key = row
    doc_type = doc_type or "paper"

    if not mineru_md or len(mineru_md.strip()) < 100:
        return {"status": "error", "error": f"Doc {doc_id} has no valid mineru_md markdown"}

    try:
        # 调用全新自适应大模型分析接口
        record = router.analyze_paper(mineru_md, doc_type=doc_type)
        
        # 将分析结果回写数据库
        result_dict = record.model_dump()
        result_json_str = json.dumps(result_dict, ensure_ascii=False)
        c.execute("UPDATE papers SET result_json=? WHERE id=?", (result_json_str, doc_id))
        
        # 更新 keywords 与 details
        if record.keywords:
            c.execute("UPDATE paper_details SET keywords=? WHERE doc_id=?", (record.keywords, doc_id))
        conn.commit()

        # 刷新对应 Obsidian 笔记
        try:
            record.publish_obsidian_note(settings.OBSIDIAN_VAULT_PATH, title=title, doc_id=doc_id)
        except Exception:
            pass

        # 估算本篇消耗 tokens
        tokens_est = int(len(mineru_md) / 3.5) + 1200
        return {
            "status": "success",
            "doc_id": doc_id,
            "title": title,
            "tldr": record.tldr,
            "tokens_est": tokens_est
        }
    except Exception as e:
        return {"status": "error", "doc_id": doc_id, "error": str(e)}


def run_full_reanalysis(limit: Optional[int] = None, doc_id: Optional[str] = None,
                        topics: Optional[List[str]] = None, max_daily_tokens: int = 2_500_000,
                        offset: int = 0) -> dict:
    """运行全量文献全文分析管道 (方案 A)，带严格的单日 Token 熔断保护。"""
    settings = get_settings()
    router = LLMRouter()
    conn = sqlite3.connect(settings.DB_PATH)
    ensure_llm_quota_tables(conn)
    c = conn.cursor()

    query = """
        SELECT p.id, p.title, COALESCE(d.doc_type, 'paper'), LENGTH(p.mineru_md)
        FROM papers p
        LEFT JOIN paper_details d ON p.id = d.doc_id
        WHERE p.status='EXPORTED' 
          AND p.mineru_md IS NOT NULL 
          AND LENGTH(p.mineru_md) > 100
          AND COALESCE(d.doc_type, 'paper') != 'book'
    """
    params = []
    if doc_id:
        query += " AND p.id LIKE ?"
        params.append(f"{doc_id}%")
    if topics:
        topic_conds = []
        for t in topics:
            topic_conds.append("(p.title LIKE ? OR d.title_zh LIKE ? OR d.keywords LIKE ? OR p.mineru_md LIKE ?)")
            params.extend([f"%{t}%", f"%{t}%", f"%{t}%", f"%{t}%"])
        query += f" AND ({' OR '.join(topic_conds)})"

    query += " ORDER BY p.id ASC"
    if limit:
        query += f" LIMIT {int(limit)}"
    if offset > 0:
        query += f" OFFSET {int(offset)}"

    c.execute(query, params)
    targets = c.fetchall()

    if not targets:
        console.print("[yellow]未找到符合重分析条件的 EXPORTED 文献。[/yellow]")
        conn.close()
        return {"processed": 0, "tokens": 0}

    topic_label = f" (主题过滤: {', '.join(topics)})" if topics else ""
    console.rule(f"[bold cyan]🚀 启动全量文献全文深度重分析 (方案 A) - 共 {len(targets)} 篇{topic_label}")
    console.print(f"[bold yellow]🛡️ 单日 Token 保护上限: {max_daily_tokens:,} Tokens[/bold yellow]")

    total_tokens_consumed = 0
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
        task = progress.add_task("全量分析中...", total=len(targets))

        for tid, title, dtype, m_len in targets:
            # 熔断检查
            if total_tokens_consumed >= max_daily_tokens:
                console.print(f"\n[bold yellow]⚠️ 已达到今日预设 Token 上限 ({max_daily_tokens:,})，自动安全熔断停机，明日继续！[/bold yellow]")
                break

            progress.update(task, description=f"深度解析: {title[:18]}... ({m_len//1000}k字)")
            res = reanalyze_single_paper(conn, tid, router, settings)
            if res["status"] == "success":
                success_count += 1
                total_tokens_consumed += res.get("tokens_est", 2000)
            else:
                fail_count += 1
                err_msg = str(res.get('error', ''))
                console.print(f"[red]✗ 失败: {title[:20]} - {err_msg}[/red]")
                if "当日额度不足" in err_msg or "quota" in err_msg.lower():
                    console.print(f"\n[bold yellow]⚠️ 触碰 UTC 当日额度上限保护，安全终止本批任务，明日 08:00 (UTC 00:00) 自动恢复后再续跑！[/bold yellow]")
                    break
            progress.advance(task)

    conn.close()
    cost_total = time.time() - t_start

    # 同步重写 Excel 总表
    console.print("\n[bold cyan]📊 正在按最新重分析数据全量同步 Excel 总表...[/bold cyan]")
    count = sync_excel_with_db(settings.DB_PATH, settings.EXCEL_OUTPUT_PATH)
    console.print(f"[bold green]✔ Excel 同步完成: {count} 篇文献已全部对齐至 {settings.EXCEL_OUTPUT_PATH}[/bold green]")

    console.rule("[bold green]🎉 全文深度重分析阶段完成统计")
    console.print(f"• 成功处理: [bold green]{success_count}[/bold green] 篇")
    if fail_count > 0:
        console.print(f"• 失败篇数: [bold red]{fail_count}[/bold red] 篇")
    console.print(f"• 本批消耗 Tokens: [bold yellow]{total_tokens_consumed:,}[/bold yellow] Tokens")
    console.print(f"• 总耗时: [bold cyan]{cost_total:.1f} 秒[/bold cyan] (平均 {cost_total/max(1, success_count):.1f}s/篇)")

    return {
        "success_count": success_count,
        "fail_count": fail_count,
        "total_tokens": total_tokens_consumed,
        "cost_seconds": cost_total
    }
