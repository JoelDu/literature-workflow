"""书籍入库单元测试（无网络：拆分/拼接/本地元数据/[M] 引用分流）。"""
import os
import sys
import sqlite3
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json

from pypdf import PdfReader, PdfWriter

import litreview.bookintake as bookintake
from litreview.bookintake import (split_pdf_if_needed, stitch_markdown,
                                  _local_book_meta, _looks_chinese,
                                  collect_book_assets, extract_epub_meta,
                                  epub_asset_relpath, run_scheduled_intake)
from litreview.models import Outline, OutlineSection, SectionDraft
from litreview.stages import assemble_review


def _make_pdf(path, pages):
    w = PdfWriter()
    for _ in range(pages):
        w.add_blank_page(width=200, height=200)
    with open(path, "wb") as f:
        w.write(f)
    return path


def test_split_boundary(tmp_path):
    pdf = _make_pdf(str(tmp_path / "book.pdf"), 5)
    parts, n = split_pdf_if_needed(pdf, max_pages=2, workdir=str(tmp_path))
    assert n == 5 and len(parts) == 3
    assert [len(PdfReader(p).pages) for p in parts] == [2, 2, 1]


def test_no_split_when_under_limit(tmp_path):
    pdf = _make_pdf(str(tmp_path / "small.pdf"), 5)
    parts, n = split_pdf_if_needed(pdf, max_pages=10, workdir=str(tmp_path))
    assert n == 5 and parts == [pdf]


def test_stitch_markdown_order_and_blankline():
    assert stitch_markdown(["# A\n正文1  ", "", "  # B\n正文2"]) == "# A\n正文1\n\n# B\n正文2"


def test_local_book_meta_regex():
    head = "机械工业出版社\n化工原理（第3版）\nISBN 978-7-111-12345-6\n2019年3月第1次印刷"
    m = _local_book_meta("/x/化工原理.pdf", head)
    assert m["title"] == "化工原理"
    assert m["year"] == "2019"
    assert m["isbn"].startswith("978")
    assert m["doc_type"] == "book"


def test_looks_chinese():
    assert _looks_chinese("化工原理第三版")
    assert not _looks_chinese("chemical engineering basics")


def test_collect_book_assets_path_and_page_offset(tmp_path):
    """各 part 的图资产：img_path 前缀成 mineru_output 根相对路径，page_idx 跨 part 偏移。"""
    for i, (img, pg) in enumerate([("a.jpg", 3), ("b.jpg", 5)], 1):
        d = tmp_path / f"part{i}"
        d.mkdir()
        blocks = [{"type": "image", "img_path": f"images/{img}",
                   "image_caption": [f"图{i}"], "page_idx": pg}]
        (d / "p_content_list.json").write_text(json.dumps(blocks), encoding="utf-8")
    assets = collect_book_assets(str(tmp_path), "BOOK_书_deadbeef", n_parts=2, max_pages=180)
    assert assets[0]["img_path"] == "BOOK_书_deadbeef/part1/images/a.jpg"
    assert assets[0]["page_idx"] == 3
    assert assets[1]["img_path"] == "BOOK_书_deadbeef/part2/images/b.jpg"
    assert assets[1]["page_idx"] == 5 + 180   # part2 偏移一份页数


def test_book_citation_is_monograph_M():
    """书籍走 [M]（无 DOI/wikilink），论文仍走 [J]（带 DOI/wikilink）。"""
    paper_meta = {
        "aaaaaaaa1111": {"title": "复合肥防结块研究", "language": "zh", "doc_type": "paper",
                         "authors": "张三, 李四", "journal": "化工进展", "year": "2023",
                         "doi": "10.1234/abcd", "title_zh": "复合肥防结块研究", "title_en": ""},
        "bbbbbbbb2222": {"title": "化工原理", "language": "zh", "doc_type": "book",
                         "authors": "王五", "publisher": "机械工业出版社", "pub_place": "北京",
                         "edition": "第3版", "year": "2019", "title_zh": "化工原理", "title_en": ""},
    }
    drafts = [SectionDraft(heading="机理",
                           markdown="论文观点 [@aaaaaaaa] 与教材说法 [@bbbbbbbb]。",
                           cited_doc_ids=["aaaaaaaa1111", "bbbbbbbb2222"])]
    outline = Outline(title="测试综述", topic="防结块",
                      sections=[OutlineSection(heading="机理", questions=[])])
    review = assemble_review(outline, drafts, intro="引言", conclusion="结论",
                             paper_meta=paper_meta, evidence_total=2)
    book_line = next(r for r in review.references if "[M]" in r)
    paper_line = next(r for r in review.references if "[J]" in r)
    assert "机械工业出版社" in book_line and "北京" in book_line and "第3版" in book_line
    assert "DOI" not in book_line and "[[" not in book_line
    assert "DOI: 10.1234/abcd" in paper_line and "[[" in paper_line


def _make_epub(path):
    container = ("<?xml version='1.0'?>"
                 "<container version='1.0' xmlns='urn:oasis:names:tc:opendocument:xmlns:container'>"
                 "<rootfiles><rootfile full-path='OEBPS/content.opf' "
                 "media-type='application/oebps-package+xml'/></rootfiles></container>")
    opf = ("<?xml version='1.0'?>"
           "<package xmlns='http://www.idpf.org/2007/opf'>"
           "<metadata xmlns:dc='http://purl.org/dc/elements/1.1/'>"
           "<dc:title>测试书</dc:title><dc:creator>张三</dc:creator>"
           "<dc:publisher>测试出版社</dc:publisher><dc:date>2020-01-01</dc:date>"
           "<dc:identifier>urn:isbn:9780000000000</dc:identifier>"
           "</metadata></package>")
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", container)
        z.writestr("OEBPS/content.opf", opf)
    return path


def test_extract_epub_meta_reads_opf_dc_fields(tmp_path):
    epub = _make_epub(str(tmp_path / "书.epub"))
    meta = extract_epub_meta(epub)
    assert meta["title"] == "测试书"
    assert meta["authors"] == "张三"
    assert meta["publisher"] == "测试出版社"
    assert meta["year"] == "2020"
    assert meta["isbn"] == "9780000000000"
    assert meta["doc_type"] == "book"


def test_extract_epub_meta_falls_back_to_filename_on_bad_zip(tmp_path):
    bad = tmp_path / "损坏的书.epub"
    bad.write_bytes(b"not a zip")
    meta = extract_epub_meta(str(bad))
    assert meta["title"] == "损坏的书"
    assert meta["authors"] == ""


def test_epub_asset_relpath_does_not_double_prefix_out_root(tmp_path, monkeypatch):
    """回归测试：pandoc --extract-media 打出的 src 已含 out_root 前缀，
    不能再和 out_root 拼接一次，否则算出的相对路径对不上真实文件位置。"""
    monkeypatch.chdir(tmp_path)
    mineru_dir = "./mineru_output"
    out_root = os.path.join(mineru_dir, "BOOK_test_deadbeef")
    real_img = os.path.join(out_root, "images", "pic.png")
    os.makedirs(os.path.dirname(real_img))
    open(real_img, "wb").close()

    # pandoc 实测行为：markdown 里打印的 src 就是这个相对 CWD 的路径
    src_from_pandoc = out_root + "/images/pic.png"
    rel = epub_asset_relpath(src_from_pandoc, mineru_dir)

    assert rel == "BOOK_test_deadbeef/images/pic.png"
    assert os.path.isfile(os.path.join(mineru_dir, rel))


class _FakeSettings:
    def __init__(self, book_input_dir, budget):
        self.BOOK_INPUT_DIR = book_input_dir
        self.BOOK_DAILY_PAGE_BUDGET = budget
        self.BOOK_SPLIT_PAGES = 180
        # 子目录即可：扫描只认 .pdf/.epub 后缀，目录名不会被当成书
        self.BOOK_PROCESSED_DIR = os.path.join(book_input_dir, "processed_books")
        self.BOOK_FAILED_DIR = os.path.join(book_input_dir, "failed_books")
        self.DB_PATH = os.path.join(book_input_dir, "no_such.db")


def _make_db_with_book(db_path, pdf_path, status="EXPORTED"):
    """建一个只含 papers 表的库，把 pdf_path 的内容哈希登记成已入库。"""
    from utils import calculate_pdf_hash
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE IF NOT EXISTS papers (id TEXT PRIMARY KEY, status TEXT)")
    conn.execute("INSERT OR REPLACE INTO papers (id, status) VALUES (?, ?)",
                 (calculate_pdf_hash(pdf_path), status))
    conn.commit()
    conn.close()


def test_run_scheduled_intake_defers_once_budget_exceeded(tmp_path, monkeypatch):
    """预算用完后，后续的书留到下一晚；但已经在处理中的第一本不受影响。"""
    _make_pdf(str(tmp_path / "a_6p.pdf"), 6)
    _make_pdf(str(tmp_path / "b_6p.pdf"), 6)
    _make_pdf(str(tmp_path / "c_2p.pdf"), 2)

    processed = []
    monkeypatch.setattr(bookintake, "add_book",
                        lambda path, *a, **k: processed.append(os.path.basename(path)))

    settings = _FakeSettings(str(tmp_path), budget=10)
    report = run_scheduled_intake(settings, console=_QuietConsole())

    # a(6) 处理；b(6) 会让 6+6=12>10，推迟；c(2) 6+2=8<=10，处理
    assert processed == ["a_6p.pdf", "c_2p.pdf"]
    assert report == {"scanned": 3, "ok": 2, "skipped": 0, "failed": 0,
                      "quarantined": 0, "deferred": 1, "pages_used": 8}


def test_run_scheduled_intake_first_book_may_exceed_budget_alone(tmp_path, monkeypatch):
    """预算比第一本书还小时，第一本仍然要处理（否则超大部头永远排不到），
    但后面的书就都要推迟了。"""
    _make_pdf(str(tmp_path / "big.pdf"), 8)
    _make_pdf(str(tmp_path / "small.pdf"), 1)

    processed = []
    monkeypatch.setattr(bookintake, "add_book",
                        lambda path, *a, **k: processed.append(os.path.basename(path)))

    settings = _FakeSettings(str(tmp_path), budget=5)
    report = run_scheduled_intake(settings, console=_QuietConsole())

    assert processed == ["big.pdf"]
    assert report["ok"] == 1 and report["deferred"] == 1 and report["pages_used"] == 8


def test_run_scheduled_intake_epub_does_not_count_toward_budget(tmp_path, monkeypatch):
    """EPUB 走 pandoc，不占 MinerU 页数预算，不该影响后续 PDF 的可用额度。"""
    _make_epub(str(tmp_path / "a_book.epub"))
    _make_pdf(str(tmp_path / "b_4p.pdf"), 4)

    processed = []
    monkeypatch.setattr(bookintake, "add_epub",
                        lambda path, *a, **k: processed.append(os.path.basename(path)))
    monkeypatch.setattr(bookintake, "add_book",
                        lambda path, *a, **k: processed.append(os.path.basename(path)))

    settings = _FakeSettings(str(tmp_path), budget=4)
    report = run_scheduled_intake(settings, console=_QuietConsole())

    assert processed == ["a_book.epub", "b_4p.pdf"]
    assert report == {"scanned": 2, "ok": 2, "skipped": 0, "failed": 0,
                      "quarantined": 0, "deferred": 0, "pages_used": 4}


def test_run_scheduled_intake_skipped_book_does_not_eat_budget(tmp_path, monkeypatch):
    """回归：已入库的书不该占页数预算。

    它曾经占——`add_book` 内部跳过时页数已经记上了，而已入库的原件一直留在输入
    目录，于是每晚先被重新收费一遍。真实库里按字母序靠前的三本大部头（449+928+499
    页）就吃掉 1876/2000，后面 32 本新书连续多晚一本都排不上，日志却报"成功入库 8 本"。
    """
    old = _make_pdf(str(tmp_path / "a_old_8p.pdf"), 8)   # 已入库，不该计预算
    _make_pdf(str(tmp_path / "b_new_3p.pdf"), 3)         # 新书，预算只剩 4 页时仍应处理

    processed = []
    monkeypatch.setattr(bookintake, "add_book",
                        lambda path, *a, **k: processed.append(os.path.basename(path)))

    settings = _FakeSettings(str(tmp_path), budget=4)
    settings.DB_PATH = str(tmp_path / "tracking.db")
    _make_db_with_book(settings.DB_PATH, old)

    report = run_scheduled_intake(settings, console=_QuietConsole())

    assert processed == ["b_new_3p.pdf"]        # 旧书没重跑，新书没被挤掉
    assert report["skipped"] == 1 and report["ok"] == 1 and report["deferred"] == 0
    assert report["pages_used"] == 3            # 旧书那 8 页没算进来
    # 旧书被移出输入目录，下一晚不再参与扫描
    assert not os.path.exists(old)
    assert os.path.exists(os.path.join(settings.BOOK_PROCESSED_DIR, "a_old_8p.pdf"))


def test_run_scheduled_intake_moves_ingested_book_and_its_docx(tmp_path, monkeypatch):
    """入库成功后原件连同 MinerU 生成的同名 .docx 一起归档，输入目录不残留。"""
    pdf = _make_pdf(str(tmp_path / "book_2p.pdf"), 2)
    docx = str(tmp_path / "book_2p.docx")
    with open(docx, "wb") as f:
        f.write(b"fake docx")

    monkeypatch.setattr(bookintake, "add_book", lambda path, *a, **k: "docid")

    settings = _FakeSettings(str(tmp_path), budget=100)
    report = run_scheduled_intake(settings, console=_QuietConsole())

    assert report["ok"] == 1
    assert not os.path.exists(pdf) and not os.path.exists(docx)
    assert os.path.exists(os.path.join(settings.BOOK_PROCESSED_DIR, "book_2p.pdf"))
    assert os.path.exists(os.path.join(settings.BOOK_PROCESSED_DIR, "book_2p.docx"))


def test_run_scheduled_intake_quarantines_unreadable_pdf(tmp_path, monkeypatch):
    """PDF 本身坏了（pypdf 读不出页数）→ 隔离，别每晚白试；后面的书照常处理。"""
    bad = str(tmp_path / "a_broken.pdf")
    with open(bad, "wb") as f:
        f.write(b"%PDF-1.4 truncated garbage")
    _make_pdf(str(tmp_path / "b_good_2p.pdf"), 2)

    processed = []
    monkeypatch.setattr(bookintake, "add_book",
                        lambda path, *a, **k: processed.append(os.path.basename(path)))
    monkeypatch.setattr(bookintake, "log_run_event", lambda **k: None)

    settings = _FakeSettings(str(tmp_path), budget=100)
    report = run_scheduled_intake(settings, console=_QuietConsole())

    assert processed == ["b_good_2p.pdf"]
    assert report["quarantined"] == 1 and report["ok"] == 1 and report["failed"] == 0
    assert not os.path.exists(bad)
    assert os.path.exists(os.path.join(settings.BOOK_FAILED_DIR, "a_broken.pdf"))


def test_run_scheduled_intake_keeps_book_in_place_on_network_failure(tmp_path, monkeypatch):
    """MinerU/网络类失败不隔离、不归档——原地留着等下一晚重试。"""
    pdf = _make_pdf(str(tmp_path / "book_2p.pdf"), 2)

    def _boom(*a, **k):
        raise RuntimeError("所有配置的 MinerU API Keys 均尝试失败")
    monkeypatch.setattr(bookintake, "add_book", _boom)
    monkeypatch.setattr(bookintake, "log_run_event", lambda **k: None)

    settings = _FakeSettings(str(tmp_path), budget=100)
    report = run_scheduled_intake(settings, console=_QuietConsole())

    assert report["failed"] == 1 and report["quarantined"] == 0 and report["ok"] == 0
    assert os.path.exists(pdf)          # 还在输入目录，下一晚会重试
    assert not os.path.exists(os.path.join(settings.BOOK_FAILED_DIR, "book_2p.pdf"))


def test_run_scheduled_intake_keeps_encrypted_pdf_for_human(tmp_path, monkeypatch):
    """加密/DRM 的 PDF 文件本身没坏，只是需要人工去壳：算 failed 原地留着，
    不能当成"损坏"扔进 failed_books——那是第一次扫到就永久埋掉。"""
    enc = str(tmp_path / "a_encrypted.pdf")
    w = PdfWriter()
    w.add_blank_page(width=200, height=200)
    w.encrypt("pw")
    with open(enc, "wb") as f:
        w.write(f)
    _make_pdf(str(tmp_path / "b_good_2p.pdf"), 2)

    processed = []
    monkeypatch.setattr(bookintake, "add_book",
                        lambda path, *a, **k: processed.append(os.path.basename(path)))
    monkeypatch.setattr(bookintake, "log_run_event", lambda **k: None)

    settings = _FakeSettings(str(tmp_path), budget=100)
    report = run_scheduled_intake(settings, console=_QuietConsole())

    assert processed == ["b_good_2p.pdf"]           # 后面的书照常处理
    assert report["failed"] == 1 and report["quarantined"] == 0
    assert os.path.exists(enc)                      # 还在输入目录，等人工处理
    assert not os.path.exists(os.path.join(settings.BOOK_FAILED_DIR, "a_encrypted.pdf"))


def test_run_scheduled_intake_reraises_transient_db_error(tmp_path, monkeypatch):
    """查重时撞上锁超时等瞬时故障，绝不能吞成"没入过库"。

    吞掉的后果是把一本已入库的大部头重跑一遍 MinerU 并重新收费，正是页数预算
    被吃光、新书连续多晚排不上的那个 bug 的复现路径。正确做法是抛出去记 failed，
    原件原地留到下一晚重试。
    """
    _make_pdf(str(tmp_path / "book_2p.pdf"), 2)

    processed = []
    monkeypatch.setattr(bookintake, "add_book",
                        lambda path, *a, **k: processed.append(os.path.basename(path)))
    monkeypatch.setattr(bookintake, "log_run_event", lambda **k: None)

    def _locked(*a, **k):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(bookintake.sqlite3, "connect", _locked)

    settings = _FakeSettings(str(tmp_path), budget=100)
    report = run_scheduled_intake(settings, console=_QuietConsole())

    assert processed == []                          # 没有被当成新书重跑
    assert report["failed"] == 1 and report["skipped"] == 0 and report["ok"] == 0
    assert report["pages_used"] == 0                # 也没有被重新收费
    assert os.path.exists(str(tmp_path / "book_2p.pdf"))


def test_move_book_aside_never_overwrites_same_name(tmp_path):
    """归档目录已有同名文件时改存 .1 后缀，先前那份不能被静默销毁。"""
    dest = tmp_path / "processed"
    dest.mkdir()
    (dest / "book.pdf").write_bytes(b"first")
    src = tmp_path / "book.pdf"
    src.write_bytes(b"second")

    assert bookintake._move_book_aside(str(src), str(dest), _QuietConsole())
    assert (dest / "book.pdf").read_bytes() == b"first"     # 原来那份完好
    assert (dest / "book.1.pdf").read_bytes() == b"second"  # 新来的另存
    assert not src.exists()


class _QuietConsole:
    def print(self, *a, **k):
        pass
