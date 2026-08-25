"""review.py — 综述生成器 CLI（手动触发）。

用法：
  python review.py index [--force]        建/增量更新向量索引（幂等）
  python review.py status                 索引统计
  python review.py search "关键词" [-k 10]  检索调试
  python review.py outline "主题" [-o outline.json]
  python review.py generate "主题" [--outline f] [--dry-run]
  python review.py types [--rescan [--apply]]   文献分类总览 / 重扫存量归位
  python review.py set-type <ID> --type report  人工指定类型（报告/网页/财报…）
  python review.py patents | standards          专利 / 标准台账
"""
import os
import sys
import json
import argparse

from dotenv import load_dotenv
load_dotenv()

from rich.console import Console
from rich.table import Table

from utils import get_settings, log_run_event
# 命令行选项（--corpus / --type / --status）全部由类型词表现生成，
# 加一种文献类型只改 doctype.py，不用回来改 argparse。纯正则模块，导入不带任何重依赖。
from litreview import doctype as dt

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
    from llm_router import make_openai_client
    client, _analysis_model = make_openai_client(quiet=True)
    return client, settings.REVIEW_MODEL


def cmd_index(args):
    from litreview.indexer import build_index
    # 嵌入后端=local 时 build_index 只报待办、把活留给夜间脚本，全程不碰 SiliconFlow，
    # 不该因为没配 key 就把命令整个挡掉（本地方案的默认部署就是不配 key 的）。
    client = None
    if getattr(settings, "REVIEW_EMBED_BACKEND", "remote") != "local":
        _require_siliconflow()
        # 嵌入端点继续使用硅基流动的 OpenAI-compatible client。
        from openai import OpenAI
        client = OpenAI(api_key=os.getenv("SILICONFLOW_API_KEY"),
                        base_url=os.getenv("SILICONFLOW_API_BASE"), timeout=180)
    report = build_index(settings, console, client, force=args.force)
    deferred = report.get("deferred", 0)
    log_run_event(mode="review", event="index_build",
                  status="deferred" if deferred else "success",
                  extra={k: report.get(k, 0)
                         for k in ("total", "indexed", "failed", "chunks", "deferred")})


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


def cmd_db_doctor(args):
    """Run the three-database audit without opening any write connection."""
    from litreview.db_doctor import inspect_databases

    data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
    graph_path = os.getenv(
        "CITATION_NETWORK_DB_PATH",
        os.getenv("TEST_CITATION_DB_PATH", os.path.join(data_dir, "test_citation_network.db")),
    )
    cache_path = os.getenv(
        "EXTERNAL_DISCOVERY_CACHE_DB_PATH",
        os.path.join(data_dir, "external_discovery_cache.db"),
    )
    report = inspect_databases(settings.DB_PATH, graph_path, cache_path)
    if args.json:
        console.print_json(report.to_json())
        return
    table = Table(title="SQLite 数据健康体检", show_lines=True)
    table.add_column("数据库")
    table.add_column("表/行")
    table.add_column("quick_check")
    table.add_column("FK 违规", justify="right")
    for db in report.databases:
        table.add_row(db.name, f"{len(db.tables)} / {sum(db.tables.values())}",
                      db.quick_check, str(db.foreign_key_violations))
    console.print(table)
    for issue in report.issues:
        colour = "red" if issue.severity == "blocking" else "yellow"
        console.print(f"[{colour}]{issue.severity.upper()} {issue.database}/{issue.code}: "
                      f"{issue.message}" + (f"（{issue.count}）" if issue.count else ""))


def cmd_db_repair_graph(args):
    """Plan a graph repair or explicitly build a separate validated shadow."""
    from litreview.db_repair import repair_graph_shadow

    data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
    graph_path = os.getenv(
        "CITATION_NETWORK_DB_PATH",
        os.getenv("TEST_CITATION_DB_PATH", os.path.join(data_dir, "test_citation_network.db")),
    )
    report = repair_graph_shadow(settings.DB_PATH, graph_path, apply=args.apply,
                                 shadow_path=args.shadow_out)
    console.print_json(report.to_json())


def cmd_db_repair_production(args):
    """Plan or explicitly apply a backed-up production micro-repair."""
    from litreview.db_repair import repair_production_database

    report = repair_production_database(
        settings.DB_PATH, apply=args.apply, backup_path=args.backup_out,
        excel_path=settings.EXCEL_OUTPUT_PATH,
    )
    console.print_json(report.to_json())


def cmd_validate_run(args):
    """校验单个综述运行清单的契约合规性、文件完整性与哈希真实性。"""
    from litreview.run_validation import validate_review_run
    report = validate_review_run(args.manifest)
    if getattr(args, "json", False):
        sys.stdout.write(json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n")
        sys.stdout.flush()
    else:
        colour_map = {"PASS": "green", "WARN": "yellow", "FAIL": "red"}
        verdict_colour = colour_map.get(report.verdict, "white")
        console.print(f"\n[bold {verdict_colour}]📋 综述运行校验报告: {report.verdict}[/bold {verdict_colour}]")
        console.print(f"清单文件: [cyan]{report.manifest_filename}[/cyan]")
        if report.topic:
            console.print(f"综述主题: {report.topic}")
        if report.run_id:
            console.print(f"运行 ID: {report.run_id}")
        if report.model_name:
            console.print(f"生成模型: {report.model_name}")
        if report.markdown_filename:
            md_status = "✅ 匹配" if report.markdown_sha256_match else "❌ 缺失或不匹配"
            console.print(f"Markdown 文件: {report.markdown_filename} ({md_status})")
        if report.docx_filename:
            docx_status = "✅ 匹配" if report.docx_sha256_match else "❌ 缺失或不匹配"
            console.print(f"Word Docx 文件: {report.docx_filename} ({docx_status})")
        console.print(f"引用映射状态: {report.reference_mapping_status} (文献数: {report.cited_references_count})")
        console.print(f"章节数: {report.section_count} ｜ 证据总数: {report.evidence_count}")

        if report.issues:
            console.print(f"\n[bold]发现问题与告警项 ({len(report.issues)} 项):[/bold]")
            for iss in report.issues:
                c = "red" if iss.severity == "FAIL" else "yellow"
                console.print(f"  [{c}][{iss.severity}] {iss.code}: {iss.message} (位置: {iss.location})[/{c}]")
        console.print()

    exit_codes = {"PASS": 0, "WARN": 1, "FAIL": 2}
    sys.exit(exit_codes.get(report.verdict, 2))


def cmd_compare_runs(args):
    """比对两次综述运行清单（基线 vs 候选），评估质量退化与配置演进。"""
    from litreview.run_validation import compare_review_runs
    report = compare_review_runs(args.base, args.candidate)
    if getattr(args, "json", False):
        sys.stdout.write(json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n")
        sys.stdout.flush()
    else:
        colour_map = {"PASS": "green", "WARN": "yellow", "FAIL": "red"}
        verdict_colour = colour_map.get(report.verdict, "white")
        console.print(f"\n[bold {verdict_colour}]⚖️ 综述基线比对报告: {report.verdict}[/bold {verdict_colour}]")
        console.print(f"基线清单: [cyan]{report.base_manifest_filename}[/cyan] ({report.base_validation.verdict})")
        console.print(f"候选清单: [cyan]{report.candidate_manifest_filename}[/cyan] ({report.candidate_validation.verdict})")
        console.print(f"比对主题: {report.topic}")

        if report.reference_diff.status == "complete":
            console.print(f"引用文献 Jaccard 相似度: {report.reference_diff.jaccard_similarity} (基线: {report.reference_diff.base_count} 篇, 候选: {report.reference_diff.candidate_count} 篇)")
            if report.reference_diff.added_doc_ids:
                console.print(f"  新增引用 (+{len(report.reference_diff.added_doc_ids)}): {', '.join(report.reference_diff.added_doc_ids)}")
            if report.reference_diff.removed_doc_ids:
                console.print(f"  移除引用 (-{len(report.reference_diff.removed_doc_ids)}): {', '.join(report.reference_diff.removed_doc_ids)}")
        else:
            console.print("引用文献对比: 存在历史兼容清单，未进行完整集合比对")

        if report.deltas:
            console.print(f"\n[bold]参数与产物演进差异 ({len(report.deltas)} 项):[/bold]")
            for d in report.deltas:
                c = "red" if d.is_regression else "cyan"
                console.print(f"  [{c}][{d.category}] {d.message}[/{c}]")

        if report.issues:
            console.print(f"\n[bold red]比对发现的问题与退化项 ({len(report.issues)} 项):[/bold red]")
            for iss in report.issues:
                c = "red" if iss.severity == "FAIL" else "yellow"
                console.print(f"  [{c}][{iss.severity}] {iss.code}: {iss.message} (位置: {iss.location})[/{c}]")
        console.print()

    exit_codes = {"PASS": 0, "WARN": 1, "FAIL": 2}
    sys.exit(exit_codes.get(report.verdict, 2))


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
    from openai import OpenAI
    client = OpenAI(api_key=os.getenv("SILICONFLOW_API_KEY"),
                    base_url=os.getenv("SILICONFLOW_API_BASE"), timeout=180)
    store = VectorStore(settings.DB_PATH, settings.EMBEDDING_DIM)
    embedder = _make_embedder(client)
    qvec = embedder.embed_query(args.query)
    reranker = None if args.no_rerank else _make_reranker()
    recall_k = settings.RERANK_CANDIDATES if reranker else args.top_k
    doc_types = None if args.corpus == "all" else [args.corpus]
    results = store.search(qvec, top_k=recall_k, max_per_doc=settings.REVIEW_MAX_CHUNKS_PER_DOC,
                           doc_types=doc_types)
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
        client, _ = _make_client()
    report = run_enrich(settings, console, client, force=args.force, use_llm=use_llm)
    log_run_event(mode="review", event="enrich", status="success",
                  extra={"enriched": report["enriched"], "failed": report["failed"],
                         "with_doi": report["with_doi"], "assets": report["assets"]})


def cmd_add_book(args):
    from litreview.bookintake import ingest_one
    max_pages = args.pages or settings.BOOK_SPLIT_PAGES
    use_llm = settings.REVIEW_ENRICH_LLM and not args.no_llm
    client = None
    if use_llm:
        client, _ = _make_client()

    # 不传路径时默认扫描统一的书籍输入目录（与 INPUT_PDF_DIR 平行，自动创建）
    path = args.path or settings.BOOK_INPUT_DIR
    if not args.path:
        os.makedirs(path, exist_ok=True)
        console.print(f"[dim]未指定路径，扫描默认书籍目录：{path}")

    # 收集待入库文件（PDF/EPUB）：单文件或目录
    if os.path.isdir(path):
        files = sorted(os.path.join(path, f) for f in os.listdir(path)
                       if f.lower().endswith((".pdf", ".epub")))
    elif os.path.isfile(path):
        files = [path]
    else:
        console.print(f"[bold red]❌ 路径不存在：{path}")
        sys.exit(1)
    if not files:
        console.print(f"[yellow]目录中没有 PDF/EPUB：{path}")
        return

    # 与定时任务共用 ingest_one：查重跳过 + 坏文件隔离，两条路行为一致。
    # 只有位于 BOOK_INPUT_DIR 里的文件才会被搬走——用户显式指定的别处路径不去动。
    input_dir = os.path.abspath(settings.BOOK_INPUT_DIR)
    ok = skipped = quarantined = failed = 0
    for path in files:
        try:
            result, _ = ingest_one(
                path, settings, console, client=client, use_llm=use_llm,
                max_pages=max_pages,
                archive=os.path.abspath(os.path.dirname(path)) == input_dir)
            if result == "skipped":
                skipped += 1
            elif result == "quarantined":
                quarantined += 1
            else:
                ok += 1
        except Exception as e:
            failed += 1
            console.print(f"[red]✖ 入库失败 {os.path.basename(path)}: {e}")
            log_run_event(mode="book", event="book_added", title=os.path.basename(path),
                          status="failed", error=str(e))
    console.print(f"[bold]书籍入库完成：成功 {ok} 本，已入库跳过 {skipped} 本，"
                  f"文件损坏隔离 {quarantined} 本，失败 {failed} 本。"
                  + ("" if failed else " 运行 `python review.py index` 建索引。"))


def _set_status(store, args, doc_type: str, allowed: list, noun: str):
    """人工录入状态（专利的法律状态 / 标准的现行与否）。两类共用一份实现。

    必须同时写核实日期：状态会随时间变化，没有日期就无从判断这条还准不准。
    """
    from datetime import date
    if not args.status:
        console.print(f"[red]--set 必须同时给 --status，可选：{'/'.join(allowed)}")
        sys.exit(1)
    today = date.today().isoformat()
    hit = store.set_doc_status(args.set, args.status, today, doc_type)
    if not hit:
        console.print(f"[yellow]未匹配到{noun}「{args.set}」（可用编号、文献ID前缀或标题片段）。")
        sys.exit(1)
    console.print(f"[green]✔ 已将 {len(hit)} 篇标记为「{args.status}」，核实日期 {today}。")
    log_run_event(mode="review", event="doc_status", status="success",
                  extra={"ident": args.set, "type": doc_type,
                         "status": args.status, "docs": len(hit)})


def cmd_patents(args):
    """专利台账：列表 / 人工核实法律状态。

    列表里「出版阶段」和「保护期至」是从专利号和申请日现算出来的事实，不落库；
    「法律状态」只有人工核实过才有值——驳回、撤回、欠年费失效这些事件 PDF 里没有，
    也没法从本地推出来，所以宁可显示"未核实"，也不拿出版阶段冒充法律状态。
    """
    from litreview.store import VectorStore
    from litreview.patent import (patent_stage, patent_expiry, is_term_expired,
                                  LEGAL_STATUSES)
    store = VectorStore(settings.DB_PATH, settings.EMBEDDING_DIM)

    if args.set:
        _set_status(store, args, "patent", LEGAL_STATUSES, "专利")
        return

    rows = store.list_docs("patent")
    if not rows:
        console.print("[yellow]库里还没有 doc_type='patent' 的记录。"
                      "若确信已入库过专利，先跑 `python review.py types --rescan` 预演一下。")
        return
    # 列数压到 6 且名称允许折行——7 列挤在 80 字符终端里会被全部截成省略号，等于没显示。
    table = Table(title=f"📄 专利台账（{len(rows)} 篇）", show_lines=True)
    table.add_column("专利号", style="cyan", no_wrap=True)
    table.add_column("名称", overflow="fold")
    table.add_column("申请日", justify="center", no_wrap=True)
    table.add_column("阶段", justify="center", no_wrap=True)
    table.add_column("保护期至", justify="center", no_wrap=True)
    table.add_column("法律状态", justify="center", overflow="fold")
    short = {"申请公布（审查中）": "公布\n审查中", "发明专利已授权": "发明\n已授权",
             "发明专利已授权（更正）": "发明\n已授权", "实用新型已授权": "实用新型\n已授权",
             "外观设计已授权": "外观\n已授权", "申请公开（审查中）": "公开\n审查中",
             "已授权": "已授权", "未知": "未知"}
    for r in rows:
        exp = patent_expiry(r["patent_no"], r["filing_date"])
        expired = is_term_expired(r["patent_no"], r["filing_date"])
        stage = patent_stage(r["patent_no"])
        legal = (f"{r['legal_status']}\n[dim]{r['status_checked_at']}[/dim]"
                 if r["legal_status"] else "[dim]未核实[/dim]")
        table.add_row(r["patent_no"] or f"[dim]{r['doc_id'][:8]}[/dim]", r["title"] or "-",
                      r["filing_date"] or "-", short.get(stage, stage),
                      f"[red]{exp}[/red]" if expired else (exp or "-"), legal)
    console.print(table)
    console.print("[dim]「阶段」「保护期至」由专利号种别码与申请日现算，是 PDF 上的事实，不会变；\n"
                  "「法律状态」PDF 里没有且会随时间变化，需人工核实后录入：\n"
                  "  python review.py patents --set <专利号|文献ID> --status 驳回")


def cmd_standards(args):
    """标准台账：列表 / 人工核实现行状态。

    标准最要命的是"被代替"和"废止"——封面上永远印着发布日和实施日，看不出它今天还算不算数，
    而拿一份废止标准的限量值去写综述是会出事的。所以状态一栏同样只信人工核实。
    """
    from litreview.store import VectorStore
    from litreview.doctype import statuses_for
    store = VectorStore(settings.DB_PATH, settings.EMBEDDING_DIM)
    allowed = statuses_for("standard")

    if args.set:
        _set_status(store, args, "standard", allowed, "标准")
        return

    rows = store.list_docs("standard")
    if not rows:
        console.print("[yellow]库里还没有 doc_type='standard' 的记录。"
                      "若确信已入库过标准，先跑 `python review.py types --rescan` 预演一下。")
        return
    table = Table(title=f"📐 标准台账（{len(rows)} 篇）", show_lines=True)
    table.add_column("标准号", style="cyan", no_wrap=True)
    table.add_column("名称", overflow="fold")
    table.add_column("实施日期", justify="center", no_wrap=True)
    table.add_column("发布机构", overflow="fold")
    table.add_column("状态", justify="center", overflow="fold")
    for r in rows:
        status = (f"{r['legal_status']}\n[dim]{r['status_checked_at']}[/dim]"
                  if r["legal_status"] else "[dim]未核实[/dim]")
        table.add_row(r["doc_no"] or f"[dim]{r['doc_id'][:8]}[/dim]", r["title"] or "-",
                      r["effective_date"] or r["year"] or "-",
                      r["inventors"] or r["assignee"] or "-", status)
    console.print(table)
    console.print("[dim]标准是否现行、有没有被新版代替，PDF 封面上看不出来，需查国家标准全文公开系统后录入：\n"
                  f"  python review.py standards --set <标准号|文献ID> --status {allowed[0]}")


def cmd_types(args):
    """分类总览 / 重扫存量归位。"""
    from litreview.store import VectorStore
    from litreview import doctype as dt
    if args.rescan:
        from litreview.enrich import rescan_types
        rescan_types(settings, console, apply=args.apply, only=args.only or "")
        return

    store = VectorStore(settings.DB_PATH, settings.EMBEDDING_DIM)
    counts = store.doc_type_counts()
    table = Table(title=f"🗂 文献分类总览（{sum(counts.values())} 篇）", show_lines=False)
    table.add_column("类型", no_wrap=True)
    table.add_column("GB/T 7714 码", justify="center", no_wrap=True)
    table.add_column("篇数", justify="right", no_wrap=True)
    table.add_column("归类方式", overflow="fold")
    for key in dt.all_types():
        n = counts.get(key, 0)
        how = "封面正则自动识别" if dt.DOC_TYPES[key]["detect"] else "入库时指定 / set-type 人工指定"
        table.add_row(dt.label(key), f"[{dt.gb_code(key)}]",
                      str(n) if n else "[dim]0[/dim]", f"[dim]{how}[/dim]")
    # 库里出现了词表以外的 doc_type（历史遗留或手改过库）也要显示出来，不能悄悄漏掉
    for key, n in sorted(counts.items()):
        if key not in dt.DOC_TYPES:
            table.add_row(f"[yellow]{key}[/yellow]", "[dim]?[/dim]", str(n),
                          "[yellow]不在类型词表里，参考文献会按 [J] 打印[/yellow]")
    console.print(table)
    console.print("[dim]只有专利和标准的封面带强制性格式，能靠正则可靠识别；\n"
                  "报告、财报、协会资料、网页在正则眼里跟论文长得一样，只能人工指定：\n"
                  "  python review.py set-type <文献ID|编号|标题片段> --type report")


def cmd_set_type(args):
    """人工指定文献类型，顺带补录参考文献著录要用到的字段。"""
    from litreview.store import VectorStore
    from litreview import doctype as dt
    kind = dt.normalize(args.type)
    if not kind:
        console.print(f"[red]认不出类型「{args.type}」。可用："
                      + "、".join(f"{k}({dt.label(k)})" for k in dt.all_types()))
        sys.exit(1)

    store = VectorStore(settings.DB_PATH, settings.EMBEDDING_DIM)
    hits = store.resolve_docs(args.ident)
    if not hits:
        console.print(f"[yellow]未匹配到「{args.ident}」（可用文献ID前缀、编号或标题片段）。")
        sys.exit(1)
    meta = store.get_paper_meta(hits)
    if len(hits) > 1 and not args.all:
        console.print(f"[yellow]「{args.ident}」匹配到 {len(hits)} 篇，不确定你要改哪一篇：")
        for doc_id in hits:
            m = meta.get(doc_id, {})
            console.print(f"  {doc_id[:8]}  [{dt.gb_code(m.get('doc_type'))}] "
                          f"{(m.get('title_zh') or m.get('title') or '')[:50]}")
        console.print("[dim]请用更精确的编号或文献ID前缀；确实要全改就加 --all。")
        sys.exit(1)

    fields = {"doc_no": args.no or "", "url": args.url or "", "title": args.title or "",
              "authors": args.authors or "", "publisher": args.publisher or "",
              "pub_place": args.place or "", "year": args.year or ""}
    fields = {k: v for k, v in fields.items() if v}
    for doc_id in hits:
        m = meta.get(doc_id, {})
        cur = m.get("doc_type") or dt.DEFAULT_TYPE
        store.mark_doc_type(doc_id, kind, fields)
        old = f"（原 {dt.label(cur)}）" if cur != kind else ""
        title = args.title or m.get("title_zh") or m.get("title") or ""
        console.print(f"[green]✔ {doc_id[:8]} → [{dt.gb_code(kind)}] {dt.label(kind)}{old}  "
                      f"{title[:40]}")
    log_run_event(mode="review", event="set_type", status="success",
                  extra={"ident": args.ident, "type": kind, "docs": len(hits)})


def cmd_rm(args):
    from litreview.store import VectorStore
    store = VectorStore(settings.DB_PATH, settings.EMBEDDING_DIM)
    hits = store.resolve_docs(args.ident)
    if not hits:
        console.print(f"[yellow]未找到与「{args.ident}」匹配的文献。[/yellow]")
        return
    if len(hits) > 1 and not args.all:
        console.print(f"[yellow]「{args.ident}」匹配到 {len(hits)} 篇文献，为防误删请精确指定，或加 --all 确认全部删除：[/yellow]")
        meta = store.get_paper_meta(hits)
        for d in hits:
            m = meta.get(d, {})
            console.print(f"  {d[:8]}  《{m.get('title','-')}》 ({m.get('year') or '-'})")
        return

    keep_source = not args.delete_source
    dry_run = not args.yes

    if dry_run:
        console.print(f"[cyan]=== 预演删除 (Dry-Run: 实际未删除，加 --yes 执行真正删除) ===[/cyan]")

    for doc_id in hits:
        res = store.delete_doc(doc_id, settings=settings, keep_source=keep_source, dry_run=dry_run)
        status_tag = "[yellow]预演删除[/yellow]" if dry_run else "[green]已安全删除[/green]"
        files_str = ", ".join(os.path.basename(f) for f in res["files_removed"]) if res["files_removed"] else "无"
        console.print(f"{status_tag}: {doc_id[:8]} 《{res['title']}》\n"
                      f"  - 数据库：papers 1 行, details {res['details_deleted']} 行, "
                      f"chunks {res['chunks_deleted']} 条, assets {res['assets_deleted']} 条, "
                      f"refs {res['refs_deleted']} 条, 证据缓存 {res['evidence_cache_deleted']} 条\n"
                      f"  - Excel：移除 {res['excel_rows_removed']} 行\n"
                      f"  - 物理文件/目录 ({len(res['files_removed'])} 项)：{files_str}")

    if dry_run:
        console.print(f"\n[bold yellow]以上为预演结果。确认要彻底删除请执行：\n  python review.py rm \"{args.ident}\" --yes{' --delete-source' if args.delete_source else ''}{' --all' if args.all else ''}[/bold yellow]")
    else:
        console.print(f"[bold green]✔ 级联删除完成：共清理 {len(hits)} 篇文献。[/bold green]")
        log_run_event(mode="review", event="doc_remove", status="success",
                      extra={"ident": args.ident, "docs": len(hits), "keep_source": keep_source})


def cmd_ls(args):
    import mcp_server
    text = mcp_server.list_documents.__wrapped__(
        query=args.query or "",
        doc_type=args.type or "",
        vector_status=args.vector_status or "",
        sort_by=args.sort_by or "processed_at",
        sort_order=args.sort_order or "desc",
        cursor=args.cursor or 0,
        limit=args.limit or 20
    )
    console.print(text)


def cmd_reconcile(args):
    import mcp_server
    text = mcp_server.library_reconcile.__wrapped__()
    console.print(text)


def cmd_propose(args):
    """预立论探测：从本地文献库提炼 3 套差异鲜明的顶刊级立论方案（Thesis + Research Gap + 主线逻辑）。"""
    from litreview.store import VectorStore
    from litreview.stages import propose_review_angles
    client, _ = _make_client()
    store = VectorStore(settings.DB_PATH, settings.EMBEDDING_DIM)
    _require_siliconflow()
    from openai import OpenAI
    embedder = _make_embedder(OpenAI(api_key=os.getenv("SILICONFLOW_API_KEY"), base_url=os.getenv("SILICONFLOW_API_BASE"), timeout=180))
    console.print(f"[cyan]正在为主题「{args.topic}」扫描本地文献并提炼顶刊级立论方案...")
    angles = propose_review_angles(args.topic, store, embedder, client, settings.ANALYSIS_MODEL,
                                   settings, console, words=args.words or 0, target_lang=args.lang or "zh")
    console.rule(f"[bold green]💡 综述立论与学术缺口备选方案（共 {len(angles)} 套）")
    for a in angles:
        console.print(f"\n[bold yellow]方案 {a.get('angle_id')}: 《{a.get('title')}》[/bold yellow]")
        console.print(f"  [cyan]🎯 核心立论 (Thesis):[/cyan] {a.get('thesis')}")
        console.print(f"  [red]🔍 学术缺口 (Gap):[/red] {a.get('research_gap')}")
        console.print(f"  [blue]📐 主线逻辑:[/blue] {a.get('mainline_logic')}")
        console.print(f"  [dim]💡 预期学术增量: {a.get('target_value')}")
        secs = a.get('suggested_sections', [])
        if secs:
            console.print(f"  [dim]📋 建议章节: {' → '.join(secs)}")


def cmd_outline(args):
    from litreview.store import VectorStore
    from litreview.stages import generate_outline
    client, model = _make_client()
    store = VectorStore(settings.DB_PATH, settings.EMBEDDING_DIM)
    _require_siliconflow()
    from openai import OpenAI
    embedder = _make_embedder(OpenAI(api_key=os.getenv("SILICONFLOW_API_KEY"), base_url=os.getenv("SILICONFLOW_API_BASE"), timeout=180))
    console.print(f"[cyan]正在为主题「{args.topic}」生成综述大纲...")
    outline = generate_outline(args.topic, store, embedder, client, model, settings, console,
                               focus=args.focus or "", n_sections=args.sections or 0,
                               words=args.words or 0, target_lang=args.lang or "zh",
                               research_gap=getattr(args, 'gap', '') or '',
                               thesis=getattr(args, 'thesis', '') or '',
                               mainline_logic=getattr(args, 'logic', '') or '')
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
    import time
    from litreview.store import VectorStore
    from litreview.stages import (generate_outline, load_outline_file, gather_evidence,
                                  write_intro_conclusion,
                                  assemble_review, render_review_note, export_docx)

    client, model = _make_client()
    store = VectorStore(settings.DB_PATH, settings.EMBEDDING_DIM)
    _require_siliconflow()
    from openai import OpenAI
    embedder = _make_embedder(OpenAI(api_key=os.getenv("SILICONFLOW_API_KEY"), base_url=os.getenv("SILICONFLOW_API_BASE"), timeout=180))
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
        console.print(f"[cyan]阶段 A：生成大纲（结合立论与痛点）...")
        outline = generate_outline(args.topic, store, embedder, client, model, settings, console,
                                   focus=focus, n_sections=args.sections or 0,
                                   words=args.words or 0, target_lang=args.lang or "zh",
                                   research_gap=getattr(args, 'gap', '') or '',
                                   thesis=getattr(args, 'thesis', '') or '',
                                   mainline_logic=getattr(args, 'logic', '') or '')
        console.print(f"[green]✔ 大纲: {outline.title}（{len(outline.sections)} 章节）")

    from litreview.review_orchestrator import allocate_review_words
    word_plan = allocate_review_words(args.words or 0, len(outline.sections))
    section_words = word_plan.section_words
    intro_words = word_plan.intro_words
    concl_words = word_plan.conclusion_words
    if args.words:
        console.print(f"[dim]目标总字数 {args.words}：每章节 {section_words[0]}-{section_words[1]} 字，"
                      f"引言 {intro_words[0]}-{intro_words[1]} 字，结论 {concl_words[0]}-{concl_words[1]} 字")

    if args.dry_run:
        section_evidence = []
        for i, sec in enumerate(outline.sections, 1):
            console.print(f"[cyan]阶段 B：收集证据 [{i}/{len(outline.sections)}] {sec.heading} ...")
            ev = gather_evidence(sec, store, embedder, client, settings.ANALYSIS_MODEL,
                                 settings, console, paper_meta, reranker=reranker)
            section_evidence.append(ev)
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

    # Stage B/C 共享深模块：逐章检索→写作，确保跨章降权对 MCP/CLI 同时生效。
    from litreview.review_orchestrator import (
        ReviewDependencies, ReviewHooks, SectionGenerationConfig,
        generate_review_sections,
    )
    from litreview.stages import get_scaling_config
    scaling = get_scaling_config(args.words or 0, args.lang or "zh")
    section_result = generate_review_sections(
        outline,
        ReviewDependencies(store, embedder, client, reranker, settings, console),
        SectionGenerationConfig(
            section_words=section_words, focus=focus,
            review_top_k=scaling["rerank_top_k"], evidence_n=scaling["evidence_n"],
        ),
        ReviewHooks(on_progress=lambda message: console.print(message)),
    )
    drafts = section_result.drafts
    failed_sections = section_result.failed_sections
    section_evidence = section_result.section_evidence
    if not drafts:
        console.print("[bold red]❌ 所有章节均无内容，综述生成中止。请检查主题与文献库是否匹配，或稍后重试。")
        sys.exit(1)
    console.print("[cyan]阶段 C：撰写引言与结论...")
    try:
        intro, conclusion = write_intro_conclusion(outline, drafts, client, model,
                                                   intro_words=intro_words,
                                                   concl_words=concl_words, focus=focus,
                                                   thesis=getattr(args, 'thesis', '') or '',
                                                   research_gap=getattr(args, 'gap', '') or '')
    except Exception as e:
        console.print(f"[yellow]⚠️ 引言/结论生成失败（正文保留）: {e}")
        intro, conclusion = "", ""

    # Stage D
    evidence_total = section_result.evidence_count
    review = assemble_review(outline, drafts, intro, conclusion, paper_meta, evidence_total)
    template_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "review_template.md")
    out_path = render_review_note(review, paper_meta, settings, template_path, model)

    docx_path = ""
    try:
        docx_path = export_docx(out_path)
    except Exception as e:
        console.print(f"[yellow]⚠️ Word 导出失败（markdown 已正常生成）: {e}")

    manifest_path = ""
    manifest_error = None
    try:
        from litreview.manifest import (
            write_review_manifest, ManifestInput, RunContext, PromptSnapshot, RetrievalSnapshot
        )
        from litreview import prompts
        import uuid
        run_ctx = RunContext(
            topic=args.topic,
            run_id=str(uuid.uuid4()),
            trigger_source="cli",
            model_name=model,
            analysis_model=settings.ANALYSIS_MODEL,
            reranker_model=settings.RERANK_MODEL,
            target_words=args.words or 0,
            target_lang=args.lang or "zh",
            focus=focus,
            thesis=getattr(args, 'thesis', '') or '',
            research_gap=getattr(args, 'gap', '') or '',
            mainline_logic=getattr(args, 'logic', '') or '',
            prompts=PromptSnapshot(
                outline_prompt_version=prompts.PROMPT_VERSION,
                section_prompt_version=prompts.PROMPT_VERSION,
                tldr_prompt_version=prompts.PROMPT_VERSION,
            ),
            retrieval=RetrievalSnapshot(
                review_top_k=scaling.get("rerank_top_k", 0),
                evidence_n=scaling.get("evidence_n", 0),
                target_words=args.words or 0,
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
            evidence_by_heading=section_result.evidence_by_heading,
            paper_meta=paper_meta,
            run_context=run_ctx,
            docx_path=docx_path or None,
        ))
    except Exception as exc:
        manifest_error = str(exc)
        console.print(f"[yellow]⚠️ Manifest 清单生成失败（Markdown 已正常保存）: {exc}")

    elapsed = int(time.time() - t0)
    if failed_sections or manifest_error:
        console.rule("[bold yellow]⚠️ 综述部分完成（存在未完成章节或清单异常）")
    else:
        console.rule("[bold green]✅ 综述生成完成")
    if failed_sections:
        console.print(f"[yellow]⚠️ 以下章节因接口故障未生成，可稍后重跑: {', '.join(failed_sections)}")
    if manifest_error:
        console.print(f"[yellow]⚠️ 清单警告: {manifest_error}")
    console.print(f"标题: [bold]{review.title}[/bold]")
    console.print(f"章节: {len(review.sections)} 个 | 引用文献: {review.doc_count} 篇 | "
                  f"证据: {review.evidence_count} 条 | 耗时: {elapsed}s")
    console.print(f"输出: [bold cyan]{out_path}[/bold cyan]")
    if docx_path:
        console.print(f"Word: [bold cyan]{docx_path}[/bold cyan]")
    if manifest_path:
        console.print(f"清单: [bold cyan]{manifest_path}[/bold cyan]")

    status_tag = "partial" if (failed_sections or manifest_error) else "success"
    log_run_event(mode="review", event="review_generated", title=review.title,
                  status=status_tag,
                  extra={"topic": args.topic, "sections": len(review.sections),
                         "cited_docs": review.doc_count, "elapsed_s": elapsed,
                         "output": out_path, "docx": docx_path, "manifest": manifest_path,
                         "manifest_error": manifest_error})


def main():
    parser = argparse.ArgumentParser(description="综述生成器 CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("index", help="建/增量更新向量索引")
    p.add_argument("--force", action="store_true", help="全量重建索引")
    p.set_defaults(func=cmd_index)

    p = sub.add_parser("status", help="索引统计")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("db-doctor", help="严格只读地体检生产库、引文图谱库与候选缓存库")
    p.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    p.set_defaults(func=cmd_db_doctor)

    p = sub.add_parser("db-repair-graph", help="预演图谱修复；显式 --apply 时仅生成独立 Shadow DB")
    p.add_argument("--apply", action="store_true", help="执行修复，但只写 --shadow-out 指定的新数据库")
    p.add_argument("--shadow-out", help="全新 Shadow DB 输出路径；禁止与现有图谱库相同")
    p.set_defaults(func=cmd_db_repair_graph)

    p = sub.add_parser("db-repair-production", help="预演生产文本/索引/DOI 微创修复")
    p.add_argument("--apply", action="store_true", help="显式执行；受影响正文会标为待重新嵌入")
    p.add_argument("--backup-out", help="执行前生成的全新物理备份路径")
    p.set_defaults(func=cmd_db_repair_production)

    p = sub.add_parser("search", help="检索调试")
    p.add_argument("query")
    p.add_argument("-k", "--top-k", type=int, default=10)
    p.add_argument("--no-rerank", action="store_true", help="只用向量分数，不做重排序")
    p.add_argument("--corpus", choices=["all"] + dt.all_types(), default="all",
                   help="限定检索来源（默认 all=全部类型混检）")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("enrich", help="提取结构化元数据（标题中英/DOI/图表/参考文献）")
    p.add_argument("--force", action="store_true", help="全量重新提取")
    p.add_argument("--no-llm", action="store_true", help="只做本地解析，不用 LLM 补缺")
    p.set_defaults(func=cmd_enrich)

    p = sub.add_parser("add-book", help="教材/书籍入库（PDF 走 MinerU 拆分拼接；EPUB 走 pandoc；均不做 LLM 全文分析）")
    p.add_argument("path", nargs="?", default=None,
                  help="单个 PDF/EPUB 或包含多本的目录；不传则默认扫描 BOOK_INPUT_DIR（默认 ./input_books）")
    p.add_argument("--pages", type=int, default=None, help="PDF 每份最大页数（默认取 BOOK_SPLIT_PAGES=180；EPUB 忽略）")
    p.add_argument("--no-llm", action="store_true", help="元数据只做本地解析，出版社/版次等留空待手填")
    p.set_defaults(func=cmd_add_book)

    p = sub.add_parser("patents", help="专利台账：查看出版阶段/保护期，人工核实法律状态")
    p.add_argument("--set", metavar="专利号或文献ID", help="指定要更新法律状态的专利")
    p.add_argument("--status", choices=dt.statuses_for("patent"),
                   help="配合 --set：人工核实到的法律状态，自动记录核实日期")
    p.set_defaults(func=cmd_patents)

    p = sub.add_parser("standards", help="标准台账：查看标准号/实施日期，人工核实是否现行")
    p.add_argument("--set", metavar="标准号或文献ID", help="指定要更新状态的标准")
    p.add_argument("--status", choices=dt.statuses_for("standard"),
                   help="配合 --set：人工核实到的状态（现行/被代替/废止…），自动记录核实日期")
    p.set_defaults(func=cmd_standards)

    p = sub.add_parser("types", help="文献分类总览；--rescan 重扫存量把专利/标准归位")
    p.add_argument("--rescan", action="store_true",
                   help="重扫存量文献，找出被误判成论文的专利和标准（默认只预演，不写库、不调 LLM）")
    p.add_argument("--apply", action="store_true", help="配合 --rescan：真正写库")
    p.add_argument("--only", choices=[k for k in dt.all_types() if dt.DOC_TYPES[k]["detect"]],
                   help="配合 --rescan：只扫某一类")
    p.set_defaults(func=cmd_types)

    p = sub.add_parser("set-type", help="人工指定文献类型（报告/网页/财报等没法自动识别的）")
    p.add_argument("ident", metavar="文献ID|编号|标题片段")
    p.add_argument("--type", required=True, metavar="类型",
                   help="可用：" + "、".join(f"{k}={dt.label(k)}[{dt.gb_code(k)}]"
                                             for k in dt.all_types()))
    p.add_argument("--no", metavar="编号", help="标准号/报告编号等")
    p.add_argument("--url", help="网络资源的访问地址（[EB/OL] 必备）")
    p.add_argument("--title", help="订正标题")
    p.add_argument("--authors", metavar="责任者", help="作者/编者/发布机构，多个用分号隔开")
    p.add_argument("--publisher", metavar="出版者", help="出版社/发布单位")
    p.add_argument("--place", metavar="出版地", help="如：北京")
    p.add_argument("--year", help="出版年")
    p.add_argument("--all", action="store_true", help="匹配到多篇时全部修改")
    p.set_defaults(func=cmd_set_type)

    p = sub.add_parser("rm", help="安全级联删除文献（清除 6 张数据库表、3 处文件及 Excel 记录，默认 dry-run 预演）")
    p.add_argument("ident", metavar="文献ID|编号|标题片段", help="要删除的文献（支持哈希前缀、专利/标准号、标题片段）")
    p.add_argument("--yes", "-y", action="store_true", help="确认执行真正删除（未加此参数时仅预演输出）")
    p.add_argument("--all", action="store_true", help="匹配到多篇时全部删除")
    p.add_argument("--delete-source", action="store_true", help="同时从 processed_pdfs 删除源 PDF/EPUB（默认保留源文件）")
    p.set_defaults(func=cmd_rm)

    p = sub.add_parser("ls", help="分页浏览文献目录与多维过滤（替代查 Excel）")
    p.add_argument("-q", "--query", help="标题/作者/DOI/关键词模糊过滤")
    p.add_argument("-t", "--type", choices=dt.all_types(), help="文献类型过滤")
    p.add_argument("-v", "--vector-status", choices=["indexed", "pending", "not_eligible"], help="向量索引状态过滤")
    p.add_argument("--sort-by", choices=["processed_at", "year", "title"], default="processed_at", help="排序字段")
    p.add_argument("--sort-order", choices=["asc", "desc"], default="desc", help="排序方向")
    p.add_argument("--cursor", type=int, default=0, help="分页游标偏移量")
    p.add_argument("-n", "--limit", type=int, default=20, help="每页数量（默认 20）")
    p.set_defaults(func=cmd_ls)

    p = sub.add_parser("reconcile", help="严格只读地执行 SQLite/Excel/文件/索引四层数据对齐审计诊断")
    p.set_defaults(func=cmd_reconcile)

    p = sub.add_parser("propose", help="【大纲前置人机对齐】扫描本地文献，提炼 3 套差异鲜明的顶刊级立论方案（Thesis + Research Gap + 主线逻辑）")
    p.add_argument("topic", help="综述主题")
    p.add_argument("--words", type=int, help="目标字数预估（默认按 10000 字标准综述）")
    p.add_argument("--lang", choices=["zh", "en"], default="zh", help="目标语言（默认 zh）")
    p.set_defaults(func=cmd_propose)

    p = sub.add_parser("outline", help="生成综述大纲（支持注入 Thesis 与 Research Gap）")
    p.add_argument("topic", help="综述主题")
    p.add_argument("-o", "--output", help="保存大纲 JSON 的路径")
    p.add_argument("--focus", help="侧重方向（如：侧重工业应用与成本对比）")
    p.add_argument("--words", type=int, help="目标总字数（自动匹配 3 档伸缩与门槛）")
    p.add_argument("--sections", type=int, help="主体章节数（默认由模型根据字数档位自定）")
    p.add_argument("--gap", help="一句话学术缺口与痛点（如：未解决的微塑料土壤降解矛盾）")
    p.add_argument("--thesis", help="一句话核心立论（如：论证生物基改性可完全替代石化聚氨酯）")
    p.add_argument("--logic", help="选定单一主线分类逻辑（如：要素并列利弊暗转 / 现状-危害-对策）")
    p.add_argument("--lang", choices=["zh", "en"], default="zh", help="目标语言（默认 zh）")
    p.set_defaults(func=cmd_outline)

    p = sub.add_parser("generate", help="生成完整综述（支持立论注入、多文献对比矩阵与一问一策展望）")
    p.add_argument("topic", help="综述主题")
    p.add_argument("--outline", help="外部大纲文件（JSON 或 markdown）")
    p.add_argument("--focus", help="侧重方向，贯穿大纲设计与全文写作")
    p.add_argument("--words", type=int, help="目标总字数（自动摊分到各章节与引言/结论）")
    p.add_argument("--sections", type=int, help="主体章节数（默认由模型定 3-6 个）")
    p.add_argument("--gap", help="一句话学术缺口与痛点")
    p.add_argument("--thesis", help="一句话核心立论")
    p.add_argument("--logic", help="选定单一主线分类逻辑")
    p.add_argument("--lang", choices=["zh", "en"], default="zh", help="目标语言（默认 zh）")
    p.add_argument("--dry-run", action="store_true", help="停在证据阶段，打印证据表")
    p.set_defaults(func=cmd_generate)

    p = sub.add_parser("validate-run", help="校验单次综述运行清单的契约合规性、文件完整性与哈希真实性")
    p.add_argument("manifest", help="待校验的 .manifest.json 清单文件路径")
    p.add_argument("--json", action="store_true", help="输出机器可读纯 JSON")
    p.set_defaults(func=cmd_validate_run)

    p = sub.add_parser("compare-runs", help="比对两次综述运行清单（基线 vs 候选），评估质量退化与配置演进")
    p.add_argument("base", help="基线 .manifest.json 文件路径")
    p.add_argument("candidate", help="候选 .manifest.json 文件路径")
    p.add_argument("--json", action="store_true", help="输出机器可读纯 JSON")
    p.set_defaults(func=cmd_compare_runs)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
