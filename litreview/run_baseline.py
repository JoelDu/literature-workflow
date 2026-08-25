"""综述可信基线注册表深模块 (Baseline Registry Deep Module) - Phase 1C-A.

提供综述运行清单（Review Run Manifest）的认可基线提升、候选对比与注册表查询能力。
全程约束：0 LLM、0 网络、0 数据库、0 Git 命令、严格只读（除 registry 原子写入）、无绝对路径泄露。
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import tempfile
import unicodedata
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from litreview.run_validation import (
    RunComparisonReport,
    RunValidationReport,
    ValidationIssue,
    _calc_file_sha256,
    _sanitize_path_in_str,
    canonicalize_topic,
    compare_review_runs,
    validate_review_run,
)

SCHEMA_VERSION = 1
ALLOWED_REGISTRY_TOP_KEYS = frozenset({"schema_version", "updated_at", "baselines"})
ALLOWED_BASELINE_ENTRY_KEYS = frozenset({
    "topic_key",
    "topic",
    "manifest_locator",
    "manifest_sha256",
    "run_id",
    "generated_at",
    "promoted_at",
    "validation_verdict",
})
_HASH_PATTERN = re.compile(r"^[a-f0-9]{64}$")


def compute_topic_key(topic: str) -> str:
    """计算主题的标准化 SHA256 唯一指纹 Key。

    规则：Unicode NFKC 标准化、首尾去除空白、连续内部空白折叠为单空格、casefold 小写化后计算 SHA256。
    若处理后为空，则返回空字符串。
    """
    canon = canonicalize_topic(topic)
    if not canon:
        return ""
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _resolve_and_verify_containment(manifest_path: str, output_root: str) -> tuple[bool, str, str]:
    """校验 manifest_path 是否安全位于 output_root 内部，返回 (is_safe, real_manifest, relative_locator).

    严格防御目录穿越 (..) 与 symlink 逃逸。
    """
    if not manifest_path or not output_root:
        return False, "", ""
    try:
        real_output_root = os.path.realpath(os.path.abspath(output_root))
        real_manifest = os.path.realpath(os.path.abspath(manifest_path))

        common = os.path.commonpath([real_output_root, real_manifest])
        if common != real_output_root or real_manifest == real_output_root:
            return False, "", ""

        rel_locator = os.path.relpath(real_manifest, real_output_root).replace("\\", "/")
        if rel_locator.startswith("../") or rel_locator == ".." or rel_locator.startswith("/"):
            return False, "", ""
        return True, real_manifest, rel_locator
    except Exception:
        return False, "", ""


def _resolve_locator(manifest_locator: str, output_root: str) -> tuple[bool, str]:
    """从相对 locator 安全解析到 output_root 内部的物理路径，防穿越与逃逸。"""
    if not manifest_locator or not output_root or not isinstance(manifest_locator, str):
        return False, ""
    try:
        if os.path.isabs(manifest_locator) or manifest_locator.startswith("/") or "\\" in manifest_locator:
            return False, ""
        parts = PurePosixPath(manifest_locator).parts
        if any(p in ("..", ".", "") for p in parts):
            return False, ""

        real_output_root = os.path.realpath(os.path.abspath(output_root))
        target_path = os.path.join(output_root, manifest_locator)
        real_target = os.path.realpath(os.path.abspath(target_path))

        common = os.path.commonpath([real_output_root, real_target])
        if common != real_output_root or real_target == real_output_root:
            return False, ""
        return True, target_path
    except Exception:
        return False, ""


def _atomic_write_registry(registry_path: str, registry_data: dict[str, Any]) -> None:
    """原子写入基线注册表，使用同目录唯一临时文件、flush、fsync 与 os.replace。

    失败时自动清理临时文件，确保旧注册表不被破坏且不产生半写入文件。
    """
    registry_dir = os.path.dirname(os.path.abspath(registry_path))
    os.makedirs(registry_dir, exist_ok=True)

    tmp_fd, tmp_path = tempfile.mkstemp(prefix="baselines_registry_", suffix=".tmp", dir=registry_dir)
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(registry_data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, registry_path)
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        raise


def _validate_registry_structure(registry_data: Any, output_root: str, issues: list[ValidationIssue]) -> bool:
    """严格白名单与语义完整性校验已加载的注册表字典结构与类型。"""
    if not isinstance(registry_data, dict):
        issues.append(ValidationIssue(
            code="REGISTRY_CORRUPTED",
            severity="FAIL",
            message="注册表根节点必须为字典对象",
            location="registry",
        ))
        return False

    actual_top = set(registry_data.keys())
    missing_top = ALLOWED_REGISTRY_TOP_KEYS - actual_top
    unknown_top = actual_top - ALLOWED_REGISTRY_TOP_KEYS

    if missing_top:
        for k in sorted(missing_top):
            issues.append(ValidationIssue(code="REGISTRY_CORRUPTED", severity="FAIL", message=f"注册表缺少必填字段: {k}", location=f"registry.{k}"))
    if unknown_top:
        for k in sorted(unknown_top):
            issues.append(ValidationIssue(code="UNKNOWN_FIELDS", severity="FAIL", message=f"注册表发现未知字段: {_sanitize_path_in_str(k)}", location=f"registry.{_sanitize_path_in_str(k)}"))

    raw_sv = registry_data.get("schema_version")
    if type(raw_sv) is not int or isinstance(raw_sv, bool) or raw_sv != 1:
        issues.append(ValidationIssue(code="UNKNOWN_SCHEMA_VERSION", severity="FAIL", message="注册表 schema_version 必须为整数 1", location="registry.schema_version"))

    if not isinstance(registry_data.get("updated_at"), str) and "updated_at" in registry_data:
        issues.append(ValidationIssue(code="SCHEMA_TYPE_MISMATCH", severity="FAIL", message="updated_at 必须为字符串", location="registry.updated_at"))

    baselines = registry_data.get("baselines")
    if not isinstance(baselines, dict) and "baselines" in registry_data:
        issues.append(ValidationIssue(code="SCHEMA_TYPE_MISMATCH", severity="FAIL", message="baselines 必须为字典对象", location="registry.baselines"))
        return False

    if isinstance(baselines, dict):
        for t_key, entry in baselines.items():
            clean_tkey = _sanitize_path_in_str(t_key)
            if not isinstance(t_key, str) or not _HASH_PATTERN.match(t_key):
                issues.append(ValidationIssue(code="INVALID_HASH_FORMAT", severity="FAIL", message=f"注册表 topic key 格式非法: '{clean_tkey}'", location=f"registry.baselines.{clean_tkey}"))

            if not isinstance(entry, dict):
                issues.append(ValidationIssue(code="SCHEMA_TYPE_MISMATCH", severity="FAIL", message=f"baselines['{clean_tkey}'] 必须为字典对象", location=f"registry.baselines.{clean_tkey}"))
                continue
            act_keys = set(entry.keys())
            mis_keys = ALLOWED_BASELINE_ENTRY_KEYS - act_keys
            unk_keys = act_keys - ALLOWED_BASELINE_ENTRY_KEYS
            if mis_keys:
                for mk in sorted(mis_keys):
                    issues.append(ValidationIssue(code="REGISTRY_CORRUPTED", severity="FAIL", message=f"基线条目缺少必填字段: {mk}", location=f"registry.baselines.{clean_tkey}.{mk}"))
            if unk_keys:
                for uk in sorted(unk_keys):
                    issues.append(ValidationIssue(code="UNKNOWN_FIELDS", severity="FAIL", message=f"基线条目发现未知字段: {_sanitize_path_in_str(uk)}", location=f"registry.baselines.{clean_tkey}.{_sanitize_path_in_str(uk)}"))

            for fk in ("topic_key", "topic", "manifest_locator", "manifest_sha256", "run_id", "generated_at", "promoted_at", "validation_verdict"):
                if fk in entry and not isinstance(entry[fk], str):
                    issues.append(ValidationIssue(code="SCHEMA_TYPE_MISMATCH", severity="FAIL", message=f"{fk} 必须为字符串", location=f"registry.baselines.{clean_tkey}.{fk}"))

            # 语义完整性：entry.topic_key 与 map key 必须一致
            raw_entry_tkey = entry.get("topic_key")
            if isinstance(raw_entry_tkey, str):
                if not _HASH_PATTERN.match(raw_entry_tkey):
                    issues.append(ValidationIssue(code="INVALID_HASH_FORMAT", severity="FAIL", message=f"entry.topic_key 格式非法: '{_sanitize_path_in_str(raw_entry_tkey)}'", location=f"registry.baselines.{clean_tkey}.topic_key"))
                elif raw_entry_tkey != t_key:
                    issues.append(ValidationIssue(code="TOPIC_KEY_MISMATCH", severity="FAIL", message=f"entry.topic_key 与 map key 不一致", location=f"registry.baselines.{clean_tkey}.topic_key"))

            # 语义完整性：entry.topic 必须非空且计算出的 canonical topic_key 与 t_key 一致
            raw_topic = entry.get("topic")
            if isinstance(raw_topic, str):
                recomputed_key = compute_topic_key(raw_topic)
                if not recomputed_key:
                    issues.append(ValidationIssue(code="EMPTY_TOPIC", severity="FAIL", message="基线条目 topic 为空或仅包含空白字符", location=f"registry.baselines.{clean_tkey}.topic"))
                elif recomputed_key != t_key:
                    issues.append(ValidationIssue(code="TOPIC_KEY_MISMATCH", severity="FAIL", message=f"基线条目 topic 与 topic_key 不匹配", location=f"registry.baselines.{clean_tkey}.topic"))

            # 语义完整性：manifest_locator 必须是规范安全的相对 POSIX 路径，且在 output_root 内
            raw_locator = entry.get("manifest_locator")
            if isinstance(raw_locator, str):
                is_safe_loc, _ = _resolve_locator(raw_locator, output_root)
                if not is_safe_loc:
                    issues.append(ValidationIssue(code="DANGEROUS_LOCATOR", severity="FAIL", message=f"基线条目 manifest_locator 非法或存在路径逃逸: '{_sanitize_path_in_str(raw_locator)}'", location=f"registry.baselines.{clean_tkey}.manifest_locator"))

            raw_sha = entry.get("manifest_sha256")
            if isinstance(raw_sha, str) and not _HASH_PATTERN.match(raw_sha):
                issues.append(ValidationIssue(code="INVALID_HASH_FORMAT", severity="FAIL", message=f"manifest_sha256 格式非法: '{_sanitize_path_in_str(raw_sha)}'", location=f"registry.baselines.{clean_tkey}.manifest_sha256"))

            raw_verdict = entry.get("validation_verdict")
            if isinstance(raw_verdict, str) and raw_verdict not in ("PASS", "WARN"):
                issues.append(ValidationIssue(code="INVALID_VERDICT", severity="FAIL", message=f"validation_verdict 必须为 PASS 或 WARN (实际: '{_sanitize_path_in_str(raw_verdict)}')", location=f"registry.baselines.{clean_tkey}.validation_verdict"))

    return not any(it.severity == "FAIL" for it in issues)


@dataclass(frozen=True)
class BaselineEntry:
    topic_key: str
    topic: str
    manifest_locator: str
    manifest_sha256: str
    run_id: str
    generated_at: str
    promoted_at: str
    validation_verdict: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "topic_key": self.topic_key,
            "topic": _sanitize_path_in_str(self.topic),
            "manifest_locator": _sanitize_path_in_str(self.manifest_locator),
            "manifest_sha256": self.manifest_sha256,
            "run_id": _sanitize_path_in_str(self.run_id),
            "generated_at": _sanitize_path_in_str(self.generated_at),
            "promoted_at": _sanitize_path_in_str(self.promoted_at),
            "validation_verdict": self.validation_verdict,
        }


@dataclass(frozen=True)
class BaselinePromoteReport:
    verdict: str  # "PASS" | "WARN" | "FAIL"
    topic_key: str
    topic: str
    manifest_locator: str
    manifest_sha256: str
    run_id: str
    validation_verdict: str
    validation_report: RunValidationReport | None
    issues: list[ValidationIssue] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "topic_key": self.topic_key,
            "topic": _sanitize_path_in_str(self.topic),
            "manifest_locator": _sanitize_path_in_str(self.manifest_locator),
            "manifest_sha256": self.manifest_sha256,
            "run_id": _sanitize_path_in_str(self.run_id),
            "validation_verdict": self.validation_verdict,
            "validation_report": self.validation_report.to_dict() if self.validation_report else None,
            "issues": [it.to_dict() for it in self.issues],
        }


@dataclass(frozen=True)
class BaselineCheckReport:
    verdict: str  # "PASS" | "WARN" | "FAIL"
    status: str   # "compared" | "no_baseline" | "candidate_invalid" | "baseline_invalid" | "error"
    topic_key: str
    topic: str
    candidate_manifest_locator: str
    baseline_manifest_locator: str | None
    candidate_validation: RunValidationReport | None
    comparison_report: RunComparisonReport | None
    issues: list[ValidationIssue] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "status": self.status,
            "topic_key": self.topic_key,
            "topic": _sanitize_path_in_str(self.topic),
            "candidate_manifest_locator": _sanitize_path_in_str(self.candidate_manifest_locator),
            "baseline_manifest_locator": _sanitize_path_in_str(self.baseline_manifest_locator) if self.baseline_manifest_locator else None,
            "candidate_validation": self.candidate_validation.to_dict() if self.candidate_validation else None,
            "comparison_report": self.comparison_report.to_dict() if self.comparison_report else None,
            "issues": [it.to_dict() for it in self.issues],
        }


@dataclass(frozen=True)
class BaselineSummary:
    topic_key: str
    topic: str
    manifest_locator: str
    manifest_sha256: str
    run_id: str
    generated_at: str
    promoted_at: str
    validation_verdict: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "topic_key": self.topic_key,
            "topic": _sanitize_path_in_str(self.topic),
            "manifest_locator": _sanitize_path_in_str(self.manifest_locator),
            "manifest_sha256": self.manifest_sha256,
            "run_id": _sanitize_path_in_str(self.run_id),
            "generated_at": _sanitize_path_in_str(self.generated_at),
            "promoted_at": _sanitize_path_in_str(self.promoted_at),
            "validation_verdict": self.validation_verdict,
        }


@dataclass(frozen=True)
class BaselineListReport:
    verdict: str  # "PASS" | "FAIL"
    registry_filename: str
    total_baselines: int
    baselines: list[BaselineSummary] = field(default_factory=list)
    issues: list[ValidationIssue] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "registry_filename": _sanitize_path_in_str(self.registry_filename),
            "total_baselines": self.total_baselines,
            "baselines": [b.to_dict() for b in self.baselines],
            "issues": [it.to_dict() for it in self.issues],
        }


def promote_review_baseline(
    manifest_path: str,
    registry_path: str,
    output_root: str,
    allow_warn: bool = False,
) -> BaselinePromoteReport:
    """将指定综述运行清单提升/更新为该主题的官方认可基线。

    规则：
    1. 必须先通过 validate_review_run 校验；
    2. 校验 FAIL 坚决拒绝且不修改注册表；
    3. 校验 WARN 默认拒绝，仅在显式 allow_warn=True 时允许提升；
    4. 必须通过 containment 校验确保位于 output_root 内部且无路径逃逸；
    5. 同一 topic 原子替换旧基线；
    6. 写入使用同目录临时文件 + fsync + os.replace 原子写入。
    """
    manifest_basename = _sanitize_path_in_str(os.path.basename(manifest_path)) if isinstance(manifest_path, str) and manifest_path else "unknown.manifest.json"
    issues: list[ValidationIssue] = []

    # 1. 校验路径收敛性与安全 Locator (防御路径穿越与 symlink 逃逸)
    is_safe, real_manifest, rel_locator = _resolve_and_verify_containment(manifest_path, output_root)
    if not is_safe:
        issues.append(ValidationIssue(
            code="DANGEROUS_PATH",
            severity="FAIL",
            message="清单文件不在指定的输出根目录内部或存在路径穿越逃逸",
            location="manifest",
        ))
        return BaselinePromoteReport(
            verdict="FAIL",
            topic_key="",
            topic="",
            manifest_locator="",
            manifest_sha256="",
            run_id="",
            validation_verdict="FAIL",
            validation_report=None,
            issues=issues,
        )

    # 2. 运行清单校验
    val_report = validate_review_run(real_manifest)
    if val_report.verdict == "FAIL":
        issues.append(ValidationIssue(
            code="PROMOTION_REJECTED_VALIDATION_FAILED",
            severity="FAIL",
            message=f"候选清单校验失败 (FAIL)，拒绝提升为基线: {manifest_basename}",
            location="manifest",
        ))
        return BaselinePromoteReport(
            verdict="FAIL",
            topic_key="",
            topic=_sanitize_path_in_str(val_report.topic),
            manifest_locator=rel_locator,
            manifest_sha256="",
            run_id=_sanitize_path_in_str(val_report.run_id),
            validation_verdict="FAIL",
            validation_report=val_report,
            issues=issues,
        )

    if val_report.verdict == "WARN" and not allow_warn:
        issues.append(ValidationIssue(
            code="PROMOTION_REJECTED_WARN",
            severity="FAIL",
            message=f"候选清单包含告警项 (WARN) 且未指定 allow_warn，拒绝提升为基线: {manifest_basename}",
            location="manifest",
        ))
        return BaselinePromoteReport(
            verdict="FAIL",
            topic_key="",
            topic=_sanitize_path_in_str(val_report.topic),
            manifest_locator=rel_locator,
            manifest_sha256="",
            run_id=_sanitize_path_in_str(val_report.run_id),
            validation_verdict="WARN",
            validation_report=val_report,
            issues=issues,
        )

    # 3. 校验主题非空
    clean_topic = _sanitize_path_in_str(val_report.topic)
    topic_key = compute_topic_key(val_report.topic)
    if not topic_key:
        issues.append(ValidationIssue(
            code="EMPTY_TOPIC",
            severity="FAIL",
            message="主题 (topic) 为空或仅包含空白字符，禁止提升为基线",
            location="topic_intent.topic",
        ))
        return BaselinePromoteReport(
            verdict="FAIL",
            topic_key="",
            topic="",
            manifest_locator=rel_locator,
            manifest_sha256="",
            run_id=_sanitize_path_in_str(val_report.run_id),
            validation_verdict=val_report.verdict,
            validation_report=val_report,
            issues=issues,
        )

    # 4. 计算清单 SHA256
    manifest_sha256 = _calc_file_sha256(real_manifest) or ""

    # 5. 加载/校验现有注册表
    registry_data: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "updated_at": "",
        "baselines": {},
    }
    if os.path.exists(registry_path):
        try:
            with open(registry_path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            reg_issues: list[ValidationIssue] = []
            if not _validate_registry_structure(loaded, output_root, reg_issues):
                reg_issues.append(ValidationIssue(
                    code="REGISTRY_CORRUPTED",
                    severity="FAIL",
                    message="现有基线注册表文件格式非法或损坏，拒绝写入以保护数据",
                    location="registry",
                ))
                return BaselinePromoteReport(
                    verdict="FAIL",
                    topic_key=topic_key,
                    topic=clean_topic,
                    manifest_locator=rel_locator,
                    manifest_sha256=manifest_sha256,
                    run_id=_sanitize_path_in_str(val_report.run_id),
                    validation_verdict=val_report.verdict,
                    validation_report=val_report,
                    issues=reg_issues,
                )
            registry_data = loaded
        except Exception:
            issues.append(ValidationIssue(
                code="REGISTRY_CORRUPTED",
                severity="FAIL",
                message="无法读取或解析现有基线注册表 JSON",
                location="registry",
            ))
            return BaselinePromoteReport(
                verdict="FAIL",
                topic_key=topic_key,
                topic=clean_topic,
                manifest_locator=rel_locator,
                manifest_sha256=manifest_sha256,
                run_id=_sanitize_path_in_str(val_report.run_id),
                validation_verdict=val_report.verdict,
                validation_report=val_report,
                issues=issues,
            )

    # 6. 原子更新注册表
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    registry_data["updated_at"] = now_iso
    if not isinstance(registry_data.get("baselines"), dict):
        registry_data["baselines"] = {}

    registry_data["baselines"][topic_key] = {
        "topic_key": topic_key,
        "topic": clean_topic,
        "manifest_locator": rel_locator,
        "manifest_sha256": manifest_sha256,
        "run_id": _sanitize_path_in_str(val_report.run_id),
        "generated_at": _sanitize_path_in_str(val_report.generated_at),
        "promoted_at": now_iso,
        "validation_verdict": val_report.verdict,
    }

    try:
        _atomic_write_registry(registry_path, registry_data)
    except Exception:
        issues.append(ValidationIssue(
            code="REGISTRY_WRITE_ERROR",
            severity="FAIL",
            message="原子写入基线注册表文件失败",
            location="registry",
        ))
        return BaselinePromoteReport(
            verdict="FAIL",
            topic_key=topic_key,
            topic=clean_topic,
            manifest_locator=rel_locator,
            manifest_sha256=manifest_sha256,
            run_id=_sanitize_path_in_str(val_report.run_id),
            validation_verdict=val_report.verdict,
            validation_report=val_report,
            issues=issues,
        )

    final_verdict = "PASS" if val_report.verdict == "PASS" else "WARN"
    return BaselinePromoteReport(
        verdict=final_verdict,
        topic_key=topic_key,
        topic=clean_topic,
        manifest_locator=rel_locator,
        manifest_sha256=manifest_sha256,
        run_id=_sanitize_path_in_str(val_report.run_id),
        validation_verdict=val_report.verdict,
        validation_report=val_report,
        issues=issues,
    )


def check_review_candidate(
    candidate_manifest_path: str,
    registry_path: str,
    output_root: str,
) -> BaselineCheckReport:
    """自动比对候选综述与已注册基线。

    规则：
    1. 校验候选清单路径收敛性与内容；若候选 FAIL 则直接早退返回 FAIL；
    2. 无注册表或无该主题基线时，诚实返回 WARN (status=no_baseline)；
    3. 注册表损坏或基线文件损坏/哈希篡改/逃逸，返回 FAIL；
    4. 安全解析基线后调用 compare_review_runs，比较结果原样作为 verdict。
    """
    cand_basename = _sanitize_path_in_str(os.path.basename(candidate_manifest_path)) if isinstance(candidate_manifest_path, str) and candidate_manifest_path else "candidate.manifest.json"
    cand_safe, real_candidate, cand_locator = _resolve_and_verify_containment(candidate_manifest_path, output_root)
    if not cand_safe:
        return BaselineCheckReport(
            verdict="FAIL",
            status="candidate_invalid",
            topic_key="",
            topic="",
            candidate_manifest_locator="",
            baseline_manifest_locator=None,
            candidate_validation=None,
            comparison_report=None,
            issues=[ValidationIssue(
                code="DANGEROUS_PATH",
                severity="FAIL",
                message="候选清单文件不在指定的输出根目录内部或存在路径穿越逃逸",
                location="candidate_manifest",
            )],
        )

    # 1. 校验候选清单
    cand_val = validate_review_run(real_candidate)
    clean_topic = _sanitize_path_in_str(cand_val.topic)
    topic_key = compute_topic_key(cand_val.topic)

    if cand_val.verdict == "FAIL":
        return BaselineCheckReport(
            verdict="FAIL",
            status="candidate_invalid",
            topic_key=topic_key,
            topic=clean_topic,
            candidate_manifest_locator=cand_locator,
            baseline_manifest_locator=None,
            candidate_validation=cand_val,
            comparison_report=None,
            issues=[ValidationIssue(
                code="CANDIDATE_VALIDATION_FAILED",
                severity="FAIL",
                message=f"候选清单校验失败 (FAIL): {cand_basename}",
                location="candidate_manifest",
            )],
        )

    if not topic_key:
        return BaselineCheckReport(
            verdict="FAIL",
            status="candidate_invalid",
            topic_key="",
            topic="",
            candidate_manifest_locator=cand_locator,
            baseline_manifest_locator=None,
            candidate_validation=cand_val,
            comparison_report=None,
            issues=[ValidationIssue(
                code="EMPTY_TOPIC",
                severity="FAIL",
                message="候选清单主题为空或非字符串",
                location="topic_intent.topic",
            )],
        )

    # 2. 检查注册表存在性
    if not os.path.exists(registry_path):
        return BaselineCheckReport(
            verdict="WARN",
            status="no_baseline",
            topic_key=topic_key,
            topic=clean_topic,
            candidate_manifest_locator=cand_locator,
            baseline_manifest_locator=None,
            candidate_validation=cand_val,
            comparison_report=None,
            issues=[ValidationIssue(
                code="NO_BASELINE",
                severity="WARN",
                message=f"未找到已注册基线（注册表文件尚不存在）: 《{clean_topic}》",
                location="registry",
            )],
        )

    # 3. 读取并校验注册表
    try:
        with open(registry_path, "r", encoding="utf-8") as f:
            registry_data = json.load(f)
    except Exception:
        return BaselineCheckReport(
            verdict="FAIL",
            status="baseline_invalid",
            topic_key=topic_key,
            topic=clean_topic,
            candidate_manifest_locator=cand_locator,
            baseline_manifest_locator=None,
            candidate_validation=cand_val,
            comparison_report=None,
            issues=[ValidationIssue(
                code="REGISTRY_CORRUPTED",
                severity="FAIL",
                message="基线注册表 JSON 解析失败",
                location="registry",
            )],
        )

    reg_issues: list[ValidationIssue] = []
    if not _validate_registry_structure(registry_data, output_root, reg_issues):
        return BaselineCheckReport(
            verdict="FAIL",
            status="baseline_invalid",
            topic_key=topic_key,
            topic=clean_topic,
            candidate_manifest_locator=cand_locator,
            baseline_manifest_locator=None,
            candidate_validation=cand_val,
            comparison_report=None,
            issues=reg_issues,
        )

    baselines = registry_data.get("baselines", {})
    if topic_key not in baselines:
        return BaselineCheckReport(
            verdict="WARN",
            status="no_baseline",
            topic_key=topic_key,
            topic=clean_topic,
            candidate_manifest_locator=cand_locator,
            baseline_manifest_locator=None,
            candidate_validation=cand_val,
            comparison_report=None,
            issues=[ValidationIssue(
                code="NO_BASELINE",
                severity="WARN",
                message=f"注册表中未找到该主题的已注册基线: 《{clean_topic}》",
                location="registry.baselines",
            )],
        )

    # 4. 解析基线清单并比对
    base_entry = baselines[topic_key]
    base_locator = base_entry.get("manifest_locator", "")
    is_safe, base_manifest_path = _resolve_locator(base_locator, output_root)
    if not is_safe:
        return BaselineCheckReport(
            verdict="FAIL",
            status="baseline_invalid",
            topic_key=topic_key,
            topic=clean_topic,
            candidate_manifest_locator=cand_locator,
            baseline_manifest_locator=_sanitize_path_in_str(base_locator),
            candidate_validation=cand_val,
            comparison_report=None,
            issues=[ValidationIssue(
                code="DANGEROUS_LOCATOR",
                severity="FAIL",
                message=f"注册表中的基线 locator 非法或逃逸: {_sanitize_path_in_str(base_locator)}",
                location="registry.baselines.manifest_locator",
            )],
        )

    if not os.path.exists(base_manifest_path):
        return BaselineCheckReport(
            verdict="FAIL",
            status="baseline_invalid",
            topic_key=topic_key,
            topic=clean_topic,
            candidate_manifest_locator=cand_locator,
            baseline_manifest_locator=_sanitize_path_in_str(base_locator),
            candidate_validation=cand_val,
            comparison_report=None,
            issues=[ValidationIssue(
                code="BASELINE_FILE_MISSING",
                severity="FAIL",
                message=f"注册的基线清单文件已丢失: {_sanitize_path_in_str(base_locator)}",
                location="registry.baselines.manifest_locator",
            )],
        )

    base_sha = _calc_file_sha256(base_manifest_path)
    if base_sha != base_entry.get("manifest_sha256"):
        return BaselineCheckReport(
            verdict="FAIL",
            status="baseline_invalid",
            topic_key=topic_key,
            topic=clean_topic,
            candidate_manifest_locator=cand_locator,
            baseline_manifest_locator=_sanitize_path_in_str(base_locator),
            candidate_validation=cand_val,
            comparison_report=None,
            issues=[ValidationIssue(
                code="BASELINE_HASH_MISMATCH",
                severity="FAIL",
                message=f"基线清单文件 SHA256 与注册表记录不一致 (文件基名: {_sanitize_path_in_str(os.path.basename(base_manifest_path))})",
                location="registry.baselines.manifest_sha256",
            )],
        )

    # 5. 调用对比模块
    cmp_report = compare_review_runs(base_manifest_path, real_candidate)
    return BaselineCheckReport(
        verdict=cmp_report.verdict,
        status="compared",
        topic_key=topic_key,
        topic=clean_topic,
        candidate_manifest_locator=cand_locator,
        baseline_manifest_locator=_sanitize_path_in_str(base_locator),
        candidate_validation=cand_val,
        comparison_report=cmp_report,
        issues=cmp_report.issues,
    )


def list_review_baselines(
    registry_path: str,
    output_root: str,
) -> BaselineListReport:
    """列出注册表中的所有基线摘要，按主题与 topic_key 确定性排序。"""
    reg_basename = _sanitize_path_in_str(os.path.basename(registry_path)) if isinstance(registry_path, str) and registry_path else "baselines.registry.json"
    if not os.path.exists(registry_path):
        return BaselineListReport(
            verdict="PASS",
            registry_filename=reg_basename,
            total_baselines=0,
            baselines=[],
            issues=[],
        )

    try:
        with open(registry_path, "r", encoding="utf-8") as f:
            registry_data = json.load(f)
    except Exception:
        return BaselineListReport(
            verdict="FAIL",
            registry_filename=reg_basename,
            total_baselines=0,
            baselines=[],
            issues=[ValidationIssue(
                code="REGISTRY_CORRUPTED",
                severity="FAIL",
                message="无法读取或解析基线注册表 JSON",
                location="registry",
            )],
        )

    issues: list[ValidationIssue] = []
    if not _validate_registry_structure(registry_data, output_root, issues):
        return BaselineListReport(
            verdict="FAIL",
            registry_filename=reg_basename,
            total_baselines=0,
            baselines=[],
            issues=issues,
        )

    baselines_map = registry_data.get("baselines", {})
    summaries: list[BaselineSummary] = []
    for t_key, entry in baselines_map.items():
        if isinstance(entry, dict):
            summaries.append(BaselineSummary(
                topic_key=_sanitize_path_in_str(entry.get("topic_key", t_key)),
                topic=_sanitize_path_in_str(entry.get("topic", "")),
                manifest_locator=_sanitize_path_in_str(entry.get("manifest_locator", "")),
                manifest_sha256=_sanitize_path_in_str(entry.get("manifest_sha256", "")),
                run_id=_sanitize_path_in_str(entry.get("run_id", "")),
                generated_at=_sanitize_path_in_str(entry.get("generated_at", "")),
                promoted_at=_sanitize_path_in_str(entry.get("promoted_at", "")),
                validation_verdict=_sanitize_path_in_str(entry.get("validation_verdict", "PASS")),
            ))

    summaries.sort(key=lambda b: (b.topic, b.topic_key))

    return BaselineListReport(
        verdict="PASS",
        registry_filename=reg_basename,
        total_baselines=len(summaries),
        baselines=summaries,
        issues=[],
    )
