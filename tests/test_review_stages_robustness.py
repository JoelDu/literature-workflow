from types import SimpleNamespace


def test_outline_validation_enforces_tier_and_question_counts():
    from litreview.models import Outline, OutlineSection
    from litreview.stages import _outline_validation_errors, get_scaling_config

    outline = Outline(
        title="不完整大纲",
        sections=[OutlineSection(heading="唯一章节", questions=["只有一个问题"])],
    )

    errors = _outline_validation_errors(outline, get_scaling_config(6000), n_sections=0)

    assert any("3-4 章" in error for error in errors)
    assert any("2-4 个" in error for error in errors)


def test_broad_track_penalizes_used_docs_and_skips_placeholder_metadata():
    from litreview.models import OutlineSection
    from litreview.stages import gather_broad_bibliography

    section = OutlineSection(heading="章节", questions=["检索问题"])
    search_results = [
        SimpleNamespace(doc_id="used", score=0.90),
        SimpleNamespace(doc_id="fresh", score=0.60),
        SimpleNamespace(doc_id="empty", score=0.99),
    ]
    store = SimpleNamespace(search=lambda *_a, **_k: search_results)
    embedder = SimpleNamespace(embed_query=lambda _query: object())
    metadata = {
        "used": {"title": "旧文献", "tldr": "旧文献摘要"},
        "fresh": {"title": "新文献", "tldr": "新文献摘要"},
        "empty": {"title": "无摘要文献"},
    }

    result = gather_broad_bibliography(
        section, store, embedder, metadata,
        doc_usage_history={"used": 2}, top_n=3, char_budget=100,
    )

    assert [item["doc_id"] for item in result] == ["fresh", "used"]
    assert all(item["tldr"] for item in result)


def test_word_plan_respects_small_total_instead_of_forcing_long_sections():
    from litreview.review_orchestrator import allocate_review_words

    plan = allocate_review_words(5000, 5)

    assert plan.section_words[0] < 1000
    midpoint_total = (
        sum(plan.section_words) / 2 * 5
        + sum(plan.intro_words) / 2
        + sum(plan.conclusion_words) / 2
    )
    assert 4500 <= midpoint_total <= 5500


def test_cross_chapter_penalty_also_works_without_reranker(monkeypatch):
    from litreview.models import OutlineSection
    from litreview import stages

    chunks = [
        SimpleNamespace(chunk_id=1, doc_id="used", score=0.90, content="旧",
                        section_title=""),
        SimpleNamespace(chunk_id=2, doc_id="fresh", score=0.60, content="新",
                        section_title=""),
    ]
    store = SimpleNamespace(search=lambda *_a, **_k: chunks)
    embedder = SimpleNamespace(embed_query=lambda _query: object())
    settings = SimpleNamespace(
        REVIEW_TOP_K=1, REVIEW_EVIDENCE_N=1, REVIEW_MAX_CHUNKS_PER_DOC=2,
        REVIEW_MIN_SCORE=6, REVIEW_MAP_CONCURRENCY=1, RERANK_CANDIDATES=2,
    )
    monkeypatch.setattr(
        stages, "_map_one_chunk",
        lambda _client, _model, _heading, _questions, chunk, _meta:
        {"chunk": chunk, "score": 8, "summary": "有效证据"},
    )

    result = stages.gather_evidence(
        OutlineSection(heading="章节", questions=["问题"]),
        store, embedder, object(), "mini", settings,
        SimpleNamespace(print=lambda *_a: None), {}, reranker=None,
        doc_usage_history={"used": 2}, review_top_k=1, evidence_n=1,
    )

    assert [item.doc_id for item in result] == ["fresh"]
