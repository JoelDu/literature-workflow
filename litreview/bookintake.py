"""教材/书籍轻量入库通道。

与论文流水线不同，书籍**不做 LLM 全文分析、不生成 Obsidian 笔记**——
只把整本 PDF（必要时先拆分绕过 MinerU 单任务页数上限）解析成 markdown、
直接落 status=EXPORTED 供检索库索引，另写一行 Excel 与一条 paper_details(doc_type='book')。
元数据本地正则为主，出版社/版次等由一次前置页 LLM 小调用补齐（可关）。
"""
import os
import re
import glob
import zipfile
import sqlite3
import tempfile
import subprocess
from datetime import datetime
from xml.etree import ElementTree as ET

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


def collect_book_assets(out_root: str, folder_base: str, n_parts: int, max_pages: int) -> list:
    """从各 part 的 content_list.json 汇总图/表资产，供综述插图引用。
    img_path 前缀成相对 BOOK_OUTPUT_DIR 根的路径（folder_base/part{i}/images/...），
    page_idx 按 part 顺序偏移为整本页码。复用 enrich 的本地解析器。"""
    from .enrich import _load_content_list, parse_content_list
    assets = []
    for i in range(1, n_parts + 1):
        part_dir = os.path.join(out_root, f"part{i}")
        blocks = _load_content_list(part_dir)
        if not blocks:
            continue
        parsed = parse_content_list(blocks, f"{folder_base}/part{i}")
        offset = (i - 1) * max_pages
        for a in parsed["assets"]:
            if a.get("page_idx") is not None:
                a["page_idx"] = a["page_idx"] + offset
            assets.append(a)
    return assets


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


def merge_book_docx(part_docx: list, dest_path: str) -> str:
    """把各 part 的 MinerU 原生 Word 顺序合并成整本 Word（docxcompose，保留表格/公式/图）。
    part_docx 为按页序排列的 .docx 路径列表（可含 None，会被跳过）。单份时直接另存。"""
    from docx import Document
    from docxcompose.composer import Composer
    paths = [p for p in part_docx if p and os.path.isfile(p)]
    if not paths:
        raise RuntimeError("MinerU 未返回任何 Word（docx），无法合并")
    master = Document(paths[0])
    composer = Composer(master)
    for p in paths[1:]:
        composer.append(Document(p))
    composer.save(dest_path)
    return dest_path


def export_book_docx(out_root: str, n_parts: int, dest_path: str) -> str:
    """把各 part 的 MinerU markdown 合成一份带图的可读 Word（pandoc）。
    从磁盘读取 part{i}/*.md（可独立于 add_book 重跑），图片相对路径 images/ 前缀成
    part{i}/images/ 并用 --resource-path=out_root 让 pandoc 找到图。"""
    from .stages import _find_pandoc
    parts_md = []
    for i in range(1, n_parts + 1):
        part_dir = os.path.join(out_root, f"part{i}")
        mds = glob.glob(os.path.join(part_dir, "*.md"))
        if not mds:
            continue
        with open(max(mds, key=os.path.getsize), "r", encoding="utf-8") as f:
            txt = f.read()
        txt = re.sub(r"(!\[[^\]]*\]\()images/", rf"\1part{i}/images/", txt)
        parts_md.append(txt)
    if not parts_md:
        raise RuntimeError("未找到任何 part 的 markdown，无法生成 Word")
    proc = subprocess.run(
        [_find_pandoc(), "-f", "markdown", "-t", "docx",
         f"--resource-path={out_root}", "-o", dest_path],
        input="\n\n".join(parts_md).encode("utf-8"), capture_output=True, timeout=600)
    if proc.returncode != 0:
        raise RuntimeError(f"pandoc 转换失败: {proc.stderr.decode('utf-8', 'ignore')}")
    return dest_path


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
    folder_base = f"{title}_{doc_id[:8]}"
    out_root = os.path.join(settings.BOOK_OUTPUT_DIR, folder_base)
    os.makedirs(out_root, exist_ok=True)

    with tempfile.TemporaryDirectory() as workdir:
        parts, n_pages = split_pdf_if_needed(pdf_path, max_pages, workdir)
        console.print(f"[cyan]《{title}》{n_pages} 页 → {len(parts)} 个子任务，逐份 MinerU 解析…")
        parts_md = []
        part_docx = []          # 各 part 的 MinerU 原生 Word（extra_formats=docx）
        for i, part in enumerate(parts, 1):
            console.print(f"[cyan]  解析 part {i}/{len(parts)} …")
            res = mineru.process_pdf(part, os.path.join(out_root, f"part{i}"), timeout=3600)
            parts_md.append(res.get("markdown", ""))
            part_docx.append(res.get("docx_path"))

    md_text = stitch_markdown(parts_md)
    if not md_text:
        conn.close()
        raise RuntimeError("MinerU 未返回任何 markdown 内容")

    # 抽取书内图/表 → paper_assets，让综述能引用教材里的图（引用该书的章节时候选）
    book_assets = collect_book_assets(out_root, folder_base, len(parts), max_pages)

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
        "n_figures": sum(1 for a in book_assets if a["asset_type"] in ("image", "chart")),
        "n_tables": sum(1 for a in book_assets if a["asset_type"] == "table"),
        "source": meta.get("source", "local"),
    }
    store.save_enrichment(doc_id, details, assets=book_assets, refs=[])

    export_to_excel([{
        "文献ID": doc_id, "标题": title, "文献类型": "教材/图书",
        "作者": meta.get("authors", ""), "年份": meta.get("year", ""),
        "出版社": meta.get("publisher", ""), "出版地": meta.get("pub_place", ""),
        "版次": meta.get("edition", ""), "ISBN": meta.get("isbn", ""),
        "文件路径": pdf_path, "语言": lang, "状态": "EXPORTED",
        "解析时间": now,
    }], settings.EXCEL_OUTPUT_PATH)

    # 可读 Word：优先合并 MinerU 原生 docx（保留表格/公式/图），
    # 缺失时才退回本地 pandoc 拼装。失败均不影响入库/检索。
    dest_docx = os.path.splitext(pdf_path)[0] + ".docx"
    docx_path = ""
    try:
        docx_path = merge_book_docx(part_docx, dest_docx)
    except Exception as e:
        console.print(f"[yellow]⚠️ MinerU 原生 Word 合并失败，改用 pandoc 拼装: {e}")
        try:
            docx_path = export_book_docx(out_root, len(parts), dest_docx)
        except Exception as e2:
            console.print(f"[yellow]⚠️ 可读 Word 生成失败（不影响入库/检索）: {e2}")

    log_run_event(mode="book", event="book_added", title=title, doc_id=doc_id,
                  status="success", extra={"pages": n_pages, "parts": len(parts),
                                           "figures": len(book_assets),
                                           "docx": docx_path,
                                           "source": meta.get("source", "local")})
    console.print(f"[bold green]✅ 入库：《{title}》(doc_id {doc_id[:8]}，{n_pages} 页，"
                  f"{len(parts)} 份，图表 {len(book_assets)} 项)")
    if docx_path:
        console.print(f"[green]  可读 Word: {docx_path}")
    console.print("[dim]  提示：运行 `python review.py index` 让它进入检索库。")
    return doc_id


# ----------------------------------------------------------------------------
# EPUB：数字文本，无需 MinerU / 拆分。pandoc 直接 epub→markdown（入库）+ epub→docx（阅读）。
# ----------------------------------------------------------------------------
_DC = "{http://purl.org/dc/elements/1.1/}"
_CONTAINER_NS = "{urn:oasis:names:tc:opendocument:xmlns:container}"


def extract_epub_meta(epub_path: str) -> dict:
    """从 EPUB 的 OPF(dc: 元数据)本地读取书名/作者/出版社/年份/ISBN。读不到则回退文件名。"""
    title = os.path.splitext(os.path.basename(epub_path))[0]
    meta = {"title": title, "doc_type": "book", "authors": "", "publisher": "",
            "pub_place": "", "edition": "", "year": "", "isbn": "", "source": "epub"}
    try:
        with zipfile.ZipFile(epub_path) as z:
            container = ET.fromstring(z.read("META-INF/container.xml"))
            opf_path = container.find(f".//{_CONTAINER_NS}rootfile").get("full-path")
            opf = ET.fromstring(z.read(opf_path))

            def _vals(tag):
                return [e.text.strip() for e in opf.iter(f"{_DC}{tag}") if e.text and e.text.strip()]

            titles = _vals("title")
            if titles:
                meta["title"] = titles[0]                 # OPF 书名优先于文件名
            meta["authors"] = ", ".join(_vals("creator"))
            pubs = _vals("publisher")
            if pubs:
                meta["publisher"] = pubs[0]
            for d in _vals("date"):
                ym = _YEAR_RE.search(d)
                if ym:
                    meta["year"] = ym.group(0)
                    break
            for ident in _vals("identifier"):
                digits = re.sub(r"[^0-9Xx]", "", ident)
                if "isbn" in ident.lower() or len(digits) in (10, 13):
                    meta["isbn"] = ident.split(":")[-1].strip()
                    break
    except Exception:
        pass  # 元数据缺失不阻断入库
    return meta


def epub_asset_relpath(src: str, mineru_output_dir: str) -> str:
    """pandoc epub --extract-media 输出的图片 src 转成 mineru_output 根相对路径。

    pandoc 打印的 src 已经是相对当前工作目录的路径（本身就带 out_root 前缀），
    不能再和 out_root 拼接，否则路径重复、图片找不到（曾用真实 EPUB 测出此坑）。
    """
    absimg = os.path.abspath(src)
    try:
        return os.path.relpath(absimg, os.path.abspath(mineru_output_dir))
    except ValueError:
        return src


def add_epub(epub_path: str, settings, console, client=None, use_llm: bool = True) -> str:
    """把一本 EPUB 入库为可检索语料（pandoc 转换，无 MinerU）。返回 doc_id。已入库则跳过。"""
    if not os.path.isfile(epub_path):
        raise FileNotFoundError(epub_path)
    fallback_title = os.path.splitext(os.path.basename(epub_path))[0]
    doc_id = calculate_pdf_hash(epub_path)   # 按文件字节 hash → 幂等

    store = VectorStore(settings.DB_PATH, settings.EMBEDDING_DIM)
    conn = sqlite3.connect(settings.DB_PATH)
    conn.execute("PRAGMA busy_timeout=30000")
    row = conn.execute("SELECT status FROM papers WHERE id=?", (doc_id,)).fetchone()
    if row and row[0] == "EXPORTED":
        conn.close()
        console.print(f"[dim]已入库，跳过：《{fallback_title}》")
        return doc_id

    from .stages import _find_pandoc
    pandoc = _find_pandoc()
    folder_base = f"{fallback_title}_{doc_id[:8]}"
    out_root = os.path.join(settings.BOOK_OUTPUT_DIR, folder_base)
    os.makedirs(out_root, exist_ok=True)

    # epub → markdown（并把内嵌图片抽到 out_root/media/）
    proc = subprocess.run(
        [pandoc, "-f", "epub", "-t", "gfm", f"--extract-media={out_root}", epub_path],
        capture_output=True, timeout=600)
    if proc.returncode != 0:
        conn.close()
        raise RuntimeError(f"pandoc epub→markdown 失败: {proc.stderr.decode('utf-8', 'ignore')[:300]}")
    raw_md = proc.stdout.decode("utf-8", "ignore").strip()
    if not raw_md:
        conn.close()
        raise RuntimeError("EPUB 解析为空")

    # 图片资产（先于清洗抽取）：pandoc 图注可能是 markdown ![alt](src) 或 HTML <img ...>，两种都收
    assets = []

    def _add_asset(src, cap):
        src = (src or "").strip()
        if not src:
            return
        assets.append({"asset_type": "image", "img_path": epub_asset_relpath(src, settings.BOOK_OUTPUT_DIR),
                       "caption": (cap or "").strip(), "page_idx": None})

    for m in re.finditer(r"!\[([^\]]*)\]\(([^)\s]+)", raw_md):
        _add_asset(m.group(2), m.group(1))
    for m in re.finditer(r"<img\b[^>]*>", raw_md, re.IGNORECASE):
        src = re.search(r'src\s*=\s*"([^"]+)"', m.group(0))
        alt = re.search(r'alt\s*=\s*"([^"]*)"', m.group(0))
        _add_asset(src.group(1) if src else "", alt.group(1) if alt else "")

    # 清洗 epub 章节包裹 HTML（<div>/<span id>/<figure>），保留 figcaption 文本供检索
    md_text = raw_md
    md_text = re.sub(r"<span[^>]*></span>", "", md_text)
    md_text = re.sub(r"</?div[^>]*>", "", md_text)
    md_text = re.sub(r"</?figure[^>]*>", "", md_text)
    md_text = re.sub(r"<figcaption[^>]*>(.*?)</figcaption>", r"\1", md_text, flags=re.DOTALL)
    md_text = re.sub(r"\n{3,}", "\n\n", md_text).strip()
    with open(os.path.join(out_root, "full.md"), "w", encoding="utf-8") as f:
        f.write(md_text)

    lang = "zh" if _looks_chinese(md_text[:3000]) else "en"
    meta = extract_epub_meta(epub_path)
    # 仅在本地元数据缺作者时才动用一次小 LLM 调用补齐
    if use_llm and client is not None and not meta.get("authors"):
        try:
            from .stages import _chat_json
            data = _chat_json(client, settings.REVIEW_MODEL, BOOK_META_PROMPT.format(
                filename=os.path.basename(epub_path), md_head=md_text[:2500]))
            for k in ("authors", "publisher", "pub_place", "edition", "year"):
                v = str(data.get(k, "") or "").strip()
                if v and not meta.get(k):
                    meta[k] = v
            meta["source"] = "epub+llm"
        except Exception:
            pass

    # pandoc epub 的 --extract-media 按原书内部路径平铺进 out_root（无 media/ 子目录）
    images_dir = out_root
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        """INSERT OR REPLACE INTO papers
           (id, title, pdf_path, language, mineru_md, images_dir, status, processed_at)
           VALUES (?,?,?,?,?,?, 'EXPORTED', ?)""",
        (doc_id, meta["title"], epub_path, lang, md_text, images_dir, now))
    conn.commit()
    conn.close()

    details = {
        "title": meta["title"],
        "title_zh": meta["title"] if lang == "zh" else "",
        "title_en": meta["title"] if lang == "en" else "",
        "doc_type": "book", "authors": meta.get("authors", ""),
        "publisher": meta.get("publisher", ""), "pub_place": meta.get("pub_place", ""),
        "edition": meta.get("edition", ""), "year": meta.get("year", ""),
        "isbn": meta.get("isbn", ""), "journal": "", "doi": "", "keywords": "",
        "n_figures": len(assets), "n_tables": 0,
        "source": meta.get("source", "epub"),
    }
    store.save_enrichment(doc_id, details, assets=assets, refs=[])

    export_to_excel([{
        "文献ID": doc_id, "标题": meta["title"], "文献类型": "教材/图书(EPUB)",
        "作者": meta.get("authors", ""), "年份": meta.get("year", ""),
        "出版社": meta.get("publisher", ""), "出版地": meta.get("pub_place", ""),
        "版次": meta.get("edition", ""), "ISBN": meta.get("isbn", ""),
        "文件路径": epub_path, "语言": lang, "状态": "EXPORTED",
        "解析时间": now,
    }], settings.EXCEL_OUTPUT_PATH)

    # epub → docx（pandoc 原生，供阅读），放源文件同目录同名
    dest_docx = os.path.splitext(epub_path)[0] + ".docx"
    docx_path = ""
    try:
        p2 = subprocess.run([pandoc, "-f", "epub", "-t", "docx", epub_path, "-o", dest_docx],
                            capture_output=True, timeout=600)
        if p2.returncode == 0:
            docx_path = dest_docx
        else:
            console.print(f"[yellow]⚠️ epub→docx 失败（不影响入库/检索）: "
                          f"{p2.stderr.decode('utf-8', 'ignore')[:200]}")
    except Exception as e:
        console.print(f"[yellow]⚠️ 可读 Word 生成失败（不影响入库/检索）: {e}")

    log_run_event(mode="book", event="book_added", title=meta["title"], doc_id=doc_id,
                  status="success", extra={"format": "epub", "figures": len(assets),
                                           "docx": docx_path, "source": meta.get("source", "epub")})
    console.print(f"[bold green]✅ 入库(EPUB)：《{meta['title']}》"
                  f"(doc_id {doc_id[:8]}，图片 {len(assets)} 项)")
    if docx_path:
        console.print(f"[green]  可读 Word: {docx_path}")
    console.print("[dim]  提示：运行 `python review.py index` 让它进入检索库。")
    return doc_id


def _already_ingested(path: str, settings) -> bool:
    """整本内容哈希已在库且 EXPORTED —— 这本书之前入过库了。

    定时任务必须在**动页数预算之前**问这一句。`add_book`/`add_epub` 内部虽然也会
    跳过已入库的书，但那时页数已经记到预算头上了：已入库的原件一直躺在输入目录里，
    于是每晚都先被"重新收费"一遍，按字母序靠前的几本大部头就能吃光当晚额度，
    真正的新书永远排不上（曾造成连续多晚 0 实际入库，日志里却报"成功入库 N 本"）。

    只有"表还不存在"（全新部署）才当作没入过库。锁超时之类的**瞬时**故障必须抛出去：
    夜间脚本 nightly_index.py 与本任务并发写同一个库，此处若把锁超时也吞成 False，
    一本已入库的大部头就会被重跑一遍 MinerU 并重新收费，正是上面这个饿死 bug 的复现路径。
    """
    doc_id = calculate_pdf_hash(path)
    conn = None
    try:
        conn = sqlite3.connect(settings.DB_PATH)
        conn.execute("PRAGMA busy_timeout=30000")
        row = conn.execute("SELECT status FROM papers WHERE id=?", (doc_id,)).fetchone()
    except sqlite3.Error as e:
        if "no such table" in str(e).lower():
            return False    # 库还没建起来（全新部署）→ 当作没入过，照常走入库
        raise               # 锁超时等瞬时故障 → 上层记 failed、原地留到下一晚重试
    finally:
        if conn is not None:
            conn.close()
    return bool(row and row[0] == "EXPORTED")


def _unique_dest(dest_dir: str, name: str) -> str:
    """目标目录里已有同名文件时，给新来的加 .1/.2 后缀，绝不覆盖。

    归档/隔离目录里同名文件是很可能的（不同书重名、同一本书重下），
    直接 shutil.move 会把先前那份静默销毁，没有报错也没有备份。
    """
    base, ext = os.path.splitext(name)
    cand = os.path.join(dest_dir, name)
    i = 1
    while os.path.exists(cand):
        cand = os.path.join(dest_dir, f"{base}.{i}{ext}")
        i += 1
    return cand


def _move_book_aside(path: str, dest_dir: str, console) -> bool:
    """把书从输入目录移到归档/隔离目录，连同 MinerU 生成的同名 .docx 一起带走。
    移动失败只告警不抛错——归档不成功也不该影响入库结果本身。"""
    import shutil
    try:
        os.makedirs(dest_dir, exist_ok=True)
        for src in (path, os.path.splitext(path)[0] + ".docx"):
            if os.path.exists(src):
                dest = _unique_dest(dest_dir, os.path.basename(src))
                if os.path.basename(dest) != os.path.basename(src):
                    console.print(f"[dim]  {dest_dir} 已有同名文件，改存为 "
                                  f"{os.path.basename(dest)}")
                shutil.move(src, dest)
        return True
    except Exception as e:
        console.print(f"[yellow]⚠️ 移动 {os.path.basename(path)} 到 {dest_dir} 失败"
                      f"（不影响入库结果，但它下次还会被扫到）: {e}")
        return False


def _pdf_page_count(path: str):
    """读 PDF 页数，并判断读不出来时**是不是该永久隔离**。

    返回 (页数, 结论, 错误信息)，结论 ∈ {"ok", "corrupt", "transient"}。

    区分是必要的：pypdf 的 FileNotDecryptedError（加密/DRM）也是 PdfReadError 的子类，
    而加密书本身没坏，只是需要人工去壳；OSError/PermissionError 更是挂载抖动之类的
    临时问题。这两类若一律按"文件损坏"扔进 BOOK_FAILED_DIR，第一次扫到就永久埋掉了。
    """
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError, FileNotDecryptedError
    try:
        return len(PdfReader(path).pages), "ok", ""
    except FileNotDecryptedError as e:
        return None, "transient", f"PDF 已加密/有 DRM，需人工去壳: {e}"
    except PdfReadError as e:
        return None, "corrupt", str(e)          # 截断、根本不是 PDF 等结构性损坏
    except Exception as e:                      # OSError/PermissionError 等
        return None, "transient", str(e)


def ingest_one(path: str, settings, console, client=None, use_llm: bool = True,
               max_pages: int = None, budget_left: int = None, archive: bool = True):
    """单本书过完"已入库 / 文件能不能读 / 预算够不够"三道闸，返回 (结果, 页数)。

    结果 ∈ {"skipped", "quarantined", "deferred", "ok"}；页数只对 PDF 有意义（EPUB 记 0）。
    瞬时故障（加密、读盘出错、MinerU/网络失败）一律抛出去，由调用方记为 failed、
    原件原地留着等下次重试。

    定时任务与手动 `review.py add-book` 共用这一份，避免两条路行为分叉——
    早先手动入库那条路不查重也不隔离，坏文件会一直躺在输入目录里被反复重扫。

    budget_left=None 表示不限页数预算；archive=False 表示不搬动原件
    （手动指定输入目录以外的路径时用，别去动用户自己放的文件）。
    """
    fname = os.path.basename(path)

    if _already_ingested(path, settings):
        console.print(f"[dim]已入库，{'归档出输入目录' if archive else '跳过'}：《{fname}》")
        if archive:
            _move_book_aside(path, settings.BOOK_PROCESSED_DIR, console)
        return "skipped", 0

    if fname.lower().endswith(".epub"):
        add_epub(path, settings, console, client=client, use_llm=use_llm)
        if archive:
            _move_book_aside(path, settings.BOOK_PROCESSED_DIR, console)
        return "ok", 0

    n_pages, verdict, err = _pdf_page_count(path)
    if verdict == "corrupt":
        console.print(f"[red]✖ PDF 文件损坏，隔离不再重试 {fname}: {err}")
        log_run_event(mode="book", event="book_added", title=fname,
                      status="quarantined", error=f"PDF 无法解析: {err}")
        if archive:
            _move_book_aside(path, settings.BOOK_FAILED_DIR, console)
        return "quarantined", 0
    if verdict == "transient":
        raise RuntimeError(f"PDF 暂时读不了（原地保留，下次重试）: {err}")

    if budget_left is not None and n_pages > budget_left:
        return "deferred", n_pages

    add_book(path, settings, console, client=client,
             max_pages=max_pages or settings.BOOK_SPLIT_PAGES, use_llm=use_llm)
    if archive:
        _move_book_aside(path, settings.BOOK_PROCESSED_DIR, console)
    return "ok", n_pages


def run_scheduled_intake(settings, console, client=None, use_llm: bool = True) -> dict:
    """定时任务用：扫描 BOOK_INPUT_DIR，按 PDF 总页数预算逐本入库。

    每晚额度用完就停，剩下的书留到下一次运行（不会漏，只是延后）。第一本书
    即使单本页数就超过预算也会处理完（否则超大部头会永远排不到），只有
    "已经处理过至少一本、再加下一本会超预算"时才会推迟。EPUB 走 pandoc、
    不占 MinerU 页数配额，因此不计入预算。

    每本书按固定顺序过三道闸（见 `ingest_one`），顺序本身是有讲究的：
      1. 已入库 → 归档到 BOOK_PROCESSED_DIR，**不计预算、不算成功**（见 `_already_ingested`）。
      2. pypdf 判定文件结构坏了 → 隔离到 BOOK_FAILED_DIR，别每晚白试。加密/读盘出错
         以及 MinerU/网络类失败不走这条路，原地留着等下一晚重试。
      3. 预算够不够。
    """
    report = {"scanned": 0, "ok": 0, "skipped": 0, "failed": 0,
              "quarantined": 0, "deferred": 0, "pages_used": 0}
    book_dir = settings.BOOK_INPUT_DIR
    if not os.path.isdir(book_dir):
        return report

    files = sorted(f for f in os.listdir(book_dir) if f.lower().endswith((".pdf", ".epub")))
    report["scanned"] = len(files)
    budget = settings.BOOK_DAILY_PAGE_BUDGET
    used = 0

    for fname in files:
        path = os.path.join(book_dir, fname)
        try:
            # used==0 时传 None：第一本即使单本就超预算也要处理完，否则超大部头永远排不到
            result, n_pages = ingest_one(
                path, settings, console, client=client, use_llm=use_llm,
                max_pages=settings.BOOK_SPLIT_PAGES,
                budget_left=None if used == 0 else budget - used)
            if result == "deferred":
                report["deferred"] += 1
                console.print(f"[yellow]📚 今晚教材页数预算已用完（{used}/{budget}），"
                              f"《{fname}》留到下一晚")
            elif result == "ok":
                used += n_pages
                report["ok"] += 1
            else:                       # skipped / quarantined
                report[result] += 1
        except Exception as e:
            # MinerU 超时、网络抖动等：原地留着，下一晚自动重试
            report["failed"] += 1
            console.print(f"[red]✖ 教材定时入库失败 {fname}: {e}")
            log_run_event(mode="book", event="book_added", title=fname,
                          status="failed", error=str(e))

    report["pages_used"] = used
    return report
