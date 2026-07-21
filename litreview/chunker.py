"""markdown 清洗与标题感知分块。

分块来源是 papers.mineru_md（MinerU 输出的完整 markdown），
不使用有损的 extract_key_sections。
"""
import re
from dataclasses import dataclass


@dataclass
class Chunk:
    doc_id: str
    chunk_index: int
    section_title: str
    content: str


# 文末参考文献段标题（仅当出现在全文后 40% 时才截断，防止误伤正文中的引用讨论）
_REFS_HEADING_RE = re.compile(
    r"^#{0,6}\s*(参考文献|References|REFERENCES|Bibliography)\s*[:：]?\s*$",
    re.MULTILINE,
)
_IMG_MD_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_IMG_HTML_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_HEADING_RE = re.compile(r"^(#{1,4})\s+(.+?)\s*$", re.MULTILINE)
# 中英文句末标点（用于句子级切分与重叠对齐）
_SENT_SPLIT_RE = re.compile(r"(?<=[。！？；.!?])")


def clean_markdown(md: str) -> str:
    """剔除图片链接、截断文末参考文献列表、压缩多余空行。"""
    text = _IMG_MD_RE.sub("", md)
    text = _IMG_HTML_RE.sub("", text)

    # 找最后一个参考文献标题；只有它落在全文后 40% 时才截断
    last_match = None
    for m in _REFS_HEADING_RE.finditer(text):
        last_match = m
    if last_match and last_match.start() > len(text) * 0.6:
        text = text[: last_match.start()]

    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _split_oversized(body: str, chunk_size: int) -> list:
    """递归切分超长文本：段落 → 行 → 句子 → 硬切。"""
    if len(body) <= chunk_size:
        return [body] if body.strip() else []

    for sep_split in (
        lambda t: t.split("\n\n"),
        lambda t: t.split("\n"),
        lambda t: _SENT_SPLIT_RE.split(t),
    ):
        parts = [p for p in sep_split(body) if p.strip()]
        if len(parts) > 1:
            # 贪心合并相邻片段到 chunk_size 以内
            merged, cur = [], ""
            for p in parts:
                if cur and len(cur) + len(p) + 1 > chunk_size:
                    merged.append(cur)
                    cur = p
                else:
                    cur = f"{cur}\n{p}" if cur else p
            if cur:
                merged.append(cur)
            # 个别片段仍超长（如无标点长表格），递归处理
            result = []
            for m in merged:
                if len(m) > chunk_size * 1.3:
                    result.extend(_split_oversized(m, chunk_size))
                else:
                    result.append(m)
            return result

    # 完全无分隔符：硬切
    return [body[i : i + chunk_size] for i in range(0, len(body), chunk_size)]


def _tail_overlap(text: str, overlap: int) -> str:
    """取末尾 overlap 字符，向前对齐到句子边界。"""
    if len(text) <= overlap:
        return text
    tail = text[-overlap:]
    m = _SENT_SPLIT_RE.search(tail)
    if m and m.end() < len(tail):
        return tail[m.end():]
    return tail


def split_markdown(doc_id: str, md: str, chunk_size: int = 1000, overlap: int = 150) -> list:
    """清洗后按标题分节、递归切分为带重叠的 Chunk 列表。"""
    text = clean_markdown(md)
    if not text:
        return []

    # 按标题切成 (heading, body) 块；无标题则整篇一块
    blocks = []
    matches = list(_HEADING_RE.finditer(text))
    if not matches:
        blocks.append(("", text))
    else:
        if matches[0].start() > 0:
            blocks.append(("", text[: matches[0].start()]))
        for i, m in enumerate(matches):
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            blocks.append((m.group(2).strip(), text[m.end() : end]))

    # 每块内递归切分，块内小片段合并
    raw = []  # (section_title, content)
    for heading, body in blocks:
        pieces = _split_oversized(body.strip(), chunk_size)
        # 同一标题下的过小片段并入前一片段
        merged = []
        for p in pieces:
            if merged and len(merged[-1]) < chunk_size * 0.4:
                merged[-1] = merged[-1] + "\n" + p
            else:
                merged.append(p)
        for p in merged:
            raw.append((heading, p))

    # 相邻块间加重叠前缀（跨标题不加，避免上一节尾巴污染下一节语义）
    chunks = []
    for i, (heading, content) in enumerate(raw):
        body = content.strip()
        if i > 0 and raw[i - 1][0] == heading and overlap > 0:
            prefix = _tail_overlap(raw[i - 1][1].strip(), overlap)
            if prefix and not body.startswith(prefix):
                body = prefix + "\n" + body
        if len(body) < 30:
            continue
        # 小节标题并入块内容，让每个块自带上下文（对 embedding 检索有利）
        text_out = f"{heading}\n{body}" if heading else body
        chunks.append(Chunk(doc_id=doc_id, chunk_index=len(chunks), section_title=heading, content=text_out))

    return chunks
