"""Rule-based Reference Parsing and Candidate Resolution Engine (Phase 2A Baseline).

Implements regex extraction for DOI, publication year, authors, title, and venue,
quality flagging for broken fragments, and candidate resolution matching.
"""
import re
import html
import unicodedata
from dataclasses import dataclass, asdict
from typing import Optional, List, Dict, Any, Tuple


# Regex patterns for reference parsing
_DOI_RE = re.compile(r'(?:https?://)?(?:dx\.)?doi(?:\.org)?(?:/|\s+)?(10\.\d{4,9}/[^\s\"<>]+(?:\s+[^\s\"<>]+)?)|\b(10\.\d{4,9}/[-._;()/:A-Za-z0-9]+(?:\s+[-._;()/:A-Za-z0-9]+)?)\b', re.IGNORECASE)
_YEAR_RE = re.compile(r'(?:[\(\[\,\.\s])\b((?:18|19|20)\d{2})\b(?:[\)\]\,\.\s]|\b)')
_INDEX_PREFIX_RE = re.compile(r'^\s*(?:\[\s*\d+\s*\]|\d+\s*[\.\、\:\：\)]|\(\s*\d+\s*\))\s*')
_PAGE_NUM_ONLY_RE = re.compile(r'^\s*[\d\s\.\,\-\–\—\~\\\/\:]+\s*$')
_PUNCT_ONLY_RE = re.compile(r'^[\s\W\d_]+$')


@dataclass
class ParsedReference:
    raw_text: str
    raw_length: int
    quality_flag: str  # NORMAL, SHORT_SUSPECT, OCR_CORRUPTED, BLANK
    parsed_doi: Optional[str] = None
    parsed_title: Optional[str] = None
    parsed_authors: Optional[str] = None
    parsed_year: Optional[int] = None
    parsed_venue: Optional[str] = None
    clean_text: str = ""
    parse_status: str = "UNPARSED"  # PARSED_COMPLETE, PARSED_PARTIAL, PARSE_FAILED


def normalize_title(title: str) -> str:
    """标准化题名：统一大小写、去除首尾标点、去除非字母数字符号用于相似度匹配。"""
    if not title:
        return ""
    # 转为 NFKC 规范形式并小写
    t = unicodedata.normalize("NFKC", title).lower()
    # 移除 HTML 实体与多余空白
    t = html.unescape(t)
    t = re.sub(r'[\r\n\t]+', ' ', t)
    # 去除首尾引号与标点
    t = re.sub(r'^[\s\"\'\“\”\‘\’\《\〈\(\[\{\.\,\-\_]+', '', t)
    t = re.sub(r'[\s\"\'\“\”\‘\’\》\〉\)\]\}\.\,\-\_]+$', '', t)
    t = re.sub(r'\s+', ' ', t).strip()
    return t


def extract_first_author_surname(author_str: Optional[str]) -> str:
    """提取第一作者的标准姓氏（处理中英文、姓在前/名在前、Initials格式）。"""
    if not author_str:
        return ""
    author_str = author_str.strip(' .,;:\t\r\n')
    # 中文作者提取首字姓氏
    if re.search(r'[\u4e00-\u9fa5]', author_str):
        clean = re.sub(r'[等著编校\s]+$', '', author_str)
        m = re.search(r'[\u4e00-\u9fa5]', clean)
        return m.group(0) if m else ""

    # 英文作者：切分出第一作者
    first_token = re.split(r'\s*(?:,|;|&|\band\b)\s*', author_str)[0].strip()
    tokens = [t.strip(' .') for t in first_token.split() if t.strip(' .')]
    if not tokens:
        return ""
    if len(tokens) == 1:
        return tokens[0].lower()
    # "Surname, Initial" 或 "Surname Initial" (首词长，第二词为缩写) -> 首词为姓
    if len(tokens[0]) > 2 and len(tokens[1]) <= 2:
        return tokens[0].lower()
    # "Initial. Surname" (如 X. Rondeau) 或 "Given Surname" (如 Mingyang Chen) -> 末词为姓
    return tokens[-1].lower()


_AUTHOR_FRAGMENT_PATTERNS = [
    re.compile(r'^[A-Z][a-zA-Z\'-]+,\s+[A-Z]\.?(?:\s*[A-Z]\.?)*$'),  # Smith, J. D.
    re.compile(r'^(?:[A-Z]\.\s*)+[A-Z][a-zA-Z\'-]+$'),  # J. D. Smith / J.D. Smith
    re.compile(r'^[A-Z][a-zA-Z\'-]+,\s+(?:[A-Z]\.?\s*)+(?:[;,\&]\s*(?:\&|and\s*)?[A-Z][a-zA-Z\'-]+,\s+(?:[A-Z]\.?\s*)+)+'),  # Billinge, S.J.L.; Rieker, T.P
    re.compile(r'\bet\s+al\b', re.I),  # et al.
    re.compile(r'^[\u4e00-\u9fa5]{2,4}(?:[\,\，\、\s]+[\u4e00-\u9fa5]{2,4}){1,5}[等著编校]?$')  # 张三，李四，王五等
]


def is_author_fragment(t: Optional[str]) -> bool:
    """检测字符串是否为作者片段（避免作者姓名误落入题名字段）。"""
    if not t:
        return False
    t_clean = t.strip(' .,;:\t\r\n')
    for rx in _AUTHOR_FRAGMENT_PATTERNS:
        if rx.search(t_clean):
            return True
    return False


def parse_reference_mention(raw_text: str) -> ParsedReference:
    """对单条参考文献原始字符串进行规则/正则解析、清洗与质量拦截。"""
    if not raw_text or not raw_text.strip():
        return ParsedReference(
            raw_text=raw_text or "",
            raw_length=len(raw_text or ""),
            quality_flag="BLANK",
            parse_status="PARSE_FAILED",
            clean_text=""
        )

    raw_len = len(raw_text)
    clean_t = _INDEX_PREFIX_RE.sub('', raw_text).strip()
    quality_flag = "NORMAL"

    # 1. 质量定性 (Quality Flagging)
    if raw_len <= 10 or len(clean_t) <= 10:
        if _PAGE_NUM_ONLY_RE.match(clean_t) or _PUNCT_ONLY_RE.match(clean_t):
            quality_flag = "OCR_CORRUPTED"
        else:
            quality_flag = "SHORT_SUSPECT"
    elif _PUNCT_ONLY_RE.match(clean_t):
        quality_flag = "OCR_CORRUPTED"

    if quality_flag == "OCR_CORRUPTED":
        return ParsedReference(
            raw_text=raw_text,
            raw_length=raw_len,
            quality_flag=quality_flag,
            parse_status="PARSE_FAILED",
            clean_text=clean_t
        )

    # 2. 提取并清洗 DOI (DOI Cleaning & Interception)
    parsed_doi = None
    doi_match = _DOI_RE.search(clean_t)
    if doi_match:
        raw_doi = (doi_match.group(1) or doi_match.group(2) or doi_match.group(0)).rstrip('.,;)]}')
        clean_doi = re.sub(r'\s+', '', raw_doi).lower()
        if clean_doi.startswith('10.1016/i.'):
            clean_doi = '10.1016/j.' + clean_doi[10:]
            quality_flag = "OCR_DOI_CORRECTED" if quality_flag == "NORMAL" else quality_flag

        if re.match(r'^10\.\d{4,5}/[a-z]?$', clean_doi):
            quality_flag = "SUSPECT_DOI_TRUNCATED"
            parsed_doi = None  # 拦截截断 DOI
        else:
            parsed_doi = clean_doi

    # 3. 提取发表年份 (Year，自动排除 accessed/retrieved 访问日期)
    parsed_year = None
    clean_t_for_year = re.sub(r'\(?\b(?:accessed|retrieved)\b[^\)]*\b(?:18|19|20)\d{2}\b[^\)]*\)?', '', clean_t, flags=re.I)
    year_matches = _YEAR_RE.findall(clean_t_for_year)
    if year_matches:
        valid_years = [int(y) for y in year_matches if 1800 <= int(y) <= 2030]
        if valid_years:
            parsed_year = valid_years[-1] if len(valid_years) > 1 and valid_years[-1] > 1950 else valid_years[0]

    # 4. 抽取作者与题名 (Authors & Title Heuristic)
    parsed_authors = None
    parsed_title = None
    parsed_venue = None

    # 分割段落符号 (常见中文 [J], [M], [D], [C], [P] 或英文 ". ")
    # A. 检查中文国标标记 [J], [M], [D], [C], [P], [S], [R]
    cn_gb_match = re.search(r'\[([JMDCPRSEB/OL]+)\]', clean_t, re.IGNORECASE)
    if cn_gb_match:
        split_pos = cn_gb_match.start()
        before_tag = clean_t[:split_pos].strip()
        after_tag = clean_t[cn_gb_match.end():].strip(' .,;:\t\r\n')

        # before_tag 通常为 "作者. 题名" 或 "作者, 题名"
        parts = re.split(r'[\.\。]\s*', before_tag)
        if len(parts) >= 2:
            parsed_authors = parts[0].strip()
            parsed_title = '. '.join(parts[1:]).strip()
        else:
            comma_parts = re.split(r'[\,\，]\s*', before_tag)
            if len(comma_parts) >= 2:
                parsed_authors = comma_parts[0].strip()
                parsed_title = ', '.join(comma_parts[1:]).strip()
            else:
                parsed_title = before_tag

        v_parts = [p.strip() for p in re.split(r'[\,\，\.\。]\s*', after_tag) if p.strip()]
        if v_parts:
            parsed_venue = v_parts[0]
    else:
        # B. 检查模式：Authors. (Year). Title. Venue, Vol(Issue), Pages
        paren_year_match = re.search(r'\(\s*((?:18|19|20)\d{2})\s*\)\s*([\.\,\:\s])\s*', clean_t)
        if paren_year_match:
            parsed_authors = clean_t[:paren_year_match.start()].strip(' .,;:')
            parsed_year = int(paren_year_match.group(1))
            delim = paren_year_match.group(2)
            remainder = clean_t[paren_year_match.end():].strip()
            # 移除尾部 DOI
            remainder = re.sub(r'https?://[^\s]+', '', remainder).strip()
            r_parts = [p.strip() for p in re.split(r'(?<=[a-zA-Z\d\)])\.\s+', remainder) if p.strip()]
            if len(r_parts) >= 2:
                parsed_title = r_parts[0]
                parsed_venue = '. '.join(r_parts[1:])
            elif len(r_parts) == 1:
                parsed_title = r_parts[0]

            if parsed_title:
                # 剔除附录/学位论文后缀 (如 '(PhD thesis)', '(Master thesis)')
                parsed_title = re.sub(r'\s*\((?:phd thesis|master thesis|msc thesis|doctoral dissertation|dissertation)\)', '', parsed_title, flags=re.I).strip(' .,;:')
        else:
            # C. 英文标准多作者分号 / 点切分 (IEEE / APA / Nature)
            semi_match = re.match(r'^((?:[A-Z][a-zA-Z\'-]+,\s+[A-Z]\.?(?:[A-Z]\.?)*(?:;\s*)?)+)\.?\s+(.*)', clean_t)
            if semi_match:
                parsed_authors = semi_match.group(1).strip(' .,;:')
                rest = semi_match.group(2).strip()
                r_parts = [p.strip() for p in re.split(r'(?<=[a-zA-Z\d\)])[\;\.]\s+', rest) if p.strip()]
                if r_parts:
                    parsed_title = r_parts[0]
                    if len(r_parts) > 1:
                        parsed_venue = '; '.join(r_parts[1:])
            else:
                parts = [p.strip() for p in re.split(r'(?:\.\s+|\.\Z)', clean_t) if p.strip()]
                if len(parts) >= 3:
                    parsed_authors = parts[0]
                    if parsed_year and str(parsed_year) in parts[1]:
                        parsed_title = parts[2] if len(parts) > 2 else parts[1]
                        parsed_venue = parts[3] if len(parts) > 3 else None
                    else:
                        parsed_title = parts[1]
                        parsed_venue = parts[2]
                elif len(parts) == 2:
                    parsed_authors = parts[0]
                    parsed_title = parts[1]
                elif len(parts) == 1:
                    parsed_title = parts[0]

    # 5. 题名质量拦截与清洗 (Title Quality Interception)
    if parsed_title:
        # 去除机构/组织后缀 (如 '; Food and Agriculture Organization...')
        if ';' in parsed_title:
            t_parts = re.split(r'\;\s*', parsed_title)
            if len(t_parts) >= 2 and any(kw in t_parts[1].lower() for kw in ['organization', 'publisher', 'isbn', 'press', 'university', 'department', 'ed.', 'eds.']):
                parsed_title = t_parts[0]

        norm_t = normalize_title(parsed_title)
        if len(norm_t) <= 2 or (re.match(r'^[a-zA-Z\d\s\.\,\-]+$', norm_t) and len(norm_t) <= 3):
            quality_flag = "SUSPECT_TITLE_TOO_SHORT"
            parsed_title = None
        elif re.match(r'^(?:18|19|20)\d{2}$', norm_t):
            quality_flag = "SUSPECT_TITLE_IS_YEAR"
            parsed_title = None
        elif is_author_fragment(parsed_title) or is_author_fragment(norm_t):
            quality_flag = "SUSPECT_TITLE_IS_AUTHOR"
            parsed_title = None
        else:
            parsed_title = norm_t

    if parsed_authors:
        parsed_authors = re.sub(r'[\r\n\t]+', ' ', parsed_authors).strip(' .,;:')
    if parsed_venue:
        parsed_venue = re.sub(r'[\r\n\t]+', ' ', parsed_venue).strip(' .,;:')

    # 判断解析完整度
    if parsed_doi and parsed_title:
        status = "PARSED_COMPLETE"
    elif parsed_title and (parsed_authors or parsed_year):
        status = "PARSED_COMPLETE"
    elif parsed_title or parsed_doi:
        status = "PARSED_PARTIAL"
    else:
        status = "PARSE_FAILED" if quality_flag in ("BLANK", "OCR_CORRUPTED") else "SUSPECT_FLAGGED"

    return ParsedReference(
        raw_text=raw_text,
        raw_length=raw_len,
        quality_flag=quality_flag,
        parsed_doi=parsed_doi,
        parsed_title=parsed_title,
        parsed_authors=parsed_authors,
        parsed_year=parsed_year,
        parsed_venue=parsed_venue,
        clean_text=clean_t,
        parse_status=status
    )


@dataclass
class ResolutionCandidate:
    candidate_work_id: str
    match_method: str  # exact_doi, exact_title, normalized_title_fuzzy, author_year_venue
    confidence_score: float  # 0.0 ~ 1.0
    match_evidence: Dict[str, Any]


@dataclass
class ResolutionDecision:
    decision_type: str  # LINK, NO_MATCH_IN_DEV_CANDIDATE_POOL, DEFER, REJECT
    decision_source: str  # RULE, MODEL, HUMAN, REVIEW_OVERRIDE
    selected_work_id: Optional[str]
    confidence_score: float
    reason: str
    candidates: List[ResolutionCandidate]


def generate_resolution_candidates(
    parsed: ParsedReference,
    known_works: List[Dict[str, Any]]
) -> List[ResolutionCandidate]:
    """在已知 Works 库中检索匹配候选，支持 DOI 精确、中英双题名完全与模糊、第一作者姓氏+年份多键召回。"""
    if parsed.quality_flag in ("OCR_CORRUPTED", "NON_REFERENCE_BACK_MATTER") or (not parsed.parsed_title and not parsed.parsed_doi and not parsed.parsed_authors):
        return []

    candidates = []
    norm_ref_title = normalize_title(parsed.parsed_title or "")
    ref_first_surname = extract_first_author_surname(parsed.parsed_authors)

    for w in known_works:
        work_id = w["work_id"]
        w_dois = [d.lower().strip() for d in w.get("dois", []) if d]
        w_titles = [normalize_title(t) for t in w.get("titles", []) if t]
        w_year = str(w.get("year") or "").strip()
        w_first_surname = extract_first_author_surname(w.get("authors", ""))

        # 1. Exact DOI match (最高置信度 1.0)
        if parsed.parsed_doi and parsed.parsed_doi in w_dois:
            candidates.append(ResolutionCandidate(
                candidate_work_id=work_id,
                match_method="exact_doi",
                confidence_score=1.0,
                match_evidence={"doi": parsed.parsed_doi, "rule": "exact_doi_equality"}
            ))
            continue

        # 2. Exact Title match (中英题名与别名全覆盖，置信度 0.96)
        if norm_ref_title and len(norm_ref_title) >= 6:
            if any(norm_ref_title == wt for wt in w_titles):
                candidates.append(ResolutionCandidate(
                    candidate_work_id=work_id,
                    match_method="exact_title",
                    confidence_score=0.96,
                    match_evidence={"title": norm_ref_title, "matched_title": norm_ref_title}
                ))
                continue

        # 3. First Author Surname + Year + Title Fuzzy Match (高置信度召回，解决中英文互译与长题名对齐)
        if ref_first_surname and len(ref_first_surname) >= 1 and ref_first_surname == w_first_surname:
            if parsed.parsed_year and w_year and abs(int(parsed.parsed_year) - int(w_year)) <= 1:
                # Check title similarity
                if norm_ref_title:
                    ref_tokens = set(re.findall(r'[\w\u4e00-\u9fa5]+', norm_ref_title))
                    for wt in w_titles:
                        wt_tokens = set(re.findall(r'[\w\u4e00-\u9fa5]+', wt))
                        if ref_tokens and wt_tokens:
                            jaccard = len(ref_tokens & wt_tokens) / len(ref_tokens | wt_tokens)
                            containment = len(ref_tokens & wt_tokens) / len(wt_tokens)
                            if jaccard >= 0.60 or containment >= 0.80:
                                candidates.append(ResolutionCandidate(
                                    candidate_work_id=work_id,
                                    match_method="author_year_venue",
                                    confidence_score=0.96,
                                    match_evidence={"author": ref_first_surname, "year": parsed.parsed_year, "jaccard": round(jaccard, 3), "containment": round(containment, 3)}
                                ))
                                break

        # 4. Fuzzy Normalized Title Match (Jaccard / Token Overlap)
        if norm_ref_title and len(norm_ref_title) >= 10:
            ref_tokens = set(re.findall(r'[\w\u4e00-\u9fa5]+', norm_ref_title))
            if ref_tokens:
                best_sim = 0.0
                best_t = ""
                for wt in w_titles:
                    wt_tokens = set(re.findall(r'[\w\u4e00-\u9fa5]+', wt))
                    if wt_tokens:
                        jaccard = len(ref_tokens & wt_tokens) / len(ref_tokens | wt_tokens)
                        if jaccard > best_sim:
                            best_sim = jaccard
                            best_t = wt
                if best_sim >= 0.85:
                    score = min(0.92, 0.70 + (best_sim - 0.85) * 1.4)
                    candidates.append(ResolutionCandidate(
                        candidate_work_id=work_id,
                        match_method="normalized_title_fuzzy",
                        confidence_score=round(score, 3),
                        match_evidence={"jaccard": round(best_sim, 3), "target_title": best_t}
                    ))

    # 按置信度从高到低排序
    candidates.sort(key=lambda c: c.confidence_score, reverse=True)
    return candidates


def make_resolution_decision(
    parsed: ParsedReference,
    candidates: List[ResolutionCandidate]
) -> ResolutionDecision:
    """根据候选证据做出消歧决策 (高置信 LINK、存疑 DEFER 或 预标注 NO_MATCH)。"""
    if parsed.quality_flag in ("OCR_CORRUPTED", "BLANK"):
        return ResolutionDecision(
            decision_type="REJECT",
            decision_source="RULE",
            selected_work_id=None,
            confidence_score=0.0,
            reason="OCR_CORRUPTED_FRAGMENT",
            candidates=[]
        )

    if not candidates:
        # 未在 DEV 候选池 (约 38 篇) 中检索到候选，不代表在全量 344 篇主库中不存在
        base_score = 0.85 if parsed.parse_status == "PARSED_COMPLETE" else 0.70
        return ResolutionDecision(
            decision_type="NO_MATCH_IN_DEV_CANDIDATE_POOL",
            decision_source="RULE",
            selected_work_id=None,
            confidence_score=base_score,
            reason="NO_MATCH_IN_DEV_CANDIDATE_POOL",
            candidates=[]
        )

    top = candidates[0]
    if top.confidence_score >= 0.95:
        # 如果有第2候选且分差过小
        if len(candidates) > 1 and (top.confidence_score - candidates[1].confidence_score) < 0.05:
            return ResolutionDecision(
                decision_type="DEFER",
                decision_source="RULE",
                selected_work_id=None,
                confidence_score=0.60,
                reason="AMBIGUOUS_MULTIPLE_HIGH_CANDIDATES",
                candidates=candidates
            )
        return ResolutionDecision(
            decision_type="LINK",
            decision_source="RULE",
            selected_work_id=top.candidate_work_id,
            confidence_score=top.confidence_score,
            reason=f"HIGH_CONFIDENCE_{top.match_method.upper()}",
            candidates=candidates
        )
    elif top.confidence_score >= 0.60:
        # 中等置信度进入人工复核队列 (DEFER)
        return ResolutionDecision(
            decision_type="DEFER",
            decision_source="RULE",
            selected_work_id=None,
            confidence_score=top.confidence_score,
            reason=f"MEDIUM_CONFIDENCE_{top.match_method.upper()}_REQUIRES_REVIEW",
            candidates=candidates
        )
    else:
        return ResolutionDecision(
            decision_type="NO_MATCH",
            decision_source="RULE",
            selected_work_id=None,
            confidence_score=1.0 - top.confidence_score,
            reason="LOW_CONFIDENCE_BELOW_THRESHOLD",
            candidates=candidates
        )
