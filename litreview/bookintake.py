"""教材/书籍轻量入库通道。

与论文流水线不同，书籍**不做 LLM 全文分析、不生成 Obsidian 笔记**——
只把整本 PDF（必要时先拆分绕过 MinerU 单任务页数上限）解析成 markdown、
直接落 status=EXPORTED 供检索库索引，另写一行 Excel 与一条 paper_details(doc_type='book')。
元数据本地正则为主，出版社/版次等由一次前置页 LLM 小调用补齐（可关）。
"""
import os
import re
import sqlite3
import tempfile
from datetime import datetime

from utils import calculate_pdf_hash, export_to_excel, log_run_event
from mineru_client import MinerUClient
from .store import VectorStore

_ISBN_RE = re.compile(r"ISBN[\s:：]*([0-9][0-9\-\s]{8,16}[0-9Xx])")
_YEAR_RE = re.compile(r"(19|20)\d{2}")
_CJK_RE = re.compile(r"[一-鿿]")

# 每本一次的前置页元数据小调用（只送封面/版权页附近的文本，几分钱级）
BOOK_META_PROMPT = """你是图书馆编目员。下面是一本书的文件名和正文前部（通常含封面、版权页）。
请只依据给出的文本提取著录信息，无法确定的字段返回空字符串，不要编造。

文件名：{filename}
正文前部：
{md_head}

以 JSON 返回（不要任何解释）：
{{"authors": "作者，多人用逗号分隔", "publisher": "出版社", "pub_place": "出版地(城市)", "edition": "版次，如 第2版；若是第1版或未标注则留空", "year": "出版年，4 位数字"}}"""


def split_pdf_if_needed(pdf_path: str, max_pages: int, workdir: str):
    """页数 > max_pages 时按每 max_pages 页切成多个子 PDF（写入 workdir，按页序）。
    返回 (子PDF路径列表, 总页数)。未超限时返回 ([原路径], 页数)。"""
    from pypdf import PdfReader, PdfWriter
    reader = PdfReader(pdf_path)
    n = len(reader.pages)
    if n <= max_pages:
        return [pdf_path], n
    base = os.path.splitext(os.path.basename(pdf_path))[0]
    parts = []
    for start in range(0, n, max_pages):
        writer = PdfWriter()
        for p in range(start, min(start + max_pages, n)):
            writer.add_page(reader.pages[p])
        part_path = os.path.join(workdir, f"{base}_part{start // max_pages + 1}.pdf")
        with open(part_path, "wb") as f:
            writer.write(f)
        parts.append(part_path)
    return parts, n


def stitch_markdown(parts_md: list) -> str:
    """按顺序拼接各部分 markdown，部分间以空行分隔。"""
    return "\n\n".join(m.strip() for m in parts_md if m and m.strip())


def _looks_chinese(text: str) -> bool:
    """CJK 字符占比 > 20% 判为中文（避免为语言判定再花一次 LLM）。"""
    if not text:
        return True
    cjk = len(_CJK_RE.findall(text))
    return cjk / max(1, len(text)) > 0.2


def _local_book_meta(pdf_path: str, md_head: str) -> dict:
    """纯本地解析：书名取文件名，年份/ISBN 正则扫前置页。"""
    title = os.path.splitext(os.path.basename(pdf_path))[0]
    isbn_m = _ISBN_RE.search(md_head or "")
    year_m = _YEAR_RE.search(md_head or "")
    return {
        "title": title, "doc_type": "book",
        "isbn": (isbn_m.group(1).strip() if isbn_m else ""),
        "year": (year_m.group(0) if year_m else ""),
        "authors": "", "publisher": "", "pub_place": "", "edition": "",
        "source": "local",
    }


def extract_book_meta(md_head: str, pdf_path: str, client, settings, use_llm: bool = True) -> dict:
    """本地为主 + 一次前置页 LLM 小调用补 authors/publisher/pub_place/edition/year（不覆盖书名）。"""
    meta = _local_book_meta(pdf_path, md_head)
    if use_llm and client is not None:
        try:
            from .stages import _chat_json
            prompt = BOOK_META_PROMPT.format(
                filename=os.path.basename(pdf_path), md_head=(md_head or "")[:2500])
            data = _chat_json(client, settings.REVIEW_MODEL, prompt)
            for k in ("authors", "publisher", "pub_place", "edition", "year"):
                v = str(data.get(k, "") or "").strip()
                if v and not meta.get(k):
                    meta[k] = v
            meta["source"] = "local+llm"
        except Exception:
            pass  # 保留本地结果
    return meta


def add_book(pdf_path: str, settings, console, client=None,
            max_pages: int = 180, use_llm: bool = True) -> str:
    """把一本书 PDF 入库为可检索语料。返回 doc_id。已入库(EXPORTED)则跳过。"""
    if not os.path.isfile(pdf_path):
        raise FileNotFoundError(pdf_path)
    title = os.path.splitext(os.path.basename(pdf_path))[0]
    doc_id = calculate_pdf_hash(pdf_path)   # 整本 hash → 幂等

    store = VectorStore(settings.DB_PATH, settings.EMBEDDING_DIM)  # 确保 schema/新列就绪
    conn = sqlite3.connect(settings.DB_PATH)
    conn.execute("PRAGMA busy_timeout=30000")
    row = conn.execute("SELECT status FROM papers WHERE id=?", (doc_id,)).fetchone()
    if row and row[0] == "EXPORTED":
        conn.close()
        console.print(f"[dim]已入库，跳过：《{title}》")
        return doc_id

    mineru = MinerUClient(settings.MINERU_API_KEY)
    out_root = os.path.join(settings.MINERU_OUTPUT_DIR, f"BOOK_{title}_{doc_id[:8]}")
    os.makedirs(out_root, exist_ok=True)

    with tempfile.TemporaryDirectory() as workdir:
        parts, n_pages = split_pdf_if_needed(pdf_path, max_pages, workdir)
        console.print(f"[cyan]《{title}》{n_pages} 页 → {len(parts)} 个子任务，逐份 MinerU 解析…")
        parts_md = []
        for i, part in enumerate(parts, 1):
            console.print(f"[cyan]  解析 part {i}/{len(parts)} …")
            res = mineru.process_pdf(part, os.path.join(out_root, f"part{i}"), timeout=3600)
            parts_md.append(res.get("markdown", ""))

    md_text = stitch_markdown(parts_md)
    if not md_text:
        conn.close()
        raise RuntimeError("MinerU 未返回任何 markdown 内容")

    lang = "zh" if _looks_chinese(md_text[:3000]) else "en"
    meta = extract_book_meta(md_text[:3000], pdf_path, client, settings, use_llm=use_llm)

    images_dir = os.path.join(out_root, "part1", "images")
    if not os.path.isdir(images_dir):
        images_dir = out_root
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        """INSERT OR REPLACE INTO papers
           (id, title, pdf_path, language, mineru_md, images_dir, status, processed_at)
           VALUES (?,?,?,?,?,?, 'EXPORTED', ?)""",
        (doc_id, title, pdf_path, lang, md_text, images_dir, now))
    conn.commit()
    conn.close()

    # paper_details：doc_type=book，预置 enriched_at 让通用 enrich 自动跳过书籍
    details = {
        "title": title,
        "title_zh": title if lang == "zh" else "",
        "title_en": title if lang == "en" else "",
        "doc_type": "book", "authors": meta.get("authors", ""),
        "publisher": meta.get("publisher", ""), "pub_place": meta.get("pub_place", ""),
        "edition": meta.get("edition", ""), "year": meta.get("year", ""),
        "isbn": meta.get("isbn", ""), "journal": "", "doi": "", "keywords": "",
        "n_figures": 0, "n_tables": 0, "source": meta.get("source", "local"),
    }
    store.save_enrichment(doc_id, details, assets=[], refs=[])

    export_to_excel([{
        "文献ID": doc_id, "标题": title, "文献类型": "教材/图书",
        "作者": meta.get("authors", ""), "年份": meta.get("year", ""),
        "出版社": meta.get("publisher", ""), "出版地": meta.get("pub_place", ""),
        "版次": meta.get("edition", ""), "ISBN": meta.get("isbn", ""),
        "文件路径": pdf_path, "语言": lang, "状态": "EXPORTED",
        "解析时间": now,
    }], settings.EXCEL_OUTPUT_PATH)

    log_run_event(mode="book", event="book_added", title=title, doc_id=doc_id,
                  status="success", extra={"pages": n_pages, "parts": len(parts),
                                           "source": meta.get("source", "local")})
    console.print(f"[bold green]✅ 入库：《{title}》(doc_id {doc_id[:8]}，{n_pages} 页，{len(parts)} 份)")
    console.print("[dim]  提示：运行 `python review.py index` 让它进入检索库。")
    return doc_id
