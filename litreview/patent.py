"""专利识别与出版阶段推导：纯本地正则，零网络、零 API 调用。

识别依据是 INID 码（WIPO ST.9 著录项目代码），全球专利文献通用且集中在扉页，
所以 enrich 已经取到的 md_head 前 3000 字就够判定——不额外读盘、不额外花钱。

【本模块的边界，改代码前务必看清】
只产出**事实**，即印在 PDF 上、永不改变的两项：
  · patent_no   专利号（末尾种别码 A/B/U… 本身就是出版阶段）；
  · filing_date 申请日（法定到期日由它 + 保护期算出，是纯算术）。

真正的法律状态——驳回、撤回、视为撤回、未缴年费失效、无效宣告、权利终止——
**不印在 PDF 上，而且会随时间变化**：一篇 2021 年授权的专利，PDF 永远是那个 PDF，
但它可能 2024 年因欠缴年费就失效了。所以本模块一律不猜法律状态。那部分只能由人工
（`python review.py patents --set <号> --status 驳回`）或将来的外部数据源写入
legal_status，且必须同时写 status_checked_at，否则无从判断这条状态是哪天的、还准不准。
"""
import re
from datetime import date

# ── INID 标记：命中 ≥2 个才判定为专利 ────────────────────────────────────
# 单个关键词会误杀——库里就有一篇 ResearchGate 章节，正文引用里出现过
# "United States Patent"，靠关键词匹配必然错分，靠 INID 组合则不会。
_MARKERS = [
    r"\(\s*21\s*\)\s*申请号",
    r"\(\s*22\s*\)\s*申请日",
    r"\(\s*54\s*\)\s*(?:发明名称|实用新型名称|发明创造名称)",
    r"\(\s*57\s*\)\s*摘要",
    r"\(\s*72\s*\)\s*发明人",
    r"\(\s*71\s*\)\s*申请人",
    r"\(\s*73\s*\)\s*专利权人",
    r"申请公布号",
    r"授权公告号",
    r"\(\s*74\s*\)\s*专利代理机构",
    r"United\s+States\s+Patent",
    r"Patent\s+Application\s+Publication",
    r"\(\s*21\s*\)\s*Appl\.?\s*No",
    r"\(\s*22\s*\)\s*Filed",
    r"\(\s*45\s*\)\s*Date\s+of\s+Patent",
    r"European\s+Patent\s+(?:Application|Specification)",
]
_MARKER_RES = [re.compile(p, re.I) for p in _MARKERS]
MIN_MARKERS = 2

# ── 专利号 ───────────────────────────────────────────────────────────────
# MinerU 会在数字中间塞空格（实测 "CN 112856361 B"、"2019 .06 .03"），全部要容忍。
_RE_GRANT_NO = re.compile(r"授权公告号\s*([A-Z]{2})\s*([\d\s]{5,15}?)\s*([A-Z]\d?)\b")
_RE_PUB_NO = re.compile(r"申请公布号\s*([A-Z]{2})\s*([\d\s]{5,15}?)\s*([A-Z]\d?)\b")
_RE_US_NO = re.compile(r"Patent\s+(?:No\.?|Number)\s*[:：]?\s*(US)\s*([\d,\s]{6,14}?)\s*([AB]\d?)\b", re.I)
_RE_ANY_NO = re.compile(r"\b(CN|US|EP|JP|KR|WO|DE|GB|FR)\s?([\d,\s]{5,15}?)\s?([A-Z]\d?)\b")

# ── 著录项目 ─────────────────────────────────────────────────────────────
_RE_FILING = re.compile(r"\(\s*22\s*\)\s*申请日\s*[:：]?\s*([\d\s./\-]{8,16})")
_RE_FILING_US = re.compile(r"\(\s*22\s*\)\s*Filed\s*[:：]?\s*([A-Za-z]{3,9}\.?\s+\d{1,2}\s*,\s*\d{4})")
_RE_APPNO = re.compile(r"\(\s*21\s*\)\s*申请号\s*[:：]?\s*([\d\s.]{8,20})")
_RE_INVENTORS = re.compile(r"\(\s*72\s*\)\s*发明人\s*[:：]?\s*([^\n]+)")
_RE_ASSIGNEE = re.compile(r"\(\s*73\s*\)\s*专利权人\s*[:：]?\s*([^\n]+)")
_RE_APPLICANT = re.compile(r"\(\s*71\s*\)\s*申请人\s*[:：]?\s*([^\n]+)")
_RE_TITLE = re.compile(r"\(\s*54\s*\)\s*(?:发明名称|实用新型名称|发明创造名称)\s*[:：]?\s*([^\n]*)")
_RE_IPC = re.compile(r"\b([A-H]\d{2}[A-Z]\s?\d{1,4}\s?/\s?\d{1,4})")

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}

# 中国保护期（专利法 2020 年修正）：发明 20 年、实用新型 10 年、外观设计 15 年，均自申请日起算。
# 键取种别码首字母，故 US 的 B1/B2/A1 也落到 20 年这一档。
_TERM_YEARS = {"A": 20, "B": 20, "C": 20, "U": 10, "Y": 10, "S": 15}

_STAGE_CN = {"A": "申请公布（审查中）", "B": "发明专利已授权", "C": "发明专利已授权（更正）",
             "U": "实用新型已授权", "Y": "实用新型已授权", "S": "外观设计已授权"}


def _norm_date(raw: str) -> str:
    """'2019 .06 .03' / '2021.03.08' / 'Mar. 8, 2021' → '2019-06-03'；认不出返回 ''。"""
    if not raw:
        return ""
    if raw.strip()[:1].isalpha():
        mm = re.match(r"([A-Za-z]{3,9})\.?\s+(\d{1,2})\s*,\s*(\d{4})", raw.strip())
        if mm and mm.group(1)[:3].lower() in _MONTHS:
            return f"{mm.group(3)}-{_MONTHS[mm.group(1)[:3].lower()]:02d}-{int(mm.group(2)):02d}"
        return ""
    s = re.sub(r"\s+", "", raw)
    m = re.search(r"(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})", s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    else:
        m = re.search(r"(\d{4})(\d{2})(\d{2})", s)
        if not m:
            return ""
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if not (1900 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31):
        return ""
    return f"{y:04d}-{mo:02d}-{d:02d}"


def _clean_no(country: str, digits: str, kind: str) -> str:
    return f"{country.upper()}{re.sub(r'[^0-9]', '', digits)}{kind.upper()}"


def _first_org(raw: str) -> str:
    """'昆明理工大学地址 650599 云南省…' → '昆明理工大学'。扉页里单位和地址是粘连的。"""
    if not raw:
        return ""
    s = re.split(r"地\s*址|Address", raw, maxsplit=1)[0]
    return re.sub(r"\s+", " ", s).strip(" ,;，；")


def extract_patent_no(text: str) -> str:
    """取专利号。授权公告号优先于申请公布号——同一篇专利两者都印在扉页上
    （实测 CN112856361 既有 (10)授权公告号 …B，又有 (65) 里的申请公布号 …A），
    授权号才代表它当前到达的出版阶段。"""
    for rx in (_RE_GRANT_NO, _RE_PUB_NO, _RE_US_NO):
        m = rx.search(text)
        if m:
            return _clean_no(m.group(1), m.group(2), m.group(3))
    m = _RE_ANY_NO.search(text)
    return _clean_no(m.group(1), m.group(2), m.group(3)) if m else ""


def kind_code(patent_no: str) -> str:
    """专利号尾部的种别码，如 'CN112856361B' → 'B'、'US10123456B2' → 'B2'。"""
    m = re.search(r"([A-Z]\d?)$", (patent_no or "").upper())
    return m.group(1) if m else ""


def patent_stage(patent_no: str) -> str:
    """出版阶段——印在 PDF 上、永不改变的事实，直接由种别码推出，不需要联网。
    注意这**不是**法律状态：种别码 B 只说明它当年被授权过，不代表今天仍然有效。"""
    kind = kind_code(patent_no)
    if not kind:
        return "未知"
    country = (patent_no or "")[:2].upper()
    if country == "US":
        return "申请公开（审查中）" if kind.startswith("A") and kind != "A" else "已授权"
    if country == "EP":
        return "申请公开（审查中）" if kind.startswith("A") else "已授权"
    return _STAGE_CN.get(kind[0], "未知")


def patent_expiry(patent_no: str, filing_date: str) -> str:
    """法定保护期届满日 = 申请日 + 保护期（发明 20 / 实用新型 10 / 外观 15 年）。
    纯算术，不查库。但它只是**期限上限**：欠缴年费、主动放弃、被宣告无效都会让专利
    早于此日失效，那些事件本模块看不到。"""
    if not filing_date:
        return ""
    years = _TERM_YEARS.get(kind_code(patent_no)[:1], 20)
    try:
        y, mo, d = (int(x) for x in filing_date.split("-"))
        try:
            return date(y + years, mo, d).isoformat()
        except ValueError:                      # 闰日 2 月 29 → 平年退到 28
            return date(y + years, mo, d - 1).isoformat()
    except Exception:
        return ""


def is_term_expired(patent_no: str, filing_date: str, today: date = None) -> bool:
    exp = patent_expiry(patent_no, filing_date)
    if not exp:
        return False
    return exp < (today or date.today()).isoformat()


def detect_patent(head: str) -> dict:
    """判定 head（正文前 3000 字）是否为专利扉页。是则返回著录项目 dict，否则返回 {}。

    命中 ≥MIN_MARKERS 个 INID 标记才算数，单个关键词一律不作数。
    """
    if not head:
        return {}
    hits = sum(1 for rx in _MARKER_RES if rx.search(head))
    if hits < MIN_MARKERS:
        return {}

    m_filing = _RE_FILING.search(head) or _RE_FILING_US.search(head)
    filing = _norm_date(m_filing.group(1)) if m_filing else ""

    m_inv = _RE_INVENTORS.search(head)
    m_own = _RE_ASSIGNEE.search(head) or _RE_APPLICANT.search(head)
    m_title = _RE_TITLE.search(head)
    m_app = _RE_APPNO.search(head)

    title = (m_title.group(1).strip() if m_title else "")
    if m_title and not title:
        # '## (54)发明名称' 单独成行时，名称在下一行（实测两种排版都存在）
        tail = head[m_title.end():].lstrip("\n")
        title = tail.split("\n", 1)[0].strip()

    patent_no = extract_patent_no(head)
    return {
        "patent_no": patent_no,
        "app_no": re.sub(r"\s+", "", m_app.group(1)) if m_app else "",
        "filing_date": filing,
        "title": title,
        "inventors": re.sub(r"\s+", " ", m_inv.group(1)).strip() if m_inv else "",
        "assignee": _first_org(m_own.group(1)) if m_own else "",
        "ipc": "; ".join(dict.fromkeys(
            re.sub(r"\s+", "", x) for x in _RE_IPC.findall(head)))[:300],
        "stage": patent_stage(patent_no),
        "expiry": patent_expiry(patent_no, filing),
        "markers": hits,
    }


# 人工可设置的法律状态。收敛成固定集合，免得同一含义写出七八种字面值导致后续没法统计。
LEGAL_STATUSES = ["审查中", "已授权", "驳回", "撤回", "视为撤回", "失效", "已过期", "无效", "未知"]


def describe(patent_no: str, filing_date: str, legal_status: str = "",
             checked_at: str = "") -> str:
    """给人看的一行状态描述，把「事实」和「人工核实结果」分开呈现，不含糊。"""
    parts = [f"出版阶段 {patent_stage(patent_no)}"]
    exp = patent_expiry(patent_no, filing_date)
    if exp:
        parts.append(f"保护期至 {exp}" + ("（已过期）" if is_term_expired(patent_no, filing_date) else ""))
    if legal_status:
        parts.append(f"法律状态 {legal_status}" + (f"（{checked_at} 核实）" if checked_at else ""))
    else:
        parts.append("法律状态 未核实")
    return " | ".join(parts)
