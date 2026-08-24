"""综述生成四阶段：A 大纲 → B 证据 → C 写作 → D 装配。"""
import os
import re
import json
import shutil
import hashlib
import subprocess
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
from jinja2 import Template
from tenacity import retry, stop_after_attempt, wait_exponential

from .models import Outline, OutlineSection, Evidence, SectionDraft, ReviewDoc
from . import prompts
from . import doctype as dt


# ── LLM 调用与 JSON 解析 ──────────────────────────────────────────────────────

@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=60), reraise=True)
def _chat_once(client, model: str, prompt: str, json_mode: bool,
               call_type: str = "review_llm", stage: str = "", meta: dict = None) -> str:
    import time
    from utils import get_settings
    from .quota import reserve, reconcile, record_llm_call
    settings = get_settings()
    max_tokens = 12_000 if model == "gpt-5.4" else 4_000
    reservation = reserve(settings, model, prompt, max_tokens)
    kwargs = {"model": model,
              "messages": [{"role": "user", "content": prompt}],
              "temperature": 0.3,
              "max_completion_tokens": max_tokens}
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    
    t0 = time.time()
    resp = None
    try:
        resp = client.chat.completions.create(**kwargs)
        reconcile(settings, model, reservation, resp.usage)
        content = resp.choices[0].message.content
        cost_sec = time.time() - t0
        record_llm_call(settings, call_type=call_type, model=model, prompt=prompt,
                        response_text=content, stage=stage, usage=resp.usage,
                        cost_seconds=cost_sec, status="SUCCESS", meta=meta)
        return content
    except Exception as e:
        cost_sec = time.time() - t0
        record_llm_call(settings, call_type=call_type, model=model, prompt=prompt,
                        response_text="", stage=stage, usage=None,
                        cost_seconds=cost_sec, status="FAILED", error_message=str(e), meta=meta)
        raise


def _chat(client, model: str, prompt: str, json_mode: bool = True,
          call_type: str = "review_llm", stage: str = "", meta: dict = None) -> str:
    return _chat_once(client, model, prompt, json_mode, call_type=call_type, stage=stage, meta=meta)


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


def _chat_json(client, model: str, prompt: str, call_type: str = "review_llm", stage: str = "", meta: dict = None) -> dict:
    """带一次纠错重试的 JSON 调用。"""
    text = _chat(client, model, prompt, json_mode=True, call_type=call_type, stage=stage, meta=meta)
    try:
        return parse_llm_json(text)
    except Exception:
        text2 = _chat(client, model,
                      prompt + "\n\n你上次输出的 JSON 无法解析，请严格只输出合法 JSON，不要输出任何其它文字。",
                      json_mode=True, call_type=call_type, stage=f"{stage}_retry" if stage else "retry", meta=meta)
        return parse_llm_json(text2)


# ── 3 档字数自适应动力学伸缩矩阵 ─────────────────────────────────────────────

def get_scaling_config(words: int = 0, target_lang: str = "zh") -> dict:
    """根据综述目标字数与目标语言，计算 3 档自适应动力学参数与文献门槛。"""
    is_en = target_lang.lower() in ("en", "english", "英文")
    # 统一中英字数尺度：英文按 1.6 倍单词折算等效工作量
    effective_words = words if words > 0 else (10000 if not is_en else 6000)
    if is_en and words > 0:
        effective_words = int(words * 1.6)

    if effective_words < 8000:
        # 档位 1: 精简快报型综述 (<8k 字)
        return {
            "tier": 1,
            "tier_name": "精简快报综述 (<8,000字)",
            "default_sections": 3,
            "section_rule": "3-4 个紧凑主体章节",
            "outline_max_papers": 50,
            "rerank_top_k": 30,
            "evidence_n": 12,
            "intro_ratio": 0.10,
            "concl_ratio": 0.12,
            "min_papers_threshold": 30 if not is_en else 40,
        }
    elif effective_words <= 12000:
        # 档位 2: 标准学术综述 (8k~12k 字)
        return {
            "tier": 2,
            "tier_name": "标准学术综述 (8,000~12,000字)",
            "default_sections": 5,
            "section_rule": "5-6 个主体章节",
            "outline_max_papers": 80,
            "rerank_top_k": 50,
            "evidence_n": 20,
            "intro_ratio": 0.12,
            "concl_ratio": 0.15,
            "min_papers_threshold": 50 if not is_en else 65,
        }
    else:
        # 档位 3: 深度长篇综述 (>12k 字)
        return {
            "tier": 3,
            "tier_name": "长篇深度综述 (>12,000字)",
            "default_sections": 7,
            "section_rule": "7-9 个细分主体章节",
            "outline_max_papers": 120,
            "rerank_top_k": 80,
            "evidence_n": 30,
            "intro_ratio": 0.12,
            "concl_ratio": 0.15,
            "min_papers_threshold": 80 if not is_en else 100,
        }


# ── Stage 0：预立论与角度探测（Thesis & Gap Exploration）──────────────────────

def propose_review_angles(topic: str, store, embedder, client, model, settings, console,
                          words: int = 0, target_lang: str = "zh") -> list:
    """基于馆藏真实文献，为综述主题提炼 3 套差异鲜明的顶刊级立论方案（Thesis + Research Gap + 主线逻辑）。"""
    meta = store.get_paper_meta()
    if not meta:
        raise RuntimeError("文献库为空，无法提炼综述角度。请先运行主管道处理文献。")

    scaling = get_scaling_config(words, target_lang=target_lang)
    max_papers = scaling["outline_max_papers"]

    keep_ids = set(meta.keys())
    try:
        topic_vec = embedder.embed_query(topic)
        doc_vecs = store.doc_mean_vectors()
        scored = [(doc_id, float(v @ topic_vec)) for doc_id, v in doc_vecs.items() if doc_id in meta]
        if len(scored) > 10:
            scores = sorted(s for _, s in scored)
            threshold = scores[int(len(scores) * 0.3)]
            keep_ids = {d for d, s in scored if s >= threshold}
            if len(keep_ids) > max_papers:
                keep_ids = {d for d, _ in sorted(scored, key=lambda x: -x[1])[:max_papers]}
    except Exception as e:
        console.print(f"[yellow]⚠️ 角度探测预筛失败（使用全部文献）: {e}")

    digest_lines = []
    for i, (doc_id, m) in enumerate(sorted(meta.items()), 1):
        if doc_id not in keep_ids:
            continue
        cite_key = m.get("cite_key") or doc_id[:8]
        doc_type_label = dt.label(m.get("doc_type", "paper"))
        kw_tag = f" [关键词: {m['keywords']}]" if m.get("keywords") else ""
        tldr_tag = f" 简评: {m['tldr']}" if m.get("tldr") else ""
        line = f"[{i}] [{doc_type_label}] 《{_best_title(m)}》({m.get('year') or '年份不详'}, 编号:{cite_key}){kw_tag}{tldr_tag}"
        digest_lines.append(line[:400])

    prompt = prompts.PROPOSE_ANGLES_PROMPT.format(n=len(digest_lines), topic=topic,
                                                 paper_digest="\n".join(digest_lines))
    data = _chat_json(client, settings.ANALYSIS_MODEL, prompt,
                      call_type="propose_angles", stage="stage_0_angles",
                      meta={"topic": topic, "words": words, "papers_count": len(digest_lines)})
    angles = data.get("angles", [])
    if not angles:
        raise RuntimeError(f"未能提炼出有效综述方案: {data}")
    return angles


# ── Stage A：大纲（结合立论与痛点精准构建）───────────────────────────────────


def _outline_validation_errors(outline: Outline, scaling: dict, n_sections: int = 0) -> list:
    """校验可执行的大纲结构；语义内容仍由馆藏上下文和 Prompt 约束。"""
    errors = []
    section_count = len(outline.sections)
    if n_sections and section_count != n_sections:
        errors.append(f"要求恰好 {n_sections} 章，实际 {section_count} 章")
    elif not n_sections:
        bounds = {1: (3, 4), 2: (5, 6), 3: (7, 9)}
        lower, upper = bounds[scaling["tier"]]
        if not lower <= section_count <= upper:
            errors.append(f"当前档位要求 {lower}-{upper} 章，实际 {section_count} 章")
    for index, section in enumerate(outline.sections, 1):
        if not section.heading.strip():
            errors.append(f"第 {index} 章标题为空")
        questions = [str(question).strip() for question in section.questions if str(question).strip()]
        if not 2 <= len(questions) <= 4:
            errors.append(f"第 {index} 章必须有 2-4 个非空检索问题，实际 {len(questions)} 个")
    return errors


def generate_outline(topic: str, store, embedder, client, model, settings, console,
                     focus: str = "", n_sections: int = 0, words: int = 0, target_lang: str = "zh",
                     research_gap: str = "", thesis: str = "", mainline_logic: str = "") -> Outline:
    meta = store.get_paper_meta()
    if not meta:
        raise RuntimeError("文献库为空，无法生成大纲。请先运行主管道处理文献。")

    scaling = get_scaling_config(words, target_lang=target_lang)
    max_outline_papers = scaling["outline_max_papers"]

    # 主题向量预筛：剔除与主题明显无关的文献
    keep_ids = set(meta.keys())
    try:
        topic_vec = embedder.embed_query(topic)
        doc_vecs = store.doc_mean_vectors()
        scored = [(doc_id, float(v @ topic_vec)) for doc_id, v in doc_vecs.items() if doc_id in meta]
        if len(scored) > 10:
            scores = sorted(s for _, s in scored)
            threshold = scores[int(len(scores) * 0.3)]  # 30 分位以下剔除
            keep_ids = {d for d, s in scored if s >= threshold}
            # 按动力学档位动态截取候选上限
            if len(keep_ids) > max_outline_papers:
                keep_ids = {d for d, _ in sorted(scored, key=lambda x: -x[1])[:max_outline_papers]}
            console.print(f"[dim]主题预筛：{len(meta)} 篇 → {len(keep_ids)} 篇进入大纲上下文（{scaling['tier_name']}）。")
    except Exception as e:
        console.print(f"[yellow]⚠️ 主题预筛失败（使用全部文献）: {e}")

    digest_lines = []
    for i, (doc_id, m) in enumerate(sorted(meta.items()), 1):
        if doc_id not in keep_ids:
            continue
        cite_key = m.get("cite_key") or doc_id[:8]
        doc_type_label = dt.label(m.get("doc_type", "paper"))
        kw_tag = f" [关键词: {m['keywords']}]" if m.get("keywords") else ""
        tldr_tag = f" 简评: {m['tldr']}" if m.get("tldr") else ""
        line = f"[{i}] [{doc_type_label}] 《{_best_title(m)}》({m.get('year') or '年份不详'}, 编号:{cite_key}){kw_tag}{tldr_tag}"
        digest_lines.append(line[:400])

    section_rule = f"恰好 {n_sections} 个主体章节" if n_sections else scaling["section_rule"]
    thesis_block = f"- 核心立论（Thesis）: {thesis}" if thesis else "- 核心立论（Thesis）: 针对主题核心技术瓶颈与机理分歧展开批判性综合，避免泛泛汇报进展"
    gap_block = f"- 学术缺口（Research Gap）: {research_gap}" if research_gap else "- 学术缺口（Research Gap）: 聚焦现有研究所忽略的关键矛盾、工艺缺陷或理论实测不一致"
    mainline_block = f"- 选定主线分类逻辑: {mainline_logic}" if mainline_logic else "- 选定主线分类逻辑: 沿清晰的因果链/问题导向层层递进，严禁无序拼盘"

    focus_line = (f"\n5. 用户指定的综述侧重方向：{focus}。章节设计必须围绕该侧重展开，"
                  f"与该侧重关系不大的方向不设章节。") if focus else ""
    prompt = prompts.OUTLINE_PROMPT.format(n=len(digest_lines), topic=topic,
                                           thesis_block=thesis_block,
                                           gap_block=gap_block,
                                           mainline_block=mainline_block,
                                           section_rule=section_rule, focus_line=focus_line,
                                           paper_digest="\n".join(digest_lines))
    data = _chat_json(client, model, prompt,
                      call_type="generate_outline", stage="stage_A_outline",
                      meta={"topic": topic, "focus": focus, "words": words, "sections": n_sections, "papers_count": len(digest_lines)})
    outline = Outline.from_dict(data, topic=topic)
    validation_errors = _outline_validation_errors(outline, scaling, n_sections)
    if validation_errors:
        repair_prompt = (
            prompt + "\n\n上次大纲结构校验失败：\n- " + "\n- ".join(validation_errors)
            + "\n请修正所有问题，并重新严格只输出合法 JSON。"
        )
        data = _chat_json(client, model, repair_prompt,
                          call_type="generate_outline", stage="stage_A_outline_validation_retry",
                          meta={"topic": topic, "validation_errors": validation_errors})
        outline = Outline.from_dict(data, topic=topic)
        validation_errors = _outline_validation_errors(outline, scaling, n_sections)
    if validation_errors:
        raise RuntimeError(f"大纲结构校验失败: {'；'.join(validation_errors)}")
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
    cite_key = m.get("cite_key") or chunk.doc_id[:8]
    prompt = prompts.MAP_PROMPT.format(
        heading=heading, questions=questions_text,
        title=_best_title(m), year=m.get("year") or "年份不详",
        cite_key=cite_key,
        chunk_content=chunk.content[:5000],
    )
    data = _chat_json(client, model, prompt, call_type="map_chunk", stage="stage_B_map",
                      meta={"heading": heading, "doc_id": chunk.doc_id, "chunk_id": chunk.chunk_id})
    score = int(data.get("score", 0))
    return {"chunk": chunk, "score": max(1, min(10, score)), "summary": str(data.get("summary", ""))}


def gather_evidence(section: OutlineSection, store, embedder, client, model,
                    settings, console, paper_meta: dict, reranker=None,
                    doc_usage_history: dict = None, review_top_k: int = None,
                    evidence_n: int = None) -> list:
    # 1) 每个问题分别向量召回，按 chunk_id 并集去重，保留最高检索分
    review_top_k = review_top_k or settings.REVIEW_TOP_K
    evidence_n = evidence_n or settings.REVIEW_EVIDENCE_N
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

    def _cross_chapter_score(item, score):
        usage_count = (doc_usage_history or {}).get(item.doc_id, 0)
        return score * max(0.55, 1.0 - 0.25 * usage_count)

    # 1.5) 重排序：每个问题分别 rerank，按 chunk 取各问题最高分，重排后取 top-k
    if reranker is not None:
        pooled = list(pool.values())
        docs = [sc.content for sc in pooled]
        best = {}
        successful_rerank = False
        for q in section.questions:
            reranked = reranker.rerank(q, docs)
            if not getattr(reranked, "degraded", False):
                successful_rerank = True
            for idx, score in reranked:
                # 跨章节长尾文献探索机制：前置章节已被多次引用的文献施加适当降权，让未曝光的优质文献浮现
                adj_score = _cross_chapter_score(pooled[idx], score)
                if idx not in best or adj_score > best[idx]:
                    best[idx] = adj_score
        if not successful_rerank:
            # 服务故障与真实低相关不能混为一谈；故障时确定性回退到原向量分。
            ranked = [
                (index, _cross_chapter_score(item, item.score))
                for index, item in sorted(
                    enumerate(pooled), key=lambda pair: -pair[1].score
                )[:review_top_k]
            ]
        else:
            ranked = sorted(best.items(), key=lambda kv: -kv[1])[:review_top_k]
        candidates = []
        for idx, score in ranked:
            sc = pooled[idx]
            sc.score = score  # 用 rerank 分覆盖向量分（写入 Evidence.retrieval_score）
            candidates.append(sc)
    else:
        candidates = sorted(
            pool.values(), key=lambda item: -_cross_chapter_score(item, item.score)
        )[:int(review_top_k * 1.5)]
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

    # 3) 门槛过滤 + 排序 + 文献多样性优选保留 top-N
    valid_results = [r for r in results if r["score"] >= settings.REVIEW_MIN_SCORE and r["summary"].strip()]
    valid_results.sort(key=lambda r: -r["score"])

    # 强制多样性：单篇文献在最终证据集中最多占 2 条，确保单章覆盖 12-18 篇独立文献
    diverse_kept = []
    doc_counts = {}
    for r in valid_results:
        d_id = r["chunk"].doc_id
        if doc_counts.get(d_id, 0) < 2:
            diverse_kept.append(r)
            doc_counts[d_id] = doc_counts.get(d_id, 0) + 1
            if len(diverse_kept) >= evidence_n:
                break

    # 若多样性过滤后数量未满，再放宽从其余高分条目中补充
    if len(diverse_kept) < evidence_n:
        for r in valid_results:
            if r not in diverse_kept:
                diverse_kept.append(r)
            if len(diverse_kept) >= evidence_n:
                    break
    kept = diverse_kept

    if len(kept) < 3:
        console.print(f"[yellow]⚠️ 章节「{section.heading}」证据不足（仅 {len(kept)} 条），生成内容可能较薄。")
    return [Evidence(chunk_id=r["chunk"].chunk_id, doc_id=r["chunk"].doc_id,
                     score=r["score"], summary=r["summary"],
                     section_title=r["chunk"].section_title,
                     retrieval_score=r["chunk"].score) for r in kept]


def gather_broad_bibliography(section: OutlineSection, store, embedder, paper_meta: dict,
                             exclude_doc_ids: set = None, top_n: int = 25,
                             doc_usage_history: dict = None, reranker=None,
                             char_budget: int = 6000) -> list:
    """第二轨道：为本章节检索 20~25 篇外围/支撑性背景文献清单（排除 Track A 深度证据文献），用于宏观归纳与分类铺垫。"""
    exclude_set = exclude_doc_ids or set()
    scored_docs = {}
    for q in section.questions:
        qvec = embedder.embed_query(q)
        for sc in store.search(qvec, top_k=60, max_per_doc=1):
            if sc.doc_id in exclude_set:
                continue
            penalty = max(0.55, 1.0 - 0.25 * (doc_usage_history or {}).get(sc.doc_id, 0))
            adjusted_score = sc.score * penalty
            if sc.doc_id not in scored_docs or adjusted_score > scored_docs[sc.doc_id]:
                scored_docs[sc.doc_id] = adjusted_score

    # 先保留更宽的向量候选，再用章节全部问题统一重排；重排服务降级为全零时回退向量顺序。
    sorted_doc_ids = sorted(scored_docs.keys(), key=lambda d: -scored_docs[d])[:max(top_n * 3, top_n)]
    candidates = []
    for d_id in sorted_doc_ids:
        m = paper_meta.get(d_id, {})
        if not m:
            continue
        # 第二轨只能使用已有元数据摘要；没有内容时跳过，禁止用通用占位句冒充证据。
        tldr_text = m.get("tldr") or m.get("abstract") or m.get("summary")
        if not str(tldr_text or "").strip():
            continue
        candidates.append({
            "doc_id": d_id,
            "title": _best_title(m),
            "authors": _short_authors(m.get("authors", ""), m.get("language", "zh"), m.get("title", "")),
            "year": m.get("year") or "年份不详",
            "doc_type": m.get("doc_type", "paper"),
            "cite_key": m.get("cite_key") or d_id[:8],
            "tldr": str(tldr_text)[:200],
            "retrieval_score": scored_docs[d_id],
        })

    if reranker is not None and candidates:
        query = f"{section.heading}\n" + "\n".join(section.questions)
        docs = [f"{item['title']}\n{item['tldr']}" for item in candidates]
        ranked = reranker.rerank(query, docs, top_n=len(docs), stage="stage_B_broad_rerank")
        if ranked and any(score > 0 for _, score in ranked):
            candidates = [candidates[idx] for idx, _score in ranked]

    bib_items = []
    used_chars = 0
    for item in candidates:
        item_chars = len(item["tldr"])
        if used_chars + item_chars > max(0, char_budget):
            continue
        bib_items.append(item)
        used_chars += item_chars
        if len(bib_items) >= top_n:
            break
    return bib_items


# ── Stage C：写作 ─────────────────────────────────────────────────────────────

# 支持匹配规范编号 [@U-J25-001]、语义缩写 [@Wang2025] 以及哈希 [@d773f081]
_CITE_RE = re.compile(r"\[@([A-Za-z0-9_\-]+)\]")


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


def _imprint(place: str, publisher: str, year: str) -> str:
    """GB/T 7714 出版项：' 出版地: 出版者, 出版年.'。缺哪项就少哪项，不留占位符。"""
    head = f"{place}: {publisher}" if (place and publisher) else (publisher or place)
    if head:
        return f" {head}, {year}." if year else f" {head}."
    return f" {year}." if year else ""


def format_reference(n: int, doc_id: str, m: dict) -> str:
    """按文献类型排一条 GB/T 7714-2015 参考文献。

    ⚠️ 必须按类型分发。早先这里写成 "if 书籍 … else 一律按 [J]"，结果专利、标准这些
    非论文类型全被印成了期刊论文（`…[J].` 后面还跟个空的期刊名）。新增 doc_type 时，
    doctype.DOC_TYPES 里加一行的同时，**这里也要补上对应的一支**，否则又会掉回 [J]。
    """
    doc_type = m.get("doc_type") or dt.DEFAULT_TYPE
    code = dt.gb_code(doc_type)
    raw_title = m.get("title", "未知文献")
    title = _best_title(m)
    year = m.get("year", "")
    place, publisher = m.get("pub_place", ""), m.get("publisher", "")

    # 标准和网络资源通常不署个人责任者，硬填"作者不详"反而是噪声——没有就整段省掉。
    if doc_type in ("standard", "web") and not m.get("authors"):
        authors = ""
    else:
        authors = _short_authors(m.get("authors", ""), m.get("language", "zh"), raw_title)
    lead = f"{authors}. " if authors else ""

    # 书籍不进 Obsidian、网络资源不走流水线，都没有笔记可链；其余类型链回单篇笔记
    # （命名规则同 utils.generate_obsidian_note）。
    if doc_type in ("book", "web"):
        link = ""
    else:
        safe = "".join(c for c in raw_title if c.isalnum() or c in " -_").strip() or "Untitled_Paper"
        link = f" [[{safe}_{doc_id[:8]}]]"

    if code == "M":                     # 专著：作者. 书名[M]. 版本. 出版地: 出版者, 出版年.
        edition = m.get("edition", "")
        body = (f"[{n}] {lead}{title}[M]." + (f" {edition}." if edition else "")
                + _imprint(place, publisher, year))
    elif code == "P":                   # 专利：申请人. 专利名称: 专利号[P]. 申请日.
        no = m.get("patent_no", "")
        d = m.get("filing_date", "") or year
        body = f"[{n}] {lead}{title}" + (f": {no}" if no else "") + "[P]." + (f" {d}." if d else "")
    elif code == "S":                   # 标准：发布机构. 标准名称: 标准号[S]. 出版地: 出版者, 出版年.
        no = m.get("doc_no", "")
        body = (f"[{n}] {lead}{title}" + (f": {no}" if no else "") + "[S]."
                + _imprint(place, publisher, year))
    elif code == "EB/OL":               # 电子资源：责任者. 题名[EB/OL]. 出版年. 网址.
        url = m.get("url", "")
        body = f"[{n}] {lead}{title}[EB/OL]." + (f" {year}." if year else "") + (f" {url}." if url else "")
    elif code in ("R", "D", "C"):       # 报告/学位论文/会议论文：责任者. 题名[X]. 出版项.
        body = f"[{n}] {lead}{title}[{code}]." + _imprint(place, publisher, year)
    else:                               # 期刊论文：作者. 题名[J]. 期刊, 年. DOI: xxx.
        journal = m.get("journal", "")
        tail = f" {journal}, {year}." if (journal and year) else \
               (f" {journal}." if journal else (f" {year}." if year else ""))
        doi = m.get("doi", "")
        body = f"[{n}] {lead}{title}[J].{tail}" + (f" DOI: {doi}." if doi else "")
    return body.rstrip() + link


def write_section(section: OutlineSection, evidence: list, paper_meta: dict,
                  review_title: str, client, model, id_prefix_len: int = 8,
                  words: tuple = (1800, 2600), focus: str = "",
                  broad_bib: list = None, validation_feedback: str = "") -> SectionDraft:
    blocks = []
    valid_cite_marks = set()
    cite_to_doc = {}
    for i, ev in enumerate(evidence, 1):
        m = paper_meta.get(ev.doc_id, {})
        # 优先使用学术语义编号（如 U-J25-001），回退使用 doc_id 前缀
        cite_key = m.get("cite_key") or ev.doc_id[:id_prefix_len]
        valid_cite_marks.add(cite_key)
        valid_cite_marks.add(ev.doc_id[:id_prefix_len])
        valid_cite_marks.add(ev.doc_id)
        cite_to_doc[cite_key] = ev.doc_id
        cite_to_doc[ev.doc_id[:id_prefix_len]] = ev.doc_id
        cite_to_doc[ev.doc_id] = ev.doc_id

        doc_type_label = dt.label(m.get("doc_type", "paper"))
        blocks.append(
            f"[E{i}] 来源：{_short_authors(m.get('authors',''), m.get('language','zh'), m.get('title',''))}"
            f"（{m.get('year') or '年份不详'}）《{_best_title(m)}》[{doc_type_label}] 引用标记 [@{cite_key}]\n"
            f"     证据：{ev.summary}"
        )

    broad_blocks = []
    for j, b in enumerate(broad_bib or [], 1):
        cite_key = b.get("cite_key") or b["doc_id"][:id_prefix_len]
        valid_cite_marks.add(cite_key)
        valid_cite_marks.add(b["doc_id"][:id_prefix_len])
        valid_cite_marks.add(b["doc_id"])
        cite_to_doc[cite_key] = b["doc_id"]
        cite_to_doc[b["doc_id"][:id_prefix_len]] = b["doc_id"]
        cite_to_doc[b["doc_id"]] = b["doc_id"]

        doc_type_label = dt.label(b.get("doc_type", "paper"))
        broad_blocks.append(
            f"[B{j}] 来源：{b.get('authors', '')}（{b.get('year', '年份不详')}）《{b.get('title', '')}》[{doc_type_label}] 引用标记 [@{cite_key}]\n"
            f"     核心论点/背景：{b.get('tldr', '')}"
        )

    available_doc_count = len({ev.doc_id for ev in evidence} | {b["doc_id"] for b in (broad_bib or [])})
    if available_doc_count >= 3:
        table_requirement = (
            "本章有至少 3 篇可比较文献，必须在正文合适位置嵌入一张 Markdown 横向对比表。"
        )
    else:
        table_requirement = "可比较文献不足 3 篇，不得为了凑表格虚构数据。"
    focus_line = f"\n8. 本综述整体侧重方向：{focus}。行文视角、案例取舍与评述角度均以该侧重为准。" if focus else ""
    repair_line = f"\n9. 上次输出校验未通过：{validation_feedback}。本次必须逐项修复。" if validation_feedback else ""
    sample_1 = list(cite_to_doc.keys())[0] if cite_to_doc else "U-J25-001"
    sample_2 = list(cite_to_doc.keys())[1] if len(cite_to_doc) > 1 else sample_1

    prompt = prompts.WRITE_SECTION_PROMPT.format(
        review_title=review_title, heading=section.heading,
        evidence_blocks="\n".join(blocks) if blocks else "暂无第一轨深度证据",
        broad_bib_blocks="\n".join(broad_blocks) if broad_blocks else "暂无第二轨背景文献",
        sample_cite_1=sample_1, sample_cite_2=sample_2,
        words_min=words[0], words_max=words[1], table_requirement=table_requirement,
        focus_line=focus_line, repair_line=repair_line)
    text = _chat(client, model, prompt, json_mode=False,
                 call_type="write_section", stage="stage_C_write_section",
                 meta={"review_title": review_title, "heading": section.heading,
                       "evidence_count": len(evidence), "broad_bib_count": len(broad_bib or [])}).strip()

    # 丢弃不在证据集中的引用标记（幻觉引用）
    def _filter_cite(m):
        return m.group(0) if m.group(1) in valid_cite_marks else ""
    text = _CITE_RE.sub(_filter_cite, text)

    cited = []
    for m in _CITE_RE.finditer(text):
        doc_id = cite_to_doc.get(m.group(1))
        if doc_id and doc_id not in cited:
            cited.append(doc_id)
    return SectionDraft(heading=section.heading, markdown=text, cited_doc_ids=cited)


def write_intro_conclusion(outline: Outline, drafts: list, client, model,
                           intro_words: tuple = (300, 500),
                           concl_words: tuple = (400, 700), focus: str = "",
                           thesis: str = "", research_gap: str = "") -> tuple:
    # 提取各章节核心论点与关键发现矩阵（前 400 字符 + 结尾上位评述）
    summary_blocks = []
    for d in drafts:
        body = d.markdown.strip()
        preview = body[:350] + ("..." if len(body) > 350 else "")
        # 提取结尾段落作为结论提炼抓手
        paragraphs = [p.strip() for p in body.split("\n\n") if p.strip()]
        last_p = paragraphs[-1][:200] if len(paragraphs) > 1 else ""
        summary_blocks.append(f"## {d.heading}\n- 核心段落展开：{preview}\n- 本章上位结论与瓶颈：{last_p}")

    summaries = "\n\n".join(summary_blocks)
    focus_line = f"\n3. 本综述整体侧重方向：{focus}，引言的意义阐述与结论的展望均围绕它展开。" if focus else ""
    alignment_lines = []
    if thesis:
        alignment_lines.append(f"- 核心立论：{thesis}")
    if research_gap:
        alignment_lines.append(f"- 作者确认的 Research Gap：{research_gap}")
    alignment_block = "\n".join(alignment_lines) or "- 未额外指定，依据正文提炼核心立论与缺口"
    prompt = prompts.INTRO_CONCLUSION_PROMPT.format(
        review_title=outline.title, topic=outline.topic, section_summaries=summaries,
        intro_min=intro_words[0], intro_max=intro_words[1],
        concl_min=concl_words[0], concl_max=concl_words[1], focus_line=focus_line,
        alignment_block=alignment_block)
    data = _chat_json(client, model, prompt,
                      call_type="write_intro_conclusion", stage="stage_C_intro_conclusion",
                      meta={"review_title": outline.title, "topic": outline.topic, "sections_count": len(drafts)})
    return str(data.get("intro", "")), str(data.get("conclusion", ""))


# ── Stage D：装配与渲染 ───────────────────────────────────────────────────────

def assemble_review(outline: Outline, drafts: list, intro: str, conclusion: str,
                    paper_meta: dict, evidence_total: int,
                    id_prefix_len: int = 8) -> ReviewDoc:
    # 建立全局 cite_key 和 doc_id 前缀到完整 doc_id 的映射
    cite_to_doc = {}
    for d in drafts:
        for doc_id in d.cited_doc_ids:
            m = paper_meta.get(doc_id, {})
            cite_key = m.get("cite_key")
            if cite_key:
                cite_to_doc[cite_key] = doc_id
            cite_to_doc[doc_id[:id_prefix_len]] = doc_id
            cite_to_doc[doc_id] = doc_id

    numbering = {}   # doc_id → n
    ordered_docs = []

    def _renumber(text: str) -> str:
        def repl(m):
            doc_id = cite_to_doc.get(m.group(1))
            if doc_id is None:
                return ""
            if doc_id not in numbering:
                numbering[doc_id] = len(numbering) + 1
                ordered_docs.append(doc_id)
            return f"[{numbering[doc_id]}]"
        return _CITE_RE.sub(repl, text)

    new_sections = [SectionDraft(heading=d.heading, markdown=_renumber(d.markdown),
                                 cited_doc_ids=d.cited_doc_ids) for d in drafts]

    references = [format_reference(numbering[d], d, paper_meta.get(d, {})) for d in ordered_docs]

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


def _find_pandoc() -> str:
    p = shutil.which("pandoc")
    if p:
        return p
    fallback = os.path.expanduser("~/.local/bin/pandoc")
    if os.path.exists(fallback):
        return fallback
    raise RuntimeError("未找到 pandoc，请先安装（如 apt install pandoc 或放到 PATH 里）。")


def export_docx(md_path: str) -> str:
    """把渲染好的综述 markdown 转成同名 .docx（pandoc）。
    参考文献行末尾的 Obsidian wikilink（如 " [[标题_docid]]"）只在 Obsidian 里有意义，
    转换前去掉，避免在 Word 里显示成一段没用的方括号文字。
    正文插图是相对路径（assets/xxx.jpg，相对 .md 所在目录），用 --resource-path
    告诉 pandoc 去 .md 同目录找图，否则 Word 里图片会缺失。
    """
    with open(md_path, "r", encoding="utf-8") as f:
        content = f.read()
    content = re.sub(r" \[\[[^\]]+\]\]$", "", content, flags=re.MULTILINE)

    docx_path = os.path.splitext(md_path)[0] + ".docx"
    md_dir = os.path.dirname(os.path.abspath(md_path))
    proc = subprocess.run(
        [_find_pandoc(), "-f", "markdown", "-t", "docx",
         f"--resource-path={md_dir}", "-o", docx_path],
        input=content.encode("utf-8"), capture_output=True, timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(f"pandoc 转换失败: {proc.stderr.decode('utf-8', 'ignore')}")
    return docx_path
