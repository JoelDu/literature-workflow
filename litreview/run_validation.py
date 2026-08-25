"""Review Run Validation & Comparison 深度模块 (Phase 1B 规范实现)。

提供综述运行清单 (.manifest.json) 的强类型契约校验与基线比对功能。
全程 0 LLM、0 网络、0 数据库、0 Git 命令，严格只读。
绝不泄露宿主机物理绝对路径，仅暴露 Manifest/Artifact 文件基名与逻辑位置。
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
import re
from typing import Any, Mapping, Sequence


HEX64_REGEX = re.compile(r"^[a-f0-9]{64}$")

ALLOWLIST_PAPER_META_KEYS = (
    "title", "title_zh", "title_en", "doi", "authors", "journal",
    "year", "volume", "issue", "pages", "doc_type", "cite_key"
)

# Schema Version 1 严格结构与键白名单
SCHEMA_V1_REQUIRED_TOP_KEYS = {
    "schema_version", "run_meta", "topic_intent", "timestamps",
    "model_and_prompts", "retrieval_config", "reference_mapping_status",
    "cited_references", "section_evidence", "quality_gate", "artifacts", "env_fingerprint"
}

ALLOWED_RUN_META_KEYS = {"run_id", "trigger_source"}
ALLOWED_TOPIC_INTENT_KEYS = {"topic", "focus", "thesis", "research_gap", "mainline_logic", "target_lang"}
ALLOWED_TIMESTAMPS_KEYS = {"generated_at", "duration_seconds"}
ALLOWED_MODEL_AND_PROMPTS_KEYS = {"model_name", "analysis_model", "reranker_model", "prompt_versions"}
ALLOWED_PROMPT_VERSIONS_KEYS = {"outline_prompt_version", "section_prompt_version", "tldr_prompt_version"}
ALLOWED_RETRIEVAL_CONFIG_KEYS = {"review_top_k", "evidence_n", "target_words", "section_words_min", "section_words_max", "reranker_model"}
ALLOWED_QUALITY_GATE_KEYS = {"quality_blocking", "quality_issues", "doc_count", "evidence_count"}
ALLOWED_QUALITY_ISSUE_KEYS = {"code", "severity", "message", "section"}
ALLOWED_ARTIFACTS_KEYS = {"output_md_filename", "markdown_sha256", "output_docx_filename", "docx_sha256"}
ALLOWED_ENV_FINGERPRINT_KEYS = {"git_commit", "git_dirty", "corpus_fingerprint"}

ALLOWED_CITED_REFERENCE_KEYS = {
    "doc_id", "title", "title_zh", "title_en", "doi", "authors",
    "journal", "year", "volume", "issue", "pages", "doc_type", "cite_key", "meta_hash"
}

ALLOWED_SECTION_EVIDENCE_KEYS = {
    "heading", "evidence_status", "cited_doc_ids", "chunks"
}

ALLOWED_CHUNK_KEYS = {
    "chunk_id", "doc_id", "score", "summary_hash", "retrieval_score", "section_title"
}


def _sanitize_path_in_str(s: Any) -> str:
    """对任意输入中的 Unix 与 Windows 绝对路径及相对逃逸路径进行严格脱敏替换。"""
    if s is None:
        return ""
    txt = str(s)
    # 替换 Windows 绝对路径: C:\foo\bar -> bar
    txt = re.sub(r'[a-zA-Z]:[\\/](?:[^ \t\n\r"\'<>|]+[\\/])+([^ \t\n\r"\'<>|]+)', r'\1', txt)
    # 替换 Unix 绝对路径: /home/user/file -> file
    txt = re.sub(r'/(?:[^ \t\n\r"\'<>|/]+/)+([^ \t\n\r"\'<>|/]+)', r'\1', txt)
    # 替换残留根路径
    txt = re.sub(r'/(?:home|root|tmp|var|etc)(?:/[^ \t\n\r"\'<>|/]*)*', r'[path]', txt)
    txt = re.sub(r'[a-zA-Z]:[\\/][^ \t\n\r"\'<>|]*', r'[path]', txt)
    # 替换相对路径逃逸 ../foo -> foo
    txt = re.sub(r'(?:\.\.[\\/])+', '', txt)
    return txt


@dataclass(frozen=True)
class ValidationIssue:
    """校验或比对中发现的问题或告警项。"""
    code: str
    severity: str        # "FAIL" | "WARN"
    message: str
    location: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": _sanitize_path_in_str(self.code),
            "severity": self.severity,
            "message": _sanitize_path_in_str(self.message),
            "location": _sanitize_path_in_str(self.location),
        }


@dataclass(frozen=True)
class RunValidationReport:
    """单次综述运行清单校验报告（强类型冻结数据模型）。"""
    verdict: str                        # "PASS" | "WARN" | "FAIL"
    manifest_filename: str              # 仅包含清单基名，无绝对路径
    schema_version: int | None
    run_id: str
    topic: str
    generated_at: str
    duration_seconds: int
    model_name: str
    markdown_filename: str
    markdown_sha256_match: bool | None
    docx_filename: str | None
    docx_sha256_match: bool | None
    reference_mapping_status: str
    cited_references_count: int
    section_count: int
    evidence_count: int
    quality_blocking: bool
    issues: list[ValidationIssue] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "manifest_filename": _sanitize_path_in_str(self.manifest_filename),
            "schema_version": self.schema_version,
            "run_id": _sanitize_path_in_str(self.run_id),
            "topic": _sanitize_path_in_str(self.topic),
            "generated_at": _sanitize_path_in_str(self.generated_at),
            "duration_seconds": self.duration_seconds,
            "model_name": _sanitize_path_in_str(self.model_name),
            "markdown_filename": _sanitize_path_in_str(self.markdown_filename),
            "markdown_sha256_match": self.markdown_sha256_match,
            "docx_filename": _sanitize_path_in_str(self.docx_filename) if self.docx_filename is not None else None,
            "docx_sha256_match": self.docx_sha256_match,
            "reference_mapping_status": _sanitize_path_in_str(self.reference_mapping_status),
            "cited_references_count": self.cited_references_count,
            "section_count": self.section_count,
            "evidence_count": self.evidence_count,
            "quality_blocking": self.quality_blocking,
            "issues": [issue.to_dict() for issue in self.issues],
        }


@dataclass(frozen=True)
class ComparisonDelta:
    """运行对比的差异记录。"""
    category: str        # "model_and_prompts" | "retrieval_config" | "env_fingerprint" | "artifacts" | "sections" | "metrics"
    name: str
    base_value: Any
    candidate_value: Any
    is_regression: bool
    message: str

    def to_dict(self) -> dict[str, Any]:
        safe_b = _sanitize_path_in_str(self.base_value) if isinstance(self.base_value, str) else self.base_value
        safe_c = _sanitize_path_in_str(self.candidate_value) if isinstance(self.candidate_value, str) else self.candidate_value
        return {
            "category": _sanitize_path_in_str(self.category),
            "name": _sanitize_path_in_str(self.name),
            "base_value": safe_b,
            "candidate_value": safe_c,
            "is_regression": self.is_regression,
            "message": _sanitize_path_in_str(self.message),
        }


@dataclass(frozen=True)
class ReferenceDiff:
    """引用文献差异集合分析。"""
    status: str                         # "complete" | "unavailable"
    jaccard_similarity: float | None
    base_count: int
    candidate_count: int
    retained_doc_ids: list[str]
    added_doc_ids: list[str]
    removed_doc_ids: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": _sanitize_path_in_str(self.status),
            "jaccard_similarity": round(self.jaccard_similarity, 4) if self.jaccard_similarity is not None else None,
            "base_count": self.base_count,
            "candidate_count": self.candidate_count,
            "retained_doc_ids": [_sanitize_path_in_str(d) for d in self.retained_doc_ids],
            "added_doc_ids": [_sanitize_path_in_str(d) for d in self.added_doc_ids],
            "removed_doc_ids": [_sanitize_path_in_str(d) for d in self.removed_doc_ids],
        }


@dataclass(frozen=True)
class SectionDiff:
    """章节结构与证据状态对比。"""
    heading: str
    status: str                         # "retained" | "added" | "removed"
    base_evidence_status: str | None
    candidate_evidence_status: str | None
    base_chunks_count: int
    candidate_chunks_count: int
    is_regression: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "heading": _sanitize_path_in_str(self.heading),
            "status": _sanitize_path_in_str(self.status),
            "base_evidence_status": _sanitize_path_in_str(self.base_evidence_status) if self.base_evidence_status is not None else None,
            "candidate_evidence_status": _sanitize_path_in_str(self.candidate_evidence_status) if self.candidate_evidence_status is not None else None,
            "base_chunks_count": self.base_chunks_count,
            "candidate_chunks_count": self.candidate_chunks_count,
            "is_regression": self.is_regression,
        }


@dataclass(frozen=True)
class RunComparisonReport:
    """两次综述运行基线对比报告（强类型冻结数据模型）。"""
    verdict: str                        # "PASS" | "WARN" | "FAIL"
    base_manifest_filename: str
    candidate_manifest_filename: str
    base_validation: RunValidationReport
    candidate_validation: RunValidationReport
    topic: str
    same_topic: bool
    deltas: list[ComparisonDelta] = field(default_factory=list)
    section_diffs: list[SectionDiff] = field(default_factory=list)
    reference_diff: ReferenceDiff = field(default_factory=lambda: ReferenceDiff(
        status="unavailable", jaccard_similarity=None, base_count=0, candidate_count=0,
        retained_doc_ids=[], added_doc_ids=[], removed_doc_ids=[]
    ))
    issues: list[ValidationIssue] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "base_manifest_filename": _sanitize_path_in_str(self.base_manifest_filename),
            "candidate_manifest_filename": _sanitize_path_in_str(self.candidate_manifest_filename),
            "base_validation": self.base_validation.to_dict(),
            "candidate_validation": self.candidate_validation.to_dict(),
            "topic": _sanitize_path_in_str(self.topic),
            "same_topic": self.same_topic,
            "deltas": [d.to_dict() for d in self.deltas],
            "section_diffs": [s.to_dict() for s in self.section_diffs],
            "reference_diff": self.reference_diff.to_dict(),
            "issues": [issue.to_dict() for issue in self.issues],
        }


def _calc_file_sha256(file_path: str) -> str | None:
    """计算物理文件的 64 位 SHA256 哈希值；只读读取。"""
    if not file_path or not os.path.exists(file_path) or not os.path.isfile(file_path):
        return None
    try:
        h = hashlib.sha256()
        with open(file_path, "rb") as f:
            while chunk := f.read(65536):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return None


def _is_safe_basename(filename: Any) -> bool:
    """判断文件名是否为纯安全文件名（拒绝绝对路径、路径分隔符与目录穿越）。"""
    if not isinstance(filename, str) or not filename.strip():
        return False
    if os.path.basename(filename) != filename:
        return False
    if "/" in filename or "\\" in filename or ".." in filename:
        return False
    return True


def _check_hash_format(hash_str: Any, location: str, issues: list[ValidationIssue], optional: bool = False) -> bool:
    """校验 64 位十六进制 SHA256 格式。"""
    if optional and (hash_str is None or hash_str == ""):
        return True
    if not isinstance(hash_str, str) or not HEX64_REGEX.match(hash_str):
        issues.append(ValidationIssue(
            code="INVALID_HASH_FORMAT",
            severity="FAIL",
            message="哈希格式非法，非 64 位小写十六进制 SHA256 字符串",
            location=location,
        ))
        return False
    return True


def _validate_dict_keys(
    data: Any,
    allowed_keys: set[str],
    required_keys: set[str],
    location: str,
    issues: list[ValidationIssue]
) -> bool:
    """校验字典的必填键、允许键与类型。"""
    if not isinstance(data, dict):
        issues.append(ValidationIssue(
            code="SCHEMA_TYPE_MISMATCH",
            severity="FAIL",
            message=f"节点期望为字典对象，实际为 {type(data).__name__}",
            location=location,
        ))
        return False

    actual_keys = set(data.keys())
    missing = required_keys - actual_keys
    if missing:
        for mk in sorted(missing):
            issues.append(ValidationIssue(
                code="MISSING_REQUIRED_FIELD",
                severity="FAIL",
                message=f"缺少必填字段 '{mk}'",
                location=f"{location}.{mk}" if location else mk,
            ))

    unknown = actual_keys - allowed_keys
    if unknown:
        for uk in sorted(unknown):
            issues.append(ValidationIssue(
                code="UNKNOWN_FIELDS",
                severity="FAIL",
                message=f"发现未知或非白名单字段 '{_sanitize_path_in_str(uk)}'",
                location=f"{location}.{_sanitize_path_in_str(uk)}" if location else _sanitize_path_in_str(uk),
            ))

    return len(missing) == 0 and len(unknown) == 0


def validate_review_run(manifest_path: str) -> RunValidationReport:
    """严格只读校验单个综述运行清单文件的完整性、哈希真实性与契约合规性。

    对任何损坏或非法输入保证安全返回 FAIL 报告，绝对不抛出异常。
    """
    manifest_basename = _sanitize_path_in_str(os.path.basename(manifest_path)) if isinstance(manifest_path, str) and manifest_path else "unknown.manifest.json"
    manifest_dir = os.path.dirname(os.path.abspath(manifest_path)) if isinstance(manifest_path, str) and manifest_path else ""

    issues: list[ValidationIssue] = []

    # 1. 基础文件读取与 JSON 解析
    if not manifest_path or not os.path.exists(manifest_path) or not os.path.isfile(manifest_path):
        return RunValidationReport(
            verdict="FAIL",
            manifest_filename=manifest_basename,
            schema_version=None,
            run_id="",
            topic="",
            generated_at="",
            duration_seconds=0,
            model_name="",
            markdown_filename="",
            markdown_sha256_match=None,
            docx_filename=None,
            docx_sha256_match=None,
            reference_mapping_status="empty",
            cited_references_count=0,
            section_count=0,
            evidence_count=0,
            quality_blocking=False,
            issues=[ValidationIssue(
                code="MANIFEST_NOT_FOUND",
                severity="FAIL",
                message="清单文件不存在",
                location="manifest",
            )],
        )

    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest_data = json.load(f)
    except Exception:
        return RunValidationReport(
            verdict="FAIL",
            manifest_filename=manifest_basename,
            schema_version=None,
            run_id="",
            topic="",
            generated_at="",
            duration_seconds=0,
            model_name="",
            markdown_filename="",
            markdown_sha256_match=None,
            docx_filename=None,
            docx_sha256_match=None,
            reference_mapping_status="empty",
            cited_references_count=0,
            section_count=0,
            evidence_count=0,
            quality_blocking=False,
            issues=[ValidationIssue(
                code="JSON_PARSE_ERROR",
                severity="FAIL",
                message="清单 JSON 格式无效，无法解析",
                location="manifest",
            )],
        )

    if not isinstance(manifest_data, dict):
        return RunValidationReport(
            verdict="FAIL",
            manifest_filename=manifest_basename,
            schema_version=None,
            run_id="",
            topic="",
            generated_at="",
            duration_seconds=0,
            model_name="",
            markdown_filename="",
            markdown_sha256_match=None,
            docx_filename=None,
            docx_sha256_match=None,
            reference_mapping_status="empty",
            cited_references_count=0,
            section_count=0,
            evidence_count=0,
            quality_blocking=False,
            issues=[ValidationIssue(
                code="INVALID_ROOT_STRUCTURE",
                severity="FAIL",
                message="清单根节点必须为 JSON 对象",
                location="manifest",
            )],
        )

    # 2. 校验顶层键白名单与必填项
    _validate_dict_keys(manifest_data, SCHEMA_V1_REQUIRED_TOP_KEYS, SCHEMA_V1_REQUIRED_TOP_KEYS, "", issues)

    # 3. 严格校验 schema_version (不能为 bool，必须为 int 且 == 1)
    raw_sv = manifest_data.get("schema_version")
    if type(raw_sv) is not int or isinstance(raw_sv, bool) or raw_sv != 1:
        issues.append(ValidationIssue(
            code="UNKNOWN_SCHEMA_VERSION",
            severity="FAIL",
            message="不支持或非法的 Schema 版本 (仅支持 schema_version=1)",
            location="schema_version",
        ))
        schema_version = None
    else:
        schema_version = 1

    # 4. 校验 run_meta
    run_meta = manifest_data.get("run_meta")
    run_id = ""
    if isinstance(run_meta, dict):
        _validate_dict_keys(run_meta, ALLOWED_RUN_META_KEYS, ALLOWED_RUN_META_KEYS, "run_meta", issues)
        if isinstance(run_meta.get("run_id"), str):
            run_id = _sanitize_path_in_str(run_meta["run_id"])
        elif "run_id" in run_meta:
            issues.append(ValidationIssue(code="SCHEMA_TYPE_MISMATCH", severity="FAIL", message="run_id 必须为字符串", location="run_meta.run_id"))
        if not isinstance(run_meta.get("trigger_source"), str) and "trigger_source" in run_meta:
            issues.append(ValidationIssue(code="SCHEMA_TYPE_MISMATCH", severity="FAIL", message="trigger_source 必须为字符串", location="run_meta.trigger_source"))
    else:
        issues.append(ValidationIssue(code="SCHEMA_TYPE_MISMATCH", severity="FAIL", message="run_meta 必须为字典对象", location="run_meta"))

    # 5. 校验 topic_intent
    topic_intent = manifest_data.get("topic_intent")
    topic = ""
    if isinstance(topic_intent, dict):
        _validate_dict_keys(topic_intent, ALLOWED_TOPIC_INTENT_KEYS, ALLOWED_TOPIC_INTENT_KEYS, "topic_intent", issues)
        if isinstance(topic_intent.get("topic"), str):
            topic = _sanitize_path_in_str(topic_intent["topic"])
        elif "topic" in topic_intent:
            issues.append(ValidationIssue(code="SCHEMA_TYPE_MISMATCH", severity="FAIL", message="topic 必须为字符串", location="topic_intent.topic"))
        for k in ("focus", "thesis", "research_gap", "mainline_logic", "target_lang"):
            if k in topic_intent and not isinstance(topic_intent[k], str):
                issues.append(ValidationIssue(code="SCHEMA_TYPE_MISMATCH", severity="FAIL", message=f"{k} 必须为字符串", location=f"topic_intent.{k}"))
    else:
        issues.append(ValidationIssue(code="SCHEMA_TYPE_MISMATCH", severity="FAIL", message="topic_intent 必须为字典对象", location="topic_intent"))

    # 6. 校验 timestamps
    timestamps = manifest_data.get("timestamps")
    generated_at = ""
    duration_seconds = 0
    if isinstance(timestamps, dict):
        _validate_dict_keys(timestamps, ALLOWED_TIMESTAMPS_KEYS, ALLOWED_TIMESTAMPS_KEYS, "timestamps", issues)
        if isinstance(timestamps.get("generated_at"), str):
            generated_at = _sanitize_path_in_str(timestamps["generated_at"])
        elif "generated_at" in timestamps:
            issues.append(ValidationIssue(code="SCHEMA_TYPE_MISMATCH", severity="FAIL", message="generated_at 必须为字符串", location="timestamps.generated_at"))
        raw_dur = timestamps.get("duration_seconds")
        if type(raw_dur) is int and not isinstance(raw_dur, bool) and raw_dur >= 0:
            duration_seconds = raw_dur
        elif "duration_seconds" in timestamps:
            issues.append(ValidationIssue(code="SCHEMA_TYPE_MISMATCH", severity="FAIL", message="duration_seconds 必须为非负整数", location="timestamps.duration_seconds"))
    else:
        issues.append(ValidationIssue(code="SCHEMA_TYPE_MISMATCH", severity="FAIL", message="timestamps 必须为字典对象", location="timestamps"))

    # 7. 校验 model_and_prompts
    model_and_prompts = manifest_data.get("model_and_prompts")
    model_name = ""
    if isinstance(model_and_prompts, dict):
        _validate_dict_keys(model_and_prompts, ALLOWED_MODEL_AND_PROMPTS_KEYS, ALLOWED_MODEL_AND_PROMPTS_KEYS, "model_and_prompts", issues)
        if isinstance(model_and_prompts.get("model_name"), str):
            model_name = _sanitize_path_in_str(model_and_prompts["model_name"])
        elif "model_name" in model_and_prompts:
            issues.append(ValidationIssue(code="SCHEMA_TYPE_MISMATCH", severity="FAIL", message="model_name 必须为字符串", location="model_and_prompts.model_name"))
        for k in ("analysis_model", "reranker_model"):
            if k in model_and_prompts and not isinstance(model_and_prompts[k], str):
                issues.append(ValidationIssue(code="SCHEMA_TYPE_MISMATCH", severity="FAIL", message=f"{k} 必须为字符串", location=f"model_and_prompts.{k}"))

        pv = model_and_prompts.get("prompt_versions")
        if isinstance(pv, dict):
            _validate_dict_keys(pv, ALLOWED_PROMPT_VERSIONS_KEYS, ALLOWED_PROMPT_VERSIONS_KEYS, "model_and_prompts.prompt_versions", issues)
            for p_k in ALLOWED_PROMPT_VERSIONS_KEYS:
                if p_k in pv and not isinstance(pv[p_k], str):
                    issues.append(ValidationIssue(code="SCHEMA_TYPE_MISMATCH", severity="FAIL", message=f"{p_k} 必须为字符串", location=f"model_and_prompts.prompt_versions.{p_k}"))
        elif "prompt_versions" in model_and_prompts:
            issues.append(ValidationIssue(code="SCHEMA_TYPE_MISMATCH", severity="FAIL", message="prompt_versions 必须为字典对象", location="model_and_prompts.prompt_versions"))
    else:
        issues.append(ValidationIssue(code="SCHEMA_TYPE_MISMATCH", severity="FAIL", message="model_and_prompts 必须为字典对象", location="model_and_prompts"))

    # 8. 校验 retrieval_config
    retrieval_config = manifest_data.get("retrieval_config")
    if isinstance(retrieval_config, dict):
        _validate_dict_keys(retrieval_config, ALLOWED_RETRIEVAL_CONFIG_KEYS, ALLOWED_RETRIEVAL_CONFIG_KEYS, "retrieval_config", issues)
        for r_k in ("review_top_k", "evidence_n", "target_words", "section_words_min", "section_words_max"):
            if r_k in retrieval_config:
                v = retrieval_config[r_k]
                if type(v) is not int or isinstance(v, bool) or v < 0:
                    issues.append(ValidationIssue(code="SCHEMA_TYPE_MISMATCH", severity="FAIL", message=f"{r_k} 必须为非负整数", location=f"retrieval_config.{r_k}"))
        if "reranker_model" in retrieval_config and not isinstance(retrieval_config["reranker_model"], str):
            issues.append(ValidationIssue(code="SCHEMA_TYPE_MISMATCH", severity="FAIL", message="reranker_model 必须为字符串", location="retrieval_config.reranker_model"))
    else:
        issues.append(ValidationIssue(code="SCHEMA_TYPE_MISMATCH", severity="FAIL", message="retrieval_config 必须为字典对象", location="retrieval_config"))

    # 9. 校验 quality_gate (quality_issues 必须严格为 list[dict] 且每项具有 4 字段)
    quality_gate = manifest_data.get("quality_gate")
    quality_blocking = False
    doc_count = 0
    evidence_count = 0
    if isinstance(quality_gate, dict):
        _validate_dict_keys(quality_gate, ALLOWED_QUALITY_GATE_KEYS, ALLOWED_QUALITY_GATE_KEYS, "quality_gate", issues)
        if isinstance(quality_gate.get("quality_blocking"), bool):
            quality_blocking = quality_gate["quality_blocking"]
            if quality_blocking:
                issues.append(ValidationIssue(code="QUALITY_BLOCKING", severity="FAIL", message="质量门检测存在阻塞性缺陷 (quality_blocking=true)", location="quality_gate.quality_blocking"))
        elif "quality_blocking" in quality_gate:
            issues.append(ValidationIssue(code="SCHEMA_TYPE_MISMATCH", severity="FAIL", message="quality_blocking 必须为布尔值", location="quality_gate.quality_blocking"))

        raw_dc = quality_gate.get("doc_count")
        if type(raw_dc) is int and not isinstance(raw_dc, bool) and raw_dc >= 0:
            doc_count = raw_dc
        elif "doc_count" in quality_gate:
            issues.append(ValidationIssue(code="SCHEMA_TYPE_MISMATCH", severity="FAIL", message="doc_count 必须为非负整数", location="quality_gate.doc_count"))

        raw_ec = quality_gate.get("evidence_count")
        if type(raw_ec) is int and not isinstance(raw_ec, bool) and raw_ec >= 0:
            evidence_count = raw_ec
        elif "evidence_count" in quality_gate:
            issues.append(ValidationIssue(code="SCHEMA_TYPE_MISMATCH", severity="FAIL", message="evidence_count 必须为非负整数", location="quality_gate.evidence_count"))

        q_issues = quality_gate.get("quality_issues")
        if isinstance(q_issues, list):
            for qi_idx, qi_item in enumerate(q_issues):
                if not isinstance(qi_item, dict):
                    issues.append(ValidationIssue(code="SCHEMA_TYPE_MISMATCH", severity="FAIL", message=f"quality_issues[{qi_idx}] 必须为字典对象", location=f"quality_gate.quality_issues[{qi_idx}]"))
                    continue
                _validate_dict_keys(qi_item, ALLOWED_QUALITY_ISSUE_KEYS, ALLOWED_QUALITY_ISSUE_KEYS, f"quality_gate.quality_issues[{qi_idx}]", issues)
                for q_k in ALLOWED_QUALITY_ISSUE_KEYS:
                    if q_k in qi_item and not isinstance(qi_item[q_k], str):
                        issues.append(ValidationIssue(code="SCHEMA_TYPE_MISMATCH", severity="FAIL", message=f"quality_issues[{qi_idx}].{q_k} 必须为字符串", location=f"quality_gate.quality_issues[{qi_idx}].{q_k}"))

                raw_sev = qi_item.get("severity")
                if raw_sev not in ("blocking", "warning"):
                    issues.append(ValidationIssue(code="INVALID_SEVERITY", severity="FAIL", message=f"无效的 quality issue 严重级别: '{_sanitize_path_in_str(raw_sev)}'", location=f"quality_gate.quality_issues[{qi_idx}].severity"))
                elif raw_sev == "blocking" and not quality_blocking:
                    issues.append(ValidationIssue(code="QUALITY_GATE_CONTRADICTION", severity="FAIL", message="质量门内部自相矛盾：包含阻塞性缺陷 (severity=blocking) 但 quality_blocking=false", location="quality_gate.quality_blocking"))
                elif raw_sev == "warning" and not quality_blocking:
                    code_val = _sanitize_path_in_str(qi_item.get("code", "UNKNOWN"))
                    sev_val = _sanitize_path_in_str(qi_item.get("severity", "warning"))
                    issues.append(ValidationIssue(code="QUALITY_WARNING", severity="WARN", message=f"质量门告警 [{code_val}] 严重级别: {sev_val}", location=f"quality_gate.quality_issues[{qi_idx}]"))
        elif "quality_issues" in quality_gate:
            issues.append(ValidationIssue(code="SCHEMA_TYPE_MISMATCH", severity="FAIL", message="quality_issues 必须为列表", location="quality_gate.quality_issues"))
    else:
        issues.append(ValidationIssue(code="SCHEMA_TYPE_MISMATCH", severity="FAIL", message="quality_gate 必须为字典对象", location="quality_gate"))

    # 10. 校验 artifacts 与文件真实性
    artifacts = manifest_data.get("artifacts")
    md_filename = ""
    md_sha256_match: bool | None = None
    docx_filename = None
    docx_sha256_match: bool | None = None

    if isinstance(artifacts, dict):
        _validate_dict_keys(artifacts, ALLOWED_ARTIFACTS_KEYS, ALLOWED_ARTIFACTS_KEYS, "artifacts", issues)
        md_fn = artifacts.get("output_md_filename")
        md_sha = artifacts.get("markdown_sha256")
        docx_fn = artifacts.get("output_docx_filename")
        docx_sha = artifacts.get("docx_sha256")

        if not _is_safe_basename(md_fn):
            issues.append(ValidationIssue(code="DANGEROUS_PATH", severity="FAIL", message="Markdown 产物文件名包含危险路径或目录穿越", location="artifacts.output_md_filename"))
        else:
            md_filename = _sanitize_path_in_str(md_fn)
            _check_hash_format(md_sha, "artifacts.markdown_sha256", issues)
            md_real_path = os.path.join(manifest_dir, md_filename)
            if not os.path.exists(md_real_path):
                issues.append(ValidationIssue(code="FILE_MISSING", severity="FAIL", message=f"Markdown 产物文件不存在: {md_filename}", location="artifacts.output_md_filename"))
                md_sha256_match = False
            else:
                calc_hash = _calc_file_sha256(md_real_path)
                md_sha256_match = bool(calc_hash and calc_hash == md_sha)
                if not md_sha256_match:
                    issues.append(ValidationIssue(code="HASH_MISMATCH", severity="FAIL", message=f"Markdown 文件 SHA256 与清单声明不一致 (文件基名: {md_filename})", location="artifacts.markdown_sha256"))

        if docx_fn is not None or docx_sha is not None:
            if docx_fn is None or not _is_safe_basename(docx_fn):
                issues.append(ValidationIssue(code="DANGEROUS_PATH", severity="FAIL", message="DOCX 产物文件名包含危险路径或目录穿越", location="artifacts.output_docx_filename"))
            else:
                docx_filename = _sanitize_path_in_str(docx_fn)
                _check_hash_format(docx_sha, "artifacts.docx_sha256", issues, optional=False)
                docx_real_path = os.path.join(manifest_dir, docx_filename)
                if not os.path.exists(docx_real_path):
                    issues.append(ValidationIssue(code="FILE_MISSING", severity="FAIL", message=f"声明的 DOCX 产物文件不存在: {docx_filename}", location="artifacts.output_docx_filename"))
                    docx_sha256_match = False
                else:
                    calc_docx_hash = _calc_file_sha256(docx_real_path)
                    docx_sha256_match = bool(calc_docx_hash and calc_docx_hash == docx_sha)
                    if not docx_sha256_match:
                        issues.append(ValidationIssue(code="HASH_MISMATCH", severity="FAIL", message=f"DOCX 文件 SHA256 与清单声明不一致 (文件基名: {docx_filename})", location="artifacts.docx_sha256"))
    else:
        issues.append(ValidationIssue(code="SCHEMA_TYPE_MISMATCH", severity="FAIL", message="artifacts 必须为字典对象", location="artifacts"))

    # 11. 校验 env_fingerprint
    env_fingerprint = manifest_data.get("env_fingerprint")
    if isinstance(env_fingerprint, dict):
        _validate_dict_keys(env_fingerprint, ALLOWED_ENV_FINGERPRINT_KEYS, ALLOWED_ENV_FINGERPRINT_KEYS, "env_fingerprint", issues)
        gc = env_fingerprint.get("git_commit")
        gd = env_fingerprint.get("git_dirty")
        fp = env_fingerprint.get("corpus_fingerprint")

        if gc is None or gc == "unknown":
            issues.append(ValidationIssue(code="GIT_UNKNOWN", severity="WARN", message="Git 提交哈希未知", location="env_fingerprint.git_commit"))
        elif not isinstance(gc, str):
            issues.append(ValidationIssue(code="SCHEMA_TYPE_MISMATCH", severity="FAIL", message="git_commit 必须为字符串或 null", location="env_fingerprint.git_commit"))

        if gd is True:
            issues.append(ValidationIssue(code="GIT_DIRTY", severity="WARN", message="生成时 Git 工作区处于脏状态 (dirty)", location="env_fingerprint.git_dirty"))
        elif gd is not None and not isinstance(gd, bool):
            issues.append(ValidationIssue(code="SCHEMA_TYPE_MISMATCH", severity="FAIL", message="git_dirty 必须为布尔值或 null", location="env_fingerprint.git_dirty"))

        if fp is None:
            issues.append(ValidationIssue(code="CORPUS_UNSET", severity="WARN", message="语料库指纹未设置 (null)", location="env_fingerprint.corpus_fingerprint"))
        else:
            _check_hash_format(fp, "env_fingerprint.corpus_fingerprint", issues, optional=False)
    else:
        issues.append(ValidationIssue(code="SCHEMA_TYPE_MISMATCH", severity="FAIL", message="env_fingerprint 必须为字典对象", location="env_fingerprint"))

    # 12. 校验 reference_mapping_status 与 cited_references
    ref_status = manifest_data.get("reference_mapping_status")
    cited_references = manifest_data.get("cited_references")
    ref_status_str = _sanitize_path_in_str(ref_status) if isinstance(ref_status, str) else ""

    if not isinstance(cited_references, list):
        issues.append(ValidationIssue(code="SCHEMA_TYPE_MISMATCH", severity="FAIL", message="cited_references 必须为列表", location="cited_references"))
        cited_references = []

    if ref_status == "complete":
        if doc_count <= 0 or len(cited_references) != doc_count:
            issues.append(ValidationIssue(
                code="MAPPING_MISMATCH",
                severity="FAIL",
                message=f"引用映射状态为 complete 时 doc_count 必须 > 0 且与 cited_references 数量一致 (实际: {len(cited_references)} vs {doc_count})",
                location="reference_mapping_status",
            ))
        seen_doc_ids: set[str] = set()
        for idx, ref_item in enumerate(cited_references):
            if not isinstance(ref_item, dict):
                issues.append(ValidationIssue(code="SCHEMA_TYPE_MISMATCH", severity="FAIL", message=f"cited_references[{idx}] 必须为字典对象", location=f"cited_references[{idx}]"))
                continue
            _validate_dict_keys(ref_item, ALLOWED_CITED_REFERENCE_KEYS, ALLOWED_CITED_REFERENCE_KEYS, f"cited_references[{idx}]", issues)
            d_id = ref_item.get("doc_id")
            if not isinstance(d_id, str) or not d_id.strip():
                issues.append(ValidationIssue(code="MAPPING_MISMATCH", severity="FAIL", message=f"cited_references[{idx}].doc_id 不能为空", location=f"cited_references[{idx}].doc_id"))
            else:
                clean_did = _sanitize_path_in_str(d_id)
                if clean_did in seen_doc_ids:
                    issues.append(ValidationIssue(code="DUPLICATE_DOC_ID", severity="FAIL", message=f"cited_references 中出现重复的 doc_id: '{clean_did}'", location=f"cited_references[{idx}].doc_id"))
                else:
                    seen_doc_ids.add(clean_did)

            for m_k in ALLOWLIST_PAPER_META_KEYS:
                if m_k in ref_item and not isinstance(ref_item[m_k], str):
                    issues.append(ValidationIssue(code="SCHEMA_TYPE_MISMATCH", severity="FAIL", message=f"cited_references[{idx}].{m_k} 必须为字符串", location=f"cited_references[{idx}].{m_k}"))

            cleaned_meta = {k: str(ref_item.get(k, "") or "") for k in ALLOWLIST_PAPER_META_KEYS}
            canon_json = json.dumps(cleaned_meta, ensure_ascii=False, separators=(',', ':'), sort_keys=True)
            expected_hash = hashlib.sha256(canon_json.encode("utf-8")).hexdigest()
            actual_hash = ref_item.get("meta_hash")
            if actual_hash != expected_hash:
                issues.append(ValidationIssue(code="META_HASH_TAMPERED", severity="FAIL", message=f"cited_references[{idx}] 的 meta_hash 与元数据内容重算不匹配", location=f"cited_references[{idx}].meta_hash"))
            else:
                _check_hash_format(actual_hash, f"cited_references[{idx}].meta_hash", issues)
    elif ref_status == "unavailable_legacy_output":
        if len(cited_references) > 0:
            issues.append(ValidationIssue(code="MAPPING_MISMATCH", severity="FAIL", message="引用映射状态为 unavailable_legacy_output 时 cited_references 必须为空列表", location="cited_references"))
        issues.append(ValidationIssue(code="LEGACY_MAPPING", severity="WARN", message="引用映射状态为历史兼容模式 (unavailable_legacy_output)，引文 ID 映射缺失", location="reference_mapping_status"))
    elif ref_status == "empty":
        if doc_count != 0 or len(cited_references) > 0:
            issues.append(ValidationIssue(code="MAPPING_MISMATCH", severity="FAIL", message="引用映射状态为 empty 时 doc_count 必须为 0 且 cited_references 必须为空", location="reference_mapping_status"))
    else:
        issues.append(ValidationIssue(code="INVALID_MAPPING_STATUS", severity="FAIL", message="无效的引用映射状态值", location="reference_mapping_status"))

    # 13. 校验 section_evidence
    section_evidence = manifest_data.get("section_evidence")
    section_count = 0
    if isinstance(section_evidence, list):
        section_count = len(section_evidence)
        for s_idx, sec_item in enumerate(section_evidence):
            if not isinstance(sec_item, dict):
                issues.append(ValidationIssue(code="SCHEMA_TYPE_MISMATCH", severity="FAIL", message=f"section_evidence[{s_idx}] 必须为字典对象", location=f"section_evidence[{s_idx}]"))
                continue
            _validate_dict_keys(sec_item, ALLOWED_SECTION_EVIDENCE_KEYS, ALLOWED_SECTION_EVIDENCE_KEYS, f"section_evidence[{s_idx}]", issues)

            heading = _sanitize_path_in_str(sec_item.get("heading", f"Section_{s_idx}"))
            if not isinstance(sec_item.get("heading"), str) and "heading" in sec_item:
                issues.append(ValidationIssue(code="SCHEMA_TYPE_MISMATCH", severity="FAIL", message=f"section_evidence[{s_idx}].heading 必须为字符串", location=f"section_evidence[{s_idx}].heading"))

            ev_status = sec_item.get("evidence_status")
            if not isinstance(ev_status, str) and "evidence_status" in sec_item:
                issues.append(ValidationIssue(code="SCHEMA_TYPE_MISMATCH", severity="FAIL", message=f"section_evidence[{s_idx}].evidence_status 必须为字符串", location=f"section_evidence[{s_idx}].evidence_status"))
            elif ev_status == "unavailable_legacy_checkpoint":
                issues.append(ValidationIssue(code="LEGACY_CHECKPOINT", severity="WARN", message=f"章节《{heading}》证据来源于历史断点恢复 (unavailable_legacy_checkpoint)", location=f"section_evidence[{s_idx}].evidence_status"))
            elif ev_status == "unavailable_missing_mapping":
                issues.append(ValidationIssue(code="MISSING_MAPPING", severity="WARN", message=f"章节《{heading}》缺少显式证据映射 (unavailable_missing_mapping)", location=f"section_evidence[{s_idx}].evidence_status"))
            elif ev_status not in ("complete", "empty"):
                issues.append(ValidationIssue(code="INVALID_EVIDENCE_STATUS", severity="FAIL", message=f"章节《{heading}》存在无效的 evidence_status: '{_sanitize_path_in_str(ev_status)}'", location=f"section_evidence[{s_idx}].evidence_status"))

            c_ids = sec_item.get("cited_doc_ids")
            if isinstance(c_ids, list):
                for cid_idx, cid in enumerate(c_ids):
                    if not isinstance(cid, str):
                        issues.append(ValidationIssue(code="SCHEMA_TYPE_MISMATCH", severity="FAIL", message=f"section_evidence[{s_idx}].cited_doc_ids[{cid_idx}] 必须为字符串", location=f"section_evidence[{s_idx}].cited_doc_ids[{cid_idx}]"))
            elif "cited_doc_ids" in sec_item:
                issues.append(ValidationIssue(code="SCHEMA_TYPE_MISMATCH", severity="FAIL", message=f"section_evidence[{s_idx}].cited_doc_ids 必须为列表", location=f"section_evidence[{s_idx}].cited_doc_ids"))

            chunks = sec_item.get("chunks")
            if isinstance(chunks, list):
                for c_idx, chunk in enumerate(chunks):
                    if not isinstance(chunk, dict):
                        issues.append(ValidationIssue(code="SCHEMA_TYPE_MISMATCH", severity="FAIL", message=f"section_evidence[{s_idx}].chunks[{c_idx}] 必须为字典对象", location=f"section_evidence[{s_idx}].chunks[{c_idx}]"))
                        continue
                    _validate_dict_keys(chunk, ALLOWED_CHUNK_KEYS, ALLOWED_CHUNK_KEYS, f"section_evidence[{s_idx}].chunks[{c_idx}]", issues)

                    ck_id = chunk.get("chunk_id")
                    if type(ck_id) is not int or isinstance(ck_id, bool) or ck_id < 0:
                        issues.append(ValidationIssue(code="SCHEMA_TYPE_MISMATCH", severity="FAIL", message=f"chunks[{c_idx}].chunk_id 必须为非负整数", location=f"section_evidence[{s_idx}].chunks[{c_idx}].chunk_id"))
                    score = chunk.get("score")
                    if type(score) is not int or isinstance(score, bool) or score < 0:
                        issues.append(ValidationIssue(code="SCHEMA_TYPE_MISMATCH", severity="FAIL", message=f"chunks[{c_idx}].score 必须为非负整数", location=f"section_evidence[{s_idx}].chunks[{c_idx}].score"))
                    r_score = chunk.get("retrieval_score")
                    if (type(r_score) not in (int, float) or isinstance(r_score, bool)) and "retrieval_score" in chunk:
                        issues.append(ValidationIssue(code="SCHEMA_TYPE_MISMATCH", severity="FAIL", message=f"chunks[{c_idx}].retrieval_score 必须为数值", location=f"section_evidence[{s_idx}].chunks[{c_idx}].retrieval_score"))
                    if "doc_id" in chunk and not isinstance(chunk["doc_id"], str):
                        issues.append(ValidationIssue(code="SCHEMA_TYPE_MISMATCH", severity="FAIL", message=f"chunks[{c_idx}].doc_id 必须为字符串", location=f"section_evidence[{s_idx}].chunks[{c_idx}].doc_id"))
                    if "section_title" in chunk and not isinstance(chunk["section_title"], str):
                        issues.append(ValidationIssue(code="SCHEMA_TYPE_MISMATCH", severity="FAIL", message=f"chunks[{c_idx}].section_title 必须为字符串", location=f"section_evidence[{s_idx}].chunks[{c_idx}].section_title"))

                    _check_hash_format(chunk.get("summary_hash"), f"section_evidence[{s_idx}].chunks[{c_idx}].summary_hash", issues)
            elif "chunks" in sec_item:
                issues.append(ValidationIssue(code="SCHEMA_TYPE_MISMATCH", severity="FAIL", message=f"section_evidence[{s_idx}].chunks 必须为列表", location=f"section_evidence[{s_idx}].chunks"))

            # Evidence Invariants
            if ev_status == "empty":
                if (isinstance(c_ids, list) and len(c_ids) > 0) or (isinstance(chunks, list) and len(chunks) > 0):
                    issues.append(ValidationIssue(code="EVIDENCE_STATUS_INVARIANT_VIOLATED", severity="FAIL", message=f"章节《{heading}》evidence_status 为 empty 但包含引文 ID 或证据块", location=f"section_evidence[{s_idx}].evidence_status"))
            elif ev_status in ("unavailable_legacy_checkpoint", "unavailable_missing_mapping"):
                if isinstance(chunks, list) and len(chunks) > 0:
                    issues.append(ValidationIssue(code="EVIDENCE_STATUS_INVARIANT_VIOLATED", severity="FAIL", message=f"章节《{heading}》evidence_status 为不可用模式 ({ev_status}) 但包含证据块", location=f"section_evidence[{s_idx}].evidence_status"))
    else:
        issues.append(ValidationIssue(code="SCHEMA_TYPE_MISMATCH", severity="FAIL", message="section_evidence 必须为列表", location="section_evidence"))

    # 14. 确定性排序问题列表并计算最终 Verdict
    severity_order = {"FAIL": 0, "WARN": 1}
    issues.sort(key=lambda item: (severity_order.get(item.severity, 2), item.code, item.location, item.message))

    has_fail = any(it.severity == "FAIL" for it in issues)
    has_warn = any(it.severity == "WARN" for it in issues)

    if has_fail:
        final_verdict = "FAIL"
    elif has_warn:
        final_verdict = "WARN"
    else:
        final_verdict = "PASS"

    return RunValidationReport(
        verdict=final_verdict,
        manifest_filename=manifest_basename,
        schema_version=schema_version,
        run_id=run_id,
        topic=topic,
        generated_at=generated_at,
        duration_seconds=duration_seconds,
        model_name=model_name,
        markdown_filename=md_filename,
        markdown_sha256_match=md_sha256_match,
        docx_filename=docx_filename,
        docx_sha256_match=docx_sha256_match,
        reference_mapping_status=ref_status_str,
        cited_references_count=len(cited_references),
        section_count=section_count,
        evidence_count=evidence_count,
        quality_blocking=quality_blocking,
        issues=issues,
    )


def compare_review_runs(base_manifest_path: str, candidate_manifest_path: str) -> RunComparisonReport:
    """严格只读对比两次综述运行清单，评估质量退化与配置演进。

    判定规则：
    1. 基础或候选清单校验 FAIL -> 对比直接判 FAIL；
    2. 主题 (topic) 不一致 -> 对比直接判 FAIL；
    3. 相同主题下，仅客观退化判定为 FAIL：
       - quality_blocking: False -> True；
       - 同名章节 evidence_status 从 complete/empty 退化为 unavailable_*；
    4. 模型、Prompt、检索配置、Git/语料指纹、文件哈希、章节增删、引用/证据/字数变化记录为确定性 Delta / WARN。
    5. 绝不因引用数或证据数下降设置硬失败阈值。
    """
    base_basename = _sanitize_path_in_str(os.path.basename(base_manifest_path)) if isinstance(base_manifest_path, str) and base_manifest_path else "base.manifest.json"
    cand_basename = _sanitize_path_in_str(os.path.basename(candidate_manifest_path)) if isinstance(candidate_manifest_path, str) and candidate_manifest_path else "candidate.manifest.json"

    base_val = validate_review_run(base_manifest_path)
    cand_val = validate_review_run(candidate_manifest_path)

    issues: list[ValidationIssue] = []
    deltas: list[ComparisonDelta] = []
    section_diffs: list[SectionDiff] = []

    # 1. 如果任一清单校验未通过，直接返回 FAIL，绝不继续解析原始 JSON
    if base_val.verdict == "FAIL":
        issues.append(ValidationIssue(
            code="BASE_VALIDATION_FAILED",
            severity="FAIL",
            message=f"基线清单校验失败: {base_basename}",
            location="base_manifest",
        ))
    if cand_val.verdict == "FAIL":
        issues.append(ValidationIssue(
            code="CANDIDATE_VALIDATION_FAILED",
            severity="FAIL",
            message=f"候选清单校验失败: {cand_basename}",
            location="candidate_manifest",
        ))

    if base_val.verdict == "FAIL" or cand_val.verdict == "FAIL":
        severity_order = {"FAIL": 0, "WARN": 1}
        issues.sort(key=lambda item: (severity_order.get(item.severity, 2), item.code, item.location, item.message))
        return RunComparisonReport(
            verdict="FAIL",
            base_manifest_filename=base_basename,
            candidate_manifest_filename=cand_basename,
            base_validation=base_val,
            candidate_validation=cand_val,
            topic=_sanitize_path_in_str(cand_val.topic or base_val.topic),
            same_topic=(base_val.topic == cand_val.topic and bool(base_val.topic)),
            deltas=[],
            section_diffs=[],
            reference_diff=ReferenceDiff(
                status="unavailable",
                jaccard_similarity=None,
                base_count=base_val.cited_references_count,
                candidate_count=cand_val.cited_references_count,
                retained_doc_ids=[],
                added_doc_ids=[],
                removed_doc_ids=[],
            ),
            issues=issues,
        )

    # 2. 检查主题一致性
    same_topic = (base_val.topic == cand_val.topic and bool(base_val.topic))
    if not same_topic:
        issues.append(ValidationIssue(
            code="TOPIC_MISMATCH",
            severity="FAIL",
            message=f"比对主题不一致：基线《{base_val.topic}》 vs 候选《{cand_val.topic}》",
            location="topic_intent.topic",
        ))

    # 读取两份原始 JSON 数据供细粒度比对
    base_data: dict[str, Any] = {}
    cand_data: dict[str, Any] = {}
    try:
        if os.path.exists(base_manifest_path):
            with open(base_manifest_path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                if isinstance(loaded, dict):
                    base_data = loaded
    except Exception:
        pass
    try:
        if os.path.exists(candidate_manifest_path):
            with open(candidate_manifest_path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                if isinstance(loaded, dict):
                    cand_data = loaded
    except Exception:
        pass

    # 3. 质量门客观退化检查 (quality_blocking: False -> True 判 FAIL)
    base_blocking = bool(base_val.quality_blocking)
    cand_blocking = bool(cand_val.quality_blocking)
    if not base_blocking and cand_blocking:
        issues.append(ValidationIssue(
            code="REGRESSION_QUALITY_BLOCKING",
            severity="FAIL",
            message="质量门退化：从非阻塞变为阻塞状态 (quality_blocking: False -> True)",
            location="quality_gate.quality_blocking",
        ))

    # 4. 辅助函数：确定性 Delta 提取
    def add_delta(cat: str, name: str, b_val: Any, c_val: Any, is_regression: bool = False, msg: str = ""):
        if b_val != c_val:
            safe_b = _sanitize_path_in_str(str(b_val)) if isinstance(b_val, str) else b_val
            safe_c = _sanitize_path_in_str(str(c_val)) if isinstance(c_val, str) else c_val
            deltas.append(ComparisonDelta(
                category=cat,
                name=name,
                base_value=safe_b,
                candidate_value=safe_c,
                is_regression=is_regression,
                message=msg or f"{name}: {safe_b} -> {safe_c}",
            ))

    # 5. 章节证据状态与结构比对
    base_sec_list = base_data.get("section_evidence") if isinstance(base_data.get("section_evidence"), list) else []
    cand_sec_list = cand_data.get("section_evidence") if isinstance(cand_data.get("section_evidence"), list) else []

    base_sec_map: dict[str, dict[str, Any]] = {s.get("heading", ""): s for s in base_sec_list if isinstance(s, dict)}
    cand_sec_map: dict[str, dict[str, Any]] = {s.get("heading", ""): s for s in cand_sec_list if isinstance(s, dict)}

    all_headings = sorted(set(base_sec_map.keys()) | set(cand_sec_map.keys()))
    unavailable_statuses = {"unavailable_legacy_checkpoint", "unavailable_missing_mapping"}
    complete_statuses = {"complete", "empty"}

    for heading in all_headings:
        in_base = heading in base_sec_map
        in_cand = heading in cand_sec_map

        b_item = base_sec_map.get(heading) or {}
        c_item = cand_sec_map.get(heading) or {}

        b_status = b_item.get("evidence_status")
        c_status = c_item.get("evidence_status")
        b_chunks = len(b_item.get("chunks", [])) if isinstance(b_item.get("chunks"), list) else 0
        c_chunks = len(c_item.get("chunks", [])) if isinstance(c_item.get("chunks"), list) else 0

        is_reg = False
        clean_heading = _sanitize_path_in_str(heading)
        if in_base and in_cand:
            status_tag = "retained"
            if b_status in complete_statuses and c_status in unavailable_statuses:
                is_reg = True
                issues.append(ValidationIssue(
                    code="REGRESSION_EVIDENCE_PROVENANCE",
                    severity="FAIL",
                    message=f"章节《{clean_heading}》证据溯源状态退化: {_sanitize_path_in_str(b_status)} -> {_sanitize_path_in_str(c_status)}",
                    location=f"section_evidence['{clean_heading}'].evidence_status",
                ))
        elif in_base and not in_cand:
            status_tag = "removed"
        else:
            status_tag = "added"

        section_diffs.append(SectionDiff(
            heading=clean_heading,
            status=status_tag,
            base_evidence_status=b_status,
            candidate_evidence_status=c_status,
            base_chunks_count=b_chunks,
            candidate_chunks_count=c_chunks,
            is_regression=is_reg,
        ))

    added_sec_headings = [s.heading for s in section_diffs if s.status == "added"]
    removed_sec_headings = [s.heading for s in section_diffs if s.status == "removed"]
    if added_sec_headings or removed_sec_headings:
        add_delta(
            "sections", "section_structure",
            f"{len(base_sec_map)} 章 ({','.join(sorted(base_sec_map.keys()))})",
            f"{len(cand_sec_map)} 章 ({','.join(sorted(cand_sec_map.keys()))})",
            is_regression=False,
            msg=f"章节结构演进 (新增: {len(added_sec_headings)}, 移除: {len(removed_sec_headings)})"
        )

    # 6. 引用文献集合 Jaccard 相似度与差异分析
    base_ref_status = base_data.get("reference_mapping_status")
    cand_ref_status = cand_data.get("reference_mapping_status")

    if base_ref_status == "complete" and cand_ref_status == "complete":
        base_refs = base_data.get("cited_references", [])
        cand_refs = cand_data.get("cited_references", [])
        base_ids = {r.get("doc_id") for r in base_refs if isinstance(r, dict) and r.get("doc_id")}
        cand_ids = {r.get("doc_id") for r in cand_refs if isinstance(r, dict) and r.get("doc_id")}

        union = base_ids | cand_ids
        inter = base_ids & cand_ids
        jaccard = (len(inter) / len(union)) if union else 1.0

        reference_diff = ReferenceDiff(
            status="complete",
            jaccard_similarity=jaccard,
            base_count=len(base_ids),
            candidate_count=len(cand_ids),
            retained_doc_ids=[_sanitize_path_in_str(d) for d in sorted(inter)],
            added_doc_ids=[_sanitize_path_in_str(d) for d in sorted(cand_ids - base_ids)],
            removed_doc_ids=[_sanitize_path_in_str(d) for d in sorted(base_ids - cand_ids)],
        )
        if base_ids != cand_ids:
            add_delta(
                "references", "cited_references",
                f"{len(base_ids)} 篇 ({','.join(sorted(base_ids))})",
                f"{len(cand_ids)} 篇 ({','.join(sorted(cand_ids))})",
                is_regression=False,
                msg=f"引用文献集合演进 (Jaccard: {jaccard:.4f}, 新增: {len(cand_ids - base_ids)}, 移除: {len(base_ids - cand_ids)})"
            )
    else:
        reference_diff = ReferenceDiff(
            status="unavailable",
            jaccard_similarity=None,
            base_count=base_val.cited_references_count,
            candidate_count=cand_val.cited_references_count,
            retained_doc_ids=[],
            added_doc_ids=[],
            removed_doc_ids=[],
        )

    b_mp = base_data.get("model_and_prompts", {}) if isinstance(base_data.get("model_and_prompts"), dict) else {}
    c_mp = cand_data.get("model_and_prompts", {}) if isinstance(cand_data.get("model_and_prompts"), dict) else {}
    add_delta("model_and_prompts", "model_name", b_mp.get("model_name"), c_mp.get("model_name"))
    add_delta("model_and_prompts", "analysis_model", b_mp.get("analysis_model"), c_mp.get("analysis_model"))
    add_delta("model_and_prompts", "reranker_model", b_mp.get("reranker_model"), c_mp.get("reranker_model"))

    b_prompts = b_mp.get("prompt_versions", {}) if isinstance(b_mp.get("prompt_versions"), dict) else {}
    c_prompts = c_mp.get("prompt_versions", {}) if isinstance(c_mp.get("prompt_versions"), dict) else {}
    add_delta("model_and_prompts", "outline_prompt_version", b_prompts.get("outline_prompt_version"), c_prompts.get("outline_prompt_version"))
    add_delta("model_and_prompts", "section_prompt_version", b_prompts.get("section_prompt_version"), c_prompts.get("section_prompt_version"))
    add_delta("model_and_prompts", "tldr_prompt_version", b_prompts.get("tldr_prompt_version"), c_prompts.get("tldr_prompt_version"))

    b_ret = base_data.get("retrieval_config", {}) if isinstance(base_data.get("retrieval_config"), dict) else {}
    c_ret = cand_data.get("retrieval_config", {}) if isinstance(cand_data.get("retrieval_config"), dict) else {}
    for r_k in ("review_top_k", "evidence_n", "target_words", "section_words_min", "section_words_max", "reranker_model"):
        add_delta("retrieval_config", r_k, b_ret.get(r_k), c_ret.get(r_k))

    b_env = base_data.get("env_fingerprint", {}) if isinstance(base_data.get("env_fingerprint"), dict) else {}
    c_env = cand_data.get("env_fingerprint", {}) if isinstance(cand_data.get("env_fingerprint"), dict) else {}
    add_delta("env_fingerprint", "git_commit", b_env.get("git_commit"), c_env.get("git_commit"))
    add_delta("env_fingerprint", "git_dirty", b_env.get("git_dirty"), c_env.get("git_dirty"))
    add_delta("env_fingerprint", "corpus_fingerprint", b_env.get("corpus_fingerprint"), c_env.get("corpus_fingerprint"))

    b_art = base_data.get("artifacts", {}) if isinstance(base_data.get("artifacts"), dict) else {}
    c_art = cand_data.get("artifacts", {}) if isinstance(cand_data.get("artifacts"), dict) else {}
    add_delta("artifacts", "markdown_sha256", b_art.get("markdown_sha256"), c_art.get("markdown_sha256"))
    add_delta("artifacts", "docx_sha256", b_art.get("docx_sha256"), c_art.get("docx_sha256"))

    add_delta("metrics", "duration_seconds", base_val.duration_seconds, cand_val.duration_seconds)
    add_delta("metrics", "section_count", base_val.section_count, cand_val.section_count)
    add_delta("metrics", "doc_count", base_val.cited_references_count, cand_val.cited_references_count)
    add_delta("metrics", "evidence_count", base_val.evidence_count, cand_val.evidence_count)

    # 7. 确定性排序与 Verdict 判定
    deltas.sort(key=lambda d: (d.category, d.name))
    section_diffs.sort(key=lambda s: s.heading)
    severity_order = {"FAIL": 0, "WARN": 1}
    issues.sort(key=lambda item: (severity_order.get(item.severity, 2), item.code, item.location, item.message))

    has_fail = any(it.severity == "FAIL" for it in issues) or (base_val.verdict == "FAIL") or (cand_val.verdict == "FAIL")
    has_warn = any(it.severity == "WARN" for it in issues) or (base_val.verdict == "WARN") or (cand_val.verdict == "WARN") or bool(deltas)

    if has_fail:
        final_verdict = "FAIL"
    elif has_warn:
        final_verdict = "WARN"
    else:
        final_verdict = "PASS"

    return RunComparisonReport(
        verdict=final_verdict,
        base_manifest_filename=base_basename,
        candidate_manifest_filename=cand_basename,
        base_validation=base_val,
        candidate_validation=cand_val,
        topic=_sanitize_path_in_str(cand_val.topic or base_val.topic),
        same_topic=same_topic,
        deltas=deltas,
        section_diffs=section_diffs,
        reference_diff=reference_diff,
        issues=issues,
    )
