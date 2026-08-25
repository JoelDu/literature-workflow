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


_NUMERIC_CITE_RE = re.compile(r"\[(\d+)\]")
_CITE_RUN_RE = re.compile(r"(?:\[\d+\]\s*){2,}")
_HEADING_PREFIX_RE = re.compile(
    r"^\s*(?:(?:第\s*)?(?:\d+(?:\.\d+)*|[一二三四五六七八九十百]+)(?:\s*[章节])?\s*[、.．:：\-]\s*)+"
)
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


def _dedupe_citation_runs(text: str) -> str:
    def replace(match: re.Match) -> str:
        seen = set()
        numbers = []
        for number in _NUMERIC_CITE_RE.findall(match.group(0)):
            if number not in seen:
                seen.add(number)
                numbers.append(number)
        return "".join(f"[{number}]" for number in numbers)

    return _CITE_RUN_RE.sub(replace, text)


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

    def resolve(old_number: int, section: str) -> str | None:
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
        return f"[{canonical_numbers[canonical]}]"

    def rewrite(text: str, section: str) -> str:
        cleaned = _clean_markdown(text)
        rewritten = _NUMERIC_CITE_RE.sub(
            lambda match: resolve(int(match.group(1)), section) or match.group(0),
            cleaned,
        )
        return _dedupe_citation_runs(rewritten)

    intro = rewrite(review_doc.intro, "引言")
    new_sections = []
    for draft in review_doc.sections:
        heading = _HEADING_PREFIX_RE.sub("", draft.heading or "").strip() or "未命名章节"
        markdown = rewrite(draft.markdown, heading)
        if not markdown.strip():
            report.add("empty_section", "blocking", "章节正文为空", heading)
        elif _looks_like_json_dump(markdown):
            report.add("raw_json_section", "blocking", "章节仍是未解析的 JSON 输出", heading)
        if len(max(markdown.split("\n\n"), key=len, default="")) > 800:
            report.add("long_paragraph", "warning", "存在超过 800 字符的长段落", heading)
        cited_ids = []
        for number in _NUMERIC_CITE_RE.findall(markdown):
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
                for number in _NUMERIC_CITE_RE.findall(sentence)
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

    references = [
        format_reference(number, doc_id, canonical_meta.get(doc_id, paper_meta.get(doc_id, {})))
        for number, doc_id in sorted(new_number_to_doc.items())
    ]
    cleaned_review = ReviewDoc(
        title=review_doc.title,
        topic=review_doc.topic,
        intro=intro,
        sections=new_sections,
        conclusion=conclusion,
        references=references,
        doc_count=len(references),
        evidence_count=review_doc.evidence_count,
        generated_at=review_doc.generated_at,
        quality_issues=[issue.__dict__ for issue in report.issues],
        quality_blocking=bool(report.blocking),
    )
    return ReviewQualityResult(cleaned_review, report)
