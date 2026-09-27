"""综述生成加固回归测试：额度暂停、数值溯源、幻觉引用剥离、DOI 校验、图表编号、立论贯穿。

全部使用假客户端与临时数据库，无网络、无真实 LLM 调用。
"""
import os
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from litreview.models import (Evidence, Outline, OutlineSection, ReviewDoc,
                              SectionDraft)


# ── quota ──────────────────────────────────────────────────────────────────

def _quota_settings(tmp_path, review_cap=200_000, mini_cap=2_000_000):
    return SimpleNamespace(DB_PATH=str(tmp_path / "quota.db"),
                           REVIEW_DAILY_TOKEN_CAP=review_cap,
                           LLM_DAILY_TOKEN_CAP=mini_cap)


def test_next_utc_reset_and_error_resume_at():
    from litreview.quota import QuotaExceededError, next_utc_reset
    assert next_utc_reset("2026-09-27") == datetime(2026, 9, 28, tzinfo=timezone.utc)
    err = QuotaExceededError("x", utc_date="2026-12-31", model="gpt-5.4")
    assert err.resume_at == datetime(2027, 1, 1, tzinfo=timezone.utc)
    assert err.model == "gpt-5.4"


def test_release_returns_reservation_and_remaining(tmp_path):
    from litreview import quota
    settings = _quota_settings(tmp_path)
    assert quota.remaining(settings, quota.REVIEW_MODEL) == 200_000
    reservation = quota.reserve(settings, quota.REVIEW_MODEL, "提示词", 12_000)
    assert quota.remaining(settings, quota.REVIEW_MODEL) == 200_000 - reservation[1]
    quota.release(settings, quota.REVIEW_MODEL, reservation)
    assert quota.remaining(settings, quota.REVIEW_MODEL) == 200_000


def test_reserve_over_cap_raises_quota_error(tmp_path):
    from litreview import quota
    settings = _quota_settings(tmp_path, review_cap=1_000)
    with pytest.raises(quota.QuotaExceededError) as info:
        quota.reserve(settings, quota.REVIEW_MODEL, "p", 12_000)
    assert info.value.model == quota.REVIEW_MODEL
    assert info.value.utc_date


class _FakeCompletions:
    def __init__(self, error=None, content="ok"):
        self.calls = 0
        self.error = error
        self.content = content

    def create(self, **_kwargs):
        self.calls += 1
        if self.error is not None:
            raise self.error
        usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15,
                                completion_tokens_details=None, prompt_tokens_details=None)
        return SimpleNamespace(usage=usage, choices=[SimpleNamespace(
            message=SimpleNamespace(content=self.content))])


def _fake_client(completions):
    return SimpleNamespace(chat=SimpleNamespace(completions=completions))


def _patch_settings(monkeypatch, settings):
    import utils
    monkeypatch.setattr(utils, "get_settings", lambda: settings)


def test_chat_once_does_not_retry_quota_error(tmp_path, monkeypatch):
    from litreview import quota, stages
    settings = _quota_settings(tmp_path, review_cap=100)
    _patch_settings(monkeypatch, settings)
    completions = _FakeCompletions()
    with pytest.raises(quota.QuotaExceededError):
        stages._chat_once(_fake_client(completions), quota.REVIEW_MODEL, "p", False)
    assert completions.calls == 0


def test_chat_once_releases_reservation_on_connection_error(tmp_path, monkeypatch):
    from litreview import quota, stages
    settings = _quota_settings(tmp_path)
    _patch_settings(monkeypatch, settings)
    monkeypatch.setattr(stages._chat_once.retry, "sleep", lambda _s: None)
    completions = _FakeCompletions(error=ConnectionError("refused"))
    with pytest.raises(ConnectionError):
        stages._chat_once(_fake_client(completions), quota.REVIEW_MODEL, "p", False)
    assert completions.calls == 3  # 瞬时错误重试
    assert quota.remaining(settings, quota.REVIEW_MODEL) == 200_000  # 预留全部归还


def test_chat_once_does_not_retry_non_transient_error(tmp_path, monkeypatch):
    from litreview import quota, stages
    settings = _quota_settings(tmp_path)
    _patch_settings(monkeypatch, settings)
    completions = _FakeCompletions(error=ValueError("bad"))
    with pytest.raises(ValueError):
        stages._chat_once(_fake_client(completions), quota.REVIEW_MODEL, "p", False)
    assert completions.calls == 1


def test_chat_once_success_reconciles_usage(tmp_path, monkeypatch):
    from litreview import quota, stages
    settings = _quota_settings(tmp_path)
    _patch_settings(monkeypatch, settings)
    assert stages._chat_once(_fake_client(_FakeCompletions()), quota.REVIEW_MODEL, "p", False) == "ok"
    assert quota.remaining(settings, quota.REVIEW_MODEL) == 200_000 - 15


# ── 数值溯源 ────────────────────────────────────────────────────────────────

def test_numeric_normalization_matches_comma_and_fullwidth():
    from litreview.review_quality import (_number_supported, extract_claim_numbers,
                                          extract_evidence_numbers)
    evidence = extract_evidence_numbers("释放率为 12,5 %，温度 ２５ ℃，产量 1,234 t")
    claims = extract_claim_numbers("释放率达到 12.5%，温度为25 ℃ [@doc1]。")
    assert claims == ["12.5", "25"]
    assert all(_number_supported(token, evidence) for token in claims)
    assert _number_supported("1234", evidence)
    assert not _number_supported("13.5", evidence)


def test_claim_numbers_ignore_citations_years_and_figure_labels():
    from litreview.review_quality import extract_claim_numbers
    assert extract_claim_numbers("见图 3-1，2019 年的研究 [12]。") == []
    assert extract_claim_numbers("浓度 5–10 mg/L") == ["5", "10"]


def test_check_numeric_provenance_skips_unknown_docs():
    from litreview.review_quality import check_numeric_provenance
    md = "释放率为 80% [@a]。强度 3.2 MPa [@b]。未知来源 9.9% [@z]。"
    result = check_numeric_provenance(md, {"a": {"80"}, "b": {"4.1"}},
                                      lambda s: set(__import__("re").findall(r"\[@(\w+)\]", s)))
    assert result.numeric_sentences == 2
    assert [item["numbers"] for item in result.unsupported] == [["3.2"]]
    assert result.unsupported_ratio == 0.5


def _single_section_review(markdown, **draft_fields):
    return ReviewDoc(
        title="测试综述", topic="测试",
        intro="引言背景介绍与研究意义阐述，确保引言非空。",
        conclusion="结论与展望分析，总结全文主要进展与未来方向。",
        sections=[SectionDraft(heading="释放机理", markdown=markdown,
                               cited_doc_ids=["doc-a"], **draft_fields)],
    )


def _codes(result, severity=None):
    return {issue.code for issue in result.report.issues
            if severity is None or issue.severity == severity}


def test_numeric_unsupported_blocks_above_threshold(monkeypatch):
    from litreview.review_quality import EvidenceContext, audit_and_clean_review
    monkeypatch.delenv("REVIEW_NUMERIC_UNSUPPORTED_MAX", raising=False)
    review = _single_section_review("释放率达到 95% [1]。")
    ctx = EvidenceContext(number_to_doc_id={1: "doc-a"})
    evidence = {"释放机理": [Evidence(chunk_id=1, doc_id="doc-a", score=8,
                                        summary="释放率为 60%", chunk_text="30 d 内释放 60%")]}
    result = audit_and_clean_review(review, ctx, {"doc-a": {"title": "T", "year": 2020}},
                                    evidence_by_heading=evidence)
    assert {"numeric_unsupported", "numeric_unsupported_ratio"} <= _codes(result, "blocking")


def test_numeric_unsupported_is_warning_when_threshold_relaxed(monkeypatch):
    from litreview.review_quality import EvidenceContext, audit_and_clean_review
    monkeypatch.setenv("REVIEW_NUMERIC_UNSUPPORTED_MAX", "0.6")
    md = "释放率达到 95% [1]。累计释放 60% [1]。"
    review = _single_section_review(md, evidence_numbers={"doc-a": ["30", "60"]})
    result = audit_and_clean_review(review, EvidenceContext(number_to_doc_id={1: "doc-a"}),
                                    {"doc-a": {"title": "T", "year": 2020}})
    assert "numeric_unsupported" in _codes(result, "warning")
    assert "numeric_unsupported" not in _codes(result, "blocking")


def test_numeric_supported_from_checkpointed_numbers_has_no_issue():
    from litreview.review_quality import EvidenceContext, audit_and_clean_review
    review = _single_section_review("累计释放 60% [1]。",
                                    evidence_numbers={"doc-a": ["60"]})
    result = audit_and_clean_review(review, EvidenceContext(number_to_doc_id={1: "doc-a"}),
                                    {"doc-a": {"title": "T", "year": 2020}})
    assert "numeric_unsupported" not in _codes(result)


# ── 幻觉引用剥离 ────────────────────────────────────────────────────────────

def test_unsourced_after_strip_only_reports_fully_unsourced_sentences():
    from litreview.stages import _unsourced_after_strip
    text = "甲结论成立 [@fake1]。乙结论成立 [@good] [@fake2]。丙没有引用。"
    assert _unsourced_after_strip(text, {"good"}) == ["甲结论成立 。"]


def test_write_section_records_unsourced_and_alignment(monkeypatch):
    from litreview import stages
    prompts = []

    def fake_chat(_client, _model, prompt, **_kwargs):
        prompts.append(prompt)
        return "真实结论 [@doc1abcd]。编造结论 [@ghost999]。"

    monkeypatch.setattr(stages, "_chat", fake_chat)
    ev = Evidence(chunk_id=1, doc_id="doc1abcdef", score=8, summary="证据")
    draft = stages.write_section(
        OutlineSection(heading="章"), [ev], {"doc1abcdef": {"title": "T"}}, "综述",
        client=None, model="gpt-5.4", thesis="立论X", research_gap="缺口Y", mainline_logic="主线Z")
    assert draft.cited_doc_ids == ["doc1abcdef"]
    assert "ghost999" not in draft.markdown
    assert draft.unsourced_sentences == ["编造结论 。"]
    assert "核心立论：立论X" in prompts[0] and "学术缺口：缺口Y" in prompts[0]
    assert "主线逻辑：主线Z" in prompts[0]


def test_unsourced_claims_gate(monkeypatch):
    from litreview.review_quality import EvidenceContext, audit_and_clean_review
    monkeypatch.delenv("REVIEW_UNSOURCED_CLAIM_MAX", raising=False)
    review = _single_section_review("有来源 [1]。", unsourced_sentences=["编造结论。"])
    result = audit_and_clean_review(review, EvidenceContext(number_to_doc_id={1: "doc-a"}),
                                    {"doc-a": {"title": "T", "year": 2020}})
    assert {"unsourced_claim", "unsourced_claim_ratio"} <= _codes(result, "blocking")

    monkeypatch.setenv("REVIEW_UNSOURCED_CLAIM_MAX", "0.9")
    result = audit_and_clean_review(review, EvidenceContext(number_to_doc_id={1: "doc-a"}),
                                    {"doc-a": {"title": "T", "year": 2020}})
    assert "unsourced_claim" in _codes(result, "warning")


# ── DOI / 年份 ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected,status", [
    ("10.1016/j.ijbiomac.2020.01.001", "10.1016/j.ijbiomac.2020.01.001", "ok"),
    ("10.1016/i.iibiomac.2020.01.001", "10.1016/j.ijbiomac.2020.01.001", "fixed"),
    ("https://doi.org/10.1000/ABC.", "10.1000/ABC", "fixed"),
    ("1O.1O16/j.cej.2021.1", "10.1016/j.cej.2021.1", "fixed"),
    ("", "", "empty"),
    ("not a doi", "not a doi", "invalid"),
])
def test_normalize_doi(raw, expected, status):
    from litreview.review_quality import normalize_doi
    assert normalize_doi(raw) == (expected, status)


def test_reference_doi_and_year_warnings():
    from litreview.review_quality import EvidenceContext, audit_and_clean_review
    review = ReviewDoc(
        title="测试综述", topic="测试",
        intro="引言背景介绍与研究意义阐述，确保引言非空。",
        conclusion="结论与展望分析，总结全文主要进展与未来方向。",
        sections=[SectionDraft(heading="章", markdown="甲 [1]。乙 [2]。",
                               cited_doc_ids=["a", "b"])],
    )
    meta = {"a": {"title": "A", "year": 2020, "doi": "10.1016/i.iibiomac.2020.01.001"},
            "b": {"title": "B", "doi": "garbage"}}
    result = audit_and_clean_review(review, EvidenceContext(number_to_doc_id={1: "a", 2: "b"}), meta)
    codes = _codes(result, "warning")
    assert {"reference_doi_normalized", "reference_doi_invalid", "reference_year_missing"} <= codes
    assert not result.blocking_issues
    assert any("10.1016/j.ijbiomac" in ref for ref in result.review.references)


# ── 图表编号 ────────────────────────────────────────────────────────────────

class _Store:
    def __init__(self, assets):
        self.assets = assets

    def get_assets_for_docs(self, doc_ids, captioned_only=True):
        return [a for a in self.assets if a["doc_id"] in doc_ids]


class _Reranker:
    def rerank(self, _query, documents, top_n=None):
        return [(i, 0.9) for i in range(len(documents))][:top_n]


def _figure_settings(tmp):
    return SimpleNamespace(REVIEW_OUTPUT_DIR=tmp, DB_PATH=os.path.join(tmp, "x.db"), MINERU_OUTPUT_DIR=os.path.join(tmp, "mineru_output"),
                           BOOK_OUTPUT_DIR=os.path.join(tmp, "book_output"),
                           REVIEW_INSERT_FIGURES=True, REVIEW_FIGURES_PER_SECTION=3,
                           REVIEW_FIGURE_MIN_SCORE=0.2)


def test_figures_use_chapter_numbering_and_in_text_reference(tmp_path, monkeypatch):
    from litreview import figures
    tmp = str(tmp_path)
    assets = [
        {"doc_id": "doc1abcd", "asset_type": "image", "img_path": "p/images/f1.jpg",
         "caption": "Figure 1. 结块过程示意图", "page_idx": 1},
        {"doc_id": "doc1abcd", "asset_type": "image", "img_path": "p/images/f2.jpg",
         "caption": "图2 形貌", "page_idx": 2},
        {"doc_id": "doc1abcd", "asset_type": "table", "img_path": "p/images/t1.jpg",
         "caption": "Table 1. 水分含量对比", "page_idx": 3},
    ]
    for asset in assets:
        path = os.path.join(tmp, "mineru_output", asset["img_path"])
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "wb").write(b"img")
    draft = SectionDraft(heading="章", markdown="正文 [@doc1abcd]", cited_doc_ids=["doc1abcd"])
    n = figures.select_section_figures(_Store(assets), _Reranker(), OutlineSection(heading="章"),
                                       draft, _figure_settings(tmp), SimpleNamespace(print=print),
                                       set(), 3)
    assert n == 3
    md = draft.markdown
    assert "相关图表见图 3-1、图 3-2、表 3-1。" in md
    assert "*图 3-1　结块过程示意图（源自 [@doc1abcd]）*" in md
    assert "*表 3-1　水分含量对比（源自 [@doc1abcd]）*" in md
    assert "Figure 1." not in md and "图2 形貌" not in md


def test_renumber_figure_labels_uses_final_chapter():
    from litreview.stages import renumber_figure_labels
    md = "相关图表见图 4-1、表 4-1。\n*图 4-1　说明*"
    assert renumber_figure_labels(md, 3) == "相关图表见图 3-1、表 3-1。\n*图 3-1　说明*"


# ── 编排：暂停 / 软校验 / 预算 / 立论 ─────────────────────────────────────────

def _deps(settings=None):
    from litreview import review_orchestrator as orchestrator
    return orchestrator.ReviewDependencies(
        store=SimpleNamespace(get_paper_meta=lambda: {}),
        embedder=object(), client=object(), reranker=None,
        settings=settings or SimpleNamespace(ANALYSIS_MODEL="mini", REVIEW_MODEL="review"),
        console=SimpleNamespace(print=lambda *_a: None),
    )


def _two_section_outline():
    return Outline(title="测试", topic="测试", thesis="立论T",
                   sections=[OutlineSection(heading="第一章"), OutlineSection(heading="第二章")])


def test_orchestrator_pauses_on_quota_and_keeps_checkpoint(monkeypatch):
    from litreview import review_orchestrator as orchestrator
    from litreview import stages, figures
    from litreview.quota import QuotaExceededError

    calls = []

    def fake_gather(section, *_a, **_k):
        calls.append(section.heading)
        if section.heading == "第二章":
            raise QuotaExceededError("额度不足", utc_date="2026-09-27", model="gpt-5.4")
        return [Evidence(chunk_id=1, doc_id="doc1", score=8, summary="证据")]

    monkeypatch.setattr(stages, "gather_evidence", fake_gather)
    monkeypatch.setattr(stages, "gather_broad_bibliography", lambda *_a, **_k: [])
    monkeypatch.setattr(stages, "write_section", lambda sec, *_a, **_k: SectionDraft(
        heading=sec.heading, markdown="正文 [@doc1]", cited_doc_ids=["doc1"]))
    monkeypatch.setattr(figures, "select_section_figures", lambda *_a, **_k: 0)

    checkpoints = []
    result = orchestrator.generate_review_sections(
        _two_section_outline(), _deps(),
        orchestrator.SectionGenerationConfig(validate_output=False),
        orchestrator.ReviewHooks(on_checkpoint=checkpoints.append),
    )
    assert [d.heading for d in result.drafts] == ["第一章"]
    assert result.failed_sections == []
    assert result.paused_quota["resume_at"] == "2026-09-28T00:00:00+00:00"
    assert result.paused_quota["section"] == "第二章"
    assert len(checkpoints) == 1


def test_orchestrator_repairs_soft_issue_and_keeps_final_candidate(monkeypatch):
    from litreview import review_orchestrator as orchestrator
    from litreview import stages, figures
    monkeypatch.setattr(orchestrator.time, "sleep", lambda _s: None)
    monkeypatch.delenv("REVIEW_NUMERIC_UNSUPPORTED_MAX", raising=False)
    monkeypatch.setattr(stages, "gather_evidence", lambda *_a, **_k: [
        Evidence(chunk_id=1, doc_id="doc1", score=8, summary="释放 60%")])
    monkeypatch.setattr(stages, "gather_broad_bibliography", lambda *_a, **_k: [])
    feedbacks = []

    def fake_write(sec, *_a, validation_feedback="", **_k):
        feedbacks.append(validation_feedback)
        return SectionDraft(heading=sec.heading, markdown="释放 95% [@doc1]。",
                            cited_doc_ids=["doc1"])

    monkeypatch.setattr(stages, "write_section", fake_write)
    monkeypatch.setattr(figures, "select_section_figures", lambda *_a, **_k: 0)
    outline = Outline(title="测试", sections=[OutlineSection(heading="章")])
    result = orchestrator.generate_review_sections(
        outline, _deps(), orchestrator.SectionGenerationConfig(max_section_attempts=2))
    assert len(feedbacks) == 2 and "95" in feedbacks[1]
    assert len(result.drafts) == 1  # 最后一次仍只有软问题：保留草稿
    assert result.drafts[0].evidence_numbers == {"doc1": ["60"]}


def test_orchestrator_caps_track_b_and_passes_thesis(monkeypatch):
    from litreview import review_orchestrator as orchestrator
    from litreview import stages, figures
    seen = {}

    def fake_gather(*_a, max_docs=None, **_k):
        seen["max_docs"] = max_docs
        return [Evidence(chunk_id=i, doc_id=f"doc{i}", score=8, summary="s") for i in range(5)]

    def fake_broad(*_a, top_n=None, **_k):
        seen["top_n"] = top_n
        return []

    def fake_write(sec, *_a, thesis="", **_k):
        seen["thesis"] = thesis
        return SectionDraft(heading=sec.heading, markdown="x [@doc0]", cited_doc_ids=["doc0"])

    monkeypatch.setattr(stages, "gather_evidence", fake_gather)
    monkeypatch.setattr(stages, "gather_broad_bibliography", fake_broad)
    monkeypatch.setattr(stages, "write_section", fake_write)
    monkeypatch.setattr(figures, "select_section_figures", lambda *_a, **_k: 0)
    outline = Outline(title="测试", thesis="立论T", sections=[OutlineSection(heading="章")])
    orchestrator.generate_review_sections(
        outline, _deps(), orchestrator.SectionGenerationConfig(validate_output=False))
    assert seen == {"max_docs": 12, "top_n": 13, "thesis": "立论T"}


def test_budget_estimate_warns_in_chinese_but_never_refuses(tmp_path):
    from litreview import review_orchestrator as orchestrator
    settings = SimpleNamespace(DB_PATH=str(tmp_path / "q.db"), REVIEW_DAILY_TOKEN_CAP=50_000,
                               LLM_DAILY_TOKEN_CAP=2_000_000, REVIEW_TOP_K=20)
    plan = orchestrator.estimate_review_budget(8, orchestrator.SectionGenerationConfig(), settings)
    assert plan["utc_days"] > 1
    assert "UTC 日" in plan["message"] and "暂停" in plan["message"]


def test_outline_alignment_roundtrip():
    outline = Outline.from_dict({"title": "T", "sections": [], "thesis": "旧立论",
                                 "research_gap": "旧缺口"}, topic="x")
    outline.apply_alignment("新立论", "", "主线")
    assert (outline.thesis, outline.research_gap, outline.mainline_logic) == ("新立论", "旧缺口", "主线")
    assert Outline.from_dict(outline.model_dump(), topic="x").mainline_logic == "主线"


# ── 自动续跑调度 ────────────────────────────────────────────────────────────

def test_schedule_quota_resume_dedupes_and_cancels():
    from litreview import review_jobs
    now = datetime(2026, 9, 27, 23, 0, tzinfo=timezone.utc)
    fired = []
    delay = review_jobs.schedule_quota_resume("job-x", "2026-09-28T00:00:00+00:00",
                                              lambda: fired.append(1), grace_seconds=60, now=now)
    assert delay == pytest.approx(3660)
    review_jobs.schedule_quota_resume("job-x", "2026-09-28T00:00:00+00:00",
                                      lambda: fired.append(2), grace_seconds=60, now=now)
    assert review_jobs.scheduled_quota_resumes().count("job-x") == 1
    assert review_jobs.cancel_quota_resume("job-x") is True
    assert "job-x" not in review_jobs.scheduled_quota_resumes()
    assert fired == []


# ── MCP 入口：paused_quota 持久化与续跑 ───────────────────────────────────────

def _fresh_mcp(monkeypatch, tmp_path):
    import importlib
    monkeypatch.setenv("MINERU_API_KEY", "test-mineru")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai")
    monkeypatch.setenv("SILICONFLOW_API_KEY", "test-siliconflow")
    monkeypatch.setenv("SILICONFLOW_API_BASE", "https://siliconflow.invalid/v1")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    import utils
    utils._settings = None
    sys.modules.pop("mcp_server", None)
    return importlib.import_module("mcp_server")


def test_mcp_quota_exhaustion_pauses_job_and_resume_requeues(monkeypatch, tmp_path):
    import queue
    from litreview import figures, review_jobs, stages
    from litreview.quota import QuotaExceededError
    server = _fresh_mcp(monkeypatch, tmp_path)
    scheduled = []
    monkeypatch.setattr(review_jobs, "schedule_quota_resume",
                        lambda job_id, resume_at, _cb, **_k: scheduled.append((job_id, resume_at)) or 1.0)
    monkeypatch.setattr(server, "_client", lambda: object())
    monkeypatch.setattr(server, "_store", lambda: SimpleNamespace(get_paper_meta=lambda: {}))
    monkeypatch.setattr(server, "_embedder", lambda: object())
    monkeypatch.setattr(server, "_reranker", lambda: None)
    monkeypatch.setattr(server, "log_run_event", lambda **_k: None)

    def fake_gather(section, *_a, **_k):
        if section.heading == "第二章":
            raise QuotaExceededError("额度不足", utc_date="2026-09-27", model="gpt-5.4")
        return [Evidence(chunk_id=1, doc_id="doc1", score=8, summary="证据")]

    monkeypatch.setattr(stages, "gather_evidence", fake_gather)
    monkeypatch.setattr(stages, "gather_broad_bibliography", lambda *_a, **_k: [])
    monkeypatch.setattr(stages, "write_section", lambda sec, *_a, **_k: SectionDraft(
        heading=sec.heading, markdown="正文 [@doc1]", cited_doc_ids=["doc1"]))
    monkeypatch.setattr(figures, "select_section_figures", lambda *_a, **_k: 0)
    outline_json = ('{"title":"测试综述","thesis":"立论T","sections":[{"heading":"第一章"},'
                    '{"heading":"第二章"}]}')
    server._jobs["qjob"] = {"job_id": "qjob", "status": "running", "topic": "测试", "log": [],
                            "output": "", "docx": "", "error": "", "summary": "",
                            "started_at": "now"}

    server._run_review_job("qjob", "测试", "", 0, 0, outline_json)

    job = server._jobs["qjob"]
    assert job["status"] == "paused_quota"
    assert job["resume_after"] == "2026-09-28T00:00:00+00:00"
    assert scheduled == [("qjob", "2026-09-28T00:00:00+00:00")]
    persisted = server._get_persistent_review_job("qjob")
    assert persisted["status"] == "paused_quota"
    assert persisted["resume_after"] == "2026-09-28T00:00:00+00:00"
    assert "第一章" in persisted["checkpoint_json"]
    assert "paused_quota" in server.review_status.__wrapped__("qjob") or \
        "暂停" in server.review_status.__wrapped__("qjob")

    queued = queue.Queue()
    monkeypatch.setattr(server, "_review_queue", queued)
    monkeypatch.setattr(server, "_ensure_review_worker", lambda: None)
    assert "重新入队" in server.resume_review.__wrapped__("qjob")
    assert queued.get_nowait()[0] == "qjob"
    assert server._get_persistent_review_job("qjob")["status"] == "queued"
