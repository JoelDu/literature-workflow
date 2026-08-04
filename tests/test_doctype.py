"""文献类型词表 + 标准封面识别的测试。

**所有正样本都是从生产库里原样抠出来的真实封面文本**（只截了前几百字），
不是照着"标准应该长什么样"编的。这条很要紧：MinerU 会把标准号里的斜杠和破折号
包进 <sup>/</sup> 标签、会把整页封面渲染成造字乱码（犌犅／犜=GB/T），
这些坑靠想象一个都想不出来，只有拿真文本跑才会暴露。
以后改正则，先来这里加一条真实样本，再动代码。
"""
import pytest

from litreview import doctype as dt


# ── 类型词表 ─────────────────────────────────────────────────────────────

def test_gb_codes_are_the_national_standard_ones():
    """码表直接印进参考文献，错一个字整篇引用就不合规。"""
    assert dt.gb_code("paper") == "J"
    assert dt.gb_code("book") == "M"
    assert dt.gb_code("patent") == "P"
    assert dt.gb_code("standard") == "S"
    assert dt.gb_code("report") == "R"
    assert dt.gb_code("thesis") == "D"
    assert dt.gb_code("conf") == "C"
    assert dt.gb_code("web") == "EB/OL"


def test_unknown_type_falls_back_to_journal_not_crash():
    """老库里可能有词表外的 doc_type；宁可按 [J] 印，也不能让整篇综述生成挂掉。"""
    assert dt.gb_code("") == "J"
    assert dt.gb_code("某个手改进库的值") == "J"


@pytest.mark.parametrize("given,want", [
    ("报告", "report"), ("R", "report"), ("财报", "report"), ("年报", "report"),
    ("白皮书", "report"), ("研究报告", "report"),
    ("网站", "web"), ("网页", "web"), ("EB/OL", "web"), ("eb-ol", "web"),
    ("标准", "standard"), ("国标", "standard"), ("S", "standard"),
    ("教材", "book"), ("专著", "book"), ("M", "book"),
    ("硕士论文", "thesis"), ("会议论文", "conf"),
])
def test_aliases(given, want):
    """人工 set-type 时不该被逼着背英文键名，中文名和国标码都要认。"""
    assert dt.normalize(given) == want


def test_normalize_rejects_unknown_instead_of_guessing():
    """认不出就返回空，由调用方报错。**绝不能静默兜底成 paper**——
    那样用户敲错一个字，文献就被悄悄归错类，事后没人会发现。"""
    assert dt.normalize("专利申请书") == ""
    assert dt.normalize("") == ""


def test_long_form_only_for_continuous_prose():
    """分块参数按这个走。标准和专利虽然也是整本 PDF，但正文是条款和权利要求，
    一条一个意思，用大块反而检得更糊。"""
    assert dt.is_long_form("book") and dt.is_long_form("report") and dt.is_long_form("thesis")
    assert not dt.is_long_form("standard")
    assert not dt.is_long_form("patent")
    assert not dt.is_long_form("paper")


def test_only_patent_and_standard_claim_auto_detection():
    """报告/财报/网页没有任何结构化特征能跟论文分开，词表里必须老实标成不可自动识别。
    哪天有人给它们加了 detect=True，这条测试就会拦下来。"""
    auto = {k for k, v in dt.DOC_TYPES.items() if v["detect"]}
    assert auto == {"patent", "standard"}


def test_statuses_are_type_specific():
    """只有专利和标准会失效。论文发表了就是发表了，不存在"待核实状态"，
    给它返回专利那套选项等于允许把一篇论文标成"已授权"。"""
    assert "废止" in dt.statuses_for("standard")
    assert "驳回" in dt.statuses_for("patent")
    assert dt.statuses_for("paper") == []
    assert dt.statuses_for("report") == []


# ── 真实封面样本 ─────────────────────────────────────────────────────────

# 常规封面。注意"代替 GB/T 1677—1981"出现在真编号**前面**，
# 先撞上它就会把被代替的旧版号当成本标准的号。
COVER_PLAIN = """# 中华人民共和国国家标准

代替 GB/T 1677—1981, GB/T 1678—1981

GB/T 1677—2008

# 增塑剂环氧值的测定

# Determinating the epoxy value of plasticizers

2008-06-18 发布

2009-02-01 实施

## 前言

本标准代替 GB/T 1677—1981《增塑剂环氧值的测定(盐酸-丙酮法)》。
本标准由中国石油和化学工业协会提出。
本标准由全国橡胶与橡胶制品标准化技术委员会化学助剂分技术委员会归口。
"""

# MinerU 把斜杠和破折号包进了 HTML 标签：封面上印的其实是 GB/T 32952—2016。
# 不先剥标签，正则连编号都匹配不上。
COVER_SUP_MARKUP = """# 中 华 人 民 共 和 国 国 家 标 准

GB<sup>/</sup>T32952<sup>—</sup>2016

# 肥料中多环芳烃含量的测定气相色谱-质谱法

# Determinationofpolycyclicaromatichydrocarbonscontentforfertilizers— Gaschromatography-massspectrometrymethod

2016-08-29发布

2017-03-01实施

## 前 言

本标准由中国石油和化学工业联合会提出
本标准由全国肥料和土壤调理剂标准化技术委员会 归口
"""

# 造字子集乱码：整个封面被渲染成 U+728C 一带的私用区汉字，犌犅／犜 = GB/T。
COVER_MOJIBAKE = """# 中 华 人 民 共 和 国 国 家 标 准

犌犅／犜7363—2021

代替犌犅／犜7363—1987

# 犜犲狊狋犿犲狋犺狅犱犳狅狉狆狅犾狔狀狌犮犾犲犪狉犪狉狅犿犪狋犻犮狊犻狀狆犲狋狉狅犾犲狌犿狑犪狓

# 石油蜡中稠环芳烃试验法

2021-12-31 发布

2022-07-01 实施

<sup>国</sup> <sup>家</sup> <sup>市</sup> <sup>场</sup> <sup>监</sup> <sup>督</sup> <sup>管</sup> <sup>理</sup> <sup>总</sup> <sup>局</sup><sub>国 家 标 准 化 管 理 委 员 会</sub> 发 布

## 前 言

本文件按照GB／T1．1—2020《标准化工作导则》的规定起草。
"""

# 等同采用国际标准：封面印的是"本国号/国际号"。先认外文号就会把 ISO 的号当成自己的。
COVER_DUAL_NUMBER = """# 中华人民共和国国家标准

GB/T 17376—2008/ISO 5509:2000

代替 GB/T 17376—1998

# 动植物油脂 脂肪酸甲酯制备

# Animal and vegetable fats and oils—Preparation of methyl esters of fatty acids

(ISO 5509:2000, IDT)

2008-11-04 发布

2009-01-20 实施

中国标准出版社出版发行
"""

# 编号带小数点，且"食品安全国家标准"是丛书名不是标准名——真名在它下一行。
COVER_FOOD_SAFETY = """# 中华人民共和国国家标准

GB 1886.64—2015

食品安全国家标准

食品添加剂 焦糖色

2015-11-13 发布

2016-05-13 实施

## 前言

本标准代替 GB 8817—2001《食品添加剂 焦糖色》。
"""

# 编号里的破折号两边带空格；发布机构有两个。
COVER_SPACED_DASH = """# 中华人民共和国国家标准

GB 5085.6 — 2007

# 危险废物鉴别标准 毒性物质含量鉴别

# Identification standards for hazardous wastes Identification for toxic substance content

2007-04-25 发布

2007-10-01 实施

国家环境保护总局

国家质量监督检验检疫总局

发布
"""

# 反例：一篇正经论文，正文里提到标准方法，还引了 GB/T 编号。
PAPER_CITING_STANDARD = """# BDP 磷酸酯阻燃剂的酸值测试影响因素探讨

蒋霞

(怀化学院，聚乙烯醇纤维材料制备技术湖南省工程实验室，湖南 怀化 418000)

摘 要：介绍了国内外、不同供应商对 BDP 磷酸酯类阻燃剂石油产品酸值测定的标准方法及其
各自特点和差别。酸值按 GB/T 4945—2002 测定，水分按 GB/T 11133—2015 测定。

关键词：BDP 磷酸酯；酸值；测定，影响因素

中图分类号：TQ016.1  文献标志码：A  文章编号：1001-9677(2021)012-0108-04
"""


@pytest.mark.parametrize("head,no,title", [
    (COVER_PLAIN, "GB/T 1677-2008", "增塑剂环氧值的测定"),
    (COVER_SUP_MARKUP, "GB/T 32952-2016", "肥料中多环芳烃含量的测定气相色谱-质谱法"),
    (COVER_MOJIBAKE, "GB/T 7363-2021", "石油蜡中稠环芳烃试验法"),
    (COVER_DUAL_NUMBER, "GB/T 17376-2008", "动植物油脂 脂肪酸甲酯制备"),
    # "食品安全国家标准"要保留：它是这份标准官方名称的一部分
    # （《食品安全国家标准 食品添加剂 焦糖色》），跟"中华人民共和国国家标准"
    # 那种纯体系抬头不是一回事，后者才该丢掉。
    (COVER_FOOD_SAFETY, "GB 1886.64-2015", "食品安全国家标准 食品添加剂 焦糖色"),
    (COVER_SPACED_DASH, "GB 5085.6-2007", "危险废物鉴别标准 毒性物质含量鉴别"),
])
def test_real_covers_are_recognised(head, no, title):
    info = dt.detect_standard(head)
    assert info, "真实标准封面没被识别出来"
    assert info["doc_no"] == no
    assert info["title"] == title


def test_replaced_version_is_not_mistaken_for_self():
    """'代替 GB/T 1677—1981' 里的是被作废的旧版号，不是本标准的号。"""
    assert dt.detect_standard(COVER_PLAIN)["doc_no"] == "GB/T 1677-2008"


def test_dual_numbering_keeps_the_domestic_number():
    """等同采用时 ISO 号只是出处，参考文献里要著录的是国标号。"""
    assert dt.detect_standard(COVER_DUAL_NUMBER)["doc_no"] == "GB/T 17376-2008"


def test_dates_and_issuer_from_cover():
    info = dt.detect_standard(COVER_SPACED_DASH)
    assert info["issued"] == "2007-04-25"
    assert info["effective"] == "2007-10-01"
    assert info["year"] == "2007"
    assert "国家环境保护总局" in info["issuer"]
    assert "国家质量监督检验检疫总局" in info["issuer"]


def test_publisher_when_printed_on_cover():
    assert dt.detect_standard(COVER_DUAL_NUMBER)["publisher"] == "中国标准出版社"


def test_english_title_extracted():
    info = dt.detect_standard(COVER_PLAIN)
    assert info["title_en"].startswith("Determinating the epoxy value")


def test_paper_citing_standards_is_not_a_standard():
    """最要紧的一条反例：论文里引几个 GB/T 编号是家常便饭。
    单凭出现标准号就判成标准，整个库都会被污染。"""
    assert dt.detect_standard(PAPER_CITING_STANDARD) == {}


def test_empty_and_garbage_input():
    assert dt.detect_standard("") == {}
    assert dt.detect_standard("hello world") == {}


def test_min_markers_guards_single_keyword():
    """只出现一句'中华人民共和国国家标准'（比如论文在讨论标准体系）不够定案。"""
    assert dt.detect_standard("本文讨论了中华人民共和国国家标准体系的演进历程。") == {}


def test_describe_separates_fact_from_human_verification():
    """实施日期是封面事实，现行与否是人工核实结果——两者必须分开说，
    不能让调用方模型拿'2009-02-01 实施'当成'现在还现行'。"""
    line = dt.describe_standard("GB/T 1677-2008", "2009-02-01")
    assert "2009-02-01 实施" in line and "未核实" in line
    line2 = dt.describe_standard("GB/T 1677-2008", "2009-02-01", "废止", "2026-08-04")
    assert "废止" in line2 and "2026-08-04" in line2
