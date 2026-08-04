"""参考文献著录格式（GB/T 7714-2015）。

这组测试守的是一个曾经真实发生过的错误：早先 stages.py 里写的是
"if 书籍 … else 一律按 [J]"，于是专利、标准全被印成期刊论文，`[J].` 后面还跟着
一个空的期刊名。**新增 doc_type 时必须在这里加一条**——漏了就会再掉回 [J]，
而参考文献列表是给外人看的，错了很难看。
"""
from litreview import doctype as dt
from litreview.stages import format_reference

DOC = "a1b2c3d4" + "0" * 56


def _ref(**m):
    return format_reference(1, DOC, m)


def test_journal_paper():
    out = _ref(doc_type="paper", title="热法磷酸余热回收研究", authors="张三; 李四",
               journal="化工进展", year="2021", doi="10.1016/j.x.2021.01.001",
               language="zh")
    assert "[J]." in out
    assert "化工进展, 2021." in out
    assert "DOI: 10.1016/j.x.2021.01.001." in out


def test_book_has_edition_and_imprint_and_no_backlink():
    """教材不进 Obsidian，没有笔记可链，末尾不能甩一个点不开的 wikilink。"""
    out = _ref(doc_type="book", title="无机化学", authors="王五", edition="第3版",
               pub_place="北京", publisher="高等教育出版社", year="2019", language="zh")
    assert "[M]." in out
    assert "第3版." in out
    assert "北京: 高等教育出版社, 2019." in out
    assert "[[" not in out


def test_patent_uses_number_and_filing_date():
    """专利著录的是申请日，不是出版年——GB/T 7714 对 [P] 就是这么规定的。"""
    out = _ref(doc_type="patent", title="一种热法磷酸全热能回收系统",
               authors="张三", patent_no="CN112856361B", filing_date="2021-03-08",
               year="2021", language="zh")
    assert "[P]." in out
    assert ": CN112856361B[P]." in out
    assert "2021-03-08." in out
    assert "[J]" not in out


def test_standard_omits_author_when_issuer_unknown():
    """标准不署个人责任者。抽不到发布机构时整段省掉，硬填"作者不详"是噪声。"""
    out = _ref(doc_type="standard", title="肥料中有毒有害物质的限量要求",
               doc_no="GB 38400-2019", authors="", pub_place="北京",
               publisher="中国标准出版社", year="2019", language="zh")
    assert out.startswith("[1] 肥料中有毒有害物质的限量要求: GB 38400-2019[S].")
    assert "北京: 中国标准出版社, 2019." in out
    assert "不详" not in out


def test_standard_with_issuer_as_lead():
    out = _ref(doc_type="standard", title="化肥防结块剂", doc_no="HG/T 5520-2019",
               authors="国家市场监督管理总局", year="2019", language="zh")
    assert out.startswith("[1] 国家市场监督管理总局. 化肥防结块剂: HG/T 5520-2019[S].")


def test_web_resource_carries_url_and_no_backlink():
    out = _ref(doc_type="web", title="World Fertilizer Outlook", url="https://example.org/x",
               year="2024", language="en")
    assert "[EB/OL]." in out
    assert "https://example.org/x." in out
    assert "[[" not in out


def test_report_thesis_conference_get_their_own_codes():
    """报告/学位论文/会议论文共用一套"责任者. 题名[X]. 出版项."，但码不能串。"""
    for kind, code in (("report", "R"), ("thesis", "D"), ("conf", "C")):
        out = _ref(doc_type=kind, title="世界化肥趋势与展望 2024", authors="FAO; IFA",
                   pub_place="罗马", publisher="FAO", year="2024", language="zh")
        assert f"[{code}]." in out, f"{kind} 应该印成 [{code}]"
        assert "罗马: FAO, 2024." in out


def test_every_declared_type_prints_its_own_code():
    """词表里每一种类型都必须落到自己的码上。加了新类型却忘了在 format_reference
    里补分支时，这条会立刻炸——那正是它存在的理由。"""
    for kind in dt.all_types():
        out = _ref(doc_type=kind, title="某文献", authors="某人", year="2024",
                   patent_no="CN1A", doc_no="GB 1-2024", url="https://e.org",
                   journal="某刊", language="zh")
        assert f"[{dt.gb_code(kind)}]." in out, f"{kind} 没有按 [{dt.gb_code(kind)}] 著录"


def test_unknown_doc_type_degrades_to_journal_not_crash():
    """老库里若残留词表外的值，宁可按 [J] 印，也不能让整篇综述生成挂掉。"""
    out = _ref(doc_type="某个手改进库的值", title="某文献", authors="某人", language="zh")
    assert "[J]." in out


def test_backlink_points_at_the_obsidian_note():
    """论文/专利/标准都要能点回单篇笔记，命名规则须与 utils.generate_obsidian_note 一致。"""
    out = _ref(doc_type="paper", title="热法磷酸余热回收研究", authors="张三", language="zh")
    assert out.endswith(f"[[热法磷酸余热回收研究_{DOC[:8]}]]")
