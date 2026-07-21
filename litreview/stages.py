"""综述生成四阶段：A 大纲 → B 证据 → C 写作 → D 装配。"""
import os
import re
import json
import hashlib
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
from jinja2 import Template
from tenacity import retry, stop_after_attempt, wait_exponential

from .models import Outline, OutlineSection, Evidence, SectionDraft, ReviewDoc
from . import prompts


# ── LLM 调用与 JSON 解析 ──────────────────────────────────────────────────────

_fallback = {"client": None, "model": None, "tried": False}


def _get_fallback_client():
    """SiliconFlow 持续拥堵(429/503)时的官方 DeepSeek 兜底（仅 chat，embedding 仍走 SiliconFlow）。"""
    if not _fallback["tried"]:
        _fallback["tried"] = True
        key = os.getenv("DEEPSEEK_API_KEY")
        if key:
            from openai import OpenAI
            _fallback["client"] = OpenAI(
                api_key=key,
                base_url=os.getenv("DEEPSEEK_API_BASE", "https://api.deepseek.com/v1"))
            _fallback["model"] = "deepseek-chat"
    return _fallback["client"], _fallback["model"]


@retry(stop=stop_after_attempt(5), wait=wait_exponential(min=2, max=60))
def _chat_once(client, model: str, prompt: str, json_mode: bool) -> str:
    kwargs = {"model": model,
              "messages": [{"role": "user", "content": prompt}],
              "temperature": 0.3}
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    resp = client.chat.completions.create(**kwargs)
    return resp.choices[0].message.content


def _chat(client, model: str, prompt: str, json_mode: bool = True) -> str:
    try:
        return _chat_once(client, model, prompt, json_mode)
    except Exception:
        fb_client, fb_model = _get_fallback_client()
        if fb_client is None:
            raise
        return _chat_once(fb_client, fb_model, prompt, json_mode)


def parse_llm_json(text: str) -> dict:
    """先直接 json.loads；失败则用括号计数提取最外层 {...} 再试。"""
    try:
        return json.loads(text)
    except Exception:
        pass
    start = text.find("{")
    if start == -1:
        raise ValueError(f"输出中未找到 JSON: {text[:200]}")
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start : i + 1])
    raise ValueError(f"JSON 括号不闭合: {text[:200]}")


def _chat_json(client, model: str, prompt: str) -> dict:
    """带一次纠错重试的 JSON 调用。"""
    text = _chat(client, model, prompt, json_mode=True)
    try:
        return parse_llm_json(text)
    except Exception:
        text2 = _chat(client, model,
                      prompt + "\n\n你上次输出的 JSON 无法解析，请严格只输出合法 JSON，不要输出任何其它文字。",
                      json_mode=True)
        return parse_llm_json(text2)


# ── Stage A：大纲 ─────────────────────────────────────────────────────────────

def generate_outline(topic: str, store, embedder, client, model, settings, console) -> Outline:
    meta = store.get_paper_meta()
    if not meta:
        raise RuntimeError("文献库为空，无法生成大纲。请先运行主管道处理文献。")

    # 主题向量预筛：剔除与主题明显无关的文献（如混入的比色学论文）
    keep_ids = set(meta.keys())
    try:
        topic_vec = embedder.embed_query(topic)
        doc_vecs = store.doc_mean_vectors()
        scored = [(doc_id, float(v @ topic_vec)) for doc_id, v in doc_vecs.items() if doc_id in meta]
        if len(scored) > 10:
            scores = sorted(s for _, s in scored)
            threshold = scores[int(len(scores) * 0.3)]  # 30 分位以下剔除
            keep_ids = {d for d, s in scored if s >= threshold}
            # 保底：至多留 80 篇进大纲上下文
            if len(keep_ids) > 80:
                keep_ids = {d for d, _ in sorted(scored, key=lambda x: -x[1])[:80]}
            console.print(f"[dim]主题预筛：{len(meta)} 篇 → {len(keep_ids)} 篇进入大纲上下文。")
    except Exception as e:
        console.print(f"[yellow]⚠️ 主题预筛失败（使用全部文献）: {e}")

    digest_lines = []
    for i, (doc_id, m) in enumerate(sorted(meta.items()), 1):
        if doc_id not in keep_ids:
            continue
        line = f"[{i}] 《{m['title']}》({m['year'] or '年份不详'}) {m['tldr']}"
        digest_lines.append(line[:300])

    prompt = prompts.OUTLINE_PROMPT.format(n=len(digest_lines), topic=topic,
                                           paper_digest="\n".join(digest_lines))
    data = _chat_json(client, model, prompt)
    outline = Outline.from_dict(data, topic=topic)
    if not outline.sections:
        raise RuntimeError(f"大纲生成失败，未产出任何章节: {data}")
    return outline


def load_outline_file(path: str, topic: str) -> Outline:
    """支持 JSON 或简易 markdown（## 章节标题 + - 检索问题）。"""
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()
    if path.endswith(".json"):
        return Outline.from_dict(json.loads(raw), topic=topic)
    sections = []
    cur = None
    title = topic or "文献综述"
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("# ") and not line.startswith("## "):
            title = line[2:].strip()
        elif line.startswith("## "):
            cur = OutlineSection(heading=line[3:].strip(), questions=[])
            sections.append(cur)
        elif line.startswith("- ") and cur is not None:
            cur.questions.append(line[2:].strip())
    if not sections:
        raise ValueError(f"无法从 {path} 解析出章节（需要 '## 标题' + '- 问题' 格式或 JSON）。")
    return Outline(title=title, topic=topic, sections=sections)


# ── Stage B：证据收集 ─────────────────────────────────────────────────────────

def _map_one_chunk(client, model, heading, questions_text, chunk, paper_meta) -> dict:
    m = paper_meta.get(chunk.doc_id, {})
    prompt = prompts.MAP_PROMPT.format(
        heading=heading, questions=questions_text,
        title=m.get("title", "未知"), year=m.get("year") or "年份不详",
        chunk_content=chunk.content[:4000],
    )
    data = _chat_json(client, model, prompt)
    score = int(data.get("score", 0))
    return {"chunk": chunk, "score": max(1, min(10, score)), "summary": str(data.get("summary", ""))}


def gather_evidence(section: OutlineSection, store, embedder, client, model,
                    settings, console, paper_meta: dict, reranker=None) -> list:
    # 1) 每个问题分别向量召回，按 chunk_id 并集去重，保留最高检索分
    recall_k = settings.RERANK_CANDIDATES if reranker else settings.REVIEW_TOP_K
    pool = {}
    for q in section.questions:
        qvec = embedder.embed_query(q)
        for sc in store.search(qvec, top_k=recall_k,
                               max_per_doc=settings.REVIEW_MAX_CHUNKS_PER_DOC):
            if sc.chunk_id not in pool or sc.score > pool[sc.chunk_id].score:
                pool[sc.chunk_id] = sc
    if not pool:
        return []

    # 1.5) 重排序：每个问题分别 rerank，按 chunk 取各问题最高分，重排后取 top-k
    if reranker is not None:
        pooled = list(pool.values())
        docs = [sc.content for sc in pooled]
        best = {}
        for q in section.questions:
            for idx, score in reranker.rerank(q, docs):
                if idx not in best or score > best[idx]:
                    best[idx] = score
        ranked = sorted(best.items(), key=lambda kv: -kv[1])[: settings.REVIEW_TOP_K]
        candidates = []
        for idx, score in ranked:
            sc = pooled[idx]
            sc.score = score  # 用 rerank 分覆盖向量分（写入 Evidence.retrieval_score）
            candidates.append(sc)
    else:
        candidates = sorted(pool.values(), key=lambda s: -s.score)[: int(settings.REVIEW_TOP_K * 1.5)]
    if not candidates:
        return []

    # 2) map 步：每个唯一 chunk 一次调用，一次对本节全部问题打分
    questions_text = "\n".join(f"{i+1}. {q}" for i, q in enumerate(section.questions))
    results = []
    with ThreadPoolExecutor(max_workers=settings.REVIEW_MAP_CONCURRENCY) as pool_exec:
        futures = {pool_exec.submit(_map_one_chunk, client, model, section.heading,
                                    questions_text, c, paper_meta): c for c in candidates}
        for fut in as_completed(futures):
            c = futures[fut]
            try:
                results.append(fut.result())
            except Exception as e:
                console.print(f"[yellow]⚠️ 证据评估失败 chunk {c.chunk_id}: {e}")

    # 3) 门槛过滤 + 排序保留 top-N
    kept = [r for r in results if r["score"] >= settings.REVIEW_MIN_SCORE and r["summary"].strip()]
    kept.sort(key=lambda r: -r["score"])
    kept = kept[: settings.REVIEW_EVIDENCE_N]
    if len(kept) < 3:
        console.print(f"[yellow]⚠️ 章节「{section.heading}」证据不足（仅 {len(kept)} 条），生成内容可能较薄。")
    return [Evidence(chunk_id=r["chunk"].chunk_id, doc_id=r["chunk"].doc_id,
                     score=r["score"], summary=r["summary"],
                     section_title=r["chunk"].section_title,
                     retrieval_score=r["chunk"].score) for r in kept]


# ── Stage C：写作 ─────────────────────────────────────────────────────────────

_CITE_RE = re.compile(r"\[@([0-9a-f]{8,12})\]")


# 标题尾缀 "_张三" 模式（文件名习惯：标题_第一作者）
_TITLE_AUTHOR_RE = re.compile(r"_([一-龥]{2,4})$")


def _short_authors(authors: str, language: str, title: str = "") -> str:
    parts = [a.strip() for a in re.split(r"[,，;；]", authors or "") if a.strip()]
    if not parts:
        # 回退：从标题尾缀提取第一作者（如 "复混肥…测试_史亚龙" → 史亚龙）
        m = _TITLE_AUTHOR_RE.search(title or "")
        if m:
            return f"{m.group(1)}, 等"
        return "作者不详" if language == "zh" else "Unknown"
    suffix = ", 等" if language == "zh" else ", et al"  # 句点由引用行统一添加
    return ", ".join(parts[:3]) + (suffix if len(parts) > 3 else "")


def _display_title(title: str) -> str:
    """去掉标题尾部的 "_作者名" 文件名尾缀，用于参考文献显示。"""
    return _TITLE_AUTHOR_RE.sub("", title or "")


def _best_title(meta: dict) -> str:
    """优先用 enrich 提取的结构化标题（中文文献用 title_zh，其余用 title_en），回退文件名标题。"""
    lang = meta.get("language", "zh")
    if lang == "zh" and meta.get("title_zh"):
        return meta["title_zh"]
    if lang != "zh" and meta.get("title_en"):
        return meta["title_en"]
    return _display_title(meta.get("title", "未知文献"))


def write_section(section: OutlineSection, evidence: list, paper_meta: dict,
                  review_title: str, client, model, id_prefix_len: int = 8) -> SectionDraft:
    blocks = []
    valid_prefixes = set()
    for i, ev in enumerate(evidence, 1):
        m = paper_meta.get(ev.doc_id, {})
        prefix = ev.doc_id[:id_prefix_len]
        valid_prefixes.add(prefix)
        blocks.append(
            f"[E{i}] 来源：{_short_authors(m.get('authors',''), m.get('language','zh'), m.get('title',''))}"
            f"（{m.get('year') or '年份不详'}）《{_best_title(m)}》 引用标记 [@{prefix}]\n"
            f"     证据：{ev.summary}"
        )
    prompt = prompts.WRITE_SECTION_PROMPT.format(
        review_title=review_title, heading=section.heading,
        evidence_blocks="\n".join(blocks))
    text = _chat(client, model, prompt, json_mode=False).strip()

    # 丢弃不在证据集中的引用标记（幻觉引用）
    def _filter_cite(m):
        return m.group(0) if m.group(1) in valid_prefixes else ""
    text = _CITE_RE.sub(_filter_cite, text)

    prefix_to_doc = {ev.doc_id[:id_prefix_len]: ev.doc_id for ev in evidence}
    cited = []
    for m in _CITE_RE.finditer(text):
        doc_id = prefix_to_doc.get(m.group(1))
        if doc_id and doc_id not in cited:
            cited.append(doc_id)
    return SectionDraft(heading=section.heading, markdown=text, cited_doc_ids=cited)


def write_intro_conclusion(outline: Outline, drafts: list, client, model) -> tuple:
    summaries = "\n".join(
        f"## {d.heading}\n{d.markdown[:200]}..." for d in drafts)
    prompt = prompts.INTRO_CONCLUSION_PROMPT.format(
        review_title=outline.title, topic=outline.topic, section_summaries=summaries)
    data = _chat_json(client, model, prompt)
    return str(data.get("intro", "")), str(data.get("conclusion", ""))


# ── Stage D：装配与渲染 ───────────────────────────────────────────────────────

def assemble_review(outline: Outline, drafts: list, intro: str, conclusion: str,
                    paper_meta: dict, evidence_total: int,
                    id_prefix_len: int = 8) -> ReviewDoc:
    # 全局引用编号：按正文出现顺序
    prefix_to_doc = {}
    for d in drafts:
        for doc_id in d.cited_doc_ids:
            prefix_to_doc[doc_id[:id_prefix_len]] = doc_id

    numbering = {}   # doc_id → n
    ordered_docs = []

    def _renumber(text: str) -> str:
        def repl(m):
            doc_id = prefix_to_doc.get(m.group(1))
            if doc_id is None:
                return ""
            if doc_id not in numbering:
                numbering[doc_id] = len(numbering) + 1
                ordered_docs.append(doc_id)
            return f"[{numbering[doc_id]}]"
        return _CITE_RE.sub(repl, text)

    new_sections = [SectionDraft(heading=d.heading, markdown=_renumber(d.markdown),
                                 cited_doc_ids=d.cited_doc_ids) for d in drafts]

    references = []
    for doc_id in ordered_docs:
        m = paper_meta.get(doc_id, {})
        n = numbering[doc_id]
        raw_title = m.get("title", "未知文献")
        authors = _short_authors(m.get("authors", ""), m.get("language", "zh"), raw_title)
        title = _best_title(m)
        journal = m.get("journal", "")
        year = m.get("year", "")
        tail = ""
        if journal and year:
            tail = f" {journal}, {year}."
        elif journal:
            tail = f" {journal}."
        elif year:
            tail = f" {year}."
        doi = m.get("doi", "")
        doi_part = f" DOI: {doi}." if doi else ""
        # 文末 wikilink 链回单篇 Obsidian 笔记（笔记命名规则同 utils.generate_obsidian_note）
        safe_title = "".join(c for c in raw_title if c.isalnum() or c in " -_").strip() or "Untitled_Paper"
        wikilink = f" [[{safe_title}_{doc_id[:8]}]]"
        references.append((f"[{n}] {authors}. {title}[J].{tail}{doi_part}").rstrip() + wikilink)

    return ReviewDoc(
        title=outline.title, topic=outline.topic,
        intro=intro, sections=new_sections, conclusion=conclusion,
        references=references,
        doc_count=len(ordered_docs), evidence_count=evidence_total,
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )


def render_review_note(review: ReviewDoc, paper_meta: dict, settings,
                       template_path: str, model: str) -> str:
    with open(template_path, "r", encoding="utf-8") as f:
        template = Template(f.read())

    # wikilink 目标与单篇笔记文件名规则一致（utils.generate_obsidian_note）
    wikilinks = []
    for i, ref in enumerate(review.references, 1):
        wikilinks.append({"n": i, "text": ref})

    content = template.render(
        title=review.title, topic=review.topic,
        date=review.generated_at, model=model,
        doc_count=review.doc_count, evidence_count=review.evidence_count,
        intro=review.intro, sections=review.sections,
        conclusion=review.conclusion, references=review.references,
    )

    safe_topic = "".join(c for c in review.topic if c.isalnum() or c in " -_").strip() or "综述"
    os.makedirs(settings.REVIEW_OUTPUT_DIR, exist_ok=True)
    out_path = os.path.join(
        settings.REVIEW_OUTPUT_DIR,
        f"综述_{safe_topic}_{datetime.now().strftime('%Y%m%d_%H%M')}.md")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(content)
    return out_path
