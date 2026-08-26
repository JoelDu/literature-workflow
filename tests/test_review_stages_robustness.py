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


def test_parse_llm_json_rejects_duplicate_keys():
    import pytest
    from litreview.stages import parse_llm_json

    # 正常合法 JSON
    valid = '{"intro": "引言内容", "conclusion": "结论内容"}'
    assert parse_llm_json(valid) == {"intro": "引言内容", "conclusion": "结论内容"}

    # 重复键（普通格式）
    dup_raw = '{"intro": "引言", "conclusion": "结论1", "conclusion": "结论2"}'
    with pytest.raises(ValueError, match="JSON 包含重复对象键"):
        parse_llm_json(dup_raw)

    # 重复键（包含在 Markdown 代码块内）
    dup_markdown = '```json\n{"intro": "引言", "conclusion": "结论1", "conclusion": "结论2"}\n```'
    with pytest.raises(ValueError, match="JSON 包含重复对象键"):
        parse_llm_json(dup_markdown)


def test_write_intro_conclusion_retries_on_duplicate_keys_and_succeeds(monkeypatch):
    import json
    from litreview import stages
    from litreview.models import Outline, OutlineSection, SectionDraft

    # 模拟真实 V4：第一次返回 6 个重复 conclusion 键；重试后返回合法且足字数的 JSON
    v4_first_resp = (
        '{\n'
        '  "intro": "' + "这是引言部分段落内容。" * 15 + '",\n'
        '  "conclusion": "结论段1",\n'
        '  "conclusion": "结论段2",\n'
        '  "conclusion": "结论段3",\n'
        '  "conclusion": "结论段4",\n'
        '  "conclusion": "结论段5",\n'
        '  "conclusion": "' + "这是最终结论段落内容。" * 20 + '"\n'
        '}'
    )
    v4_retry_resp = json.dumps({
        "intro": "这是修复后的完整引言内容，阐述综述研究背景与意义。" * 15,    # 390 字，在 (300, 500) 的 [270, 600] 内
        "conclusion": "这是修复后的完整结论与展望内容，系统梳理研究缺口与路线。" * 18, # 486 字，在 (400, 700) 的 [360, 840] 内
    })

    calls = []
    def fake_chat(client, model, prompt, json_mode=True, call_type="", stage="", meta=None):
        calls.append(stage)
        if len(calls) == 1:
            return v4_first_resp
        return v4_retry_resp

    monkeypatch.setattr(stages, "_chat", fake_chat)

    outline = Outline(title="测试综述", topic="包膜材料", sections=[OutlineSection(heading="章节1")])
    drafts = [SectionDraft(heading="章节1", markdown="正文事实段落")]

    intro, conclusion = stages.write_intro_conclusion(
        outline, drafts, client=None, model="gpt-5.4",
        intro_words=(300, 500), concl_words=(400, 700)
    )

    assert len(calls) == 2
    assert "stage_C_intro_conclusion_retry" in calls[1] or "retry" in calls[1]
    assert "修复后的完整引言" in intro
    assert "修复后的完整结论" in conclusion


def test_write_intro_conclusion_fails_when_duplicate_keys_persist(monkeypatch):
    import pytest
    from litreview import stages
    from litreview.models import Outline, OutlineSection, SectionDraft

    dup_resp = '{"intro": "' + '引言' * 100 + '", "conclusion": "c1", "conclusion": "' + '结论' * 100 + '"}'
    monkeypatch.setattr(stages, "_chat", lambda *args, **kwargs: dup_resp)

    outline = Outline(title="测试", topic="测试", sections=[OutlineSection(heading="章节1")])
    drafts = [SectionDraft(heading="章节1", markdown="正文")]

    with pytest.raises(ValueError, match="JSON 包含重复对象键"):
        stages.write_intro_conclusion(
            outline, drafts, client=None, model="gpt-5.4",
            intro_words=(300, 500), concl_words=(400, 700)
        )


def test_write_intro_conclusion_rejects_and_retries_short_text(monkeypatch):
    import json
    import pytest
    from litreview import stages
    from litreview.models import Outline, OutlineSection, SectionDraft

    # 模拟真实 V4 191/429 字（当目标为 1020-1380 与 1275-1724 字时，低于 90% 阈值 918/1147）
    short_resp = json.dumps({
        "intro": "短引言" * 63,       # 189 字
        "conclusion": "短结论" * 143,  # 429 字
    })
    long_resp = json.dumps({
        "intro": "标准学术综述引言内容。" * 120,      # 1200 字，在 [918, 1656] 之间
        "conclusion": "标准学术综述结论与展望内容。" * 100, # 1500 字，在 [1147, 2068] 之间
    })

    calls = []
    def fake_chat(client, model, prompt, json_mode=True, call_type="", stage="", meta=None):
        calls.append(stage)
        if len(calls) == 1:
            return short_resp
        return long_resp

    monkeypatch.setattr(stages, "_chat", fake_chat)

    outline = Outline(title="测试", topic="测试", sections=[OutlineSection(heading="章节1")])
    drafts = [SectionDraft(heading="章节1", markdown="正文")]

    # 1. 第一次过短，第二次成功恢复
    intro, conclusion = stages.write_intro_conclusion(
        outline, drafts, client=None, model="gpt-5.4",
        intro_words=(1020, 1380), concl_words=(1275, 1724)
    )
    assert len(calls) == 2
    assert "stage_C_intro_conclusion_validation_retry" in calls[1]
    assert 918 <= len(intro) <= 1656
    assert 1147 <= len(conclusion) <= 2068

    # 2. 如果重试后依然过短，坚决抛出异常阻断导出
    monkeypatch.setattr(stages, "_chat", lambda *args, **kwargs: short_resp)
    with pytest.raises(ValueError, match="引言/结论结构或字数校验失败"):
        stages.write_intro_conclusion(
            outline, drafts, client=None, model="gpt-5.4",
            intro_words=(1020, 1380), concl_words=(1275, 1724)
        )


def test_intro_conclusion_validation_errors_schema_and_bidirectional_bounds():
    from litreview.stages import _intro_conclusion_validation_errors

    intro_words = (1020, 1380)  # min=1020 -> lower bound = 918; max=1380 -> upper bound = 1656
    concl_words = (1275, 1724)  # min=1275 -> lower bound = 1147; max=1724 -> upper bound = 2068

    # 1. 非字典类型
    assert any("JSON 对象" in e for e in _intro_conclusion_validation_errors([], intro_words, concl_words))

    # 2. 未知顶层键（严格限定仅允许 intro 与 conclusion）
    data_unknown = {
        "intro": "A" * 1000,
        "conclusion": "B" * 1300,
        "extra_summary": "未知字段内容",
    }
    assert any("包含未知顶层字段" in e for e in _intro_conclusion_validation_errors(data_unknown, intro_words, concl_words))

    # 3. 缺少必填字段
    assert any("缺少必填顶层字段" in e for e in _intro_conclusion_validation_errors({"intro": "A" * 1000}, intro_words, concl_words))

    # 4. 字段值非字符串或空白
    assert any("非空的 'intro'" in e for e in _intro_conclusion_validation_errors({"intro": "", "conclusion": "B" * 1300}, intro_words, concl_words))
    assert any("非空的 'conclusion'" in e for e in _intro_conclusion_validation_errors({"intro": "A" * 1000, "conclusion": "   "}, intro_words, concl_words))

    # 5. 双向边界值校验：刚好在 90% 下限与 120% 上限边界
    # 下限恰好满足 (918, 1147)
    valid_lower = {"intro": "A" * 918, "conclusion": "B" * 1147}
    assert _intro_conclusion_validation_errors(valid_lower, intro_words, concl_words) == []

    # 下限差 1 字 (917, 1146)
    invalid_lower = {"intro": "A" * 917, "conclusion": "B" * 1146}
    errs_lower = _intro_conclusion_validation_errors(invalid_lower, intro_words, concl_words)
    assert any("引言字数不足" in e for e in errs_lower)
    assert any("结论字数不足" in e for e in errs_lower)

    # 上限恰好满足 (1656, 2068)
    valid_upper = {"intro": "A" * 1656, "conclusion": "B" * 2068}
    assert _intro_conclusion_validation_errors(valid_upper, intro_words, concl_words) == []

    # 上限超 1 字 (1657, 2069)
    invalid_upper = {"intro": "A" * 1657, "conclusion": "B" * 2069}
    errs_upper = _intro_conclusion_validation_errors(invalid_upper, intro_words, concl_words)
    assert any("引言字数超标" in e for e in errs_upper)
    assert any("结论字数超标" in e for e in errs_upper)

    # 6. V3 真实字数回放（1272 / 1931）-> 必须通过
    v3_data = {"intro": "A" * 1272, "conclusion": "B" * 1931}
    assert _intro_conclusion_validation_errors(v3_data, intro_words, concl_words) == []

    # 7. V4 真实字数回放（191 / 429）-> 必须明确失败
    v4_data = {"intro": "A" * 191, "conclusion": "B" * 429}
    errs_v4 = _intro_conclusion_validation_errors(v4_data, intro_words, concl_words)
    assert any("引言字数不足" in e for e in errs_v4)
    assert any("结论字数不足" in e for e in errs_v4)


def test_legacy_outline_table_suggested_inference_and_explicit_override():
    from litreview.models import Outline, OutlineSection

    # 1. 直接构造 OutlineSection：默认保持 False，不自动推断
    direct_sec = OutlineSection(heading="二、不同包膜材料及其性能横向对比")
    assert direct_sec.table_suggested is False

    direct_sec_true = OutlineSection(heading="二、不同包膜材料及其性能横向对比", table_suggested=True)
    assert direct_sec_true.table_suggested is True

    # 2. 旧大纲反序列化（Outline.from_dict）：缺失 table_suggested 时自动推断
    legacy = Outline.from_dict({
        "title": "旧大纲",
        "sections": [
            {"heading": "一、研究背景与产业现状", "questions": ["背景问题"]},
            {"heading": "二、不同包膜材料及其性能横向对比", "questions": ["材料问题"]},
            {"heading": "三、包膜缓释肥料制备工艺对比", "questions": ["工艺问题"]},
            {"heading": "四、养分释放动力学机理与数学模型", "questions": ["机理问题"]},
            {"heading": "五、面临的挑战与未来展望", "questions": ["展望问题"]},
        ],
    })
    assert legacy.sections[0].table_suggested is False
    assert legacy.sections[1].table_suggested is True
    assert legacy.sections[2].table_suggested is True
    assert legacy.sections[3].table_suggested is False
    assert legacy.sections[4].table_suggested is False

    # 3. 显式 table_suggested=False 在反序列化中必须严格尊重，不能被推断覆盖
    explicit_false_dict = Outline.from_dict({
        "title": "显式关闭",
        "sections": [{"heading": "二、不同包膜材料横向对比", "table_suggested": False}],
    })
    assert explicit_false_dict.sections[0].table_suggested is False

    # 4. 显式 table_suggested=True 必须严格尊重
    explicit_true_dict = Outline.from_dict({
        "title": "显式开启",
        "sections": [{"heading": "四、养分释放机理", "table_suggested": True}],
    })
    assert explicit_true_dict.sections[0].table_suggested is True


def test_load_outline_file_json_and_markdown_legacy_and_explicit(tmp_path):
    import json
    from litreview.stages import load_outline_file

    # 1. 加载 JSON 旧大纲（无 table_suggested）与显式 false
    json_path = tmp_path / "legacy_outline.json"
    json_path.write_text(json.dumps({
        "title": "JSON旧大纲",
        "sections": [
            {"heading": "一、研究背景", "questions": ["Q1"]},
            {"heading": "二、不同包膜材料横向对比", "questions": ["Q2"]},
            {"heading": "三、制备工艺对比", "questions": ["Q3"], "table_suggested": False},
            {"heading": "四、释放机理", "questions": ["Q4"]},
        ]
    }, ensure_ascii=False), encoding="utf-8")

    loaded_json = load_outline_file(str(json_path), topic="包膜材料")
    assert loaded_json.sections[0].table_suggested is False
    assert loaded_json.sections[1].table_suggested is True
    assert loaded_json.sections[2].table_suggested is False  # 显式 false 保护
    assert loaded_json.sections[3].table_suggested is False

    # 2. 加载 Markdown 旧大纲（无 table_suggested 字段）
    md_path = tmp_path / "legacy_outline.md"
    md_path.write_text(
        "# Markdown旧大纲\n\n"
        "## 一、研究背景与现状\n"
        "- 背景问题1\n"
        "- 背景问题2\n\n"
        "## 二、各类催化剂性能横向对比\n"
        "- 催化剂问题1\n"
        "- 催化剂问题2\n\n"
        "## 三、反应动力学机理\n"
        "- 机理问题1\n"
        "- 机理问题2\n",
        encoding="utf-8"
    )

    loaded_md = load_outline_file(str(md_path), topic="催化剂")
    assert len(loaded_md.sections) == 3
    assert loaded_md.sections[0].table_suggested is False
    assert loaded_md.sections[1].table_suggested is True
    assert loaded_md.sections[2].table_suggested is False


def test_comparison_table_enforces_at_least_three_sources_regardless_of_inference():
    """验证即使大纲建议表格（显式或推断），仍须满足同类可比量化证据至少3篇才要求表格。"""
    from litreview.models import Evidence, OutlineSection
    from litreview.stages import comparison_table_required

    sec_inferred_true = OutlineSection(heading="二、不同材料横向对比", questions=[], table_suggested=True)
    assert sec_inferred_true.table_suggested is True

    # 仅 2 篇可比证据 -> 绝不要求表格
    evidence_two = [
        Evidence(chunk_id=1, doc_id="d1", score=8, summary="释放周期为 30 d"),
        Evidence(chunk_id=2, doc_id="d2", score=8, summary="释放周期为 45 d"),
    ]
    assert not comparison_table_required(sec_inferred_true, evidence_two)

    # 达到 3 篇同类量化指标证据 -> 要求表格
    evidence_three = [
        Evidence(chunk_id=1, doc_id="d1", score=8, summary="释放周期为 30 d"),
        Evidence(chunk_id=2, doc_id="d2", score=8, summary="释放周期为 45 d"),
        Evidence(chunk_id=3, doc_id="d3", score=8, summary="释放周期为 60 d"),
    ]
    assert comparison_table_required(sec_inferred_true, evidence_three)

    # 显式 table_suggested=False 时，即使有 3 篇量化证据也不要求表格
    sec_explicit_false = OutlineSection(heading="二、不同材料横向对比", questions=[], table_suggested=False)
    assert not comparison_table_required(sec_explicit_false, evidence_three)
