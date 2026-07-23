"""review.py — 综述生成器 CLI（手动触发）。

用法：
  python review.py index [--force]        建/增量更新向量索引（幂等）
  python review.py status                 索引统计
  python review.py search "关键词" [-k 10]  检索调试
  python review.py outline "主题" [-o outline.json]
  python review.py generate "主题" [--outline f] [--dry-run]
"""
import os
import sys
import json
import argparse

from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

from utils import get_settings, log_run_event

load_dotenv()
console = Console()

try:
    settings = get_settings()
except SystemExit:
    console.print("[bold red]❌ 配置校验未通过，请优先修复环境变量配置！[/bold red]")
    sys.exit(1)


def _require_siliconflow():
    if not (os.getenv("SILICONFLOW_API_KEY") and os.getenv("SILICONFLOW_API_BASE")):
        console.print("[bold red]❌ 综述功能依赖 SiliconFlow embeddings，"
                      "请在环境中配置 SILICONFLOW_API_KEY 和 SILICONFLOW_API_BASE。[/bold red]")
        sys.exit(1)


def _make_client():
    from llm_router import make_deepseek_client
    client, chat_model = make_deepseek_client(quiet=True)
    review_model = settings.REVIEW_MODEL or chat_model
    return client, review_model


def cmd_index(args):
    _require_siliconflow()
    from litreview.indexer import build_index
    client, _ = _make_client()
    report = build_index(settings, console, client, force=args.force)
    log_run_event(mode="review", event="index_build", status="success",
                  extra={"indexed": report["indexed"], "failed": report["failed"],
                         "chunks": report["chunks"]})


def cmd_status(args):
    from litreview.store import VectorStore
    store = VectorStore(settings.DB_PATH, settings.EMBEDDING_DIM)
    s = store.stats()
    table = Table(title="📚 综述索引状态")
    table.add_column("指标", style="cyan")
    table.add_column("数量", justify="right")
    table.add_row("已导出文献 (EXPORTED)", str(s["exported_docs"]))
    table.add_row("已索引文献", str(s["indexed_docs"]))
    table.add_row("待索引文献", str(s["pending_docs"]))
    table.add_row("文本块总数", str(s["chunks"]))
    table.add_row("已嵌入块数", str(s["embedded_chunks"]))
    console.print(table)
    if s["pending_docs"] > 0:
        console.print(f"[yellow]提示：运行 `python review.py index` 处理 {s['pending_docs']} 篇待索引文献。")


def _make_embedder(client):
    from litreview.embedder import Embedder
    return Embedder(client, settings.EMBEDDING_MODEL, settings.EMBEDDING_DIM,
                    settings.EMBEDDING_BATCH_SIZE, settings.EMBEDDING_QUERY_INSTRUCTION)


def _make_reranker():
    if not settings.RERANK_ENABLE:
        return None
    from litreview.reranker import Reranker
    return Reranker(settings.RERANK_MODEL)


def cmd_search(args):
    _require_siliconflow()
    from litreview.store import VectorStore
    client, _ = _make_client()
    store = VectorStore(settings.DB_PATH, settings.EMBEDDING_DIM)
    embedder = _make_embedder(client)
    qvec = embedder.embed_query(args.query)
    reranker = None if args.no_rerank else _make_reranker()
    recall_k = settings.RERANK_CANDIDATES if reranker else args.top_k
    results = store.search(qvec, top_k=recall_k, max_per_doc=settings.REVIEW_MAX_CHUNKS_PER_DOC)
    if not results:
        console.print("[yellow]未检索到任何结果（索引可能为空，先运行 index）。")
        return
    if reranker:
        ranked = reranker.rerank(args.query, [r.content for r in results], top_n=args.top_k)
        new_results = []
        for idx, score in ranked:
            r = results[idx]
            r.score = score
            new_results.append(r)
        results = new_results
    meta = store.get_paper_meta(list({r.doc_id for r in results}))
    score_label = "重排分" if reranker else "向量分"
    table = Table(title=f"🔍 检索: {args.query}", show_lines=True)
    table.add_column(score_label, justify="right", width=6)
    table.add_column("论文", max_width=30)
    table.add_column("小节", max_width=16)
    table.add_column("片段预览", max_width=60)
    for r in results:
        title = meta.get(r.doc_id, {}).get("title", r.doc_id[:8])
        table.add_row(f"{r.score:.3f}", title, r.section_title or "-",
                      r.content[:120].replace("\n", " "))
    console.print(table)


def cmd_enrich(args):
    from litreview.enrich import run_enrich
    client = None
    use_llm = settings.REVIEW_ENRICH_LLM and not args.no_llm
    if use_llm:
        _require_siliconflow()
        client, _ = _make_client()
    report = run_enrich(settings, console, client, force=args.force, use_llm=use_llm)
    log_run_event(mode="review", event="enrich", status="success",
                  extra={"enriched": report["enriched"], "failed": report["failed"],
                         "with_doi": report["with_doi"], "assets": report["assets"]})


def cmd_outline(args):
    _require_siliconflow()
    from litreview.store import VectorStore
    from litreview.stages import generate_outline
    client, model = _make_client()
    store = VectorStore(settings.DB_PATH, settings.EMBEDDING_DIM)
    embedder = _make_embedder(client)
    console.print(f"[cyan]正在为主题「{args.topic}」生成综述大纲...")
    outline = generate_outline(args.topic, store, embedder, client, model, settings, console,
                               focus=args.focus or "", n_sections=args.sections or 0)
    console.rule(f"[bold green]📋 {outline.title}")
    for i, sec in enumerate(outline.sections, 1):
        console.print(f"[bold]{i}. {sec.heading}[/bold]")
        for q in sec.questions:
            console.print(f"   - {q}")
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(outline.model_dump(), f, ensure_ascii=False, indent=2)
        console.print(f"\n[green]✔ 大纲已保存至 {args.output}（可手工编辑后用 --outline 传入 generate）。")


def cmd_generate(args):
    _require_siliconflow()
    import time
    from litreview.store import VectorStore
    from litreview.stages import (generate_outline, load_outline_file, gather_evidence,
                                  write_section, write_intro_conclusion,
                                  assemble_review, render_review_note, export_docx)
    from litreview.figures import select_section_figures

    client, model = _make_client()
    store = VectorStore(settings.DB_PATH, settings.EMBEDDING_DIM)
    embedder = _make_embedder(client)
    reranker = _make_reranker()
    if reranker:
        console.print(f"[dim]重排序已启用: {settings.RERANK_MODEL}")
    paper_meta = store.get_paper_meta()
    t0 = time.time()

    focus = args.focus or ""

    # Stage A
    if args.outline:
        outline = load_outline_file(args.outline, args.topic)
        console.print(f"[cyan]使用外部大纲: {outline.title}（{len(outline.sections)} 章节）")
    else:
        console.print(f"[cyan]阶段 A：生成大纲...")
        outline = generate_outline(args.topic, store, embedder, client, model, settings, console,
                                   focus=focus, n_sections=args.sections or 0)
        console.print(f"[green]✔ 大纲: {outline.title}（{len(outline.sections)} 章节）")

    # 字数分配：--words 指定全文目标总字数时，按写作指南篇幅权重摊分
    # （引言约 12%、结论与展望约 15%——展望需容纳"一问一策"逐条对应，其余归主体章节）
    if args.words:
        intro_target = max(300, min(900, int(args.words * 0.12)))
        concl_target = max(350, min(1100, int(args.words * 0.15)))
        body_per_section = max(400, (args.words - intro_target - concl_target)
                               // max(1, len(outline.sections)))
        section_words = (int(body_per_section * 0.85), int(body_per_section * 1.15))
        intro_words = (int(intro_target * 0.8), int(intro_target * 1.2))
        concl_words = (int(concl_target * 0.8), int(concl_target * 1.2))
        console.print(f"[dim]目标总字数 {args.words}：每章节 {section_words[0]}-{section_words[1]} 字，"
                      f"引言 {intro_words[0]}-{intro_words[1]} 字，结论 {concl_words[0]}-{concl_words[1]} 字")
    else:
        section_words, intro_words, concl_words = (600, 1000), (300, 500), (400, 700)

    # Stage B
    section_evidence = []
    for i, sec in enumerate(outline.sections, 1):
        console.print(f"[cyan]阶段 B：收集证据 [{i}/{len(outline.sections)}] {sec.heading} ...")
        ev = gather_evidence(sec, store, embedder, client, model, settings, console, paper_meta,
                             reranker=reranker)
        console.print(f"  [green]✔ 保留 {len(ev)} 条证据")
        section_evidence.append(ev)

    if args.dry_run:
        for sec, evs in zip(outline.sections, section_evidence):
            table = Table(title=f"证据表: {sec.heading}", show_lines=True)
            table.add_column("分", justify="right", width=4)
            table.add_column("论文", max_width=26)
            table.add_column("证据摘要", max_width=70)
            for ev in evs:
                title = paper_meta.get(ev.doc_id, {}).get("title", ev.doc_id[:8])
                table.add_row(str(ev.score), title, ev.summary)
            console.print(table)
        console.print("[yellow]--dry-run 模式：已停在证据阶段，未生成正文。")
        return

    # Stage C（单章节失败不终止全篇，跳过并在结尾提示）
    drafts = []
    failed_sections = []
    used_figures = set()   # 跨章节图片去重
    for i, (sec, evs) in enumerate(zip(outline.sections, section_evidence), 1):
        console.print(f"[cyan]阶段 C：撰写章节 [{i}/{len(outline.sections)}] {sec.heading} ...")
        if not evs:
            console.print(f"  [yellow]⚠️ 无证据，跳过该章节。")
            continue
        try:
            draft = write_section(sec, evs, paper_meta, outline.title, client, model,
                                  words=section_words, focus=focus)
            n_figs = select_section_figures(store, reranker, sec, draft, settings,
                                            console, used_figures)
            if n_figs:
                console.print(f"  [dim]插入相关图表 {n_figs} 幅")
            drafts.append(draft)
        except Exception as e:
            failed_sections.append(sec.heading)
            console.print(f"  [red]✖ 章节撰写失败（跳过）: {e}")
    if not drafts:
        console.print("[bold red]❌ 所有章节均无内容，综述生成中止。请检查主题与文献库是否匹配，或稍后重试。")
        sys.exit(1)
    console.print("[cyan]阶段 C：撰写引言与结论...")
    try:
        intro, conclusion = write_intro_conclusion(outline, drafts, client, model,
                                                   intro_words=intro_words,
                                                   concl_words=concl_words, focus=focus)
    except Exception as e:
        console.print(f"[yellow]⚠️ 引言/结论生成失败（正文保留）: {e}")
        intro, conclusion = "", ""

    # Stage D
    evidence_total = sum(len(e) for e in section_evidence)
    review = assemble_review(outline, drafts, intro, conclusion, paper_meta, evidence_total)
    template_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "review_template.md")
    out_path = render_review_note(review, paper_meta, settings, template_path, model)

    docx_path = ""
    try:
        docx_path = export_docx(out_path)
    except Exception as e:
        console.print(f"[yellow]⚠️ Word 导出失败（markdown 已正常生成）: {e}")

    elapsed = int(time.time() - t0)
    console.rule("[bold green]✅ 综述生成完成")
    if failed_sections:
        console.print(f"[yellow]⚠️ 以下章节因接口故障未生成，可稍后重跑: {', '.join(failed_sections)}")
    console.print(f"标题: [bold]{review.title}[/bold]")
    console.print(f"章节: {len(review.sections)} 个 | 引用文献: {review.doc_count} 篇 | "
                  f"证据: {review.evidence_count} 条 | 耗时: {elapsed}s")
    console.print(f"输出: [bold cyan]{out_path}[/bold cyan]")
    if docx_path:
        console.print(f"Word: [bold cyan]{docx_path}[/bold cyan]")
    log_run_event(mode="review", event="review_generated", title=review.title,
                  status="success",
                  extra={"topic": args.topic, "sections": len(review.sections),
                         "cited_docs": review.doc_count, "elapsed_s": elapsed,
                         "output": out_path, "docx": docx_path})


def main():
    parser = argparse.ArgumentParser(description="综述生成器 CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("index", help="建/增量更新向量索引")
    p.add_argument("--force", action="store_true", help="全量重建索引")
    p.set_defaults(func=cmd_index)

    p = sub.add_parser("status", help="索引统计")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("search", help="检索调试")
    p.add_argument("query")
    p.add_argument("-k", "--top-k", type=int, default=10)
    p.add_argument("--no-rerank", action="store_true", help="只用向量分数，不做重排序")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("enrich", help="提取结构化元数据（标题中英/DOI/图表/参考文献）")
    p.add_argument("--force", action="store_true", help="全量重新提取")
    p.add_argument("--no-llm", action="store_true", help="只做本地解析，不用 LLM 补缺")
    p.set_defaults(func=cmd_enrich)

    p = sub.add_parser("outline", help="生成综述大纲")
    p.add_argument("topic")
    p.add_argument("-o", "--output", help="保存大纲 JSON 的路径")
    p.add_argument("--focus", help="侧重方向（如：侧重工业应用与成本对比）")
    p.add_argument("--sections", type=int, help="主体章节数（默认由模型定 3-6 个）")
    p.set_defaults(func=cmd_outline)

    p = sub.add_parser("generate", help="生成完整综述")
    p.add_argument("topic")
    p.add_argument("--outline", help="外部大纲文件（JSON 或 markdown）")
    p.add_argument("--focus", help="侧重方向，贯穿大纲设计与全文写作（如：侧重环保型材料与降解机理）")
    p.add_argument("--words", type=int, help="目标总字数（自动摊分到各章节与引言/结论）")
    p.add_argument("--sections", type=int, help="主体章节数（默认由模型定 3-6 个）")
    p.add_argument("--dry-run", action="store_true", help="停在证据阶段，打印证据表")
    p.set_defaults(func=cmd_generate)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
