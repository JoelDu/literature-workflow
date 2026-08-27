"""综述装配质量门禁：纯确定性审计、清洗与参考文献规范化。

本模块不持有网络依赖、不调用 LLM、不写文件。调用方提供结构化综述、证据映射与
元数据，模块返回清洗后的副本和可审计问题列表；阻塞问题由调用方决定是否允许导出。
"""
from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass, field
from typing import Mapping

from . import doctype as dt
from .models import ReviewDoc, SectionDraft


_CITE_TOKEN_PATTERN = r"\[\s*\d+(?:(?:\s*[-–—~～]\s*|\s*[,，]\s*)\d+)*\s*\]"
_CITE_CLUSTER_RE = re.compile(
    rf"{_CITE_TOKEN_PATTERN}(?:[ \t]*{_CITE_TOKEN_PATTERN})*"
)
_BRACKET_CONTENT_RE = re.compile(r"\[(.*?)\]")
_NUMERIC_CITE_RE = re.compile(r"\[(\d+)\]")
_MAX_RANGE_SPAN = 200
_HEADING_PREFIX_RE = re.compile(
    r"^\s*(?:(?:第\s*)?(?:\d+(?:\.\d+)*|[一二三四五六七八九十百]+)(?:\s*[章节])?\s*[、.．:：\-]\s*)+"
)


def normalize_heading(heading: str) -> str:
    """唯一、共享、确定性的章节标题规范化函数，去除中文/阿拉伯数字章节编号前缀与多余空白。"""
    cleaned = _HEADING_PREFIX_RE.sub("", heading or "").strip()
    return cleaned or (heading.strip() if heading else "未命名章节")
_NOISE_RE = re.compile(
    r"^(?:unknown|none|null|暂无(?:全名)?|未知|未明确标注|年份不详|作者不详)$",
    re.I,
)
_UNCERTAIN_RE = re.compile(r"推测为|或类似|未明确标注|暂无全名", re.I)
_TITLE_AUTHOR_RE = re.compile(r"_([一-龥]{2,4})$")
_QUANTIFIED_UNIT_RE = re.compile(
    r"\d+(?:\.\d+)?\s*(?:%|％|°|℃|d\b|天\b|kJ/mol\b|MPa\b|g/mol\b|r/min\b|mg\b|kg\b|cm\b|mm\b|μm\b|nm\b)",
    re.I,
)
_SENTENCE_RE = re.compile(r"[^\n。！？!?;；]+(?:[。！？!?;；]|$)")


@dataclass(frozen=True)
class EvidenceContext:
    """装配编号到馆藏实体的映射；轨道集合供后续静态证据角色审计复用。"""

    number_to_doc_id: Mapping[int, str] = field(default_factory=dict)
    track_a_doc_ids: frozenset[str] = field(default_factory=frozenset)
    track_b_doc_ids: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class QualityIssue:
    code: str
    severity: str
    message: str
    section: str = ""


@dataclass
class QualityReport:
    issues: list[QualityIssue] = field(default_factory=list)

    def add(self, code: str, severity: str, message: str, section: str = "") -> None:
        self.issues.append(QualityIssue(code, severity, message, section))

    @property
    def blocking(self) -> list[QualityIssue]:
        return [issue for issue in self.issues if issue.severity == "blocking"]


@dataclass(frozen=True)
class ReviewQualityResult:
    review: ReviewDoc
    report: QualityReport

    @property
    def blocking_issues(self) -> list[QualityIssue]:
        return self.report.blocking


def _clean_scalar(value: object, *, uncertain_is_missing: bool = True) -> str:
    text = str(value or "").strip()
    if not text or _NOISE_RE.fullmatch(text):
        return ""
    if uncertain_is_missing and _UNCERTAIN_RE.search(text):
        return ""
    return text


def _clean_authors(value: object) -> str:
    if isinstance(value, (list, tuple)):
        parts = [str(item).strip() for item in value if str(item).strip()]
        return ", ".join(parts)
    text = _clean_scalar(value)
    if text.startswith("[") and text.endswith("]"):
        try:
            parsed = ast.literal_eval(text)
        except (SyntaxError, ValueError):
            parsed = None
        if isinstance(parsed, (list, tuple)):
            return ", ".join(str(item).strip() for item in parsed if str(item).strip())
    return text


def clean_paper_meta(meta: Mapping | None) -> dict:
    """清理占位噪音并保留最小可著录信息，不因局部缺失抛异常。"""
    cleaned = dict(meta or {})
    for key in (
        "title", "title_zh", "title_en", "journal", "year", "doi", "publisher",
        "pub_place", "edition", "patent_no", "doc_no", "filing_date", "url",
        "volume", "issue", "pages",
    ):
        cleaned[key] = _clean_scalar(cleaned.get(key))
    cleaned["authors"] = _clean_authors(cleaned.get("authors"))
    cleaned["language"] = _clean_scalar(cleaned.get("language"), uncertain_is_missing=False) or "zh"
    cleaned["doc_type"] = dt.normalize(_clean_scalar(
        cleaned.get("doc_type"), uncertain_is_missing=False
    )) or dt.DEFAULT_TYPE
    return cleaned


def _display_title(title: str) -> str:
    return _TITLE_AUTHOR_RE.sub("", title or "")


def _best_title(meta: Mapping) -> str:
    cleaned = clean_paper_meta(meta)
    lang = cleaned.get("language", "zh")
    if lang == "zh" and cleaned.get("title_zh"):
        return cleaned["title_zh"]
    if lang != "zh" and cleaned.get("title_en"):
        return cleaned["title_en"]
    return _display_title(cleaned.get("title", "")) or "题名不详"


def _short_authors(authors: object, language: str, title: str = "") -> str:
    text = _clean_authors(authors)
    parts = [part.strip() for part in re.split(r"[,，;；]", text) if part.strip()]
    if not parts:
        match = _TITLE_AUTHOR_RE.search(title or "")
        return f"{match.group(1)}, 等" if match else ""
    suffix = ", 等" if language == "zh" else ", et al"
    return ", ".join(parts[:3]) + (suffix if len(parts) > 3 else "")


def _imprint(place: str, publisher: str, year: str) -> str:
    head = f"{place}: {publisher}" if (place and publisher) else (publisher or place)
    year_text = year or "[出版年不详]"
    return f" {head}, {year_text}." if head else f" {year_text}."


def format_reference(n: int, doc_id: str, meta: Mapping | None) -> str:
    """按 GB/T 7714 文献类型输出最小可用著录，元数据缺失时优雅降级。"""
    m = clean_paper_meta(meta)
    doc_type = m["doc_type"]
    code = dt.gb_code(doc_type)
    raw_title = m.get("title") or _best_title(m)
    title = _best_title(m)
    year = m.get("year", "")
    place, publisher = m.get("pub_place", ""), m.get("publisher", "")
    authors = "" if doc_type in ("standard", "web") and not m.get("authors") else \
        _short_authors(m.get("authors", ""), m.get("language", "zh"), raw_title)
    lead = f"{authors}. " if authors else ""

    if doc_type in ("book", "web"):
        link = ""
    else:
        safe = "".join(c for c in raw_title if c.isalnum() or c in " -_").strip() or "Untitled_Paper"
        link = f" [[{safe}_{doc_id[:8]}]]"

    if code == "M":
        edition = m.get("edition", "")
        body = (f"[{n}] {lead}{title}[M]." + (f" {edition}." if edition else "")
                + _imprint(place, publisher, year))
    elif code == "P":
        number = m.get("patent_no", "")
        date = m.get("filing_date", "") or year or "[日期不详]"
        body = f"[{n}] {lead}{title}" + (f": {number}" if number else "") + f"[P]. {date}."
    elif code == "S":
        number = m.get("doc_no", "")
        body = (f"[{n}] {lead}{title}" + (f": {number}" if number else "") + "[S]."
                + _imprint(place, publisher, year))
    elif code == "EB/OL":
        url = m.get("url", "")
        year_text = year or "[出版年不详]"
        body = (f"[{n}] {lead}{title}[EB/OL]. {year_text}."
                + (f" {url}." if url else ""))
    elif code in ("R", "D", "C"):
        body = f"[{n}] {lead}{title}[{code}]." + _imprint(place, publisher, year)
    else:
        journal = m.get("journal", "")
        year_text = year or "[出版年不详]"
        vol = m.get("volume", "")
        issue = m.get("issue", "")
        pages = m.get("pages", "")

        vol_issue = ""
        if vol:
            vol_issue = f", {vol}"
            if issue:
                vol_issue += f"({issue})"
        elif issue:
            vol_issue = f"({issue})"

        page_part = f": {pages}" if pages else ""
        tail = f" {journal}, {year_text}{vol_issue}{page_part}." if journal else f" {year_text}{vol_issue}{page_part}."
        doi = m.get("doi", "")
        body = f"[{n}] {lead}{title}[J].{tail}" + (f" DOI: {doi}." if doi else "")
    return body.rstrip() + link


def _identity(doc_id: str, meta: Mapping) -> str:
    cleaned = clean_paper_meta(meta)
    doi = cleaned.get("doi", "").lower()
    doi = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", doi)
    if doi:
        return f"doi:{doi.rstrip('.')}"
    title = re.sub(r"[^0-9a-z一-龥]+", "", _best_title(cleaned).lower())
    return f"title:{title}" if title and title != "题名不详" else f"doc:{doc_id}"


def _merge_meta(primary: Mapping, secondary: Mapping) -> dict:
    result = clean_paper_meta(primary)
    other = clean_paper_meta(secondary)
    for key, value in other.items():
        if not result.get(key) and value:
            result[key] = value
    return result


def _parse_citation_cluster(cluster_text: str) -> tuple[list[int], list[str]]:
    """解析引用簇文本（如 '[1][2][3]', '[1-3, 5]', '[1，3，5-7]'）中的所有数字与范围。

    返回 (numbers, errors)。若包含非法序号、倒置范围或超大跨度，在 errors 中记录并安全截断。
    """
    numbers: list[int] = []
    errors: list[str] = []

    for b_match in _BRACKET_CONTENT_RE.finditer(cluster_text):
        content = b_match.group(1).strip()
        if not content:
            errors.append("空引用标记: []")
            continue
        parts = [p.strip() for p in re.split(r"[,，]", content) if p.strip()]
        for part in parts:
            range_parts = re.split(r"[-–—~～]", part)
            if len(range_parts) == 1:
                try:
                    num = int(range_parts[0].strip())
                    if num < 1:
                        errors.append(f"非法引用序号: {num} (序号必须 >= 1)")
                    else:
                        numbers.append(num)
                except ValueError:
                    errors.append(f"无法解析的引用项: {part}")
            elif len(range_parts) == 2:
                start_str, end_str = range_parts[0].strip(), range_parts[1].strip()
                if not start_str or not end_str:
                    errors.append(f"畸形范围定义: {part}")
                    continue
                try:
                    start, end = int(start_str), int(end_str)
                    if start < 1 or end < 1:
                        errors.append(f"非法范围序号: {part} (序号必须 >= 1)")
                    elif start > end:
                        errors.append(f"倒置的引用范围: {part} (起始序号大于终止序号)")
                    elif end - start > _MAX_RANGE_SPAN:
                        errors.append(
                            f"超大引用范围: {part} (范围跨度 {end - start + 1} 超过安全上限 {_MAX_RANGE_SPAN})"
                        )
                    else:
                        numbers.extend(range(start, end + 1))
                except ValueError:
                    errors.append(f"无法解析的范围项: {part}")
            else:
                errors.append(f"畸形范围标记包含过多分隔符: {part}")

    return numbers, errors


def _format_citation_cluster(sorted_unique_numbers: list[int], threshold: int = 3) -> str:
    """将升序去重后的序号列表压缩为 GB/T 7714 顺序编码样式。

    连续序号 >= threshold (默认 3) 压缩为起讫形式 (如 [1-3], [1-5])；
    连续 2 个或不连续序号以逗号连接 (如 [1,2], [1,3,5], [1-3,5,7-9])。
    """
    if not sorted_unique_numbers:
        return ""
    if len(sorted_unique_numbers) == 1:
        return f"[{sorted_unique_numbers[0]}]"

    runs: list[list[int]] = []
    curr_run = [sorted_unique_numbers[0]]
    for num in sorted_unique_numbers[1:]:
        if num == curr_run[-1] + 1:
            curr_run.append(num)
        else:
            runs.append(curr_run)
            curr_run = [num]
    runs.append(curr_run)

    parts: list[str] = []
    for run in runs:
        if len(run) >= threshold:
            parts.append(f"{run[0]}-{run[-1]}")
        else:
            parts.append(",".join(str(n) for n in run))

    inner = ",".join(parts)
    return f"[{inner}]"


def _extract_all_citation_numbers(text: str) -> list[int]:
    """从文本中提取所有引用中的文献序号（支持 [1]、[1-5]、[1,3,5] 等）。"""
    numbers: list[int] = []
    for match in _CITE_CLUSTER_RE.finditer(text or ""):
        nums, _ = _parse_citation_cluster(match.group(0))
        numbers.extend(nums)
    return numbers


def _normalize_citation_cluster(raw_cluster: str) -> str:
    numbers, errors = _parse_citation_cluster(raw_cluster)
    if errors or not numbers:
        return raw_cluster
    return _format_citation_cluster(sorted(set(numbers)))


def _dedupe_citation_runs(text: str) -> str:
    """规范化文本中的所有引用簇（合并相邻方括号、去重、升序并按 GB/T 7714 压缩）。"""
    return _CITE_CLUSTER_RE.sub(lambda m: _normalize_citation_cluster(m.group(0)), text or "")


def _clean_markdown(text: str) -> str:
    cleaned = re.sub(r"(?m)^(\s*\*?)图\s*[：:]\s*图\s*", r"\1图 ", text or "")
    return _dedupe_citation_runs(cleaned)


def _looks_like_json_dump(text: str) -> bool:
    stripped = (text or "").strip()
    if not stripped.startswith(("{", "[")):
        return False
    try:
        return isinstance(json.loads(stripped), (dict, list))
    except (TypeError, ValueError):
        return False


def audit_and_clean_review(
    review_doc: ReviewDoc,
    allowed_evidence: EvidenceContext,
    paper_meta: Mapping[str, Mapping],
) -> ReviewQualityResult:
    """通过单一接口审计并清洗综述副本；不产生 I/O 或远程调用。"""
    report = QualityReport()
    identity_to_doc: dict[str, str] = {}
    canonical_meta: dict[str, dict] = {}
    canonical_numbers: dict[str, int] = {}
    new_number_to_doc: dict[int, str] = {}
    warned_missing: set[str] = set()
    warned_duplicates: set[tuple[str, str]] = set()

    def resolve(old_number: int, section: str) -> int | None:
        doc_id = allowed_evidence.number_to_doc_id.get(old_number)
        if not doc_id:
            report.add("unknown_citation", "blocking",
                       f"引用 [{old_number}] 不在允许证据集合中", section)
            return None
        meta = clean_paper_meta(paper_meta.get(doc_id, {}))
        missing = []
        if not any(meta.get(key) for key in ("title", "title_zh", "title_en")):
            missing.append("title")
        missing.extend(key for key in ("authors", "year") if not meta.get(key))
        if missing and doc_id not in warned_missing:
            warned_missing.add(doc_id)
            report.add("metadata_incomplete", "warning",
                       f"文献 {doc_id[:8]} 缺少 {', '.join(missing)}，已采用最小著录", section)
        identity = _identity(doc_id, meta)
        canonical = identity_to_doc.get(identity)
        if canonical is None:
            canonical = doc_id
            identity_to_doc[identity] = canonical
            canonical_meta[canonical] = meta
        elif canonical != doc_id:
            canonical_meta[canonical] = _merge_meta(canonical_meta[canonical], meta)
            pair = (canonical, doc_id)
            if pair not in warned_duplicates:
                warned_duplicates.add(pair)
                report.add("duplicate_reference", "warning",
                           f"文献 {doc_id[:8]} 与 {canonical[:8]} 重复，已合并", section)
        if canonical not in canonical_numbers:
            new_number = len(canonical_numbers) + 1
            canonical_numbers[canonical] = new_number
            new_number_to_doc[new_number] = canonical
        return canonical_numbers[canonical]

    def rewrite(text: str, section: str) -> str:
        cleaned = _clean_markdown(text)

        def replace_cluster(match: re.Match) -> str:
            raw_cluster = match.group(0)
            numbers, errors = _parse_citation_cluster(raw_cluster)
            if errors:
                for err in errors:
                    report.add("malformed_citation", "blocking", err, section)
                return raw_cluster

            resolved_nums: list[int] = []
            has_unknown = False
            for num in numbers:
                new_n = resolve(num, section)
                if new_n is not None:
                    resolved_nums.append(new_n)
                else:
                    has_unknown = True

            if has_unknown:
                return raw_cluster

            if not resolved_nums:
                return ""

            unique_sorted = sorted(set(resolved_nums))
            return _format_citation_cluster(unique_sorted)

        rewritten = _CITE_CLUSTER_RE.sub(replace_cluster, cleaned)
        return rewritten

    intro = rewrite(review_doc.intro, "引言")
    if not intro.strip():
        report.add("empty_intro", "blocking", "引言正文为空", "引言")
    elif _looks_like_json_dump(intro):
        report.add("raw_json_intro", "blocking", "引言仍是未解析的 JSON 输出", "引言")

    new_sections = []
    for draft in review_doc.sections:
        heading = normalize_heading(draft.heading)
        markdown = rewrite(draft.markdown, heading)
        if not markdown.strip():
            report.add("empty_section", "blocking", "章节正文为空", heading)
        elif _looks_like_json_dump(markdown):
            report.add("raw_json_section", "blocking", "章节仍是未解析的 JSON 输出", heading)
        if len(max(markdown.split("\n\n"), key=len, default="")) > 800:
            report.add("long_paragraph", "warning", "存在超过 800 字符的长段落", heading)
        cited_ids = []
        for number in _extract_all_citation_numbers(markdown):
            doc_id = new_number_to_doc.get(int(number))
            if doc_id and doc_id not in cited_ids:
                cited_ids.append(doc_id)

        def canonicalize_roles(doc_ids: set[str]) -> set[str]:
            canonical = set()
            for doc_id in doc_ids:
                identity = _identity(doc_id, paper_meta.get(doc_id, {}))
                canonical.add(identity_to_doc.get(identity, doc_id))
            return canonical

        track_a_ids = canonicalize_roles(
            set(allowed_evidence.track_a_doc_ids) | set(draft.track_a_doc_ids)
        )
        track_b_ids = canonicalize_roles(
            set(allowed_evidence.track_b_doc_ids) | set(draft.track_b_doc_ids)
        ) - track_a_ids
        for sentence_match in _SENTENCE_RE.finditer(markdown):
            sentence = sentence_match.group(0)
            if not _QUANTIFIED_UNIT_RE.search(sentence):
                continue
            sentence_docs = {
                new_number_to_doc[int(number)]
                for number in _extract_all_citation_numbers(sentence)
                if int(number) in new_number_to_doc
            }
            if sentence_docs and sentence_docs <= track_b_ids:
                report.add(
                    "track_b_quantitative_claim",
                    "warning",
                    "含数值与单位的句子仅由第二轨背景文献支撑，建议改用第一轨直接证据",
                    heading,
                )
        new_sections.append(SectionDraft(
            heading=heading,
            markdown=markdown,
            cited_doc_ids=cited_ids,
            track_a_doc_ids=[doc_id for doc_id in cited_ids if doc_id in track_a_ids],
            track_b_doc_ids=[doc_id for doc_id in cited_ids if doc_id in track_b_ids],
            source_evidence_count=draft.source_evidence_count,
        ))
    conclusion = rewrite(review_doc.conclusion, "结论与展望")
    if not conclusion.strip():
        report.add("empty_conclusion", "blocking", "结论正文为空", "结论与展望")
    elif _looks_like_json_dump(conclusion):
        report.add("raw_json_conclusion", "blocking", "结论仍是未解析的 JSON 输出", "结论与展望")

    sorted_ref_items = sorted(new_number_to_doc.items())
    references = [
        format_reference(number, doc_id, canonical_meta.get(doc_id, paper_meta.get(doc_id, {})))
        for number, doc_id in sorted_ref_items
    ]
    reference_doc_ids = [doc_id for _, doc_id in sorted_ref_items]
    cleaned_review = ReviewDoc(
        title=review_doc.title,
        topic=review_doc.topic,
        intro=intro,
        sections=new_sections,
        conclusion=conclusion,
        references=references,
        reference_doc_ids=reference_doc_ids,
        doc_count=len(references),
        evidence_count=review_doc.evidence_count,
        generated_at=review_doc.generated_at,
        quality_issues=[issue.__dict__ for issue in report.issues],
        quality_blocking=bool(report.blocking),
    )
    return ReviewQualityResult(cleaned_review, report)
