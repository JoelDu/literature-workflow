"""书籍入库单元测试（无网络：拆分/拼接/本地元数据/[M] 引用分流）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json

from pypdf import PdfReader, PdfWriter

from litreview.bookintake import (split_pdf_if_needed, stitch_markdown,
                                  _local_book_meta, _looks_chinese,
                                  collect_book_assets)
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
