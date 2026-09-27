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

from .models import (
    Outline, OutlineSection, Evidence, SectionDraft, ReviewDoc, infer_table_suggested
)
from . import prompts
from . import doctype as dt
from .review_quality import (
    EvidenceContext,
    _best_title,
    _short_authors,
    audit_and_clean_review,
    format_reference,
)


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


def _reject_duplicate_keys(pairs):
    res = {}
    for k, v in pairs:
        if k in res:
            raise ValueError(f"JSON 包含重复对象键: '{k}'")
        res[k] = v
    return res


def parse_llm_json(text: str) -> dict:
    """先直接 json.loads；失败则用括号计数提取最外层 {...} 再试。严格拒绝重复对象键。"""
    try:
        return json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except ValueError as e:
        if "JSON 包含重复对象键" in str(e):
            raise
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
                return json.loads(text[start : i + 1], object_pairs_hook=_reject_duplicate_keys)
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
    cur_heading = ""
    cur_questions = []
    title = topic or "文献综述"
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("# ") and not line.startswith("## "):
            title = line[2:].strip()
        elif line.startswith("## "):
            if cur_heading:
                table_sug = infer_table_suggested(cur_heading, cur_questions)
                sections.append(OutlineSection(heading=cur_heading, questions=cur_questions, table_suggested=table_sug))
            cur_heading = line[3:].strip()
            cur_questions = []
        elif line.startswith("- ") and cur_heading:
            cur_questions.append(line[2:].strip())
    if cur_heading:
        table_sug = infer_table_suggested(cur_heading, cur_questions)
        sections.append(OutlineSection(heading=cur_heading, questions=cur_questions, table_suggested=table_sug))
    if not sections:
        raise ValueError(f"无法从 {path} 解析出章节（需要 '## 标题' + '- 问题' 格式或 JSON）。")
    return Outline(title=title, topic=topic, sections=sections)


# ── Stage B：证据收集 ─────────────────────────────────────────────────────────

def _history_factor(doc_id: str, doc_usage_history: dict = None) -> float:
    """跨章降权保留 0.35 下限，避免核心文献因早期被引用而永久归零。"""
    usage = (doc_usage_history or {}).get(doc_id, 0)
    return max(0.35, 1.0 - 0.20 * usage)


def _doc_type_weight(section: OutlineSection, meta: dict) -> float:
    """文献类型权重随章节语境变化，不把专利声明等同于实验验证。"""
    doc_type = dt.normalize(meta.get("doc_type", "")) or dt.DEFAULT_TYPE
    context = f"{section.heading} {' '.join(str(q) for q in section.questions)}"
    if doc_type == "standard":
        return 1.10 if any(word in context for word in ("标准", "规范", "定义", "检测", "评价")) else 0.90
    if doc_type == "patent":
        return 0.95 if any(word in context for word in ("工艺", "制备", "设备", "产业化", "放大")) else 0.85
    return {
        "paper": 1.00, "report": 0.90, "thesis": 0.90,
        "conf": 0.85, "book": 0.70, "web": 0.65,
    }.get(doc_type, 1.00)


def _marginal_value(
    result: dict,
    section: OutlineSection,
    paper_meta: dict,
    doc_usage_history: dict = None,
) -> float:
    chunk = result["chunk"]
    retrieval = max(0.0, float(getattr(chunk, "score", 0.0)))
    evidence_strength = max(0.0, min(1.0, float(result.get("score", 0)) / 10.0))
    return retrieval * evidence_strength * _doc_type_weight(
        section, paper_meta.get(chunk.doc_id, {})
    ) * _history_factor(chunk.doc_id, doc_usage_history)


_COMPARABLE_METRICS = {
    "release": (
        re.compile(r"释放|控释|缓释", re.I),
        re.compile(r"\d+(?:\.\d+)?\s*(?:d\b|天|%|％)", re.I),
    ),
    "contact_angle": (
        re.compile(r"接触角|WCA", re.I),
        re.compile(r"\d+(?:\.\d+)?\s*°", re.I),
    ),
    "degradation": (
        re.compile(r"降解|失重", re.I),
        re.compile(r"\d+(?:\.\d+)?\s*(?:%|％|d\b|天)", re.I),
    ),
    "strength": (
        re.compile(r"强度|模量", re.I),
        re.compile(r"\d+(?:\.\d+)?\s*MPa\b", re.I),
    ),
}


def _has_nearby_metric_value(text: str, terms: re.Pattern, values: re.Pattern) -> bool:
    for term in terms.finditer(text):
        for value in values.finditer(text):
            gap = max(term.start(), value.start()) - min(term.end(), value.end())
            if gap <= 24:
                return True
    return False


def comparable_metric_doc_count(evidence: list) -> int:
    """返回具备同类可比量化指标的最大独立文献数。"""
    metric_docs = {name: set() for name in _COMPARABLE_METRICS}
    for item in evidence:
        for name, (terms, values) in _COMPARABLE_METRICS.items():
            if _has_nearby_metric_value(item.summary or "", terms, values):
                metric_docs[name].add(item.doc_id)
    return max((len(doc_ids) for doc_ids in metric_docs.values()), default=0)


def comparison_table_required(section: OutlineSection, evidence: list) -> bool:
    return bool(section.table_suggested and comparable_metric_doc_count(evidence) >= 3)


EVIDENCE_SCHEMA_VERSION = "1"


def _map_one_chunk(client, model, heading, questions_text, chunk, paper_meta,
                   store=None) -> dict:
    cache_query = f"{heading}\n{questions_text}"
    if store is not None and hasattr(store, "get_evidence_cache"):
        cached = store.get_evidence_cache(
            chunk.chunk_id, chunk.content, cache_query, prompts.PROMPT_VERSION,
            model, EVIDENCE_SCHEMA_VERSION)
        if cached is not None:
            return {"chunk": chunk, **cached, "cached": True}
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
    result = {"chunk": chunk, "score": max(1, min(10, score)),
              "summary": str(data.get("summary", ""))}
    if store is not None and hasattr(store, "put_evidence_cache") and result["summary"].strip():
        store.put_evidence_cache(
            chunk.chunk_id, chunk.content, cache_query, prompts.PROMPT_VERSION,
            model, EVIDENCE_SCHEMA_VERSION, result["score"], result["summary"])
    return result


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
        return score * _history_factor(item.doc_id, doc_usage_history)

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
                if idx not in best or score > best[idx]:
                    best[idx] = score
        if not successful_rerank:
            # 服务故障与真实低相关不能混为一谈；故障时确定性回退到原向量分。
            ranked = [
                (index, item.score)
                for index, item in sorted(
                    enumerate(pooled),
                    key=lambda pair: -_cross_chapter_score(pair[1], pair[1].score),
                )[:review_top_k]
            ]
        else:
            ranked = sorted(
                best.items(),
                key=lambda pair: -_cross_chapter_score(pooled[pair[0]], pair[1]),
            )[:review_top_k]
        candidates = []
        for idx, score in ranked:
            sc = pooled[idx]
            sc.score = score  # 用未降权 rerank 分覆盖向量分，降权在边际价值中统一施加
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
                                    questions_text, c, paper_meta, store): c for c in candidates}
        for fut in as_completed(futures):
            c = futures[fut]
            try:
                results.append(fut.result())
            except Exception as e:
                console.print(f"[yellow]⚠️ 证据评估失败 chunk {c.chunk_id}: {e}")

    # 3) 门槛过滤 + 排序 + 文献多样性优选保留 top-N
    valid_results = [r for r in results if r["score"] >= settings.REVIEW_MIN_SCORE and r["summary"].strip()]
    # 边际价值综合语义相关性、证据强度与文献类型。同文献第 3 个起的
    # chunk 只保留 0.2 增量，且边际价值明显不足时自然停止，不为填满配额引入弱证据。
    per_doc_rank = {}
    ranked_results = []
    for result in sorted(
        valid_results,
        key=lambda item: -_marginal_value(
            item, section, paper_meta, doc_usage_history
        ),
    ):
        doc_id = result["chunk"].doc_id
        rank = per_doc_rank.get(doc_id, 0)
        per_doc_rank[doc_id] = rank + 1
        chunk_decay = 1.0 if rank < 2 else 0.2
        ranked_results.append((
            result,
            _marginal_value(result, section, paper_meta, doc_usage_history) * chunk_decay,
        ))
    ranked_results.sort(key=lambda pair: -pair[1])
    best_value = ranked_results[0][1] if ranked_results else 0.0
    marginal_floor = best_value * 0.20
    kept = [
        result for result, value in ranked_results[:evidence_n]
        if value > 0 and value >= marginal_floor
    ]

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
            adjusted_score = sc.score * _history_factor(sc.doc_id, doc_usage_history)
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


def _citation_aliases(paper_meta: dict, id_prefix_len: int) -> dict:
    """Only accept an alias if it identifies exactly one document in the corpus."""
    owners = {}
    for doc_id, meta in paper_meta.items():
        for alias in (doc_id, doc_id[:id_prefix_len], meta.get("cite_key")):
            if alias:
                owners.setdefault(alias, set()).add(doc_id)
    aliases = {alias: next(iter(docs)) for alias, docs in owners.items() if len(docs) == 1}
    # Exact document IDs are authoritative even if another document's human key collides.
    aliases.update({doc_id: doc_id for doc_id in paper_meta})
    return aliases


def write_section(section: OutlineSection, evidence: list, paper_meta: dict,
                  review_title: str, client, model, id_prefix_len: int = 8,
                  words: tuple = (1800, 2600), focus: str = "",
                  broad_bib: list = None, validation_feedback: str = "") -> SectionDraft:
    blocks = []
    citation_meta = dict(paper_meta)
    for ev in evidence:
        citation_meta.setdefault(ev.doc_id, {})
    for b in broad_bib or []:
        citation_meta.setdefault(b["doc_id"], b)
    aliases = _citation_aliases(citation_meta, id_prefix_len)
    allowed_docs = {ev.doc_id for ev in evidence} | {b["doc_id"] for b in broad_bib or []}
    cite_to_doc = {alias: doc for alias, doc in aliases.items() if doc in allowed_docs}
    valid_cite_marks = set(cite_to_doc)
    for i, ev in enumerate(evidence, 1):
        m = paper_meta.get(ev.doc_id, {})
        cite_key = m.get("cite_key") or ev.doc_id[:id_prefix_len]
        if cite_to_doc.get(cite_key) != ev.doc_id:
            cite_key = ev.doc_id

        doc_type_label = dt.label(m.get("doc_type", "paper"))
        blocks.append(
            f"[E{i}] 来源：{_short_authors(m.get('authors',''), m.get('language','zh'), m.get('title',''))}"
            f"（{m.get('year') or '年份不详'}）《{_best_title(m)}》[{doc_type_label}] 引用标记 [@{cite_key}]\n"
            f"     证据：{ev.summary}"
        )

    broad_blocks = []
    for j, b in enumerate(broad_bib or [], 1):
        cite_key = b.get("cite_key") or b["doc_id"][:id_prefix_len]
        if cite_to_doc.get(cite_key) != b["doc_id"]:
            cite_key = b["doc_id"]

        doc_type_label = dt.label(b.get("doc_type", "paper"))
        broad_blocks.append(
            f"[B{j}] 来源：{b.get('authors', '')}（{b.get('year', '年份不详')}）《{b.get('title', '')}》[{doc_type_label}] 引用标记 [@{cite_key}]\n"
            f"     核心论点/背景：{b.get('tldr', '')}"
        )

    track_a_available = {ev.doc_id for ev in evidence}
    track_b_available = {b["doc_id"] for b in (broad_bib or [])} - track_a_available
    available_doc_count = len(track_a_available | track_b_available)
    if comparison_table_required(section, evidence):
        table_requirement = (
            "大纲已规划横向比较，且第一轨至少 3 篇文献含同类量化指标，"
            "必须在正文合适位置嵌入一张 Markdown 横向对比表。"
        )
    else:
        table_requirement = "本章未同时满足大纲规划和同类可比数据门槛，不要生成表格。"
    focus_line = f"\n8. 本综述整体侧重方向：{focus}。行文视角、案例取舍与评述角度均以该侧重为准。" if focus else ""
    repair_line = f"\n9. 上次输出校验未通过：{validation_feedback}。本次必须逐项修复。" if validation_feedback else ""
    sample_1 = list(cite_to_doc.keys())[0] if cite_to_doc else "U-J25-001"
    sample_2 = list(cite_to_doc.keys())[1] if len(cite_to_doc) > 1 else sample_1
    recommended_min = min(available_doc_count, max(5, words[0] // 250))
    recommended_max = min(18, available_doc_count, max(8, words[1] // 150))
    citation_guidance = (
        f"按本章篇幅与可用证据，通常可参考 {recommended_min}-{recommended_max} 篇独立来源；"
        "这是写作建议而非配额，应由论点覆盖与证据增量自然决定。"
    )

    prompt = prompts.WRITE_SECTION_PROMPT.format(
        review_title=review_title, heading=section.heading,
        evidence_blocks="\n".join(blocks) if blocks else "暂无第一轨深度证据",
        broad_bib_blocks="\n".join(broad_blocks) if broad_blocks else "暂无第二轨背景文献",
        sample_cite_1=sample_1, sample_cite_2=sample_2,
        words_min=words[0], words_max=words[1], table_requirement=table_requirement,
        citation_guidance=citation_guidance,
        focus_line=focus_line, repair_line=repair_line)
    text = _chat(client, model, prompt, json_mode=False,
                 call_type="write_section", stage="stage_C_write_section",
                 meta={"review_title": review_title, "heading": section.heading,
                       "evidence_count": len(evidence), "broad_bib_count": len(broad_bib or [])}).strip()

    # 丢弃不在证据集中的引用标记（幻觉引用）
    def _filter_cite(m):
        return f"[@{cite_to_doc[m.group(1)]}]" if m.group(1) in valid_cite_marks else ""
    text = _CITE_RE.sub(_filter_cite, text)

    cited = []
    for m in _CITE_RE.finditer(text):
        doc_id = cite_to_doc.get(m.group(1))
        if doc_id and doc_id not in cited:
            cited.append(doc_id)
    return SectionDraft(
        heading=section.heading,
        markdown=text,
        cited_doc_ids=cited,
        track_a_doc_ids=[doc_id for doc_id in cited if doc_id in track_a_available],
        track_b_doc_ids=[doc_id for doc_id in cited if doc_id in track_b_available],
    )


INTRO_CONCL_LOWER_RATIO = 0.90
INTRO_CONCL_UPPER_RATIO = 1.20


def _intro_conclusion_validation_errors(data: object, intro_words: tuple, concl_words: tuple) -> list[str]:
    errors = []
    if not isinstance(data, dict):
        return [f"输出必须为 JSON 对象，实际为 {type(data).__name__}"]

    keys = set(data.keys())
    if keys != {"intro", "conclusion"}:
        missing = {"intro", "conclusion"} - keys
        extra = keys - {"intro", "conclusion"}
        if missing:
            errors.append(f"缺少必填顶层字段: {', '.join(sorted(missing))}")
        if extra:
            errors.append(f"包含未知顶层字段: {', '.join(sorted(extra))}，顶层键必须严格仅为 'intro' 与 'conclusion'")

    intro = data.get("intro")
    conclusion = data.get("conclusion")

    if intro is None or not isinstance(intro, str) or not intro.strip():
        errors.append("缺少非空的 'intro' 文本字段")
    if conclusion is None or not isinstance(conclusion, str) or not conclusion.strip():
        errors.append("缺少非空的 'conclusion' 文本字段")

    if errors:
        return errors

    intro_str = intro.strip()
    concl_str = conclusion.strip()

    # 确定性双向字数校验：下限为目标 min 的 90%，上限为目标 max 的 120%
    intro_min_bound = int(intro_words[0] * INTRO_CONCL_LOWER_RATIO) if isinstance(intro_words, (tuple, list)) and len(intro_words) >= 1 else 200
    intro_max_bound = int(intro_words[1] * INTRO_CONCL_UPPER_RATIO) if isinstance(intro_words, (tuple, list)) and len(intro_words) >= 2 else 1500

    concl_min_bound = int(concl_words[0] * INTRO_CONCL_LOWER_RATIO) if isinstance(concl_words, (tuple, list)) and len(concl_words) >= 1 else 250
    concl_max_bound = int(concl_words[1] * INTRO_CONCL_UPPER_RATIO) if isinstance(concl_words, (tuple, list)) and len(concl_words) >= 2 else 2000

    len_intro = len(intro_str)
    if len_intro < intro_min_bound:
        errors.append(f"引言字数不足：实际 {len_intro} 字，低于下限 {intro_min_bound} 字（目标区间 {intro_words[0]}-{intro_words[1]} 字，允许 90% 容差）")
    elif len_intro > intro_max_bound:
        errors.append(f"引言字数超标：实际 {len_intro} 字，超过上限 {intro_max_bound} 字（目标区间 {intro_words[0]}-{intro_words[1]} 字，允许 120% 容差）")

    len_concl = len(concl_str)
    if len_concl < concl_min_bound:
        errors.append(f"结论字数不足：实际 {len_concl} 字，低于下限 {concl_min_bound} 字（目标区间 {concl_words[0]}-{concl_words[1]} 字，允许 90% 容差）")
    elif len_concl > concl_max_bound:
        errors.append(f"结论字数超标：实际 {len_concl} 字，超过上限 {concl_max_bound} 字（目标区间 {concl_words[0]}-{concl_words[1]} 字，允许 120% 容差）")

    return errors


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
    validation_errors = _intro_conclusion_validation_errors(data, intro_words, concl_words)
    if validation_errors:
        repair_prompt = (
            prompt + "\n\n上次引言与结论输出校验失败：\n- " + "\n- ".join(validation_errors)
            + "\n请修正所有问题，确保 intro 与 conclusion 结构完整且字数达标，并重新严格只输出合法 JSON。"
        )
        data = _chat_json(client, model, repair_prompt,
                          call_type="write_intro_conclusion", stage="stage_C_intro_conclusion_validation_retry",
                          meta={"review_title": outline.title, "topic": outline.topic, "validation_errors": validation_errors})
        validation_errors = _intro_conclusion_validation_errors(data, intro_words, concl_words)
    if validation_errors:
        raise ValueError(f"引言/结论结构或字数校验失败: {'；'.join(validation_errors)}")
    return str(data["intro"]), str(data["conclusion"])


# ── Stage D：装配与渲染 ───────────────────────────────────────────────────────

def assemble_review(outline: Outline, drafts: list, intro: str, conclusion: str,
                    paper_meta: dict, evidence_total: int,
                    id_prefix_len: int = 8) -> ReviewDoc:
    cited_ids = {doc_id for draft in drafts for doc_id in draft.cited_doc_ids}
    citation_meta = dict(paper_meta)
    for doc_id in cited_ids:
        citation_meta.setdefault(doc_id, {})
    cite_to_doc = {alias: doc for alias, doc in _citation_aliases(citation_meta, id_prefix_len).items()
                   if doc in cited_ids}

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

    new_sections = [SectionDraft(
        heading=d.heading,
        markdown=_renumber(d.markdown),
        cited_doc_ids=d.cited_doc_ids,
        track_a_doc_ids=d.track_a_doc_ids,
        track_b_doc_ids=d.track_b_doc_ids,
        source_evidence_count=d.source_evidence_count,
    ) for d in drafts]

    raw_review = ReviewDoc(
        title=outline.title, topic=outline.topic,
        intro=intro, sections=new_sections, conclusion=conclusion,
        references=[],
        doc_count=len(ordered_docs), evidence_count=evidence_total,
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )
    context = EvidenceContext(
        number_to_doc_id={numbering[d]: d for d in ordered_docs},
        track_a_doc_ids=frozenset(
            doc_id for draft in drafts for doc_id in draft.track_a_doc_ids
        ),
        track_b_doc_ids=frozenset(
            doc_id for draft in drafts for doc_id in draft.track_b_doc_ids
        ),
    )
    return audit_and_clean_review(raw_review, context, paper_meta).review


def render_review_note(review: ReviewDoc, paper_meta: dict, settings,
                       template_path: str, model: str) -> str:
    if review.quality_blocking:
        details = "；".join(
            str(issue.get("message", issue)) if isinstance(issue, dict) else str(issue)
            for issue in review.quality_issues
            if not isinstance(issue, dict) or issue.get("severity") == "blocking"
        )
        raise RuntimeError(f"综述质量门禁未通过，已保留章节检查点但停止导出：{details}")
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
