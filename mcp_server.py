"""MCP server：把文献库检索与综述生成暴露为大模型可直接调用的工具（stdio 协议）。

工具一览：
- library_status      文献库与索引概况
- search_literature   向量检索+重排序，返回最相关的原文片段（供调用方大模型自行归纳作答）
- get_paper_info      按关键词查论文元数据（中英标题/DOI/作者/期刊/关键词/TLDR）
- generate_outline    生成综述大纲 JSON（调用方可修改后传给 start_review）
- start_review        后台启动完整综述生成（7-30 分钟），立即返回 job_id
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
from datetime import datetime

# utils 导入时会安装 TeeLogger 劫持 stdout（写 app.log），这会破坏 MCP 的 stdio JSON-RPC
# 协议流 —— 导入完成后立即还原真实的 stdout/stderr。
_real_stdout, _real_stderr = sys.stdout, sys.stderr
from dotenv import load_dotenv
load_dotenv()
from utils import get_settings, log_run_event
sys.stdout, sys.stderr = _real_stdout, _real_stderr

from rich.console import Console
from mcp.server.fastmcp import FastMCP

# 类型词表：工具描述里的类型名、corpus 校验、结果标签全部从这里取，
# 加一种文献类型只改 doctype.py。纯正则模块，导入不带重依赖。
from litreview import doctype as dt

settings = get_settings()
mcp = FastMCP("literature-review")

# ── 独立日志（专用 logger + FileHandler，不挂在 root 上，不碰 stdout/stderr）───────
# mcp 包会给 root logger 装 RichHandler（输出到 stderr），basicConfig 对已有
# handler 的 root 是空操作，所以这里用独立 logger + propagate=False，确保稳定写文件。
# 日志跟随数据库目录，避免 MCP 指向外部 DATA_ROOT 时日志仍落在源码仓库里。
# 排查"客户端显示已连接又立刻断开"时查看 <DB_PATH 目录>/mcp_server.log。
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


def _logged_tool():
    """给工具函数加调用日志（进入/耗时/异常），再套上 @mcp.tool() 注册。"""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            call_repr = ", ".join([repr(a) for a in args] +
                                  [f"{k}={v!r}" for k, v in kwargs.items()])
            logger.info(f"→ {fn.__name__}({call_repr})")
            t0 = time.time()
            try:
                result = fn(*args, **kwargs)
                logger.info(f"← {fn.__name__} 完成，耗时 {time.time()-t0:.1f}s")
                return result
            except Exception:
                logger.error(f"✗ {fn.__name__} 异常，耗时 {time.time()-t0:.1f}s\n"
                            f"{traceback.format_exc()}")
                raise
        return mcp.tool()(wrapper)
    return deco

# ── 惰性全局单例（矩阵缓存跨调用复用） ─────────────────────────────────────────

_state = {"client": None, "store": None, "embedder": None, "reranker": None}
_jobs = {}          # job_id -> {status, log, output, error, ...}
_jobs_lock = threading.Lock()


def _client():
    if _state["client"] is None:
        from llm_router import make_deepseek_client
        _state["client"], _ = make_deepseek_client(quiet=True)
    return _state["client"]


def _store():
    if _state["store"] is None:
        from litreview.store import VectorStore
        _state["store"] = VectorStore(settings.DB_PATH, settings.EMBEDDING_DIM)
    return _state["store"]


def _embedder():
    if _state["embedder"] is None:
        from litreview.embedder import Embedder
        _state["embedder"] = Embedder(_client(), settings.EMBEDDING_MODEL, settings.EMBEDDING_DIM,
                                      settings.EMBEDDING_BATCH_SIZE,
                                      settings.EMBEDDING_QUERY_INSTRUCTION)
    return _state["embedder"]


def _reranker():
    if _state["reranker"] is None and settings.RERANK_ENABLE:
        from litreview.reranker import Reranker
        _state["reranker"] = Reranker(settings.RERANK_MODEL)
    return _state["reranker"]


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
        hint = "（索引可能为空）" if corpus == "all" else f"（已限定 corpus={corpus}，可换成 all 再试）"
        return f"未检索到结果{hint}。"
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
        title = m.get("title_zh") or m.get("title_en") or m.get("title", r.doc_id[:8])
        # 类型标签一定要带上：调用方模型看到"[标准 GB 38400-2019]"才知道这段限量值
        # 出自强制性文件而非某篇论文的实验结论，两者的可引用分量完全不同。
        kt = m.get("doc_type") or "paper"
        no = m.get("patent_no") or m.get("doc_no") or ""
        kind = "" if kt == "paper" else f"[{dt.label(kt)}{' ' + no if no else ''}]"
        out.append(f"[{i}] {kind}《{title}》({m.get('year') or '年份不详'}) "
                   f"小节:{r.section_title or '-'} 相关度:{sc:.3f} 文献ID:{r.doc_id[:8]}\n"
                   f"{r.content[:800]}\n")
    return "\n".join(out)


@_logged_tool()
def get_paper_info(keyword: str, limit: int = 5) -> str:
    """按标题/关键词查询文献库中论文的结构化元数据（中英标题、DOI、作者、期刊、年份、关键词、TLDR）。

    Args:
        keyword: 标题或关键词的一部分（中英文均可）
        limit: 最多返回条数，默认 5
    """
    conn = _store()._conn()
    rows = conn.execute(
        """SELECT p.id, p.title, d.title_zh, d.title_en, d.doi, d.authors, d.journal,
                  d.year, d.keywords, d.n_refs, d.n_figures, d.n_tables, p.result_json,
                  COALESCE(d.doc_type,'paper'), d.patent_no, d.filing_date,
                  d.legal_status, d.status_checked_at, d.publisher,
                  d.doc_no, d.effective_date
           FROM papers p LEFT JOIN paper_details d ON d.doc_id = p.id
           WHERE p.status='EXPORTED' AND (p.title LIKE ? OR d.title_zh LIKE ?
                 OR d.title_en LIKE ? OR d.keywords LIKE ? OR d.patent_no LIKE ?
                 OR d.doc_no LIKE ?)
           LIMIT ?""",
        (f"%{keyword}%",) * 6 + (limit,)).fetchall()
    conn.close()
    if not rows:
        return f"未找到与「{keyword}」匹配的文献。"
    out = [f"找到 {len(rows)} 篇：\n"]
    for (doc_id, title, t_zh, t_en, doi, authors, journal, year,
         kw, n_refs, n_figs, n_tabs, result_json,
         doc_type, patent_no, filing_date, legal_status, checked_at, publisher,
         doc_no, effective_date) in rows:
        tldr = ""
        try:
            tldr = json.loads(result_json).get("tldr", "") if result_json else ""
        except Exception:
            pass
        if doc_type == "patent":
            from litreview.patent import describe
            out.append(f"● [专利] {t_zh or title}（文献ID {doc_id[:8]}）\n"
                       f"  专利号: {patent_no or '-'} | 申请日: {filing_date or '-'}\n"
                       f"  发明人: {authors or '-'} | 申请人/专利权人: {publisher or '-'}\n"
                       f"  {describe(patent_no, filing_date, legal_status, checked_at)}\n"
                       f"  图 {n_figs or 0} / 表 {n_tabs or 0}\n")
            continue
        if doc_type == "standard":
            out.append(f"● [标准] {t_zh or title}（文献ID {doc_id[:8]}）\n"
                       f"  英文题名: {t_en or '-'}\n"
                       f"  发布机构: {authors or '-'} | 出版者: {publisher or '-'}\n"
                       f"  {dt.describe_standard(doc_no, effective_date, legal_status, checked_at)}\n"
                       f"  图 {n_figs or 0} / 表 {n_tabs or 0}\n")
            continue
        out.append(f"● [{dt.label(doc_type)}] {t_zh or title}（文献ID {doc_id[:8]}）\n"
                   f"  英文题名: {t_en or '-'}\n"
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


@_logged_tool()
def generate_outline(topic: str, focus: str = "", sections: int = 0) -> str:
    """为综述主题生成大纲（JSON），可人工/由调用方模型修改后传给 start_review 的 outline_json 参数。
    约需 30-90 秒。

    Args:
        topic: 综述主题
        focus: 侧重方向（可选，如"侧重工业化应用与成本"）
        sections: 主体章节数（可选，0 表示由模型定 3-6 个）
    """
    from litreview.stages import generate_outline as _gen
    console = Console(file=io.StringIO(), no_color=True, width=100)
    outline_model = os.getenv("MCP_OUTLINE_MODEL", "Qwen/Qwen2.5-72B-Instruct")
    outline = _gen(topic, _store(), _embedder(), _client(), outline_model,
                   settings, console, focus=focus, n_sections=sections)
    return json.dumps(outline.model_dump(), ensure_ascii=False, indent=2)


# ── 综述生成（后台任务模式） ──────────────────────────────────────────────────

def _run_review_job(job_id: str, topic: str, focus: str, words: int,
                    n_sections: int, outline_json: str):
    from litreview.models import Outline
    from litreview.stages import (generate_outline as _gen, gather_evidence, write_section,
                                  write_intro_conclusion, assemble_review, render_review_note,
                                  export_docx)
    from litreview.figures import select_section_figures
    job = _jobs[job_id]
    buf = io.StringIO()
    console = Console(file=buf, no_color=True, width=100)

    def log(msg):
        with _jobs_lock:
            job["log"].append(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

    try:
        client = _client()
        model = settings.REVIEW_MODEL
        store, embedder, reranker = _store(), _embedder(), _reranker()
        paper_meta = store.get_paper_meta()

        if outline_json:
            outline = Outline.from_dict(json.loads(outline_json), topic=topic)
            log(f"使用调用方提供的大纲（{len(outline.sections)} 章节）")
        else:
            log("阶段 A：生成大纲...")
            outline = _gen(topic, store, embedder, client, model, settings, console,
                           focus=focus, n_sections=n_sections)
            log(f"大纲完成：{outline.title}（{len(outline.sections)} 章节）")

        if words:
            intro_target = max(300, min(900, int(words * 0.12)))
            concl_target = max(350, min(1100, int(words * 0.15)))
            per = max(400, (words - intro_target - concl_target)
                      // max(1, len(outline.sections)))
            section_words = (int(per * 0.85), int(per * 1.15))
            intro_words = (int(intro_target * 0.8), int(intro_target * 1.2))
            concl_words = (int(concl_target * 0.8), int(concl_target * 1.2))
        else:
            section_words, intro_words, concl_words = (600, 1000), (300, 500), (400, 700)

        section_evidence = []
        for i, sec in enumerate(outline.sections, 1):
            log(f"阶段 B：收集证据 [{i}/{len(outline.sections)}] {sec.heading}")
            ev = gather_evidence(sec, store, embedder, client, model, settings,
                                 console, paper_meta, reranker=reranker)
            section_evidence.append(ev)
            log(f"  保留 {len(ev)} 条证据")

        drafts = []
        used_figures = set()
        for i, (sec, evs) in enumerate(zip(outline.sections, section_evidence), 1):
            if not evs:
                log(f"阶段 C：章节 [{i}] {sec.heading} 无证据，跳过")
                continue
            log(f"阶段 C：撰写章节 [{i}/{len(outline.sections)}] {sec.heading}")
            draft = write_section(sec, evs, paper_meta, outline.title, client, model,
                                  words=section_words, focus=focus)
            n_figs = select_section_figures(store, reranker, sec, draft, settings,
                                            console, used_figures)
            if n_figs:
                log(f"  插入相关图表 {n_figs} 幅")
            drafts.append(draft)
        if not drafts:
            raise RuntimeError("所有章节均无证据，请检查主题与文献库是否匹配。")

        log("阶段 C：撰写引言与结论...")
        try:
            intro, conclusion = write_intro_conclusion(outline, drafts, client, model,
                                                       intro_words=intro_words,
                                                       concl_words=concl_words, focus=focus)
        except Exception as e:
            log(f"引言/结论生成失败（正文保留）: {e}")
            intro, conclusion = "", ""

        evidence_total = sum(len(e) for e in section_evidence)
        review = assemble_review(outline, drafts, intro, conclusion, paper_meta, evidence_total)
        template_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "review_template.md")
        out_path = render_review_note(review, paper_meta, settings, template_path, model)

        docx_path = ""
        try:
            docx_path = export_docx(out_path)
        except Exception as e:
            log(f"⚠️ Word 导出失败（markdown 已正常生成）: {e}")

        with _jobs_lock:
            job["status"] = "done"
            job["output"] = out_path
            job["docx"] = docx_path
            job["summary"] = (f"《{review.title}》：{len(review.sections)} 章节、"
                              f"引用 {review.doc_count} 篇、证据 {review.evidence_count} 条")
        log(f"✅ 完成，输出: {out_path}" + (f"，Word: {docx_path}" if docx_path else ""))
        log_run_event(mode="review", event="review_generated", title=review.title,
                      status="success", extra={"topic": topic, "via": "mcp",
                                               "output": out_path, "docx": docx_path})
    except Exception as e:
        with _jobs_lock:
            job["status"] = "failed"
            job["error"] = f"{e}\n{traceback.format_exc()[-800:]}"
        log(f"❌ 失败: {e}")


@_logged_tool()
def start_review(topic: str, focus: str = "", words: int = 0,
                 sections: int = 0, outline_json: str = "") -> str:
    """后台启动一篇完整综述的生成（约 7-30 分钟，取决于模型），立即返回 job_id。
    用 review_status 查询进度；完成后综述 markdown（写入 Obsidian 文献库的 reviews 目录）
    和 Word（.docx，同目录同名）会一起生成。

    Args:
        topic: 综述主题（必填）
        focus: 侧重方向（可选），贯穿大纲、写作与结论
        words: 目标总字数（可选，0=默认每章 600-1000 字）
        sections: 主体章节数（可选，0=模型自定 3-6 个）
        outline_json: 大纲 JSON（可选，格式同 generate_outline 的输出；提供则跳过大纲生成）
    """
    job_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    running = [j for j in _jobs.values() if j["status"] == "running"]
    with _jobs_lock:
        _jobs[job_id] = {"status": "running", "topic": topic, "log": [],
                         "output": "", "docx": "", "error": "", "summary": "",
                         "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    threading.Thread(target=_run_review_job, daemon=True,
                     args=(job_id, topic, focus, words, sections, outline_json)).start()
    note = f"（注意：当前已有 {len(running)} 个任务在跑，同时多跑会更慢更贵）" if running else ""
    return (f"综述生成已启动，job_id = {job_id}{note}\n"
            f"主题：{topic}" + (f"｜侧重：{focus}" if focus else "") +
            f"\n预计 7-30 分钟完成，请稍后调用 review_status(\"{job_id}\") 查询进度。")


@_logged_tool()
def review_status(job_id: str = "") -> str:
    """查询综述生成任务的进度与结果。job_id 留空则返回最近一个任务。"""
    with _jobs_lock:
        if not _jobs:
            return "本次会话尚未启动过综述生成任务。"
        jid = job_id or sorted(_jobs.keys())[-1]
        job = _jobs.get(jid)
        if not job:
            return f"未找到任务 {jid}（MCP 服务重启后旧任务记录会丢失）。现有任务: {', '.join(_jobs)}"
        lines = [f"任务 {jid}（{job['topic']}）状态: {job['status']}，启动于 {job['started_at']}"]
        lines += job["log"][-8:]
        if job["status"] == "done":
            lines.append(f"结果: {job['summary']}")
            lines.append(f"文件: {job['output']}")
            if job.get("docx"):
                lines.append(f"Word: {job['docx']}")
        elif job["status"] == "failed":
            lines.append(f"错误: {job['error'][:400]}")
        return "\n".join(lines)


if __name__ == "__main__":
    logger.info("MCP server 就绪，进入 stdio 事件循环")
    try:
        mcp.run()
    except Exception:
        logger.error(f"MCP server 异常退出\n{traceback.format_exc()}")
        raise
    finally:
        logger.info("MCP server 事件循环结束")
