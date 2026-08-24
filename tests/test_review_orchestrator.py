from types import SimpleNamespace


def test_sections_are_retrieved_and_written_sequentially(monkeypatch):
    """第二章检索必须看到第一章正文实际使用过的文献。"""
    from litreview.models import Evidence, Outline, OutlineSection, SectionDraft
    from litreview import review_orchestrator as orchestrator
    import litreview.stages as stages
    import litreview.figures as figures

    outline = Outline(
        title="测试综述",
        topic="测试",
        sections=[
            OutlineSection(heading="第一章", questions=["问题一"]),
            OutlineSection(heading="第二章", questions=["问题二"]),
        ],
    )
    histories = []

    def fake_gather(section, *_args, doc_usage_history=None, **_kwargs):
        histories.append(dict(doc_usage_history or {}))
        n = len(histories)
        return [Evidence(chunk_id=n, doc_id=f"doc{n}", score=8,
                         summary=f"证据{n}", retrieval_score=0.9)]

    monkeypatch.setattr(stages, "gather_evidence", fake_gather)
    monkeypatch.setattr(stages, "gather_broad_bibliography", lambda *_a, **_k: [])
    monkeypatch.setattr(
        stages,
        "write_section",
        lambda sec, evs, *_a, **_k: SectionDraft(
            heading=sec.heading,
            markdown=f"正文 [@{evs[0].doc_id}]",
            cited_doc_ids=[evs[0].doc_id],
        ),
    )
    monkeypatch.setattr(figures, "select_section_figures", lambda *_a, **_k: 0)

    deps = orchestrator.ReviewDependencies(
        store=SimpleNamespace(get_paper_meta=lambda: {}),
        embedder=object(), client=object(), reranker=None,
        settings=SimpleNamespace(ANALYSIS_MODEL="mini", REVIEW_MODEL="review"),
        console=SimpleNamespace(print=lambda *_a: None),
    )
    result = orchestrator.generate_review_sections(
        outline,
        deps,
        orchestrator.SectionGenerationConfig(section_words=(100, 200), validate_output=False),
    )

    assert len(result.drafts) == 2
    assert result.evidence_count == 2
    assert histories == [{}, {"doc1": 1}]


def test_track_b_failure_degrades_to_track_a(monkeypatch):
    """外围检索失败不能让已有核心证据的章节和整篇综述一起失败。"""
    from litreview.models import Evidence, Outline, OutlineSection, SectionDraft
    from litreview import review_orchestrator as orchestrator
    import litreview.stages as stages
    import litreview.figures as figures

    outline = Outline(title="测试", sections=[OutlineSection(heading="章节", questions=["问题"])])
    evidence = Evidence(chunk_id=1, doc_id="doc1", score=8, summary="核心证据", retrieval_score=0.9)
    monkeypatch.setattr(stages, "gather_evidence", lambda *_a, **_k: [evidence])
    monkeypatch.setattr(stages, "gather_broad_bibliography",
                        lambda *_a, **_k: (_ for _ in ()).throw(ConnectionError("offline")))
    monkeypatch.setattr(stages, "write_section",
                        lambda sec, *_a, **_k: SectionDraft(
                            heading=sec.heading, markdown="正文", cited_doc_ids=["doc1"]))
    monkeypatch.setattr(figures, "select_section_figures", lambda *_a, **_k: 0)

    logs = []
    deps = orchestrator.ReviewDependencies(
        store=SimpleNamespace(get_paper_meta=lambda: {}),
        embedder=object(), client=object(), reranker=None,
        settings=SimpleNamespace(ANALYSIS_MODEL="mini", REVIEW_MODEL="review"),
        console=SimpleNamespace(print=lambda *_a: None),
    )
    result = orchestrator.generate_review_sections(
        outline,
        deps,
        orchestrator.SectionGenerationConfig(section_words=(100, 200), validate_output=False),
        orchestrator.ReviewHooks(on_progress=logs.append),
    )

    assert len(result.drafts) == 1
    assert any("第二轨" in line and "降级" in line for line in logs)


def test_section_validation_does_not_force_a_minimum_citation_quota():
    from litreview.models import SectionDraft
    from litreview.review_orchestrator import _validate_draft

    cited = [f"doc-{index}" for index in range(6)]
    draft = SectionDraft(
        heading="证据有限但论点完整",
        markdown=(
            "正文有六篇直接证据。\n\n"
            "| 体系 | 条件 | 结果 |\n"
            "|---|---|---|\n"
            "| A | 25 ℃ | 60 d |"
        ),
        cited_doc_ids=cited,
    )

    _validate_draft(draft, {f"doc-{index}" for index in range(20)})


def test_write_prompt_uses_evidence_roles_and_sober_style():
    from litreview import prompts

    assert "数据、接触角、释放率" in prompts.WRITE_SECTION_PROMPT
    assert "第二轨" in prompts.WRITE_SECTION_PROMPT
    assert "气势磅礴" not in prompts.WRITE_SECTION_PROMPT
    assert "卡脖子" not in prompts.WRITE_SECTION_PROMPT
    assert "18~28" not in prompts.WRITE_SECTION_PROMPT
