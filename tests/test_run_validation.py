"""Phase 1B 综述运行校验与基线对比测试套件。

严格遵循隔离原则：100% 隔离于 tmp_path，0 网络、0 LLM、0 真实数据库、0 Git 命令。
全面覆盖契约校验、必填结构、类型鲁棒性、防篡改、全字段路径安全、差异比对、退化判定、确定性 Jaccard、真实 Phase 1A 兼容及 CLI 适配。
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
import pytest

from litreview.run_validation import (
    validate_review_run,
    compare_review_runs,
    RunValidationReport,
    RunComparisonReport,
)
from litreview.manifest import (
    write_review_manifest,
    ManifestInput,
    RunContext,
    PromptSnapshot,
    RetrievalSnapshot,
)
from litreview.models import ReviewDoc, SectionDraft
from litreview.review_quality import QualityIssue


def _calc_sha256(content: str | bytes) -> str:
    if isinstance(content, str):
        content = content.encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def _create_synthetic_run(
    dir_path,
    topic: str = "微塑料降解综述",
    run_id: str = "run-test-001",
    model_name: str = "deepseek-chat",
    doc_ids: list[str] | None = None,
    quality_blocking: bool = False,
    quality_issues: list[dict[str, str]] | None = None,
    ref_status: str = "complete",
    section_evidence_status: str = "complete",
    has_docx: bool = True,
    git_commit: str = "3f01702abcdef0123456789abcdef0123456789",
    git_dirty: bool = False,
    corpus_fp: str | None = "a" * 64,
) -> tuple[str, str, str | None]:
    """辅助函数：在指定目录下生成一套完全自洽的合成综述文件 (.md, .docx, .manifest.json)。"""
    os.makedirs(dir_path, exist_ok=True)
    if doc_ids is None:
        doc_ids = ["doc_1", "doc_2", "doc_3"]

    md_filename = f"综述_{run_id}.md"
    md_path = os.path.join(dir_path, md_filename)
    md_content = f"# {topic}\n\n这是综述测试正文 [1] [2]。\n\n## 参考文献\n[1] 文献A\n[2] 文献B\n"
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md_content)
    md_sha256 = _calc_sha256(md_content)

    docx_filename = None
    docx_sha256 = None
    if has_docx:
        docx_filename = f"综述_{run_id}.docx"
        docx_path = os.path.join(dir_path, docx_filename)
        docx_bytes = b"PK\x03\x04synthetic_docx_binary_data"
        with open(docx_path, "wb") as f:
            f.write(docx_bytes)
        docx_sha256 = _calc_sha256(docx_bytes)

    cited_references = []
    if ref_status == "complete":
        for did in doc_ids:
            meta = {
                "title": f"Title of {did}", "title_zh": "", "title_en": f"Title of {did}",
                "doi": f"10.1000/{did}", "authors": "Author A; Author B", "journal": "Nature",
                "year": "2024", "volume": "10", "issue": "2", "pages": "100-110",
                "doc_type": "article", "cite_key": did
            }
            canon_json = json.dumps(meta, ensure_ascii=False, separators=(',', ':'), sort_keys=True)
            meta_hash = _calc_sha256(canon_json)
            cited_references.append({
                "doc_id": did,
                **meta,
                "meta_hash": meta_hash,
            })

    chunks = []
    if section_evidence_status == "complete":
        for did in doc_ids:
            summary = f"Evidence summary for {did}"
            chunks.append({
                "chunk_id": 101,
                "doc_id": did,
                "score": 88,
                "summary_hash": _calc_sha256(summary),
                "retrieval_score": 0.92,
                "section_title": "机制探讨",
            })

    manifest_dict = {
        "schema_version": 1,
        "run_meta": {
            "run_id": run_id,
            "trigger_source": "cli",
        },
        "topic_intent": {
            "topic": topic,
            "focus": "降解机理",
            "thesis": "生物基可完全替代",
            "research_gap": "机理不明",
            "mainline_logic": "现状-危害-对策",
            "target_lang": "zh",
        },
        "timestamps": {
            "generated_at": "2026-08-25 12:00:00",
            "duration_seconds": 45,
        },
        "model_and_prompts": {
            "model_name": model_name,
            "analysis_model": "deepseek-chat",
            "reranker_model": "Qwen3-Reranker-8B",
            "prompt_versions": {
                "outline_prompt_version": "v1.2",
                "section_prompt_version": "v2.0",
                "tldr_prompt_version": "v1.0",
            },
        },
        "retrieval_config": {
            "review_top_k": 20,
            "evidence_n": 32,
            "target_words": 8000,
            "section_words_min": 1500,
            "section_words_max": 2500,
            "reranker_model": "Qwen3-Reranker-8B",
        },
        "reference_mapping_status": ref_status,
        "cited_references": cited_references,
        "section_evidence": [
            {
                "heading": "第一章 降解机理",
                "evidence_status": section_evidence_status,
                "cited_doc_ids": doc_ids,
                "chunks": chunks,
            }
        ],
        "quality_gate": {
            "quality_blocking": quality_blocking,
            "quality_issues": quality_issues if quality_issues is not None else [],
            "doc_count": len(cited_references) if ref_status == "complete" else (0 if ref_status == "empty" else len(doc_ids)),
            "evidence_count": len(chunks),
        },
        "artifacts": {
            "output_md_filename": md_filename,
            "markdown_sha256": md_sha256,
            "output_docx_filename": docx_filename,
            "docx_sha256": docx_sha256,
        },
        "env_fingerprint": {
            "git_commit": git_commit,
            "git_dirty": git_dirty,
            "corpus_fingerprint": corpus_fp,
        },
    }

    manifest_filename = f"综述_{run_id}.manifest.json"
    manifest_path = os.path.join(dir_path, manifest_filename)
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest_dict, f, ensure_ascii=False, indent=2)

    return manifest_path, md_path, os.path.join(dir_path, docx_filename) if docx_filename else None


def test_valid_pass_run(tmp_path):
    """测试完全自洽的合法运行清单，预期返回 PASS 且 issues 为空。"""
    manifest_path, _, _ = _create_synthetic_run(tmp_path)
    report = validate_review_run(manifest_path)

    assert report.verdict == "PASS"
    assert report.schema_version == 1
    assert report.topic == "微塑料降解综述"
    assert report.markdown_sha256_match is True
    assert report.docx_sha256_match is True
    assert report.reference_mapping_status == "complete"
    assert report.cited_references_count == 3
    assert len(report.issues) == 0

    d = report.to_dict()
    assert d["verdict"] == "PASS"
    assert d["manifest_filename"] == os.path.basename(manifest_path)


def test_quality_issues_schema_dict_and_scalar_types(tmp_path):
    """测试 quality_issues 的 Phase 1A 真实结构（每项严格 dict 且含 4 字段）；非阻塞项不 dump raw message。"""
    real_issue = {
        "code": "WARN_CITATION_COUNT",
        "severity": "warning",
        "message": "引文数量偏少",
        "section": "第一章",
    }
    manifest_path, _, _ = _create_synthetic_run(tmp_path, quality_issues=[real_issue])
    report = validate_review_run(manifest_path)

    assert report.verdict == "WARN"
    qi_warn = next(iss for iss in report.issues if iss.code == "QUALITY_WARNING")
    assert "WARN_CITATION_COUNT" in qi_warn.message
    assert "warning" in qi_warn.message
    # 断言不 dump 原始 message
    assert "引文数量偏少" not in qi_warn.message

    # 错误类型测试：quality_issues 内为字符串、未知键或字段缺失 -> FAIL
    with open(manifest_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    data["quality_gate"]["quality_issues"] = ["string_issue"]
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    report_bad1 = validate_review_run(manifest_path)
    assert report_bad1.verdict == "FAIL"

    data["quality_gate"]["quality_issues"] = [{"code": "C", "severity": "S", "extra_key": "X"}]
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    report_bad2 = validate_review_run(manifest_path)
    assert report_bad2.verdict == "FAIL"


def test_markdown_tampered_hash_mismatch(tmp_path):
    """测试 Markdown 正文被篡改或哈希不一致，预期 FAIL 并指明 HASH_MISMATCH。"""
    manifest_path, md_path, _ = _create_synthetic_run(tmp_path)

    with open(md_path, "a", encoding="utf-8") as f:
        f.write("\n<!-- 篡改内容 -->\n")

    report = validate_review_run(manifest_path)
    assert report.verdict == "FAIL"
    assert report.markdown_sha256_match is False
    codes = [iss.code for iss in report.issues]
    assert "HASH_MISMATCH" in codes


def test_declared_docx_missing_or_tampered(tmp_path):
    """测试声明的 DOCX 文件缺失或篡改，预期 FAIL 并指明对应问题。"""
    manifest_path, _, docx_path = _create_synthetic_run(tmp_path, has_docx=True)

    os.remove(docx_path)
    report = validate_review_run(manifest_path)
    assert report.verdict == "FAIL"
    assert any(iss.code == "FILE_MISSING" and "DOCX" in iss.message for iss in report.issues)

    with open(docx_path, "wb") as f:
        f.write(b"tampered_bytes")
    report2 = validate_review_run(manifest_path)
    assert report2.verdict == "FAIL"
    assert any(iss.code == "HASH_MISMATCH" and "DOCX" in iss.message for iss in report2.issues)


def test_optional_docx_null_is_clean(tmp_path):
    """测试可选 DOCX 为 null 时，不应产生任何关于 DOCX 的误报或警告。"""
    manifest_path, _, _ = _create_synthetic_run(tmp_path, has_docx=False)
    report = validate_review_run(manifest_path)

    assert report.verdict == "PASS"
    assert report.docx_filename is None
    assert report.docx_sha256_match is None
    assert not any("docx" in iss.location.lower() for iss in report.issues)


def test_dangerous_path_directory_traversal(tmp_path):
    """测试产物文件名包含路径遍历或绝对路径时，拒绝并判定为 FAIL，且错误信息不泄露原始绝对路径。"""
    manifest_path, _, _ = _create_synthetic_run(tmp_path)

    for bad_path in ("../escape.md", "/etc/passwd", "subdir/nested.md", "..\\win_escape.md"):
        with open(manifest_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        data["artifacts"]["output_md_filename"] = bad_path
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(data, f)

        report = validate_review_run(manifest_path)
        assert report.verdict == "FAIL"
        assert any(iss.code == "DANGEROUS_PATH" for iss in report.issues)
        for iss in report.issues:
            assert "/etc/passwd" not in iss.message


def test_unknown_schema_and_unallowlisted_sensitive_fields(tmp_path):
    """测试非 schema_version=1 或注入非白名单敏感字段，预期判定为 FAIL。"""
    manifest_path, _, _ = _create_synthetic_run(tmp_path)

    with open(manifest_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    data["schema_version"] = 2
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    report = validate_review_run(manifest_path)
    assert report.verdict == "FAIL"
    sv_iss = next(iss for iss in report.issues if iss.code == "UNKNOWN_SCHEMA_VERSION")
    assert "2" not in sv_iss.message  # 不回显 raw 值

    data["schema_version"] = 1
    data["secret_api_token"] = "sk-leaked-secret-key-12345"
    data["model_and_prompts"]["private_user_prompt"] = "Confidential prompt"
    data["cited_references"][0]["unapproved_field"] = "bad"
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(data, f)

    report2 = validate_review_run(manifest_path)
    assert report2.verdict == "FAIL"
    unknown_iss = [iss for iss in report2.issues if iss.code == "UNKNOWN_FIELDS"]
    assert len(unknown_iss) >= 3


def test_missing_required_fields_fails(tmp_path):
    """测试缺少必填顶层或嵌套字段时，返回 FAIL 并指明 MISSING_REQUIRED_FIELD。"""
    manifest_path, _, _ = _create_synthetic_run(tmp_path)

    with open(manifest_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    del data["run_meta"]
    del data["quality_gate"]
    del data["topic_intent"]["topic"]
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(data, f)

    report = validate_review_run(manifest_path)
    assert report.verdict == "FAIL"
    missing_codes = [iss.code for iss in report.issues if iss.code == "MISSING_REQUIRED_FIELD"]
    assert len(missing_codes) >= 3


def test_schema_type_mismatch_and_no_exceptions(tmp_path):
    """测试损坏字段类型（schema_version=True, doc_count='bad' 等）时，安全返回 FAIL，绝不抛出任何未捕获异常。"""
    manifest_path, _, _ = _create_synthetic_run(tmp_path)

    with open(manifest_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    data["schema_version"] = True  # bool 不能当做 1
    data["timestamps"]["duration_seconds"] = "45s"
    data["quality_gate"]["doc_count"] = "three"
    data["quality_gate"]["quality_issues"] = "not_a_list"
    data["cited_references"] = "not_a_list"
    data["section_evidence"] = "not_a_list"
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(data, f)

    report = validate_review_run(manifest_path)
    assert report.verdict == "FAIL"
    assert any(iss.code in ("UNKNOWN_SCHEMA_VERSION", "SCHEMA_TYPE_MISMATCH") for iss in report.issues)


def test_strict_types_for_all_scalars_and_lists(tmp_path):
    """测试所有标量与列表项的严格类型与非负约束（负数/非字符串/布尔值），安全返回 FAIL 不抛异常。"""
    manifest_path, _, _ = _create_synthetic_run(tmp_path)

    with open(manifest_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    # 1. 负数与布尔型数值
    data["timestamps"]["duration_seconds"] = -10
    data["retrieval_config"]["review_top_k"] = -5
    data["section_evidence"][0]["chunks"][0]["score"] = -1
    data["section_evidence"][0]["chunks"][0]["chunk_id"] = True  # bool
    data["section_evidence"][0]["chunks"][0]["retrieval_score"] = True  # bool
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    report1 = validate_review_run(manifest_path)
    assert report1.verdict == "FAIL"
    assert any(iss.code == "SCHEMA_TYPE_MISMATCH" for iss in report1.issues)

    # 2. cited_references 元数据非 string，cited_doc_ids 项非 string
    with open(manifest_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    data["cited_references"][0]["year"] = 2024  # 应当为 string
    data["section_evidence"][0]["cited_doc_ids"] = [123]  # 应当为 string
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    report2 = validate_review_run(manifest_path)
    assert report2.verdict == "FAIL"
    assert any(iss.code == "SCHEMA_TYPE_MISMATCH" for iss in report2.issues)


def test_hash_format_validation(tmp_path):
    """测试哈希格式非法（非 64 位小写 hex）时判定为 FAIL。"""
    manifest_path, _, _ = _create_synthetic_run(tmp_path)

    with open(manifest_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    data["artifacts"]["markdown_sha256"] = "ABCDEF" * 10 + "1234"
    data["section_evidence"][0]["chunks"][0]["summary_hash"] = "not_hex_chars_zzzz" * 4
    data["env_fingerprint"]["corpus_fingerprint"] = "invalid_fp"
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(data, f)

    report = validate_review_run(manifest_path)
    assert report.verdict == "FAIL"
    assert any(iss.code == "INVALID_HASH_FORMAT" for iss in report.issues)


def test_meta_hash_canonical_recalculation_and_tamper_detection(tmp_path):
    """测试 meta_hash 依据 12 字段标准规范 JSON 重算，捕获元数据篡改。"""
    manifest_path, _, _ = _create_synthetic_run(tmp_path)

    with open(manifest_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    data["cited_references"][0]["title"] = "Tampered Fake Title"
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(data, f)

    report = validate_review_run(manifest_path)
    assert report.verdict == "FAIL"
    assert any(iss.code == "META_HASH_TAMPERED" for iss in report.issues)


def test_reference_invariants_complete_empty_legacy(tmp_path):
    """测试引用文献状态不变量：empty 必须 doc_count=0 且 cited=[]；complete 必须 doc_count>0 且无重复 doc_id；unavailable 必须 cited=[]。"""
    manifest_path, _, _ = _create_synthetic_run(tmp_path)

    # 1. complete 模式下出现重复 doc_id
    with open(manifest_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    dup_ref = copy.deepcopy(data["cited_references"][0])
    data["cited_references"] = [data["cited_references"][0], dup_ref]
    data["quality_gate"]["doc_count"] = 2
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    report1 = validate_review_run(manifest_path)
    assert report1.verdict == "FAIL"
    assert any(iss.code == "DUPLICATE_DOC_ID" for iss in report1.issues)

    # 2. empty 模式但 doc_count > 0
    data["reference_mapping_status"] = "empty"
    data["cited_references"] = []
    data["quality_gate"]["doc_count"] = 1
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    report2 = validate_review_run(manifest_path)
    assert report2.verdict == "FAIL"
    assert any(iss.code == "MAPPING_MISMATCH" for iss in report2.issues)

    # 3. unavailable_legacy_output 模式但 cited_references 非空
    data["reference_mapping_status"] = "unavailable_legacy_output"
    data["cited_references"] = [dup_ref]
    data["quality_gate"]["doc_count"] = 1
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    report3 = validate_review_run(manifest_path)
    assert report3.verdict == "FAIL"
    assert any(iss.code == "MAPPING_MISMATCH" for iss in report3.issues)


def test_legacy_and_dirty_warnings(tmp_path):
    """测试历史断点恢复、未知 Git、脏工作区、空语料指纹及非阻塞质量门警告，预期降级为 WARN。"""
    legacy_issue = {
        "code": "WARN_CITATION",
        "severity": "warning",
        "message": "引用提示",
        "section": "第一节",
    }
    manifest_path, _, _ = _create_synthetic_run(
        tmp_path,
        ref_status="unavailable_legacy_output",
        section_evidence_status="unavailable_legacy_checkpoint",
        git_commit="unknown",
        git_dirty=True,
        corpus_fp=None,
        quality_blocking=False,
        quality_issues=[legacy_issue],
    )
    report = validate_review_run(manifest_path)

    assert report.verdict == "WARN"
    codes = {iss.code for iss in report.issues}
    assert "LEGACY_MAPPING" in codes
    assert "LEGACY_CHECKPOINT" in codes
    assert "GIT_UNKNOWN" in codes
    assert "GIT_DIRTY" in codes
    assert "CORPUS_UNSET" in codes
    assert "QUALITY_WARNING" in codes
    assert not any(iss.severity == "FAIL" for iss in report.issues)


def test_quality_blocking_fails(tmp_path):
    """测试质量门阻塞性缺陷 (quality_blocking=True)，预期判定为 FAIL。"""
    block_issue = {
        "code": "UNKNOWN_CITATION",
        "severity": "blocking",
        "message": "未定义引用 [99]",
        "section": "第一章",
    }
    manifest_path, _, _ = _create_synthetic_run(
        tmp_path,
        quality_blocking=True,
        quality_issues=[block_issue],
    )
    report = validate_review_run(manifest_path)
    assert report.verdict == "FAIL"
    assert any(iss.code == "QUALITY_BLOCKING" for iss in report.issues)


def test_identical_compare_passes(tmp_path):
    """测试完全相同的合法运行清单进行比对，预期为 PASS 且 Jaccard 相似度为 1.0。"""
    base_dir = tmp_path / "base"
    cand_dir = tmp_path / "cand"
    base_manifest, _, _ = _create_synthetic_run(base_dir, run_id="run-base")
    cand_manifest, _, _ = _create_synthetic_run(cand_dir, run_id="run-cand")

    cmp_report = compare_review_runs(base_manifest, cand_manifest)
    assert cmp_report.verdict == "PASS"
    assert cmp_report.same_topic is True
    assert cmp_report.reference_diff.status == "complete"
    assert cmp_report.reference_diff.jaccard_similarity == 1.0
    assert cmp_report.reference_diff.added_doc_ids == []
    assert cmp_report.reference_diff.removed_doc_ids == []
    assert len(cmp_report.issues) == 0


def test_config_model_corpus_changes_only_warn(tmp_path):
    """测试模型、提示词、检索配置与语料库指纹变化时，仅作为 Delta 记录与 WARN，绝不判 FAIL。"""
    base_dir = tmp_path / "base"
    cand_dir = tmp_path / "cand"
    base_manifest, _, _ = _create_synthetic_run(base_dir, run_id="run-base", model_name="deepseek-chat", corpus_fp="a"*64)
    cand_manifest, _, _ = _create_synthetic_run(cand_dir, run_id="run-cand", model_name="gpt-4o", corpus_fp="b"*64)

    cmp_report = compare_review_runs(base_manifest, cand_manifest)
    assert cmp_report.verdict == "WARN"
    assert not any(iss.severity == "FAIL" for iss in cmp_report.issues)

    delta_names = {d.name for d in cmp_report.deltas}
    assert "model_name" in delta_names
    assert "corpus_fingerprint" in delta_names
    assert not any(d.is_regression for d in cmp_report.deltas)


def test_quantity_count_drops_do_not_fail(tmp_path):
    """测试文献数量、证据数量及总字数指标下降时，作为客观 Delta 记录，绝不设硬失败。"""
    base_dir = tmp_path / "base"
    cand_dir = tmp_path / "cand"
    base_manifest, _, _ = _create_synthetic_run(base_dir, run_id="run-base", doc_ids=["d1", "d2", "d3", "d4"])
    cand_manifest, _, _ = _create_synthetic_run(cand_dir, run_id="run-cand", doc_ids=["d1", "d2"])

    cmp_report = compare_review_runs(base_manifest, cand_manifest)
    assert cmp_report.verdict in ("PASS", "WARN")
    assert not any(iss.severity == "FAIL" for iss in cmp_report.issues)
    assert cmp_report.reference_diff.base_count == 4
    assert cmp_report.reference_diff.candidate_count == 2
    assert cmp_report.reference_diff.removed_doc_ids == ["d3", "d4"]


def test_evidence_provenance_regression_fails(tmp_path):
    """测试同名章节证据溯源状态从 complete 退化为 unavailable_legacy_checkpoint 时，判定为 FAIL。"""
    base_dir = tmp_path / "base"
    cand_dir = tmp_path / "cand"
    base_manifest, _, _ = _create_synthetic_run(base_dir, run_id="run-base", section_evidence_status="complete")
    cand_manifest, _, _ = _create_synthetic_run(cand_dir, run_id="run-cand", section_evidence_status="unavailable_legacy_checkpoint")

    cmp_report = compare_review_runs(base_manifest, cand_manifest)
    assert cmp_report.verdict == "FAIL"
    assert any(iss.code == "REGRESSION_EVIDENCE_PROVENANCE" for iss in cmp_report.issues)
    reg_sections = [s for s in cmp_report.section_diffs if s.is_regression]
    assert len(reg_sections) == 1


def test_topic_mismatch_fails_comparison(tmp_path):
    """测试比对主题不一致时，比对判定为 FAIL。"""
    base_dir = tmp_path / "base"
    cand_dir = tmp_path / "cand"
    base_manifest, _, _ = _create_synthetic_run(base_dir, topic="微塑料降解综述")
    cand_manifest, _, _ = _create_synthetic_run(cand_dir, topic="固态电池电解质综述")

    cmp_report = compare_review_runs(base_manifest, cand_manifest)
    assert cmp_report.verdict == "FAIL"
    assert cmp_report.same_topic is False
    assert any(iss.code == "TOPIC_MISMATCH" for iss in cmp_report.issues)


def test_reference_jaccard_and_added_removed_determinism(tmp_path):
    """测试引用集合计算确定性：complete 时输出稳定 added/removed/retained 与 Jaccard；legacy 时输出 unavailable。"""
    base_dir = tmp_path / "base"
    cand_dir = tmp_path / "cand"
    base_manifest, _, _ = _create_synthetic_run(base_dir, doc_ids=["doc_A", "doc_B", "doc_C"])
    cand_manifest, _, _ = _create_synthetic_run(cand_dir, doc_ids=["doc_B", "doc_C", "doc_D"])

    cmp_report = compare_review_runs(base_manifest, cand_manifest)
    ref_diff = cmp_report.reference_diff
    assert ref_diff.status == "complete"
    assert ref_diff.retained_doc_ids == ["doc_B", "doc_C"]
    assert ref_diff.added_doc_ids == ["doc_D"]
    assert ref_diff.removed_doc_ids == ["doc_A"]
    assert ref_diff.jaccard_similarity == 0.5

    legacy_dir = tmp_path / "legacy"
    leg_manifest, _, _ = _create_synthetic_run(legacy_dir, ref_status="unavailable_legacy_output")
    cmp_legacy = compare_review_runs(base_manifest, leg_manifest)
    assert cmp_legacy.reference_diff.status == "unavailable"
    assert cmp_legacy.reference_diff.jaccard_similarity is None


def _assert_no_path_leak_recursive(node: Any, forbidden_substrings: list[str]) -> None:
    """递归遍历字典/列表，断言所有字符串叶子节点都不含敏感路径子串。"""
    if isinstance(node, dict):
        for k, v in node.items():
            _assert_no_path_leak_recursive(k, forbidden_substrings)
            _assert_no_path_leak_recursive(v, forbidden_substrings)
    elif isinstance(node, (list, tuple, set)):
        for item in node:
            _assert_no_path_leak_recursive(item, forbidden_substrings)
    elif isinstance(node, str):
        for sub in forbidden_substrings:
            assert sub.lower() not in node.lower(), f"发现泄漏的敏感路径子串 '{sub}' 存在于字段内容: '{node}'"


def test_recursive_scan_entire_to_dict_no_path_leak(tmp_path):
    """深度递归扫描校验报告与比对报告的全部 to_dict() 叶子节点，断言物理绝对路径绝不泄漏。"""
    manifest_path, _, _ = _create_synthetic_run(tmp_path)
    val_report = validate_review_run(manifest_path)
    cmp_report = compare_review_runs(manifest_path, manifest_path)

    forbidden = [str(tmp_path), "/home/", "/root/", "/tmp/", "/etc/", "C:\\", "D:\\"]
    _assert_no_path_leak_recursive(val_report.to_dict(), forbidden)
    _assert_no_path_leak_recursive(cmp_report.to_dict(), forbidden)


def test_malicious_path_injection_in_legal_fields_and_objects(tmp_path):
    """在合法字段（topic/run_id/doc_id/heading/model/unknown_key）中注入绝对路径，断言报告对象本身及 to_dict 绝无路径泄露。"""
    base_dir = tmp_path / "inj_base"
    cand_dir = tmp_path / "inj_cand"

    unix_path = "/home/dudu/private/secret.key"
    win_path = r"C:\Users\Administrator\secrets.txt"

    base_manifest, _, _ = _create_synthetic_run(base_dir, run_id="base1")
    cand_manifest, _, _ = _create_synthetic_run(cand_dir, run_id="cand1")

    # 注入合法字段与结构中包含路径
    with open(base_manifest, "r", encoding="utf-8") as f:
        data = json.load(f)
    data["topic_intent"]["topic"] = f"主题包含 {unix_path}"
    data["run_meta"]["run_id"] = f"run_{win_path}"
    data["model_and_prompts"]["model_name"] = f"model_{unix_path}"
    data["cited_references"][0]["doc_id"] = f"doc_{unix_path}"
    data["section_evidence"][0]["heading"] = f"章节_{win_path}"
    data[f"injected_{unix_path}"] = "val"
    with open(base_manifest, "w", encoding="utf-8") as f:
        json.dump(data, f)

    with open(cand_manifest, "r", encoding="utf-8") as f:
        cdata = json.load(f)
    cdata["topic_intent"]["topic"] = f"主题包含 {unix_path}"
    cdata["run_meta"]["run_id"] = f"run_{unix_path}"
    cdata["model_and_prompts"]["model_name"] = f"model_{win_path}"
    cdata["cited_references"][0]["doc_id"] = f"doc_{unix_path}"
    cdata["section_evidence"][0]["heading"] = f"章节_{win_path}"
    with open(cand_manifest, "w", encoding="utf-8") as f:
        json.dump(cdata, f)

    val_report = validate_review_run(base_manifest)
    cmp_report = compare_review_runs(base_manifest, cand_manifest)

    # 检查 report 属性本身
    assert "/home/dudu/private" not in val_report.topic
    assert "C:\\Users\\Administrator" not in val_report.run_id

    forbidden = ["/home/dudu/private", "C:\\Users\\Administrator", "/etc/passwd"]
    _assert_no_path_leak_recursive(val_report.to_dict(), forbidden)
    _assert_no_path_leak_recursive(cmp_report.to_dict(), forbidden)


def test_real_phase1a_manifest_compatibility(tmp_path):
    """端到端验证：使用 Phase 1A write_review_manifest 真实生成清单，再使用 validate_review_run 校验，确保真实自洽。"""
    from litreview.models import Outline

    md_path = str(tmp_path / "综述_真实测试.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("# 综述真实测试\n\n正文引用 [1]。\n\n## 参考文献\n[1] 文献1\n")

    doc = ReviewDoc(
        title="综述真实测试",
        topic="真实主题测试",
        sections=[SectionDraft(heading="引言", markdown="正文引用 [1]。", cited_doc_ids=["doc_real_1"])],
        references=["[1] 文献1"],
        reference_doc_ids=["doc_real_1"],
        doc_count=1,
        evidence_count=1,
        quality_blocking=False,
        quality_issues=[QualityIssue(code="WARN_TEST", severity="warning", message="测试告警", section="引言").__dict__],
    )

    manifest_input = ManifestInput(
        output_md_path=md_path,
        review=doc,
        outline=Outline(title="真实主题测试", topic="真实主题测试", sections=[]),
        evidence_by_heading={"引言": []},
        paper_meta={
            "doc_real_1": {
                "title": "Real Paper Title", "title_zh": "", "title_en": "Real Paper Title",
                "doi": "10.1000/real", "authors": "Real Author", "journal": "Nature",
                "year": "2024", "volume": "1", "issue": "1", "pages": "1-2",
                "doc_type": "article", "cite_key": "doc_real_1"
            }
        },
        run_context=RunContext(
            topic="真实主题测试",
            run_id="real-run-001",
            trigger_source="cli",
            model_name="deepseek-chat",
            analysis_model="deepseek-chat",
            reranker_model="Qwen3-Reranker-8B",
            target_words=8000,
            target_lang="zh",
            prompts=PromptSnapshot("v1", "v2", "v1"),
            retrieval=RetrievalSnapshot(20, 32, 8000, 1500, 2500, "Qwen3-Reranker-8B"),
            duration_seconds=10,
            corpus_fingerprint="c" * 64,
        ),
        docx_path=None,
    )

    manifest_path = write_review_manifest(manifest_input)
    assert os.path.exists(manifest_path)

    # 校验该清单
    report = validate_review_run(manifest_path)
    assert report.verdict == "WARN"  # 存在非阻塞 quality issue
    assert report.schema_version == 1
    assert report.topic == "真实主题测试"
    assert report.markdown_sha256_match is True
    assert report.reference_mapping_status == "complete"
    assert report.cited_references_count == 1
def test_compare_review_runs_on_invalid_json_inputs_never_crashes(tmp_path):
    """测试比对双方输入非法 JSON（如根为列表 [] 或损坏对象）时，安全返回 FAIL 且绝不抛出任何异常。"""
    base_invalid = tmp_path / "base_invalid.json"
    cand_invalid = tmp_path / "cand_invalid.json"

    # 根节点为 []
    with open(base_invalid, "w", encoding="utf-8") as f:
        f.write("[]")
    with open(cand_invalid, "w", encoding="utf-8") as f:
        f.write("[]")

    # 绝不能抛出 AttributeError 或其他异常
    cmp_report = compare_review_runs(str(base_invalid), str(cand_invalid))
    assert cmp_report.verdict == "FAIL"
    assert cmp_report.base_validation.verdict == "FAIL"
    assert cmp_report.candidate_validation.verdict == "FAIL"
    assert any(iss.code == "BASE_VALIDATION_FAILED" for iss in cmp_report.issues)
    assert any(iss.code == "CANDIDATE_VALIDATION_FAILED" for iss in cmp_report.issues)

    forbidden = [str(tmp_path), "/home/", "/root/", "/tmp/"]
    _assert_no_path_leak_recursive(cmp_report.to_dict(), forbidden)

    # 结构错误字典
    struct_bad = tmp_path / "struct_bad.json"
    with open(struct_bad, "w", encoding="utf-8") as f:
        f.write('{"schema_version": 1}')
    cmp_report2 = compare_review_runs(str(struct_bad), str(base_invalid))
    assert cmp_report2.verdict == "FAIL"
    assert cmp_report2.base_validation.verdict == "FAIL"


def test_reference_identity_replacement_warns_and_creates_delta(tmp_path):
    """测试引用文献等量替换 ({A,B} -> {C,D}, Jaccard=0) 时，比对判定为 WARN（非 PASS、非 FAIL）并产生 references delta。"""
    base_dir = tmp_path / "ref_base"
    cand_dir = tmp_path / "ref_cand"
    base_manifest, _, _ = _create_synthetic_run(base_dir, run_id="b1", doc_ids=["doc_A", "doc_B"])
    cand_manifest, _, _ = _create_synthetic_run(cand_dir, run_id="c1", doc_ids=["doc_C", "doc_D"])

    cmp_report = compare_review_runs(base_manifest, cand_manifest)
    assert cmp_report.verdict == "WARN"
    assert cmp_report.reference_diff.status == "complete"
    assert cmp_report.reference_diff.jaccard_similarity == 0.0
    assert cmp_report.reference_diff.added_doc_ids == ["doc_C", "doc_D"]
    assert cmp_report.reference_diff.removed_doc_ids == ["doc_A", "doc_B"]

    ref_delta = next((d for d in cmp_report.deltas if d.category == "references"), None)
    assert ref_delta is not None
    assert "Jaccard: 0.0000" in ref_delta.message


def test_section_heading_rename_or_added_removed_warns_and_creates_delta(tmp_path):
    """测试章节 heading 重命名（产生 added 与 removed 章节）时，判定为 WARN 并生成 sections delta。"""
    base_dir = tmp_path / "sec_base"
    cand_dir = tmp_path / "sec_cand"
    base_manifest, _, _ = _create_synthetic_run(base_dir, run_id="b1")
    cand_manifest, _, _ = _create_synthetic_run(cand_dir, run_id="c1")

    # 修改 candidate 的章节名称
    with open(cand_manifest, "r", encoding="utf-8") as f:
        cdata = json.load(f)
    cdata["section_evidence"][0]["heading"] = "第一章 背景与现状"
    with open(cand_manifest, "w", encoding="utf-8") as f:
        json.dump(cdata, f)

    cmp_report = compare_review_runs(base_manifest, cand_manifest)
    assert cmp_report.verdict == "WARN"
    sec_delta = next((d for d in cmp_report.deltas if d.category == "sections"), None)
    assert sec_delta is not None
    assert "章节结构演进" in sec_delta.message


def test_quality_gate_severity_contradiction_and_invalid_severity(tmp_path):
    """测试 quality_blocking=false 但包含 severity=blocking 判定为自相矛盾 FAIL；非法 severity 值也判定为 FAIL。"""
    manifest_path, _, _ = _create_synthetic_run(tmp_path)

    # 1. 自相矛盾：quality_blocking=False 但 severity=blocking
    with open(manifest_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    data["quality_gate"]["quality_blocking"] = False
    data["quality_gate"]["quality_issues"] = [{
        "code": "UNKNOWN_CITATION",
        "severity": "blocking",
        "message": "阻塞错误",
        "section": "第一章"
    }]
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(data, f)

    report1 = validate_review_run(manifest_path)
    assert report1.verdict == "FAIL"
    assert any(iss.code == "QUALITY_GATE_CONTRADICTION" for iss in report1.issues)

    # 2. 非法 severity 值
    data["quality_gate"]["quality_issues"][0]["severity"] = "fatal"
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    report2 = validate_review_run(manifest_path)
    assert report2.verdict == "FAIL"
    assert any(iss.code == "INVALID_SEVERITY" for iss in report2.issues)


def test_evidence_status_invariants_for_empty_and_unavailable(tmp_path):
    """测试 evidence_status 不变量：empty 必须引文与 chunks 均空；unavailable_* 必须 chunks 为空。"""
    manifest_path, _, _ = _create_synthetic_run(tmp_path)

    # 1. empty 但 chunks 非空
    with open(manifest_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    data["section_evidence"][0]["evidence_status"] = "empty"
    data["section_evidence"][0]["cited_doc_ids"] = []
    # 保留原有的 chunks
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(data, f)

    report1 = validate_review_run(manifest_path)
    assert report1.verdict == "FAIL"
    assert any(iss.code == "EVIDENCE_STATUS_INVARIANT_VIOLATED" for iss in report1.issues)

    # 2. unavailable_legacy_checkpoint 但 chunks 非空
    data["section_evidence"][0]["evidence_status"] = "unavailable_legacy_checkpoint"
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    report2 = validate_review_run(manifest_path)
    assert report2.verdict == "FAIL"
    assert any(iss.code == "EVIDENCE_STATUS_INVARIANT_VIOLATED" for iss in report2.issues)


def test_cli_validate_and_compare_json_exit_codes_and_no_path_leak(tmp_path):
    """测试真实 CLI 命令：--json 纯净输出、退出码（0/1/2）以及绝对路径绝不泄漏。"""
    pass_dir = tmp_path / "cli_pass"
    warn_dir = tmp_path / "cli_warn"
    fail_dir = tmp_path / "cli_fail"

    pass_manifest, _, _ = _create_synthetic_run(pass_dir, run_id="p1")
    warn_manifest, _, _ = _create_synthetic_run(warn_dir, run_id="w1", git_dirty=True)
    fail_manifest, md_fail, _ = _create_synthetic_run(fail_dir, run_id="f1")
    with open(md_fail, "a", encoding="utf-8") as f:
        f.write("\ntampered\n")

    py_bin = sys.executable

    # 1. validate-run PASS 测试 (退出码 0)
    proc_val_pass = subprocess.run(
        [py_bin, "review.py", "validate-run", pass_manifest, "--json"],
        capture_output=True, text=True
    )
    assert proc_val_pass.returncode == 0
    val_json = json.loads(proc_val_pass.stdout)
    assert val_json["verdict"] == "PASS"
    _assert_no_path_leak_recursive(val_json, [str(tmp_path)])

    # 2. validate-run WARN 测试 (退出码 1)
    proc_val_warn = subprocess.run(
        [py_bin, "review.py", "validate-run", warn_manifest, "--json"],
        capture_output=True, text=True
    )
    assert proc_val_warn.returncode == 1
    warn_json = json.loads(proc_val_warn.stdout)
    assert warn_json["verdict"] == "WARN"

    # 3. validate-run FAIL 测试 (退出码 2)
    proc_val_fail = subprocess.run(
        [py_bin, "review.py", "validate-run", fail_manifest, "--json"],
        capture_output=True, text=True
    )
    assert proc_val_fail.returncode == 2
    fail_json = json.loads(proc_val_fail.stdout)
    assert fail_json["verdict"] == "FAIL"

    # 4. compare-runs JSON 与退出码测试
    proc_cmp_pass = subprocess.run(
        [py_bin, "review.py", "compare-runs", pass_manifest, pass_manifest, "--json"],
        capture_output=True, text=True
    )
    assert proc_cmp_pass.returncode == 0
    cmp_json = json.loads(proc_cmp_pass.stdout)
    assert cmp_json["verdict"] == "PASS"
    assert cmp_json["same_topic"] is True

    # 5. 人读输出模式测试
    proc_val_human = subprocess.run(
        [py_bin, "review.py", "validate-run", pass_manifest],
        capture_output=True, text=True
    )
    assert proc_val_human.returncode == 0
    assert "综述运行校验报告: PASS" in proc_val_human.stdout


def test_db_repair_production_cli_regression(tmp_path, monkeypatch):
    """回归测试：确保 review.py db-repair-production 命令正常输出 JSON，未发生命令行为破坏。"""
    from tests.test_db_repair import _production_repair_fixture
    test_db = str(tmp_path / "test_prod.db")
    _production_repair_fixture(test_db)
    monkeypatch.setenv("DB_PATH", test_db)

    import review
    monkeypatch.setattr(review.settings, "DB_PATH", test_db)
    monkeypatch.setattr(review.settings, "EXCEL_OUTPUT_PATH", str(tmp_path / "test_kb.xlsx"))

    # 调用真实 cmd_db_repair_production
    class Args:
        apply = False
        backup_out = None

    # 模拟 capture stdout
    from unittest.mock import patch
    with patch.object(review.console, "print_json") as mock_print:
        review.cmd_db_repair_production(Args())
        assert mock_print.called
