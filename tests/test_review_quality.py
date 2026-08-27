from litreview.models import ReviewDoc, SectionDraft
from litreview.review_quality import EvidenceContext, audit_and_clean_review


def _review(markdown: str, heading: str = "一、释放机理") -> ReviewDoc:
    return ReviewDoc(
        title="测试综述",
        topic="包膜尿素",
        intro="引言背景介绍与研究意义阐述，确保引言非空。",
        conclusion="结论与展望分析，总结全文主要进展与未来方向。",
        sections=[SectionDraft(
            heading=heading,
            markdown=markdown,
            cited_doc_ids=["doc-a", "doc-b"],
        )],
    )


def test_quality_interface_deduplicates_citations_documents_and_cleans_output():
    review = _review(
        "接触角达到 90° [1][1][2]。\n\n"
        "![图](assets/a.jpg)\n*图：图 2 膜层形貌*"
    )
    context = EvidenceContext(number_to_doc_id={1: "doc-a", 2: "doc-b"})
    meta = {
        "doc-a": {
            "title": "膜层研究_张三",
            "authors": "['Zhang San', 'Li Si']",
            "language": "en",
            "doi": "https://doi.org/10.1000/ABC",
            "journal": "Unknown",
        },
        "doc-b": {
            "title": "膜层研究",
            "authors": "Zhang San, Li Si",
            "language": "en",
            "doi": "10.1000/abc",
            "journal": "暂无全名（推测为某期刊）",
        },
    }

    result = audit_and_clean_review(review, context, meta)

    assert result.blocking_issues == []
    assert result.review.sections[0].heading == "释放机理"
    assert "[1][1]" not in result.review.sections[0].markdown
    assert "[2]" not in result.review.sections[0].markdown
    assert "*图 2 膜层形貌*" in result.review.sections[0].markdown
    assert result.review.doc_count == 1
    assert len(result.review.references) == 1
    assert "['" not in result.review.references[0]
    assert "Unknown" not in result.review.references[0]
    assert "推测" not in result.review.references[0]
    assert "[出版年不详]" in result.review.references[0]
    assert any(issue.code == "duplicate_reference" for issue in result.report.issues)


def test_missing_metadata_is_a_warning_not_a_blocker():
    review = _review("正文事实 [1]。", heading="2. 工艺")
    context = EvidenceContext(number_to_doc_id={1: "doc-a"})

    result = audit_and_clean_review(review, context, {"doc-a": {}})

    assert result.blocking_issues == []
    assert result.review.sections[0].heading == "工艺"
    assert "题名不详" in result.review.references[0]
    assert any(issue.code == "metadata_incomplete" for issue in result.report.issues)


def test_unknown_citation_and_empty_section_are_blocking_but_recoverable():
    review = ReviewDoc(
        title="测试综述",
        topic="测试",
        intro="引言非空测试内容",
        conclusion="结论非空测试内容",
        sections=[
            SectionDraft(heading="有效章节", markdown="正文 [99]", cited_doc_ids=[]),
            SectionDraft(heading="空章节", markdown="", cited_doc_ids=[]),
        ],
    )

    result = audit_and_clean_review(review, EvidenceContext(), {})

    codes = {issue.code for issue in result.blocking_issues}
    assert codes == {"unknown_citation", "empty_section"}
    assert len(result.review.sections) == 2


def test_json_dump_is_blocking_instead_of_being_exported_as_a_section():
    review = _review('{"heading": "错误", "body": "模型输出了 JSON"}')

    result = audit_and_clean_review(review, EvidenceContext(), {})

    assert any(issue.code == "raw_json_section" for issue in result.blocking_issues)


def test_render_refuses_blocking_review_without_writing_file(tmp_path):
    from types import SimpleNamespace
    from litreview.stages import render_review_note

    review = ReviewDoc(
        title="坏综述",
        topic="测试",
        sections=[],
        quality_blocking=True,
        quality_issues=[{
            "code": "empty_section",
            "severity": "blocking",
            "message": "章节正文为空",
        }],
    )
    template = tmp_path / "review.md"
    template.write_text("{{ title }}", encoding="utf-8")

    try:
        render_review_note(
            review,
            {},
            SimpleNamespace(REVIEW_OUTPUT_DIR=str(tmp_path)),
            str(template),
            "gpt-5.4",
        )
    except RuntimeError as exc:
        assert "章节正文为空" in str(exc)
    else:
        raise AssertionError("blocking review must not be exported")

    assert not list(tmp_path.glob("综述_*.md"))


def test_track_b_only_quantitative_claim_is_warned_not_blocked():
    review = ReviewDoc(
        title="测试综述",
        topic="测试",
        intro="引言非空测试内容",
        conclusion="结论非空测试内容",
        sections=[SectionDraft(
            heading="数据边界",
            markdown="已有探索报道释放期达到 90 d [1]。",
            cited_doc_ids=["background"],
            track_b_doc_ids=["background"],
        )],
    )
    context = EvidenceContext(
        number_to_doc_id={1: "background"},
        track_b_doc_ids=frozenset({"background"}),
    )
    meta = {"background": {"title": "背景综述", "doc_type": "paper"}}

    result = audit_and_clean_review(review, context, meta)

    assert result.blocking_issues == []
    assert any(issue.code == "track_b_quantitative_claim" for issue in result.report.issues)
    assert result.review.sections[0].track_b_doc_ids == ["background"]


def test_empty_intro_or_conclusion_is_blocking():
    review_no_intro = ReviewDoc(
        title="测试综述",
        topic="测试",
        intro="",
        conclusion="结论非空",
        sections=[SectionDraft(heading="一、章节", markdown="正文 [1]", cited_doc_ids=["d1"])],
    )
    context = EvidenceContext(number_to_doc_id={1: "d1"})
    res1 = audit_and_clean_review(review_no_intro, context, {"d1": {"title": "D1"}})
    assert any(i.code == "empty_intro" and i.severity == "blocking" for i in res1.blocking_issues)

    review_no_concl = ReviewDoc(
        title="测试综述",
        topic="测试",
        intro="引言非空",
        conclusion="",
        sections=[SectionDraft(heading="一、章节", markdown="正文 [1]", cited_doc_ids=["d1"])],
    )
    res2 = audit_and_clean_review(review_no_concl, context, {"d1": {"title": "D1"}})
    assert any(i.code == "empty_conclusion" and i.severity == "blocking" for i in res2.blocking_issues)


def test_raw_json_intro_or_conclusion_is_blocking():
    review_raw_json_intro = ReviewDoc(
        title="测试综述",
        topic="测试",
        intro='{"intro": "未解析JSON"}',
        conclusion="结论非空",
        sections=[SectionDraft(heading="一、章节", markdown="正文 [1]", cited_doc_ids=["d1"])],
    )
    context = EvidenceContext(number_to_doc_id={1: "d1"})
    res1 = audit_and_clean_review(review_raw_json_intro, context, {"d1": {"title": "D1"}})
    assert any(i.code == "raw_json_intro" and i.severity == "blocking" for i in res1.blocking_issues)

    review_raw_json_concl = ReviewDoc(
        title="测试综述",
        topic="测试",
        intro="引言非空",
        conclusion='{"conclusion": "未解析JSON"}',
        sections=[SectionDraft(heading="一、章节", markdown="正文 [1]", cited_doc_ids=["d1"])],
    )
    res2 = audit_and_clean_review(review_raw_json_concl, context, {"d1": {"title": "D1"}})
    assert any(i.code == "raw_json_conclusion" and i.severity == "blocking" for i in res2.blocking_issues)


def test_journal_reference_formatting_with_volume_issue_pages():
    from litreview.review_quality import format_reference

    # 1. 完整卷期页码：Chemical Engineering Journal, 2023, 450(2): 123-134.
    ref1 = format_reference(1, "doc-1", {
        "title": "膜层改性机理",
        "authors": "张三, 李四, 王五",
        "journal": "Chemical Engineering Journal",
        "year": "2023",
        "volume": "450",
        "issue": "2",
        "pages": "123-134",
        "doi": "10.1016/j.cej.2023.141234",
        "doc_type": "paper",
    })
    assert "[1] 张三, 李四, 王五. 膜层改性机理[J]. Chemical Engineering Journal, 2023, 450(2): 123-134. DOI: 10.1016/j.cej.2023.141234." in ref1

    # 2. 仅卷号与页码（无期号）：Nature, 2024, 610: 45-50.
    ref2 = format_reference(2, "doc-2", {
        "title": "磷肥循环经济",
        "authors": "Alice Smith, Bob Jones",
        "journal": "Nature",
        "year": "2024",
        "volume": "610",
        "pages": "45-50",
        "doc_type": "paper",
    })
    assert "[2] Alice Smith, Bob Jones. 磷肥循环经济[J]. Nature, 2024, 610: 45-50." in ref2

    # 3. 仅期号与页码（无卷号）：化肥工业, 2022(4): 18-22.
    ref3 = format_reference(3, "doc-3", {
        "title": "新型肥料评价",
        "authors": "赵六",
        "journal": "化肥工业",
        "year": "2022",
        "issue": "4",
        "pages": "18-22",
        "doc_type": "paper",
    })
    assert "[3] 赵六. 新型肥料评价[J]. 化肥工业, 2022(4): 18-22." in ref3

    # 4. 仅年份（无卷期页码）：降级回退最小著录
    ref4 = format_reference(4, "doc-4", {
        "title": "早期综述",
        "authors": "钱七",
        "journal": "磷肥与复肥",
        "year": "2020",
        "doc_type": "paper",
    })
    assert "[4] 钱七. 早期综述[J]. 磷肥与复肥, 2020." in ref4


def test_citation_cluster_continuous_compression():
    # 连续 >=3 个压缩为起讫范围，如 [1-5], [6-8]；连续 2 个用逗号连接如 [1,2]
    review = _review("连续引用测试段落 [1][2][3][4][5]，三连引用 [6][7][8]，两连引用 [9][10]。")
    context = EvidenceContext(number_to_doc_id={i: f"doc-{i}" for i in range(1, 11)})
    meta = {f"doc-{i}": {"title": f"文献{i}", "doc_type": "paper", "year": "2024"} for i in range(1, 11)}

    result = audit_and_clean_review(review, context, meta)

    assert result.blocking_issues == []
    md = result.review.sections[0].markdown
    assert "[1-5]" in md
    assert "[6-8]" in md
    assert "[9,10]" in md
    assert "[1][2]" not in md
    assert "[6][7]" not in md
    assert len(result.review.references) == 10
    assert len(result.review.reference_doc_ids) == 10


def test_citation_cluster_non_continuous_and_mixed():
    # 先按顺序引出 1..10 确立序号，再测试非连续、逆序重排与混合模式
    prefix = "基础文献 " + " ".join(f"[{i}]" for i in range(1, 11)) + "。\n\n"
    review = _review(prefix + "非连续 [1][3][5]，逆序 [10][7]，混合模式 [1][2][3][5][7][8][9]。")
    context = EvidenceContext(number_to_doc_id={i: f"doc-{i}" for i in range(1, 11)})
    meta = {f"doc-{i}": {"title": f"文献{i}", "doc_type": "paper", "year": "2024"} for i in range(1, 11)}

    result = audit_and_clean_review(review, context, meta)

    assert result.blocking_issues == []
    md = result.review.sections[0].markdown
    assert "[1-10]" in md
    assert "[1,3,5]" in md
    assert "[7,10]" in md
    assert "[1-3,5,7-9]" in md


def test_citation_cluster_existing_range_and_delimiters():
    # 支持输入已有范围、中文逗号、各种连接号（如 [1-3]、[1，3，5–7]）
    prefix = "基础文献 " + " ".join(f"[{i}]" for i in range(1, 8)) + "。\n\n"
    review = _review(prefix + "已有范围 [1-3]，混合列表 [1, 3, 5-7]，中文标点 [1，3，5–7]。")
    context = EvidenceContext(number_to_doc_id={i: f"doc-{i}" for i in range(1, 8)})
    meta = {f"doc-{i}": {"title": f"文献{i}", "doc_type": "paper", "year": "2024"} for i in range(1, 8)}

    result = audit_and_clean_review(review, context, meta)

    assert result.blocking_issues == []
    md = result.review.sections[0].markdown
    assert "[1-7]" in md
    assert "[1-3]" in md
    assert "[1,3,5-7]" in md
    assert "[1，" not in md  # 引用括号内统一输出 ASCII 逗号
    assert "–" not in md  # 引用括号内统一输出 ASCII 连字符


def test_unknown_citation_inside_range_is_blocking():
    # 范围 [1-4] 中缺少文献 3，必须审计出 unknown_citation 且 blocking
    review = _review("范围缺失测试 [1-4]。")
    context = EvidenceContext(number_to_doc_id={1: "doc-1", 2: "doc-2", 4: "doc-4"})  # 缺少 3
    meta = {f"doc-{i}": {"title": f"文献{i}", "doc_type": "paper"} for i in (1, 2, 4)}

    result = audit_and_clean_review(review, context, meta)

    assert result.review.quality_blocking is True
    unknown_issues = [i for i in result.blocking_issues if i.code == "unknown_citation"]
    assert len(unknown_issues) == 1
    assert "引用 [3]" in unknown_issues[0].message


def test_duplicate_docs_merged_before_cluster_compression():
    # 重复文献合并后重新编号并再次压缩：doc-1与doc-2同DOI -> 1；doc-3与doc-4同DOI -> 2；doc-5 -> 3
    # 输入 [1][2][3][4][5] 映射后变为 [1, 1, 2, 2, 3] -> 升序去重压缩为 [1-3]
    review = _review("重复文献合并测试 [1][2][3][4][5]。")
    context = EvidenceContext(number_to_doc_id={1: "d1", 2: "d2", 3: "d3", 4: "d4", 5: "d5"})
    meta = {
        "d1": {"title": "论文A", "doi": "10.1000/a", "doc_type": "paper"},
        "d2": {"title": "论文A", "doi": "10.1000/a", "doc_type": "paper"},
        "d3": {"title": "论文B", "doi": "10.1000/b", "doc_type": "paper"},
        "d4": {"title": "论文B", "doi": "10.1000/b", "doc_type": "paper"},
        "d5": {"title": "论文C", "doi": "10.1000/c", "doc_type": "paper"},
    }

    result = audit_and_clean_review(review, context, meta)

    assert result.blocking_issues == []
    md = result.review.sections[0].markdown
    assert "[1-3]" in md
    assert result.review.doc_count == 3
    assert len(result.review.references) == 3
    assert len(result.review.reference_doc_ids) == 3


def test_citations_separated_by_text_or_punctuation_not_merged():
    # 引用标记之间若存在正文文字、句号、逗号等标点，严禁擅自跨越合并
    review = _review("文献 [1] 指出了机理，而文献 [2] 与文献 [3] 提出了工艺。数据 A 达 90% [4]，数据 B 达 80% [5]。")
    context = EvidenceContext(number_to_doc_id={i: f"doc-{i}" for i in range(1, 6)})
    meta = {f"doc-{i}": {"title": f"文献{i}", "doc_type": "paper"} for i in range(1, 6)}

    result = audit_and_clean_review(review, context, meta)

    assert result.blocking_issues == []
    md = result.review.sections[0].markdown
    assert "文献 [1] 指出了机理" in md
    assert "文献 [2] 与文献 [3] 提出了工艺" in md
    assert "达 90% [4]，数据 B 达 80% [5]" in md
    assert "[1-3]" not in md
    assert "[4,5]" not in md


def test_malformed_and_huge_ranges_defensive_zero_crash():
    # 1. 超大范围 [1-999999999]：安全拦截，不进行巨大展开或消耗内存
    review_huge = _review("超大范围 [1-999999999]。")
    res_huge = audit_and_clean_review(review_huge, EvidenceContext(), {})
    assert res_huge.review.quality_blocking is True
    assert any(i.code == "malformed_citation" and "超大引用范围" in i.message for i in res_huge.blocking_issues)

    # 2. 倒置范围 [5-1]：安全拦截
    review_inv = _review("倒置范围 [5-1]。")
    res_inv = audit_and_clean_review(review_inv, EvidenceContext(), {})
    assert res_inv.review.quality_blocking is True
    assert any(i.code == "malformed_citation" and "倒置" in i.message for i in res_inv.blocking_issues)

    # 3. 非法序号 [0-3]：安全拦截
    review_zero = _review("非法序号 [0-3]。")
    res_zero = audit_and_clean_review(review_zero, EvidenceContext(), {})
    assert res_zero.review.quality_blocking is True
    assert any(i.code == "malformed_citation" and "非法" in i.message for i in res_zero.blocking_issues)
