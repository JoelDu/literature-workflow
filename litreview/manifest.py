"""Review Run Manifest 深度模块 (Phase 1A 规范实现)。

为每次综述生成与导出旁路产出结构化同名 `.manifest.json` 清单。
支持可复现审计、基线比较与证据溯源；严格遵循白名单原则，不保存任何密钥、Token、私有全文或证据摘要原文。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
import hashlib
import json
import os
import subprocess
from typing import Any, Mapping
import uuid

from .models import Outline, ReviewDoc, Evidence
from .review_quality import normalize_heading


class ManifestError(RuntimeError):
    """Manifest 清单生成或原子持久化异常。"""
    pass


ALLOWLIST_PAPER_META_KEYS = (
    "title", "title_zh", "title_en", "doi", "authors", "journal",
    "year", "volume", "issue", "pages", "doc_type", "cite_key"
)


@dataclass(frozen=True)
class PromptSnapshot:
    """关键提示词版本的显式快照。"""
    outline_prompt_version: str = ""
    section_prompt_version: str = ""
    tldr_prompt_version: str = ""


@dataclass(frozen=True)
class RetrievalSnapshot:
    """检索与字数控制参数的显式快照。"""
    review_top_k: int = 0
    evidence_n: int = 0
    target_words: int = 0
    section_words_min: int = 0
    section_words_max: int = 0
    reranker_model: str = ""


@dataclass(frozen=True)
class RunContext:
    """综述运行环境与意图上下文（严格冻结，严禁包含路径与未授权自由字段）。"""
    topic: str
    run_id: str                      # 唯一运行 ID (UUID4 或 Web JobID)
    trigger_source: str              # "cli" | "web" | "mcp"
    model_name: str                  # 核心写作模型
    analysis_model: str              # 证据提取模型
    reranker_model: str              # 重排模型
    target_words: int                # 目标总字数
    target_lang: str                 # 目标语言 "zh" | "en"
    focus: str = ""
    thesis: str = ""
    research_gap: str = ""
    mainline_logic: str = ""
    prompts: PromptSnapshot = field(default_factory=PromptSnapshot)
    retrieval: RetrievalSnapshot = field(default_factory=RetrievalSnapshot)
    duration_seconds: int = 0
    corpus_fingerprint: str | None = None


@dataclass(frozen=True)
class ManifestInput:
    """write_review_manifest 的唯一输入载荷。"""
    output_md_path: str
    review: ReviewDoc
    outline: Outline
    evidence_by_heading: Mapping[str, list[Evidence] | None]
    paper_meta: Mapping[str, Mapping[str, Any]]
    run_context: RunContext
    docx_path: str | None = None


def _get_git_info() -> tuple[str, bool | None]:
    """读取当前代码仓库 Git 提交与 Dirty 状态；失败时安全降级，禁止假报 false。"""
    try:
        proc_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=3, check=False
        )
        if proc_head.returncode != 0 or not proc_head.stdout.strip():
            return "unknown", None
        commit = proc_head.stdout.strip()
        proc_status = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, timeout=3, check=False
        )
        if proc_status.returncode != 0:
            return commit, None
        dirty = bool(proc_status.stdout.strip())
        return commit, dirty
    except Exception:
        return "unknown", None


def _calc_file_sha256(file_path: str) -> str | None:
    """计算物理文件的完整 64 位 SHA256 哈希值。"""
    if not file_path or not os.path.exists(file_path):
        return None
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def _build_cited_references(
    reference_doc_ids: list[str],
    paper_meta: Mapping[str, Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """按引用次序装配白名单元数据快照与 64 位 SHA256 指纹。"""
    cited_list = []
    for doc_id in reference_doc_ids:
        meta_raw = paper_meta.get(doc_id) or {}
        cleaned = {k: str(meta_raw.get(k, "") or "") for k in ALLOWLIST_PAPER_META_KEYS}
        canon_json = json.dumps(cleaned, ensure_ascii=False, separators=(',', ':'), sort_keys=True)
        meta_hash = hashlib.sha256(canon_json.encode("utf-8")).hexdigest()
        item = {
            "doc_id": doc_id,
            **cleaned,
            "meta_hash": meta_hash,
        }
        cited_list.append(item)
    return cited_list


def _build_section_evidence_tree(
    sections: list[Any],
    evidence_by_heading: Mapping[str, list[Evidence] | None]
) -> list[dict[str, Any]]:
    """装配按章节证据树；精细区分 missing_mapping、legacy_checkpoint 与 complete 状态。
    严格执行 Fail-Closed：规范化标题仅在唯一时 fallback 映射；若出现碰撞，拒绝猜测并标为 unavailable_missing_mapping。
    """
    evidence_map = evidence_by_heading or {}

    # 1. 统计每个规范化标题对应的原始键集合
    norm_to_raw_keys: dict[str, list[str]] = {}
    for raw_k in evidence_map:
        norm_k = normalize_heading(raw_k)
        norm_to_raw_keys.setdefault(norm_k, []).append(raw_k)

    # 2. 仅当规范化键唯一无歧义时才允许 fallback 映射
    unique_norm_evidence_map: dict[str, list[Evidence] | None] = {}
    for norm_k, raw_keys in norm_to_raw_keys.items():
        if len(raw_keys) == 1:
            unique_norm_evidence_map[norm_k] = evidence_map[raw_keys[0]]

    tree = []
    for sec in sections:
        raw_heading = getattr(sec, "heading", "") or ""
        norm_heading = normalize_heading(raw_heading)
        cited_ids = getattr(sec, "cited_doc_ids", []) or []

        # 优先精确原始标题命中；若无精确命中，仅在规范化键唯一时 fallback
        if raw_heading in evidence_map:
            has_mapping = True
            ev_list = evidence_map[raw_heading]
        elif norm_heading in unique_norm_evidence_map:
            has_mapping = True
            ev_list = unique_norm_evidence_map[norm_heading]
        else:
            has_mapping = False
            ev_list = None

        if not has_mapping:
            status = "unavailable_missing_mapping"
            chunks = []
        elif ev_list is None:
            status = "unavailable_legacy_checkpoint"
            chunks = []
        elif not ev_list:
            status = "complete" if cited_ids else "empty"
            chunks = []
        else:
            status = "complete"
            chunks = []
            for ev in ev_list:
                summary_raw = str(getattr(ev, "summary", "") or "")
                summary_hash = hashlib.sha256(summary_raw.encode("utf-8")).hexdigest()
                chunks.append({
                    "chunk_id": int(getattr(ev, "chunk_id", 0)),
                    "doc_id": str(getattr(ev, "doc_id", "")),
                    "score": int(getattr(ev, "score", 0)),
                    "summary_hash": summary_hash,
                    "retrieval_score": float(getattr(ev, "retrieval_score", 0.0) or 0.0),
                    "section_title": str(getattr(ev, "section_title", "") or ""),
                })

        tree.append({
            "heading": raw_heading,
            "evidence_status": status,
            "cited_doc_ids": list(cited_ids),
            "chunks": chunks,
        })
    return tree


def write_review_manifest(data: ManifestInput) -> str:
    """原子生成同名 `.manifest.json` 侧车清单文件。

    成功时返回生成的清单绝对路径；发生任何严重错误时清理临时文件并抛出 ManifestError。
    """
    if not data.output_md_path:
        raise ManifestError("Manifest 生成失败：output_md_path 不能为空。")

    md_abs = os.path.abspath(data.output_md_path)
    md_dir = os.path.dirname(md_abs)
    md_basename = os.path.basename(md_abs)

    if not os.path.exists(md_abs):
        raise ManifestError(f"Manifest 生成失败：Markdown 目标文件不存在 ({md_abs})。")

    md_sha256 = _calc_file_sha256(md_abs)
    if not md_sha256:
        raise ManifestError(f"Manifest 生成失败：无法计算 Markdown 文件哈希 ({md_abs})。")

    # 确定清单文件名：综述_xxx.md -> 综述_xxx.manifest.json
    if md_basename.endswith(".md"):
        manifest_filename = f"{os.path.splitext(md_basename)[0]}.manifest.json"
    else:
        manifest_filename = f"{md_basename}.manifest.json"
    manifest_abs = os.path.join(md_dir, manifest_filename)

    docx_basename = None
    docx_sha256 = None
    if data.docx_path and os.path.exists(data.docx_path):
        docx_basename = os.path.basename(data.docx_path)
        docx_sha256 = _calc_file_sha256(data.docx_path)

    git_commit, git_dirty = _get_git_info()
    rc = data.run_context

    # 1. 确定引用文献映射状态（禁止猜顺序，严格要求长度完全一致）
    ref_count = len(data.review.references) if data.review.references else 0
    id_count = len(data.review.reference_doc_ids) if data.review.reference_doc_ids else 0

    if ref_count == 0 and id_count == 0:
        reference_mapping_status = "empty"
        cited_references = []
    elif ref_count > 0 and id_count == ref_count:
        reference_mapping_status = "complete"
        cited_references = _build_cited_references(list(data.review.reference_doc_ids), data.paper_meta)
    else:
        # 数量不一致（包括 ref_count==0 但 id_count>0，以及 ref_count>0 但 id_count!=ref_count），严禁部分映射/伪造完整映射
        reference_mapping_status = "unavailable_legacy_output"
        cited_references = []

    section_evidence_tree = _build_section_evidence_tree(data.review.sections, data.evidence_by_heading)

    # 2. 组装符合 schema_version=1 的纯白名单字典
    manifest_dict: dict[str, Any] = {
        "schema_version": 1,
        "run_meta": {
            "run_id": rc.run_id,
            "trigger_source": rc.trigger_source,
        },
        "topic_intent": {
            "topic": rc.topic,
            "focus": rc.focus,
            "thesis": rc.thesis,
            "research_gap": rc.research_gap,
            "mainline_logic": rc.mainline_logic,
            "target_lang": rc.target_lang,
        },
        "timestamps": {
            "generated_at": data.review.generated_at,
            "duration_seconds": rc.duration_seconds,
        },
        "model_and_prompts": {
            "model_name": rc.model_name,
            "analysis_model": rc.analysis_model,
            "reranker_model": rc.reranker_model,
            "prompt_versions": asdict(rc.prompts),
        },
        "retrieval_config": asdict(rc.retrieval),
        "reference_mapping_status": reference_mapping_status,
        "cited_references": cited_references,
        "section_evidence": section_evidence_tree,
        "quality_gate": {
            "quality_blocking": data.review.quality_blocking,
            "quality_issues": data.review.quality_issues,
            "doc_count": data.review.doc_count,
            "evidence_count": data.review.evidence_count,
        },
        "artifacts": {
            "output_md_filename": md_basename,
            "markdown_sha256": md_sha256,
            "output_docx_filename": docx_basename,
            "docx_sha256": docx_sha256,
        },
        "env_fingerprint": {
            "git_commit": git_commit,
            "git_dirty": git_dirty,
            "corpus_fingerprint": rc.corpus_fingerprint,
        },
    }

    # 3. 序列化与原子落盘
    try:
        json_content = json.dumps(manifest_dict, ensure_ascii=False, indent=2)
    except Exception as exc:
        raise ManifestError(f"Manifest JSON 序列化失败: {exc}") from exc

    unique_suffix = uuid.uuid4().hex[:12]
    temp_file = os.path.join(md_dir, f".{manifest_filename}.{unique_suffix}.tmp")

    try:
        with open(temp_file, "w", encoding="utf-8") as f:
            f.write(json_content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_file, manifest_abs)
    except Exception as exc:
        if os.path.exists(temp_file):
            try:
                os.remove(temp_file)
            except Exception:
                pass
        raise ManifestError(f"Manifest 原子落盘失败 ({manifest_abs}): {exc}") from exc

    return manifest_abs
