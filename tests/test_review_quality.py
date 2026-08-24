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
