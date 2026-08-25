from litreview.models import ReviewDoc, SectionDraft
from litreview.review_quality import EvidenceContext, audit_and_clean_review


def _review(markdown: str, heading: str = "一、释放机理") -> ReviewDoc:
    return ReviewDoc(
        title="测试综述",
        topic="包膜尿素",
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
