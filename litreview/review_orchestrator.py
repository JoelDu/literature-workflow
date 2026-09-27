"""共享的逐章节综述编排深模块。

MCP 与 CLI 都通过本模块执行 Stage B/C，保证检索、跨章多样性、预算、
重试和输出校验只有一个实现。入口层只提供依赖和进度/检查点 adapter。
"""
from dataclasses import dataclass, field
import math
import time
from typing import Callable, Optional

from .models import Outline, Evidence, SectionDraft
from .quota import QuotaExceededError

# 单章独立引用上限（只是上限，不是配额；引用数应由论点覆盖自然决定）。
MAX_SECTION_CITATIONS = 18
# 第一轨独立文献默认上限：给第二轨留出余量，使写作输入的独立文献总数不超过引用上限，
# 从源头避免"写完超 18 篇→整章 gpt-5.4 重写"。
DEFAULT_TRACK_A_MAX_DOCS = 12


@dataclass
class ReviewDependencies:
    store: object
    embedder: object
    client: object
    reranker: object
    settings: object
    console: object


@dataclass
class SectionGenerationConfig:
    section_words: tuple = (1800, 2600)
    focus: str = ""
    source_char_budget: int = 18_000
    broad_top_n: int = 25
    review_top_k: Optional[int] = None
    evidence_n: Optional[int] = None
    max_section_attempts: int = 3
    validate_output: bool = True
    initial_drafts: list = field(default_factory=list)
    track_a_max_docs: Optional[int] = None
    max_section_citations: int = MAX_SECTION_CITATIONS
    thesis: str = ""
    research_gap: str = ""
    mainline_logic: str = ""


@dataclass
class ReviewHooks:
    on_progress: Callable[[str], None] = lambda _message: None
    on_checkpoint: Callable[[list], None] = lambda _drafts: None


@dataclass
class SectionGenerationResult:
    drafts: list
    section_evidence: list
    section_broad_bibs: list
    failed_sections: list
    doc_usage_history: dict
    evidence_by_heading: dict = field(default_factory=dict)
    # 额度耗尽时非空：{"model","utc_date","resume_at"(ISO),"message","section"}。
    # 已完成章节均已写入检查点，入口层应把任务标记为 paused_quota 而不是 failed。
    paused_quota: Optional[dict] = None
    budget_estimate: Optional[dict] = None

    @property
    def evidence_count(self) -> int:
        return sum(draft.source_evidence_count for draft in self.drafts)


def quota_pause_info(exc: QuotaExceededError, section: str = "") -> dict:
    """把 QuotaExceededError 转换为可持久化的暂停描述（供 MCP/Web/CLI 共用）。"""
    return {
        "model": getattr(exc, "model", "") or "",
        "utc_date": getattr(exc, "utc_date", None),
        "resume_at": exc.resume_at.isoformat(),
        "message": str(exc),
        "section": section,
    }


@dataclass(frozen=True)
class ReviewWordPlan:
    section_words: tuple
    intro_words: tuple
    conclusion_words: tuple


def allocate_review_words(total_words: int, section_count: int) -> ReviewWordPlan:
    """为 MCP/CLI 提供唯一字数分配实现，并在指定总字数时保持总量可实现。"""
    if total_words <= 0:
        return ReviewWordPlan((1800, 2600), (600, 1000), (800, 1200))
    section_count = max(1, section_count)
    intro_target = max(300, min(1200, int(total_words * 0.12)))
    conclusion_target = max(400, min(1500, int(total_words * 0.15)))
    body_target = max(300, (total_words - intro_target - conclusion_target) // section_count)
    return ReviewWordPlan(
        (max(250, int(body_target * 0.90)), int(body_target * 1.10)),
        (max(250, int(intro_target * 0.85)), int(intro_target * 1.15)),
        (max(300, int(conclusion_target * 0.85)), int(conclusion_target * 1.15)),
    )


class SectionValidationError(RuntimeError):
    pass


class SoftSectionValidationError(SectionValidationError):
    """可修复的质量问题（数值无法溯源、幻觉引用剥离后失去来源）。

    会触发 validation_feedback 修复重写；若最后一次尝试仍只剩软问题，保留该草稿
    （已付费的章节不丢弃），由装配期质量门禁给出 warning/blocking。
    """


def _soft_quality_issues(draft: SectionDraft, evidence: list, broad_bib: list) -> list[str]:
    from . import review_quality as rq
    from .stages import _CITE_RE

    draft.evidence_numbers = rq.evidence_numbers_for(evidence, broad_bib)
    issues = []
    numeric = rq.check_numeric_provenance(
        draft.markdown, {k: set(v) for k, v in draft.evidence_numbers.items()},
        lambda sentence: {m.group(1) for m in _CITE_RE.finditer(sentence)},
    )
    if numeric.unsupported and numeric.unsupported_ratio > rq.numeric_unsupported_max():
        details = "；".join(
            f"「{item['sentence'][:60]}」中的 {', '.join(item['numbers'])}"
            for item in numeric.unsupported[:6]
        )
        issues.append(
            f"{len(numeric.unsupported)}/{numeric.numeric_sentences} 个量化句的数值未出现在所引证据中"
            f"（{details}）；只能使用证据原文中的数值，否则删去具体数值或改引正确文献"
        )
    unsourced = draft.unsourced_sentences or []
    if unsourced:
        cited = rq.count_cited_sentences(draft.markdown, _CITE_RE)
        ratio = len(unsourced) / (cited + len(unsourced))
        if ratio > rq.unsourced_claim_max():
            sample = "；".join(f"「{text[:60]}」" for text in unsourced[:5])
            issues.append(
                f"{len(unsourced)} 句引用了证据清单之外的文献（引用已被删除，句子失去来源：{sample}）；"
                "只能使用清单中给出的引用标记，无证据支撑的论断请删除"
            )
    return issues


def _estimate_section_tokens(config, settings) -> tuple[int, int]:
    """单章 (gpt-5.4, mini) 保守 token 估算，口径与 quota.reserve 一致（输入估算 + max_tokens）。"""
    from . import prompts
    from .token_budget import estimate_tokens

    review_prompt = estimate_tokens(prompts.WRITE_SECTION_PROMPT) + int(config.source_char_budget * 1.15) + 500
    review_tokens = review_prompt + 12_000
    top_k = config.review_top_k or getattr(settings, "REVIEW_TOP_K", 20)
    map_prompt = estimate_tokens(prompts.MAP_PROMPT) + int(2_000 * 1.15)
    mini_tokens = int(top_k) * (map_prompt + 4_000)
    return review_tokens, mini_tokens


def estimate_review_budget(pending_sections: int, config, settings,
                           include_intro_conclusion: bool = True) -> dict:
    """预估剩余章节（及引言/结论）的 gpt-5.4 / mini 用量，并与当日剩余额度对比。

    只做规划提示，从不拒绝运行：不足时说明预计跨几个 UTC 日、将在额度用尽时暂停并于次日续跑。
    证据缓存命中会让 mini 实际用量远低于估算。
    """
    from . import quota

    review_per, mini_per = _estimate_section_tokens(config, settings)
    review_needed = pending_sections * review_per + (15_000 if include_intro_conclusion else 0)
    mini_needed = pending_sections * mini_per
    plan = {"pending_sections": pending_sections}
    for label, model, needed in (("review", quota.REVIEW_MODEL, review_needed),
                                 ("mini", quota.MINI_MODEL, mini_needed)):
        try:
            cap = quota._cap(settings, model)
            left = quota.remaining(settings, model)
        except Exception:
            cap, left = 0, None
        days = 1
        if left is not None and cap > 0 and needed > left:
            days = 1 + math.ceil((needed - left) / cap)
        plan[label] = {"model": model, "estimated_tokens": needed, "remaining_today": left,
                       "daily_cap": cap, "utc_days": days}
    worst = max(plan["review"]["utc_days"], plan["mini"]["utc_days"])
    plan["utc_days"] = worst
    review = plan["review"]
    if worst > 1:
        plan["message"] = (
            f"预算预估：剩余 {pending_sections} 章预计需要 gpt-5.4 约 {review['estimated_tokens']:,} tokens"
            f"（今日剩余 {review['remaining_today']:,}）、mini 约 {plan['mini']['estimated_tokens']:,} tokens；"
            f"预计需要 {worst} 个 UTC 日，将在额度用尽时暂停并于次日自动续跑"
        )
    else:
        plan["message"] = (
            f"预算预估：剩余 {pending_sections} 章预计需要 gpt-5.4 约 {review['estimated_tokens']:,} tokens"
            f"（今日剩余 {review['remaining_today']}），当日额度可覆盖"
        )
    return plan


def _budget_core_evidence(evidence: list, char_budget: int) -> tuple[list, int]:
    kept = []
    used = 0
    for item in evidence:
        size = len(item.summary or "")
        if used + size > char_budget:
            continue
        kept.append(item)
        used += size
    return kept, used


def _validate_draft(draft: SectionDraft, available_doc_ids: set) -> None:
    issues = []
    cited = set(draft.cited_doc_ids)
    if available_doc_ids and not cited:
        issues.append("正文没有保留任何有效引用")
    if len(cited) > MAX_SECTION_CITATIONS:
        issues.append(f"独立引用达到 {len(cited)} 篇，超过单章 {MAX_SECTION_CITATIONS} 篇上限（上限而非配额）")
    if issues:
        raise SectionValidationError("；".join(issues))


def _has_markdown_table(markdown: str) -> bool:
    table_lines = [
        line for line in markdown.splitlines() if line.lstrip().startswith("|")
    ]
    return len(table_lines) >= 3


def _is_retryable_section_error(exc: Exception) -> bool:
    # 网络调用已在 stages._chat_once 内完成三次指数退避；这里只重试可修复的输出校验。
    return isinstance(exc, SectionValidationError)


def generate_review_sections(
    outline: Outline,
    deps: ReviewDependencies,
    config: Optional[SectionGenerationConfig] = None,
    hooks: Optional[ReviewHooks] = None,
) -> SectionGenerationResult:
    """按章节交错执行 Stage B→C，并在每章完成后更新跨章使用历史。"""
    from . import stages
    from .figures import select_section_figures

    config = config or SectionGenerationConfig()
    hooks = hooks or ReviewHooks()
    paper_meta = deps.store.get_paper_meta()
    drafts = list(config.initial_drafts)
    completed_headings = {draft.heading for draft in drafts}
    doc_usage_history = {}
    for draft in drafts:
        for doc_id in draft.cited_doc_ids:
            doc_usage_history[doc_id] = doc_usage_history.get(doc_id, 0) + 1

    section_evidence = []
    section_broad_bibs = []
    failed_sections = []
    used_figures = set()

    evidence_by_heading: dict = {draft.heading: None for draft in drafts}
    paused_quota = None
    track_a_max_docs = config.track_a_max_docs or int(
        getattr(deps.settings, "REVIEW_TRACK_A_MAX_DOCS", 0) or DEFAULT_TRACK_A_MAX_DOCS
    )
    thesis = config.thesis or getattr(outline, "thesis", "") or ""
    research_gap = config.research_gap or getattr(outline, "research_gap", "") or ""
    mainline_logic = config.mainline_logic or getattr(outline, "mainline_logic", "") or ""

    pending = [s for s in outline.sections if s.heading not in completed_headings]
    budget_estimate = None
    if pending:
        try:
            budget_estimate = estimate_review_budget(len(pending), config, deps.settings)
            hooks.on_progress(budget_estimate["message"])
        except Exception as exc:  # 预估只是提示，任何异常都不能阻断生成
            hooks.on_progress(f"⚠️ 预算预估失败（不影响生成）：{exc}")

    for index, section in enumerate(outline.sections, 1):
        if section.heading in completed_headings:
            evidence_by_heading[section.heading] = None
            hooks.on_progress(f"检查点恢复：跳过已完成章节 [{index}] {section.heading}")
            continue
        try:
            outcome = _generate_one_section(
                index, section, outline, deps, config, hooks, paper_meta, drafts,
                doc_usage_history, used_figures, track_a_max_docs,
                thesis, research_gap, mainline_logic, select_section_figures,
            )
        except QuotaExceededError as exc:
            paused_quota = quota_pause_info(exc, section.heading)
            hooks.on_progress(
                f"⏸ {paused_quota['model'] or 'LLM'} UTC 当日额度已用尽，于章节「{section.heading}」前暂停；"
                f"已完成 {len(drafts)} 章均已写入检查点，将于 {paused_quota['resume_at']} (UTC) 后可续跑"
            )
            break
        evidence, broad_bib, draft = outcome
        if evidence is not None:
            section_evidence.append(evidence)
            section_broad_bibs.append(broad_bib)
        if draft is None:
            evidence_by_heading[section.heading] = []
            failed_sections.append(section.heading)
            continue
        evidence_by_heading[section.heading] = list(evidence)
        drafts.append(draft)
        hooks.on_checkpoint(list(drafts))

    return SectionGenerationResult(
        drafts=drafts,
        section_evidence=section_evidence,
        section_broad_bibs=section_broad_bibs,
        failed_sections=failed_sections,
        doc_usage_history=doc_usage_history,
        evidence_by_heading=evidence_by_heading,
        paused_quota=paused_quota,
        budget_estimate=budget_estimate,
    )


def _generate_one_section(index, section, outline, deps, config, hooks, paper_meta, drafts,
                          doc_usage_history, used_figures, track_a_max_docs,
                          thesis, research_gap, mainline_logic, select_section_figures):
    """单章 Stage B→C。返回 (evidence, broad_bib, draft|None)；QuotaExceededError 原样上抛。"""
    from . import stages

    hooks.on_progress(f"阶段 B：收集证据 [{index}/{len(outline.sections)}] {section.heading}")
    evidence = stages.gather_evidence(
        section, deps.store, deps.embedder, deps.client,
        deps.settings.ANALYSIS_MODEL, deps.settings, deps.console, paper_meta,
        reranker=deps.reranker, doc_usage_history=doc_usage_history,
        review_top_k=config.review_top_k, evidence_n=config.evidence_n,
        max_docs=track_a_max_docs,
    )
    evidence, core_chars = _budget_core_evidence(evidence, config.source_char_budget)
    require_comparison_table = stages.comparison_table_required(section, evidence)
    if section.table_suggested and not require_comparison_table:
        hooks.on_progress(
            "  大纲建议表格，但同类可比量化证据少于 3 篇，已降级为文字阐述"
        )

    broad_bib = []
    remaining_chars = max(0, config.source_char_budget - core_chars)
    # 两轨独立文献合计不超过单章引用上限：引用上限几乎不会在写完后才触发。
    track_a_docs = len({item.doc_id for item in evidence})
    broad_top_n = max(0, min(config.broad_top_n, config.max_section_citations - track_a_docs))
    try:
        broad_bib = stages.gather_broad_bibliography(
            section, deps.store, deps.embedder, paper_meta,
            exclude_doc_ids={item.doc_id for item in evidence},
            top_n=broad_top_n,
            doc_usage_history=doc_usage_history,
            reranker=deps.reranker,
            char_budget=remaining_chars,
        )
    except QuotaExceededError:
        raise
    except Exception as exc:
        hooks.on_progress(f"⚠️ 第二轨检索异常，降级为第一轨继续：{exc}")

    hooks.on_progress(
        f"  保留 {len(evidence)} 条第一轨证据、{len(broad_bib)} 篇第二轨文献"
    )
    if not evidence and not broad_bib:
        hooks.on_progress(f"阶段 C：章节 [{index}] 无文献支持，跳过")
        return evidence, broad_bib, None

    hooks.on_progress(f"阶段 C：撰写章节 [{index}/{len(outline.sections)}] {section.heading}")
    draft = None
    validation_feedback = ""
    for attempt in range(config.max_section_attempts):
        candidate = None
        try:
            candidate = stages.write_section(
                section, evidence, paper_meta, outline.title,
                deps.client, deps.settings.REVIEW_MODEL,
                words=config.section_words, focus=config.focus,
                broad_bib=broad_bib, validation_feedback=validation_feedback,
                thesis=thesis, research_gap=research_gap,
                mainline_logic=mainline_logic,
            )
            if config.validate_output:
                available = {item.doc_id for item in evidence}
                available.update(item["doc_id"] for item in broad_bib)
                _validate_draft(candidate, available)
                soft = _soft_quality_issues(candidate, evidence, broad_bib)
                if soft:
                    raise SoftSectionValidationError("；".join(soft))
                if require_comparison_table and not _has_markdown_table(candidate.markdown):
                    hooks.on_progress(
                        "⚠️ 横向比较已激活，但本次正文未生成 Markdown 表格；"
                        "已记录非阻塞警告并保留文字阐述"
                    )
            draft = candidate
            break
        except QuotaExceededError:
            raise
        except Exception as exc:
            validation_feedback = str(exc)
            final_attempt = attempt + 1 >= config.max_section_attempts
            if final_attempt and isinstance(exc, SoftSectionValidationError):
                # 只剩可审计的软问题：保留已付费草稿，交由装配期质量门禁标注。
                hooks.on_progress(f"⚠️ 章节 [{index}] 修复后仍有质量问题，保留草稿待审计：{exc}")
                draft = candidate
                break
            if final_attempt or not _is_retryable_section_error(exc):
                hooks.on_progress(f"⚠️ 章节 [{index}] 撰写失败，已跳过：{exc}")
                break
            hooks.on_progress(f"  章节 [{index}] 第 {attempt + 1} 次输出未通过校验，带反馈重写：{exc}")
            time.sleep(2 ** (attempt + 1))

    if draft is None:
        return evidence, broad_bib, None

    if not draft.evidence_numbers:
        from .review_quality import evidence_numbers_for
        draft.evidence_numbers = evidence_numbers_for(evidence, broad_bib)
    draft.source_evidence_count = len(evidence)
    for doc_id in draft.cited_doc_ids:
        doc_usage_history[doc_id] = doc_usage_history.get(doc_id, 0) + 1
    try:
        n_figures = select_section_figures(
            deps.store, deps.reranker, section, draft, deps.settings,
            deps.console, used_figures, len(drafts) + 2,
        )
        if n_figures:
            hooks.on_progress(f"  插入相关图表 {n_figures} 幅")
    except Exception as exc:
        hooks.on_progress(f"⚠️ 章节插图失败，正文继续：{exc}")
    return evidence, broad_bib, draft
