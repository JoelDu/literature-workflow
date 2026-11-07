"""Opt-in structure-aware Markdown chunks, without model or database calls.

Inspired by RAGFlow's separation of tables, captions and section context. Row
grouping and formula protection are local enhancements, not copied algorithms.
Offsets refer to clean_markdown(md), never to PDF pages or bounding boxes.
"""
import re
from dataclasses import dataclass

from .chunker import Chunk, clean_markdown

STRATEGY_VERSION = "structured-v1"
_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
_CAPTION = re.compile(r"^(?:Table\s*\d|Tab\.\s*\d|表\s*\d)", re.I)
_NOTE = re.compile(r"^(?:Notes?\s*[:：.]|注\s*[:：])", re.I)
_DEFINITION = re.compile(r"^(?:where\b|in which\b|式中|其中)", re.I)
_ROW = re.compile(r"<tr\b[^>]*>.*?</tr\s*>", re.I | re.S)
_CELL = re.compile(r"<(?:td|th)\b([^>]*)>.*?</(?:td|th)\s*>", re.I | re.S)
_INLINE = re.compile(r"(?<![\\$])\$(?!\$)[^$\n]+(?<!\\)\$(?!\$)")
_PROTECTED = re.compile(
    r"<table\b[^>]*>.*?</table\s*>|"
    r"(?<!\\)\$\$.*?(?<!\\)\$\$|"
    r"\\\[.*?\\\]|"
    r"\\begin\{(?P<math_env>equation\*?|align\*?|gather\*?|multline\*?)\}"
    r".*?\\end\{(?P=math_env)\}|"
    r"^```[^\n]*\n.*?^```[^\n]*$|"
    r"^[ \t]*\|[^\n]+\|[ \t]*\n"
    r"[ \t]*\|(?:[ \t]*:?-{3,}:?[ \t]*\|)+[ \t]*(?:\n|$)"
    r"(?:[ \t]*\|[^\n]+\|[ \t]*(?:\n|$))*",
    re.I | re.S | re.M,
)


class StructureChunkError(ValueError):
    """Unsafe or oversized structure: callers must report, not truncate it."""


@dataclass
class StructuredChunk(Chunk):
    kind: str = "text"
    source_spans: tuple = ()
    source_unit: str = ""
    header_inferred: bool = False
    header_rows: int = 0
    warnings: tuple = ()


def _span(attrs: str, name: str) -> int:
    match = re.search(rf"\b{name}\s*=\s*['\"]?(\d+)", attrs, re.I)
    value = int(match.group(1)) if match else 1
    if value < 1:
        raise StructureChunkError(f"Unsupported {name}=0; preserve the original table")
    return value


def _safe_row_ends(rows: list) -> list:
    """Validate a simple rectangular grid and find closed rowspan boundaries."""
    active = {}
    width = None
    safe = []
    for index, row in enumerate(rows):
        occupied = {col for col, until in active.items() if until > index}
        cells = list(_CELL.finditer(row))
        if not cells:
            raise StructureChunkError("Table row has no complete cells")
        col = 0
        for cell in cells:
            while col in occupied:
                col += 1
            colspan = _span(cell.group(1), "colspan")
            rowspan = _span(cell.group(1), "rowspan")
            if index + rowspan > len(rows):
                raise StructureChunkError("Table rowspan extends beyond its source rows")
            columns = range(col, col + colspan)
            if any(c in occupied for c in columns):
                raise StructureChunkError("Overlapping table cells")
            for c in columns:
                occupied.add(c)
                if rowspan > 1:
                    active[c] = index + rowspan
            col += colspan
        row_width = max(occupied) + 1
        if occupied != set(range(row_width)) or (width is not None and row_width != width):
            raise StructureChunkError("Irregular table grid; cannot safely infer headers")
        width = row_width
        if not any(until > index + 1 for until in active.values()):
            safe.append(index + 1)
    return safe


def _paragraph_before(text: str, start: int) -> tuple:
    end = start
    while end and text[end - 1].isspace():
        end -= 1
    begin = text.rfind("\n\n", 0, end) + 2
    if begin == 1:
        begin = 0
    return begin, end


def _paragraph_after(text: str, end: int) -> tuple:
    start = end
    while start < len(text) and text[start].isspace():
        start += 1
    stop = text.find("\n\n", start)
    stop = len(text) if stop < 0 else stop
    heading = _HEADING.search(text, start, stop)
    return start, heading.start() if heading else stop


def _prose_spans(text: str, start: int, end: int, target: int, limit: int):
    """Slice original text at whitespace; never split/rejoin decimal points."""
    inline = [(m.start(), m.end()) for m in _INLINE.finditer(text, start, end)]
    pos = start
    while pos < end:
        while pos < end and text[pos].isspace():
            pos += 1
        if pos >= end:
            break
        if end - pos <= target:
            yield pos, end
            break
        ceiling = min(end, pos + target)
        boundaries = [m.start() for m in re.finditer(r"\s+|(?<=[。！？；])", text[pos:ceiling])]
        boundaries = [pos + b for b in boundaries
                      if b > 0 and not any(a < pos + b < z for a, z in inline)]
        if boundaries:
            stop = boundaries[-1]
        else:
            match = re.search(r"\s+|(?<=[。！？；])", text[ceiling:min(end, pos + limit)])
            stop = ceiling + match.start() if match else min(end, pos + limit)
            while any(a < stop < z for a, z in inline):
                stop = next(z for a, z in inline if a < stop < z)
            if stop - pos > limit or (stop < end and not text[stop].isspace()
                                     and text[stop - 1] not in "。！？；"):
                raise StructureChunkError("Unbreakable text or inline formula exceeds budget")
        yield pos, stop
        pos = stop


def split_structured(doc_id: str, md: str, chunk_size: int = 1000,
                     overlap: int = 150, max_chars: int = 5000,
                     table_chunk_size: int | None = None) -> list:
    if chunk_size < 30 or overlap < 0 or max_chars < 30 or max_chars > 5000:
        raise ValueError("Structured chunks need size >=30, overlap >=0, and 30<=max_chars<=5000")
    if table_chunk_size is not None and table_chunk_size < 30:
        raise ValueError("table_chunk_size must be >=30")
    text = clean_markdown(md)
    if not text:
        return []
    protected = list(_PROTECTED.finditer(text))
    # Reject malformed structures instead of letting the prose splitter corrupt them.
    residue = text
    for match in reversed(protected):
        residue = residue[:match.start()] + " " * (match.end() - match.start()) + residue[match.end():]
    if re.search(r"</?table\b|(?<!\\)\$\$|\\[\[\]]|\\(?:begin|end)\{(?:equation\*?|align\*?|gather\*?|multline\*?)\}", residue, re.I):
        raise StructureChunkError("Unbalanced source table or display formula")

    chunks = []
    headings = []

    def title():
        # Retain nearest two ancestors if the full path consumes too much budget.
        path = " > ".join(h[1] for h in headings)
        if len(path) > min(500, max_chars // 4):
            path = " > ".join(h[1] for h in headings[-2:])
        return path

    def emit(body, kind, spans, unit="", inferred=False, header_rows=0, warnings=()):
        heading = title()
        content = (heading + "\n" if heading else "") + body.strip()
        if len(content) > max_chars:
            raise StructureChunkError(f"{unit or kind} exceeds {max_chars}-character budget including context")
        if body.strip():
            chunks.append(StructuredChunk(doc_id, len(chunks), heading, content,
                                          kind, tuple(spans), unit, inferred, header_rows, tuple(warnings)))

    def prose(start, end):
        if not text[start:end].strip():
            return
        events = list(_HEADING.finditer(text, start, end))
        cursor = start
        for event in events + [None]:
            stop = event.start() if event else end
            limit = max_chars - len(title()) - 1
            if limit < 30:
                raise StructureChunkError("Section title leaves no content budget")
            target = min(chunk_size, limit)
            previous = None
            for a, b in _prose_spans(text, cursor, stop, target, limit):
                body = text[a:b]
                spans = [(a, b)]
                if previous and overlap:
                    p, q = previous
                    remaining = limit - len(body)
                    left = max(p, q - min(overlap, max(0, remaining - 1)))
                    # Move past a complete whitespace token; avoid arbitrary inline math tails.
                    boundary = re.search(r"\s+", text[left:q])
                    left = left + boundary.end() if boundary else q
                    if left < q and "$" not in text[left:q]:
                        body = text[left:q] + "\n" + body
                        spans.insert(0, (left, q))
                emit(body, "text", spans)
                previous = (a, b)
            if event:
                level = len(event.group(1))
                while headings and headings[-1][0] >= level:
                    headings.pop()
                headings.append((level, event.group(2).strip()))
                cursor = event.end()

    table_index = 0
    cursor = 0
    for index, match in enumerate(protected):
        raw = match.group()
        is_html = raw.lower().startswith("<table")
        is_markdown = raw.lstrip().startswith("|")
        is_table = is_html or is_markdown
        if is_html and len(re.findall(r"<table\b", raw, re.I)) != 1:
            raise StructureChunkError("Nested tables require original-source fallback")
        begin, end = match.start(), match.end()
        next_start = protected[index + 1].start() if index + 1 < len(protected) else len(text)
        prefix, suffix, context_spans = "", "", []
        if is_table:
            a, b = _paragraph_before(text, begin)
            if a >= cursor and b - a <= 600 and _CAPTION.match(text[a:b].strip()):
                prefix = text[a:b].strip() + "\n"
                context_spans.append((a, b))
                begin = a
            a, b = _paragraph_after(text, end)
            if b <= next_start and b - a <= 300 and _NOTE.match(text[a:b].strip()):
                suffix = "\n" + text[a:b].strip()
                context_spans.append((a, b))
                end = b
        elif not raw.startswith("```"):
            a, b = _paragraph_after(text, end)
            if (b <= next_start and b - a <= 600 and _DEFINITION.match(text[a:b].strip())
                    and b - match.start() + len(title()) + 1 <= max_chars):
                end = b

        prose(cursor, begin)
        if not is_table:
            emit(text[match.start():end], "code" if raw.startswith("```") else "formula",
                 [(match.start(), end)])
            cursor = end
            continue

        table_index += 1
        unit = f"table:{table_index}"
        heading_chars = len(title()) + (1 if title() else 0)
        hard = max_chars - heading_chars
        soft = min(table_chunk_size or chunk_size, hard)
        if len(prefix + raw + suffix) <= soft:
            emit(prefix + raw + suffix, "table", context_spans + [(match.start(), match.end())], unit)
            cursor = end
            continue

        if is_html:
            rows = list(_ROW.finditer(raw))
            try:
                if not rows:
                    raise StructureChunkError("Table has no complete rows")
                remainder = _ROW.sub("", raw)
                remainder = re.sub(r"<(?:caption|colgroup)\b[^>]*>.*?</(?:caption|colgroup)\s*>", "", remainder, flags=re.I | re.S)
                remainder = re.sub(r"</?(?:table|thead|tbody|tfoot)\b[^>]*>", "", remainder, flags=re.I)
                remainder = re.sub(r"<!--.*?-->", "", remainder, flags=re.S)
                if remainder.strip():
                    raise StructureChunkError("Table contains source content outside recognized rows")
                safe = _safe_row_ends([r.group() for r in rows])
            except StructureChunkError as exc:
                if len(prefix + raw + suffix) > hard:
                    raise StructureChunkError(f"{unit}: {exc}; original table also exceeds budget") from exc
                emit(prefix + raw + suffix, "table", context_spans + [(match.start(), match.end())],
                     unit, warnings=(f"Preserved original table without inferred headers: {exc}",))
                cursor = end
                continue
            header_count = 1
            thead = re.search(r"<thead\b[^>]*>(.*?)</thead\s*>", raw, re.I | re.S)
            if thead:
                header_count = sum(r.start() < thead.end() for r in rows)
            else:
                # MinerU generally emits td only; infer bounded grouping headers.
                while header_count < min(3, len(rows)) and any(
                    _span(cell.group(1), "colspan") > 1
                    for cell in _CELL.finditer(rows[header_count - 1].group())
                ):
                    header_count += 1
            header_count = next((n for n in safe if n >= header_count), len(rows))
            open_tag = re.match(r"<table\b[^>]*>", raw, re.I).group()
            caption = re.search(r"<caption\b[^>]*>.*?</caption\s*>", raw, re.I | re.S)
            columns = re.search(r"<colgroup\b[^>]*>.*?</colgroup\s*>", raw, re.I | re.S)
            opening = open_tag + (caption.group() if caption else "") + (columns.group() if columns else "")
            header = raw[rows[0].start():rows[header_count - 1].end()]
            closing = "</table>"
            groups = []
            pos = header_count
            for n in safe:
                if n > pos:
                    groups.append((pos, n, raw[rows[pos].start():rows[n - 1].end()],
                                   (match.start() + rows[pos].start(), match.start() + rows[n - 1].end())))
                    pos = n
            header_span = (match.start() + rows[0].start(), match.start() + rows[header_count - 1].end())
        else:
            lines = raw.splitlines(keepends=True)
            opening = closing = ""
            header = "".join(lines[:2]).rstrip("\n") + "\n"
            header_count = 2
            header_span = (match.start(), match.start() + sum(len(x) for x in lines[:2]))
            offset = header_span[1]
            groups = []
            for i, line in enumerate(lines[2:], 2):
                groups.append((i, i + 1, line, (offset, offset + len(line))))
                offset += len(line)

        context = prefix + opening + header
        tail = closing + suffix
        inferred = is_html and not bool(thead) and not bool(re.search(r"<th\b", header, re.I))
        if not groups:
            emit(prefix + raw + suffix, "table", context_spans + [(match.start(), match.end())], unit)
            cursor = end
            continue
        pending, pending_spans, first_row, last_row = "", [], 0, 0
        for first, last, body, span in groups:
            if len(context + body + tail) > hard:
                raise StructureChunkError(f"{unit} rows {first + 1}-{last}: indivisible row group exceeds budget")
            if pending and len(context + pending + body + tail) > soft:
                emit(context + pending + tail, "table", context_spans + [header_span] + pending_spans,
                     f"{unit}:rows:{first_row + 1}-{last_row}", inferred, header_count)
                pending, pending_spans = "", []
            if not pending:
                first_row = first
            pending += body
            pending_spans.append(span)
            last_row = last
        if pending:
            emit(context + pending + tail, "table", context_spans + [header_span] + pending_spans,
                 f"{unit}:rows:{first_row + 1}-{last_row}", inferred, header_count)
        cursor = end
    prose(cursor, len(text))
    # Formulas are indivisible units, not necessarily isolated search results.
    # Pack adjacent prose/math using original contiguous source text; table/code
    # boundaries remain independent and repeated overlap never alters the source.
    packed = []
    packable = {"text", "formula", "text_formula"}
    for chunk in chunks:
        previous = packed[-1] if packed else None
        if (previous and previous.kind in packable and chunk.kind in packable
                and previous.section_title == chunk.section_title):
            a = min(s[0] for s in previous.source_spans)
            b = max(s[1] for s in chunk.source_spans)
            raw = text[a:b]
            heading = previous.section_title
            content = (heading + "\n" if heading else "") + raw.strip()
            if len(content) <= min(chunk_size, max_chars) and not _HEADING.search(raw):
                kind = "text" if previous.kind == chunk.kind == "text" else "text_formula"
                packed[-1] = StructuredChunk(doc_id, 0, heading, content, kind, ((a, b),))
                continue
        packed.append(chunk)
    for index, chunk in enumerate(packed):
        chunk.chunk_index = index
    return packed
