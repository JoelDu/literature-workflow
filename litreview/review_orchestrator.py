"""共享的逐章节综述编排深模块。

MCP 与 CLI 都通过本模块执行 Stage B/C，保证检索、跨章多样性、预算、
重试和输出校验只有一个实现。入口层只提供依赖和进度/检查点 adapter。
"""
from dataclasses import dataclass, field
import time
from typing import Callable, Optional

from .models import Outline, Evidence, SectionDraft


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

    @property
    def evidence_count(self) -> int:
        return sum(draft.source_evidence_count for draft in self.drafts)


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
    if len(cited) > 18:
        issues.append(f"独立引用达到 {len(cited)} 篇，超过单章 18 篇上限")
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

    for index, section in enumerate(outline.sections, 1):
        if section.heading in completed_headings:
            hooks.on_progress(f"检查点恢复：跳过已完成章节 [{index}] {section.heading}")
            continue

        hooks.on_progress(f"阶段 B：收集证据 [{index}/{len(outline.sections)}] {section.heading}")
        evidence = stages.gather_evidence(
            section, deps.store, deps.embedder, deps.client,
            deps.settings.ANALYSIS_MODEL, deps.settings, deps.console, paper_meta,
            reranker=deps.reranker, doc_usage_history=doc_usage_history,
            review_top_k=config.review_top_k, evidence_n=config.evidence_n,
        )
        evidence, core_chars = _budget_core_evidence(evidence, config.source_char_budget)
        require_comparison_table = stages.comparison_table_required(section, evidence)
        if section.table_suggested and not require_comparison_table:
            hooks.on_progress(
                "  大纲建议表格，但同类可比量化证据少于 3 篇，已降级为文字阐述"
            )

        broad_bib = []
        remaining_chars = max(0, config.source_char_budget - core_chars)
        try:
            broad_bib = stages.gather_broad_bibliography(
                section, deps.store, deps.embedder, paper_meta,
                exclude_doc_ids={item.doc_id for item in evidence},
                top_n=config.broad_top_n,
                doc_usage_history=doc_usage_history,
                reranker=deps.reranker,
                char_budget=remaining_chars,
            )
        except Exception as exc:
            hooks.on_progress(f"⚠️ 第二轨检索异常，降级为第一轨继续：{exc}")

        section_evidence.append(evidence)
        section_broad_bibs.append(broad_bib)
        hooks.on_progress(
            f"  保留 {len(evidence)} 条第一轨证据、{len(broad_bib)} 篇第二轨文献"
        )
        if not evidence and not broad_bib:
            failed_sections.append(section.heading)
            hooks.on_progress(f"阶段 C：章节 [{index}] 无文献支持，跳过")
            continue

        hooks.on_progress(f"阶段 C：撰写章节 [{index}/{len(outline.sections)}] {section.heading}")
        draft = None
        validation_feedback = ""
        for attempt in range(config.max_section_attempts):
            try:
                draft = stages.write_section(
                    section, evidence, paper_meta, outline.title,
                    deps.client, deps.settings.REVIEW_MODEL,
                    words=config.section_words, focus=config.focus,
                    broad_bib=broad_bib, validation_feedback=validation_feedback,
                )
                if config.validate_output:
                    available = {item.doc_id for item in evidence}
                    available.update(item["doc_id"] for item in broad_bib)
                    _validate_draft(draft, available)
                    if require_comparison_table and not _has_markdown_table(draft.markdown):
                        hooks.on_progress(
                            "⚠️ 横向比较已激活，但本次正文未生成 Markdown 表格；"
                            "已记录非阻塞警告并保留文字阐述"
                        )
                break
            except Exception as exc:
                draft = None
                validation_feedback = str(exc)
                final_attempt = attempt + 1 >= config.max_section_attempts
                if final_attempt or not _is_retryable_section_error(exc):
                    hooks.on_progress(f"⚠️ 章节 [{index}] 撰写失败，已跳过：{exc}")
                    break
                time.sleep(2 ** (attempt + 1))

        if draft is None:
            failed_sections.append(section.heading)
            continue

        draft.source_evidence_count = len(evidence)
        for doc_id in draft.cited_doc_ids:
            doc_usage_history[doc_id] = doc_usage_history.get(doc_id, 0) + 1
        try:
            n_figures = select_section_figures(
                deps.store, deps.reranker, section, draft, deps.settings,
                deps.console, used_figures,
            )
            if n_figures:
                hooks.on_progress(f"  插入相关图表 {n_figures} 幅")
        except Exception as exc:
            hooks.on_progress(f"⚠️ 章节插图失败，正文继续：{exc}")
        drafts.append(draft)
        hooks.on_checkpoint(list(drafts))

    return SectionGenerationResult(
        drafts=drafts,
        section_evidence=section_evidence,
        section_broad_bibs=section_broad_bibs,
        failed_sections=failed_sections,
        doc_usage_history=doc_usage_history,
    )
