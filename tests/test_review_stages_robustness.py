from types import SimpleNamespace


def test_map_chunk_uses_versioned_cache_without_calling_model(monkeypatch):
    from litreview import stages

    chunk = SimpleNamespace(chunk_id=1, doc_id="doc", content="evidence", section_title="")
    store = SimpleNamespace(get_evidence_cache=lambda *_a: {"score": 9, "summary": "cached"})
    monkeypatch.setattr(stages, "_chat_json", lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("model must not run on a cache hit")))

    result = stages._map_one_chunk(None, "model", "heading", "question", chunk,
                                   {"doc": {}}, store)

    assert result["cached"] is True
    assert result["summary"] == "cached"


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


def test_outline_table_plan_is_lightweight_and_backward_compatible():
    from litreview import prompts
    from litreview.models import Outline

    old_outline = Outline.from_dict({
        "title": "旧检查点",
        "sections": [{"heading": "机理", "questions": ["问题一", "问题二"]}],
    })
    planned = Outline.from_dict({
        "title": "新大纲",
        "sections": [{
            "heading": "横向比较", "questions": ["问题一", "问题二"],
            "table_suggested": True,
        }],
    })

    assert old_outline.sections[0].table_suggested is False
    assert planned.sections[0].table_suggested is True
    assert "table_suggested" in prompts.OUTLINE_PROMPT
    assert "required_metrics" not in prompts.OUTLINE_PROMPT


def test_comparison_table_needs_outline_plan_and_three_comparable_sources():
    from litreview.models import Evidence, OutlineSection
    from litreview.stages import comparison_table_required

    evidence = [
        Evidence(chunk_id=index, doc_id=f"doc-{index}", score=8,
                 summary=f"在对照条件下释放周期为 {40 + index * 10} d")
        for index in range(1, 4)
    ]

    assert comparison_table_required(
        OutlineSection(heading="对比", questions=[], table_suggested=True), evidence
    )
    assert not comparison_table_required(
        OutlineSection(heading="机理", questions=[], table_suggested=False), evidence
    )
    assert not comparison_table_required(
        OutlineSection(heading="对比", questions=[], table_suggested=True), evidence[:2]
    )


def test_adaptive_weights_are_contextual_and_history_penalty_is_clamped():
    from litreview.models import OutlineSection
    from litreview.stages import _doc_type_weight, _history_factor

    standards = OutlineSection(heading="检测标准与评价规范", questions=[])
    mechanism = OutlineSection(heading="释放机理", questions=[])

    assert _doc_type_weight(standards, {"doc_type": "standard"}) == 1.10
    assert _doc_type_weight(mechanism, {"doc_type": "patent"}) == 0.85
    assert _history_factor("used", {"used": 99}) == 0.35


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
        lambda _client, _model, _heading, _questions, chunk, _meta, _store:
        {"chunk": chunk, "score": 8, "summary": "有效证据"},
    )

    result = stages.gather_evidence(
        OutlineSection(heading="章节", questions=["问题"]),
        store, embedder, object(), "mini", settings,
        SimpleNamespace(print=lambda *_a: None), {}, reranker=None,
        doc_usage_history={"used": 2}, review_top_k=1, evidence_n=1,
    )

    assert [item.doc_id for item in result] == ["fresh"]
