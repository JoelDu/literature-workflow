"""综述装配质量门禁：纯确定性审计、清洗与参考文献规范化。

本模块不持有网络依赖、不调用 LLM、不写文件。调用方提供结构化综述、证据映射与
元数据，模块返回清洗后的副本和可审计问题列表；阻塞问题由调用方决定是否允许导出。
"""
from __future__ import annotations

import ast
import json
import os
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


# ── 零 token 数值溯源 ─────────────────────────────────────────────────────────

_NUMERIC_TRANSLATE = str.maketrans({
    **{chr(0xFF10 + i): str(i) for i in range(10)},
    "％": "%", "．": ".", "‰": "‰", " ": " ", " ": " ", " ": " ",
    "−": "-", "﹪": "%",
})
# 数值 + 单位：只有带单位/百分号或带小数的数字才视为需要溯源的"量化断言"，
# 年份、计数、章节号等纯整数不参与，避免误报。
_UNIT_AFTER_RE = re.compile(
    r"\s*(?:%|‰|°C|℃|°|K\b|倍|个百分点|wt\s*%|vol\s*%|ppm|ppb|"
    r"kJ/mol|J/g|kJ|MPa|GPa|kPa|Pa\b|mPa·s|g/mol|kDa|g/L|mg/L|mg/kg|g/kg|mol/L|mmol|mol\b|"
    r"r/min|rpm|μm|um\b|nm\b|mm\b|cm\b|m²|m2\b|mL|ml\b|L\b|mg\b|kg\b|g\b|t/a|"
    r"d(?![A-Za-z])|天|周|个月|月|h(?![A-Za-z])|小时|min\b|分钟|mV|V\b|mA|N\b|kN|W\b|kW|Hz)",
)
_RANGE_SEP_RE = re.compile(r"\s*(?:-|–|—|~|～|至|到)\s*$")
_NUM_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_.])\d+(?:[.,]\d+)?(?![\d])")  # 中文字符后紧跟数字（"为25"）也要抽取
_EVIDENCE_NUM_RE = re.compile(r"\d+(?:[.,]\d+)*")
_STRIP_FOR_CLAIMS_RE = re.compile(
    rf"{_CITE_TOKEN_PATTERN}|\[@[A-Za-z0-9_\-]+\]|"
    r"(?:图|表|Fig\.?|Figure|Table|Eq\.?|式)\s*[（(]?\d+(?:\s*[-–.]\s*\d+)*[)）]?",
    re.I,
)
_CAPTION_LINE_RE = re.compile(r"^\s*(?:!\[|\*\s*(?:图|表)\s*\d)")


def _canon_decimal(text: str) -> str | None:
    from decimal import Decimal, InvalidOperation
    try:
        value = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    out = format(value.normalize(), "f")
    return out


def _number_readings(token: str) -> list[set[str]]:
    """一个数字 token 的候选读法；每个读法是一组必须全部命中的规范化数值。

    '49,32' 优先读作小数 49.32，其次读作千分位 4932 或列举 {49, 32}。
    """
    if "," not in token:
        value = _canon_decimal(token)
        return [{value}] if value else []
    head, _, tail = token.partition(",")
    readings = []
    for candidate in (f"{head}.{tail}", f"{head}{tail}"):
        value = _canon_decimal(candidate)
        if value:
            readings.append({value})
    parts = {_canon_decimal(head), _canon_decimal(tail)} - {None}
    if parts:
        readings.append(parts)
    return readings


def extract_evidence_numbers(text: str) -> set[str]:
    """证据侧宽松抽取：所有数字及其各种合理读法（小数逗号、千分位、拆分）。"""
    numbers: set[str] = set()
    for match in _EVIDENCE_NUM_RE.finditer((text or "").translate(_NUMERIC_TRANSLATE)):
        token = match.group(0)
        pieces = token.split(",")
        for reading in _number_readings(token) if len(pieces) == 2 else []:
            numbers.update(reading)
        if len(pieces) > 2:  # 1,234,567
            value = _canon_decimal("".join(pieces))
            if value:
                numbers.add(value)
        for piece in pieces:
            value = _canon_decimal(piece)
            if value:
                numbers.add(value)
    return numbers


def extract_claim_numbers(sentence: str) -> list[str]:
    """正文侧严格抽取需要溯源的数值 token（带单位/百分号，或含小数，或处于此类范围的起点）。"""
    text = _STRIP_FOR_CLAIMS_RE.sub(" ", (sentence or "").translate(_NUMERIC_TRANSLATE))
    tokens = list(_NUM_TOKEN_RE.finditer(text))
    claims = [False] * len(tokens)
    for index in range(len(tokens) - 1, -1, -1):
        match = tokens[index]
        token = match.group(0)
        if "." in token or ("," in token and len(token.partition(",")[2]) != 3):
            claims[index] = True
        elif _UNIT_AFTER_RE.match(text, match.end()):
            claims[index] = True
        elif index + 1 < len(tokens) and claims[index + 1] and _RANGE_SEP_RE.search(
            text[match.end():tokens[index + 1].start()] or "x"
        ):
            claims[index] = True
    return [match.group(0) for match, is_claim in zip(tokens, claims) if is_claim]


def _number_supported(token: str, available: set[str]) -> bool:
    return any(reading and reading <= available for reading in _number_readings(token))


@dataclass
class NumericProvenance:
    numeric_sentences: int = 0
    unsupported: list[dict] = field(default_factory=list)

    @property
    def unsupported_ratio(self) -> float:
        return len(self.unsupported) / self.numeric_sentences if self.numeric_sentences else 0.0


def check_numeric_provenance(markdown: str, doc_numbers: Mapping[str, set],
                             resolve_docs) -> NumericProvenance:
    """逐句核对带引用的量化断言：每个数值必须出现在所引文献的写作输入中。

    doc_numbers 仅包含"已知证据"的文献；所引文献全部未知时该句跳过（无法判定，不计分母）。
    resolve_docs(sentence) -> 该句引用的 doc_id 集合。
    """
    result = NumericProvenance()
    for line in (markdown or "").splitlines():
        if not line.strip() or _CAPTION_LINE_RE.match(line):
            continue
        for sentence_match in _SENTENCE_RE.finditer(line):
            sentence = sentence_match.group(0).strip()
            claims = extract_claim_numbers(sentence)
            if not claims:
                continue
            docs = set(resolve_docs(sentence))
            known = [doc for doc in docs if doc in doc_numbers]
            if not known:
                continue
            available: set[str] = set()
            for doc in known:
                available.update(doc_numbers.get(doc) or ())
            result.numeric_sentences += 1
            missing = [token for token in claims if not _number_supported(token, available)]
            if missing:
                result.unsupported.append({
                    "sentence": sentence[:240],
                    "numbers": missing,
                    "doc_ids": sorted(docs),
                })
    return result


def _env_ratio(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


def numeric_unsupported_max() -> float:
    """单章无法溯源的量化句占比上限，超过即阻塞（env REVIEW_NUMERIC_UNSUPPORTED_MAX，默认 0.2）。"""
    return _env_ratio("REVIEW_NUMERIC_UNSUPPORTED_MAX", 0.2)


def unsourced_claim_max() -> float:
    """单章因幻觉引用被剥离而失去来源的句子占比上限（env REVIEW_UNSOURCED_CLAIM_MAX，默认 0.15）。"""
    return _env_ratio("REVIEW_UNSOURCED_CLAIM_MAX", 0.15)


def evidence_numbers_for(evidence: list, broad_bib: list | None = None) -> dict[str, list[str]]:
    """把写作输入（第一轨摘要+原始片段、第二轨摘要）压缩为 doc_id -> 数值列表，供检查点持久化。"""
    table: dict[str, set[str]] = {}
    for item in evidence or []:
        numbers = table.setdefault(str(getattr(item, "doc_id", "")), set())
        numbers.update(extract_evidence_numbers(getattr(item, "summary", "") or ""))
        numbers.update(extract_evidence_numbers(getattr(item, "chunk_text", "") or ""))
    for item in broad_bib or []:
        numbers = table.setdefault(str(item.get("doc_id", "")), set())
        numbers.update(extract_evidence_numbers(str(item.get("tldr", "") or "")))
    return {doc_id: sorted(values) for doc_id, values in table.items() if doc_id}


def count_cited_sentences(markdown: str, cite_pattern: re.Pattern | None = None) -> int:
    pattern = cite_pattern or _CITE_CLUSTER_RE
    return sum(1 for match in _SENTENCE_RE.finditer(markdown or "") if pattern.search(match.group(0)))


# ── 参考文献校验（DOI / 年份），纯本地、无网络 ───────────────────────────────

DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$")
# 常见 Elsevier 期刊代码：仅用于在 OCR 混淆（i/j、l/1、O/0）下做唯一匹配修复。
_ELSEVIER_CODES = frozenset({
    "ijbiomac", "jclepro", "cej", "jhazmat", "scitotenv", "envpol", "chemosphere",
    "carbpol", "polymer", "porgcoat", "indcrop", "apsusc", "jcis", "colsurfa",
    "matdes", "compositesb", "compositesa", "eurpolymj", "polymdegradstab",
    "jenvman", "biortech", "watres", "fcr", "still", "geoderma", "agwat", "jare",
    "jece", "seppur", "powtec", "cherd", "fuel", "energy", "jiec", "reactfunctpolym",
    "molliq", "jmrt", "ceramint", "jallcom", "conbuildmat", "foodchem", "ijpharm",
    "jconrel", "msec", "actbio", "biomaterials", "susmat", "jsamd",
})
_OCR_CHAR_VARIANTS = {"i": "ij", "j": "ji", "l": "l1i", "1": "1l", "o": "o0", "0": "0o"}


def _code_variants(code: str) -> set[str]:
    variants = {""}
    for ch in code:
        options = _OCR_CHAR_VARIANTS.get(ch, ch)
        variants = {prefix + option for prefix in variants for option in options}
        if len(variants) > 4096:
            return set()
    return variants


def normalize_doi(raw: str) -> tuple[str, str]:
    """返回 (doi, status)；status ∈ empty / ok / fixed / invalid。

    只做确定且唯一的本地修正：去掉 doi.org 前缀与尾随标点、注册号里的 O/l 混淆、
    Elsevier 'i.' 前缀误识为 'j.'、以及期刊代码在 i/j、l/1、o/0 混淆下唯一命中已知代码。
    """
    import unicodedata
    original = (raw or "").strip()
    if not original:
        return "", "empty"
    doi = unicodedata.normalize("NFKC", original)
    doi = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi\s*[:：]\s*)", "", doi, flags=re.I).strip()
    doi = doi.rstrip(".,;。，；")
    # 注册号部分只能是数字：'1O.1016' / '10. 1016' / '10.1O16'
    match = re.match(r"^\s*1[0Oo]\s*\.\s*([0-9OolI]{4,9})\s*/\s*", doi)
    if match:
        registrant = match.group(1).translate(str.maketrans("OolI", "0011"))
        doi = f"10.{registrant}/" + doi[match.end():]
    if doi.startswith("10.1016/"):
        suffix = doi[len("10.1016/"):]
        if suffix.startswith("i."):
            suffix = "j." + suffix[2:]
        journal = re.match(r"^j\.([a-z0-9]+)\.", suffix)
        if journal and journal.group(1) not in _ELSEVIER_CODES:
            hits = _code_variants(journal.group(1)) & _ELSEVIER_CODES
            if len(hits) == 1:
                suffix = f"j.{hits.pop()}." + suffix[journal.end():]
        doi = "10.1016/" + suffix
    if not DOI_RE.match(doi):
        return original, "invalid"
    return doi, ("ok" if doi == original else "fixed")


def _validate_references(sorted_ref_items, canonical_meta, paper_meta, report) -> None:
    invalid, fixed, missing_year = [], [], []
    for number, doc_id in sorted_ref_items:
        meta = canonical_meta.setdefault(doc_id, clean_paper_meta(paper_meta.get(doc_id, {})))
        doi, status = normalize_doi(meta.get("doi", ""))
        if status == "invalid":
            invalid.append(f"[{number}] {meta.get('doi')}")
        elif status == "fixed":
            fixed.append(f"[{number}] {meta.get('doi')} → {doi}")
            meta["doi"] = doi
        if meta.get("doc_type") != "web" and not (meta.get("year") or meta.get("filing_date")):
            missing_year.append(f"[{number}] {_best_title(meta)[:40]}")
    if invalid:
        report.add("reference_doi_invalid", "warning",
                   "以下参考文献 DOI 格式无效且无法唯一修复，请人工核对：" + "；".join(invalid), "参考文献")
    if fixed:
        report.add("reference_doi_normalized", "warning",
                   "以下 DOI 疑似 OCR 错误，已按唯一规则本地修正（请复核）：" + "；".join(fixed), "参考文献")
    if missing_year:
        report.add("reference_year_missing", "warning",
                   "以下参考文献缺少出版年份：" + "；".join(missing_year), "参考文献")


def _audit_numeric_provenance(draft, heading, markdown, evidence_by_heading, paper_meta,
                              identity_to_doc, new_number_to_doc, report, numeric_max) -> None:
    raw_numbers: dict[str, set[str]] = {
        doc_id: set(values or ()) for doc_id, values in (draft.evidence_numbers or {}).items()
    }
    evidence = evidence_by_heading.get(draft.heading)
    if evidence is None:
        evidence = evidence_by_heading.get(heading)
    for doc_id, values in evidence_numbers_for(evidence or []).items():
        raw_numbers.setdefault(doc_id, set()).update(values)
    if not raw_numbers:
        return
    doc_numbers: dict[str, set[str]] = {}
    for doc_id, values in raw_numbers.items():
        identity = _identity(doc_id, clean_paper_meta(paper_meta.get(doc_id, {})))
        doc_numbers.setdefault(identity_to_doc.get(identity, doc_id), set()).update(values)

    def resolve_docs(sentence: str) -> set[str]:
        return {
            new_number_to_doc[int(number)]
            for number in _extract_all_citation_numbers(sentence)
            if int(number) in new_number_to_doc
        }

    result = check_numeric_provenance(markdown, doc_numbers, resolve_docs)
    if not result.unsupported:
        return
    severity = "blocking" if result.unsupported_ratio > numeric_max else "warning"
    for item in result.unsupported:
        report.add(
            "numeric_unsupported", severity,
            f"数值 {', '.join(item['numbers'])} 未出现在所引文献 "
            f"{', '.join(doc[:8] for doc in item['doc_ids'])} 的证据中：{item['sentence'][:120]}",
            heading,
        )
    report.add(
        "numeric_unsupported_ratio", severity,
        f"本章 {len(result.unsupported)}/{result.numeric_sentences} 个量化句无法溯源"
        f"（占比 {result.unsupported_ratio:.0%}，阈值 {numeric_max:.0%}）",
        heading,
    )


def _audit_unsourced_sentences(draft, heading, markdown, report, unsourced_max) -> None:
    unsourced = [s for s in (draft.unsourced_sentences or []) if str(s).strip()]
    if not unsourced:
        return
    cited = count_cited_sentences(markdown)
    ratio = len(unsourced) / (cited + len(unsourced))
    severity = "blocking" if ratio > unsourced_max else "warning"
    for sentence in unsourced:
        report.add("unsourced_claim", severity,
                   f"句子的幻觉引用已被剥离，现无来源支撑：{str(sentence)[:120]}", heading)
    report.add("unsourced_claim_ratio", severity,
               f"本章 {len(unsourced)} 句因幻觉引用被剥离而失去来源（占比 {ratio:.0%}，阈值 {unsourced_max:.0%}）",
               heading)


def audit_and_clean_review(
    review_doc: ReviewDoc,
    allowed_evidence: EvidenceContext,
    paper_meta: Mapping[str, Mapping],
    *,
    evidence_by_heading: Mapping[str, list | None] | None = None,
) -> ReviewQualityResult:
    """通过单一接口审计并清洗综述副本；不产生 I/O 或远程调用。

    evidence_by_heading（可选）：章节写作时的证据列表，用于数值溯源；与 SectionDraft.evidence_numbers
    （检查点持久化的数值表）取并集。两者皆缺失的章节跳过数值溯源。
    """
    report = QualityReport()
    evidence_by_heading = evidence_by_heading or {}
    numeric_max = numeric_unsupported_max()
    unsourced_max = unsourced_claim_max()
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
        _audit_numeric_provenance(
            draft, heading, markdown, evidence_by_heading, paper_meta,
            identity_to_doc, new_number_to_doc, report, numeric_max,
        )
        _audit_unsourced_sentences(draft, heading, markdown, report, unsourced_max)
        new_sections.append(draft.model_copy(update=dict(
            heading=heading,
            markdown=markdown,
            cited_doc_ids=cited_ids,
            track_a_doc_ids=[doc_id for doc_id in cited_ids if doc_id in track_a_ids],
            track_b_doc_ids=[doc_id for doc_id in cited_ids if doc_id in track_b_ids],
        )))
    conclusion = rewrite(review_doc.conclusion, "结论与展望")
    if not conclusion.strip():
        report.add("empty_conclusion", "blocking", "结论正文为空", "结论与展望")
    elif _looks_like_json_dump(conclusion):
        report.add("raw_json_conclusion", "blocking", "结论仍是未解析的 JSON 输出", "结论与展望")

    sorted_ref_items = sorted(new_number_to_doc.items())
    _validate_references(sorted_ref_items, canonical_meta, paper_meta, report)
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
