"""文献类型词表（GB/T 7714-2015 标识码）与「标准」的自动识别。

【为什么直接用国标的标识码当词表】
doc_type 最终要落到参考文献那一行的方括号里——论文 [J]、专著 [M]、专利 [P]、标准 [S]。
既然出口是国标，入口就别另造一套词，省得两边对不上还得维护一张映射表。
本模块是这套词表的**唯一出处**：store 建表、review 的命令行选项、stages 的引用格式、
chunker 的分块参数、mcp_server 的语料过滤，全部从这里取，加类型只改这一处。

【哪些能自动认、哪些只能人工指定——本模块最要紧的一段】
能自动认的，只有封面带**强制性格式**的两类：
  · 专利  → INID 著录项目码（见 patent.py）
  · 标准  → "中华人民共和国国家标准" + 发布/实施日期 + 标准号（本模块）
报告、财报、协会资料、网页存档**没有任何结构化特征**能把它们跟普通论文或书籍分开：
一份 FAO/IFA 年度展望的封面，和一本教材的封面，在正则眼里长得一模一样。硬猜必然误判，
所以本模块**不为它们提供检测器**，只能人工 `review.py set-type <ID> --type report` 指定。
宁可空着等人来填，也不要一个会悄悄标错、事后没人发现的分类器。
"""
import re

# ── 类型词表 ─────────────────────────────────────────────────────────────
# code   GB/T 7714-2015 文献类型标识码，直接印进参考文献
# label  中文名，给人看
# long   是不是长篇连续文本：是则用书籍那档更大的分块参数（见 chunker.chunk_params_for）
# detect 有没有自动识别器；False 表示只能人工指定
DOC_TYPES = {
    "paper":    {"code": "J",     "label": "期刊论文", "long": False, "detect": False},
    "book":     {"code": "M",     "label": "专著/教材", "long": True,  "detect": False},
    "patent":   {"code": "P",     "label": "专利",     "long": False, "detect": True},
    "standard": {"code": "S",     "label": "标准",     "long": False, "detect": True},
    "report":   {"code": "R",     "label": "报告",     "long": True,  "detect": False},
    "thesis":   {"code": "D",     "label": "学位论文", "long": True,  "detect": False},
    "conf":     {"code": "C",     "label": "会议论文", "long": False, "detect": False},
    "web":      {"code": "EB/OL", "label": "网络资源", "long": False, "detect": False},
}

DEFAULT_TYPE = "paper"          # 没判定出类型时的兜底，与 store 建表默认值保持一致
LONG_FORM = {k for k, v in DOC_TYPES.items() if v["long"]}

# 人工指定时能接受的别名：中文名、国标码、常见叫法。免得只能背英文键名。
_ALIASES = {
    "j": "paper", "论文": "paper", "期刊": "paper", "期刊论文": "paper", "文献": "paper",
    "m": "book", "书": "book", "书籍": "book", "教材": "book", "专著": "book", "图书": "book",
    "p": "patent", "专利": "patent",
    "s": "standard", "标准": "standard", "国标": "standard", "规范": "standard",
    "r": "report", "报告": "report", "研究报告": "report", "财报": "report",
    "年报": "report", "白皮书": "report", "协会报告": "report",
    "d": "thesis", "学位论文": "thesis", "硕士论文": "thesis", "博士论文": "thesis", "论著": "thesis",
    "c": "conf", "会议": "conf", "会议论文": "conf", "会议录": "conf",
    "eb/ol": "web", "eb-ol": "web", "ebol": "web", "网页": "web",
    "网站": "web", "网络": "web", "在线": "web",
}


def normalize(value: str) -> str:
    """'报告' / 'R' / 'report' → 'report'；认不出返回 ''（由调用方报错，不静默兜底成论文）。"""
    v = (value or "").strip().lower()
    if v in DOC_TYPES:
        return v
    return _ALIASES.get(v, "")


def gb_code(doc_type: str) -> str:
    """→ 'J' / 'M' / 'P' / 'S' / 'R' / 'EB/OL'。未知类型按论文处理。"""
    return DOC_TYPES.get(doc_type or DEFAULT_TYPE, DOC_TYPES[DEFAULT_TYPE])["code"]


def label(doc_type: str) -> str:
    return DOC_TYPES.get(doc_type or DEFAULT_TYPE, {}).get("label", doc_type or DEFAULT_TYPE)


def is_long_form(doc_type: str) -> bool:
    return (doc_type or DEFAULT_TYPE) in LONG_FORM


def all_types() -> list:
    return list(DOC_TYPES.keys())


# 需要人工核实、且会随时间变化的「状态」——专利和标准共用同一套机制
# （legal_status + status_checked_at 两列），因为它们面对的是同一个问题：
# 这份文件今天还有效吗？答案不印在 PDF 上，只能去官方渠道查，且必须记下核实日期。
STANDARD_STATUSES = ["现行", "即将实施", "被代替", "废止", "未知"]


def statuses_for(doc_type: str) -> list:
    """该类型允许的人工状态取值；没有"会失效"这回事的类型返回空列表。

    只有专利和标准有状态：它们会被驳回、被代替、被废止，而一篇已发表的论文
    不会哪天变得"不再有效"。返回空列表就是在说"这类文献没有状态可核实"——
    别给它退回专利那套选项，否则命令行会允许把一篇论文标成"已授权"。
    """
    from .patent import LEGAL_STATUSES
    return {"patent": LEGAL_STATUSES, "standard": STANDARD_STATUSES}.get(doc_type, [])


# ══════════════════════════════════════════════════════════════════════════
# 标准识别
# ══════════════════════════════════════════════════════════════════════════
# 判定标记：命中 ≥2 个才算。跟专利同一条纪律——单个关键词必然误杀，
# 一篇正文里引了 GB/T 1.1—2009 的普通论文，只靠"出现标准号"就会被错分。
# 中文标记在**去掉全部空白**的文本上匹配：实测封面有 "中 华 人 民 共 和 国 国 家 标 准"
# 这种逐字加空格的排版（MinerU 按字切块导致），不去空白一个都匹配不上。
_MARKERS_CJK = [
    r"中华人民共和国.{0,4}标准",
    r"\d{4}-\d{2}-\d{2}发布",
    r"\d{4}-\d{2}-\d{2}实施",
    r"国家标准化管理委员会|国家市场监督管理总局|国家质量监督检验检疫总局|国家环境保护总局",
    r"本标准(按照|规定了|代替|适用于|由|与)|本文件(按照|规定了|代替|适用于|由|与)",
    r"标准化技术委员会",
    r"食品安全国家标准",
]
_MARKERS_LATIN = [
    r"National\s+Standard\s+of\s+the\s+People",
    r"INTERNATIONAL\s+STANDARD",
    r"\bICS\s+\d",
]
_MARKER_RES = ([(re.compile(p), True) for p in _MARKERS_CJK] +
               [(re.compile(p, re.I), False) for p in _MARKERS_LATIN])
MIN_MARKERS = 2

# 常见标准代号前缀：国标、行标、地标、团标（T/xxx 另行处理）
_PREFIXES = ("GB|HG|NY|DL|SH|JB|QB|YS|SN|HJ|JC|CJ|TB|MT|AQ|WS|FZ|LY|SY|SL|QC|JT|JG|"
             "CB|EJ|SJ|YB|YY|JJG|JJF|SB|WB|MH|TY|DB")

# 破折号实测有四种写法：ASCII '-'、连接号 '‐'、短破折 '–'、长破折 '—'，还可能两边带空格。
_DASH = r"\s*[-‐–—－]\s*"
_RE_STD_NO = re.compile(
    rf"\b({_PREFIXES})\s*[/／]?\s*(T|Z)?\s*(\d{{1,6}}(?:\s*[.．]\s*\d{{1,4}})?)"
    rf"{_DASH}(\d{{4}})\b")
_RE_STD_NO_TEAM = re.compile(rf"\bT\s*[/／]\s*([A-Z]{{2,10}})\s*(\d{{1,6}}){_DASH}(\d{{4}})\b")
_RE_STD_NO_ISO = re.compile(r"\b(ISO|IEC|EN|ASTM|ANSI|JIS|DIN|BS)\s*([A-Z]?\s*\d{1,6}(?:-\d{1,3})?)"
                            r"\s*[:：]\s*(\d{4})\b")

# 出现这些词的行里的标准号不是"本标准自己的号"，而是被代替的旧版、起草依据或引用文件。
# 实测 GB/T 1677—2008 封面上，"代替 GB/T 1677—1981" 排在自己的号之前，不排除就会取错。
_RE_NOT_SELF = re.compile(r"代替|按照|依据|采用|引用|参见|见\s*GB|规则起草|相比")

_RE_ISSUED = re.compile(r"(\d{4})-(\d{2})-(\d{2})\s*发布")
_RE_EFFECTIVE = re.compile(r"(\d{4})-(\d{2})-(\d{2})\s*实施")
_RE_ISSUER = re.compile(r"(国家市场监督管理总局|国家质量监督检验检疫总局|国家环境保护总局|"
                        r"生态环境部|国家标准化管理委员会|国家卫生健康委员会|工业和信息化部)")
_RE_PUBLISHER = re.compile(r"(中国标准出版社|中国质检出版社|中国计划出版社|中国建筑工业出版社)")

# MinerU 对某些标准 PDF 的符号字体解码错位，把拉丁字母吐成"犬"部生僻字：
# 实测 GB/T 7363—2021 封面上 "GB／T" 变成 "犌犅／犜"，英文标题整行变成 "犜犲狊狋犿犲狋犺狅犱…"。
# 这里只修标准号需要的三个字母（有实样验证），不去猜整张码表——认不出号也不影响判定，
# 标记够 2 个照样归类为标准，号留空由人工 set-type --no 补。
_DEMOJI = str.maketrans({"犌": "G", "犅": "B", "犜": "T", "／": "/", "．": "."})

# 整行都是这类生僻字 = 乱码行，跳过（真中文标题不会通篇用犬部字）
_RE_MOJI_CHAR = re.compile(r"[犀-狿]")

# 单独成行的"体系名"，本身不是标准名称，但要接到后面的名称前面拼成全称
_SERIES_LINES = ("食品安全国家标准", "国家标准", "中华人民共和国国家标准")


# MinerU 会把标准号里的斜杠和破折号单独包成上下标：实测封面原文是
# "GB<sup>/</sup>T32952<sup>—</sup>2016"、标题里也有 "石油产品<sub>、</sub>润滑油"。
# 不先剥掉标签，正则在 "GB" 后面撞见 "<" 就断了，整份标准的号都取不出来。
_RE_MARKUP = re.compile(r"</?[a-zA-Z][^>]*>")


def _prep_line(line: str) -> str:
    """一行封面文本 → 可供正则匹配的干净文本：剥标签、去 Markdown 标题号、修字体乱码。"""
    return _RE_MARKUP.sub("", line).strip().lstrip("#").strip().translate(_DEMOJI)


def _fmt_no(prefix: str, t: str, num: str, year: str) -> str:
    num = re.sub(r"\s+", "", num).replace("．", ".")
    return f"{prefix.upper()}{'/' + t.upper() if t else ''} {num}-{year}"


def _match_domestic(line: str) -> str:
    """国内标准号：团标 T/xxx 优先，其次国标/行标。"""
    m = _RE_STD_NO_TEAM.search(line)
    if m:
        return f"T/{m.group(1)} {m.group(2)}-{m.group(3)}"
    m = _RE_STD_NO.search(line)
    return _fmt_no(m.group(1), m.group(2) or "", m.group(3), m.group(4)) if m else ""


def _match_foreign(line: str) -> str:
    m = _RE_STD_NO_ISO.search(line)
    if not m:
        return ""
    return f"{m.group(1)} {re.sub(r'[ ]+', '', m.group(2))}:{m.group(3)}"


def extract_std_no(head: str) -> str:
    """取这份标准**自己**的编号。

    四轮，顺序有讲究：
      1) 国内号 + 独占一行 —— 封面上标准号总是单独一行，这是最可靠的信号；
      2) 国内号 + 任意行；
      3/4) 前两轮都没有，才认 ISO/ASTM 这类外文号。
    国内号必须排在外文号前面：等同采用国际标准的封面会印成
    "GB/T 17376—2008/ISO 5509:2000"，先认外文号就会把别人的号当成自己的。
    每轮都跳过含"代替/按照/引用"的行——那些号是旧版或起草依据，不是自己的。
    """
    lines = [ln for ln in (_prep_line(x) for x in (head or "").splitlines()) if ln]
    for finder in (_match_domestic, _match_foreign):
        for standalone_only in (True, False):
            for ln in lines:
                if _RE_NOT_SELF.search(ln):
                    continue
                no = finder(ln)
                if not no:
                    continue
                if standalone_only and len(ln) > len(no) + 20:
                    continue
                return no
    return ""


def _is_mojibake(line: str) -> bool:
    cjk = [c for c in line if "一" <= c <= "鿿"]
    if len(cjk) < 3:
        return False
    return sum(1 for c in cjk if _RE_MOJI_CHAR.match(c)) / len(cjk) > 0.5


def _extract_title(head: str, std_no: str) -> str:
    """标准号之后的第一行中文，就是标准名称。跳过代替行、英文名、乱码行。"""
    lines = [ln for ln in (_prep_line(x) for x in (head or "").splitlines()) if ln]
    start = 0
    if std_no:
        key = std_no.split()[-1].split(":")[0].split("-")[0]      # '1677' / '1886.64'
        for i, ln in enumerate(lines):
            if key in ln and not _RE_NOT_SELF.search(ln):
                start = i + 1
                break
    series = ""
    for ln in lines[start:start + 12]:
        if _RE_NOT_SELF.search(ln) or _RE_ISSUED.search(ln) or _RE_EFFECTIVE.search(ln):
            continue
        # 体系名单独成行，本身不是标准名称。封面常逐字加空格排版，要去空白后再比。
        squished_ln = re.sub(r"\s+", "", ln)
        if squished_ln in _SERIES_LINES:
            series = "" if squished_ln == "中华人民共和国国家标准" else squished_ln
            continue
        if _is_mojibake(ln):
            continue
        cjk = sum(1 for c in ln if "一" <= c <= "鿿")
        if cjk < 2 or cjk < len(ln) * 0.4:            # 英文名行、纯符号行
            continue
        if not (2 <= len(ln) <= 60):
            continue
        return (f"{series} {ln}" if series else ln).strip()
    return ""


def _extract_title_en(head: str) -> str:
    """英文标题：封面上跟在中文名后的那行纯英文。乱码行不要。"""
    for raw in (head or "").splitlines():
        ln = _prep_line(raw)
        if not (12 <= len(ln) <= 160) or _is_mojibake(ln):
            continue
        letters = sum(1 for c in ln if c.isascii() and c.isalpha())
        if letters >= len(ln) * 0.7 and " " in ln and not ln.lower().startswith(("http", "www")):
            return ln
    return ""


def detect_standard(head: str) -> dict:
    """判定 head（正文前 3000 字）是否为标准文本。是则返回著录项目 dict，否则 {}。

    命中 ≥MIN_MARKERS 个标记才算数。注意本函数只产出**印在封面上的事实**
    （编号、名称、发布/实施日期、发布机构）；"现行/被代替/废止"这类会随时间变化的
    状态不在封面上，跟专利的法律状态一样只能人工核实后写进 legal_status。
    """
    if not head:
        return {}
    squished = re.sub(r"\s+", "", head)
    hits = sum(1 for rx, cjk in _MARKER_RES if rx.search(squished if cjk else head))
    if hits < MIN_MARKERS:
        return {}

    std_no = extract_std_no(head)
    m_iss, m_eff = _RE_ISSUED.search(head), _RE_EFFECTIVE.search(head)
    issued = "-".join(m_iss.groups()) if m_iss else ""
    effective = "-".join(m_eff.groups()) if m_eff else ""
    m_pub = _RE_PUBLISHER.search(head)
    issuers = list(dict.fromkeys(_RE_ISSUER.findall(squished)))
    # 年份优先取发布日期；封面没印发布日期时退回标准号里的版本年（GB/T 1677-2008 → 2008）
    year = issued[:4] if issued else (std_no[-4:] if std_no[-4:].isdigit() else "")

    return {
        "doc_no": std_no,
        "title": _extract_title(head, std_no),
        "title_en": _extract_title_en(head),
        "issued": issued,
        "effective": effective,
        "year": year,
        "issuer": "、".join(issuers),
        "publisher": m_pub.group(1) if m_pub else "",
        "markers": hits,
    }


def describe_standard(doc_no: str, effective: str, status: str = "", checked_at: str = "") -> str:
    """给人看的一行状态。实施日期是封面事实；现行与否是人工核实结果，两者分开说。"""
    parts = [doc_no or "编号未识别"]
    if effective:
        parts.append(f"{effective} 实施")
    parts.append(f"状态 {status}" + (f"（{checked_at} 核实）" if checked_at else "")
                 if status else "状态 未核实")
    return " | ".join(parts)
