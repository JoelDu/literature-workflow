"""专利识别单元测试（纯本地，无网络、无 API）。

扉页样本直接取自库里两篇真实专利的 MinerU 输出，包括它插进数字中间的空格
（"2019 .06 .03"、"CN 112856361 B"）——那些空格正是最容易让正则失效的地方，
所以样本一律不做美化。
"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from litreview.patent import (detect_patent, extract_patent_no, kind_code,
                              patent_stage, patent_expiry, is_term_expired, describe)

# 真实样本 1：申请公布（审查中），种别码 A
HEAD_A = """(10)申请公布号 CN 110346043 A
(21)申请号 201910477986 .4
(22)申请日 2019 .06 .03
(71)申请人 湖北富邦科技股份有限公司地址 432400 湖北省孝感市应城市经济技术开发区城南大道1号
(72)发明人 李淑华 阮自斌 吴初柱 陈凯
(74)专利代理机构 武汉开元知识产权代理有限公司 42104代理人 唐正玉
(43)申请公布日 2019.10.18
(51)Int .Cl . (2006 .01)
## (54)发明名称
一种快速评价颗粒肥料表面泛白程度的方法
## (57)摘要
本发明涉及一种快速评价颗粒肥料表面泛白程度的方法……
"""

# 真实样本 2：已授权，种别码 B。注意扉页上 A 号和 B 号同时存在，必须取 B。
HEAD_B = """(10)授权公告号 CN 112856361 B
(21)申请号 202110251250.2
(22)申请日 2021 .03.08
(65)同一申请的已公布的文献号申请公布号 CN 112856361 A
(43)申请公布日 2021 .05.28
(73)专利权人 昆明理工大学地址 650599 云南省昆明市呈贡景明南路727号专利权人 浙江凯明科工程开发有限公司
(72)发明人 梅毅 杜加磊 杨亚斌 梁慧力王政伟
(74)专利代理机构 河北冀华知识产权代理有限公司 13151代理人 王占华
(54)发明名称热法磷酸全热能回收系统
## (57)摘要
本发明公开了一种热法磷酸全热能回收系统……
(45)授权公告日 2021.12.31
(51)Int.Cl. (2006.01)
"""

# 反例：库里真实存在的一篇 ResearchGate 章节。正文引用里出现过 "United States Patent"，
# 单关键词匹配必然误判，这条用例就是防它。
HEAD_NOT_PATENT = """See discussions, stats, and author profiles for this publication at:
https://www.researchgate.net/publication/265901843
# Effects of Fertilizer Compositions Containing Calcium Lignosulfonate and Silicic Acid
Chapter · September 2014
CITATIONS 0 READS 4,045
2 authors:
Ahmet Ozan Gezerman, Toros Agri Industry and Trade
# ABSTRACT
Increased efficiency in the agricultural field is absolutely essential ...
Reference is made to a United States Patent describing a similar approach.
"""


def test_detect_application_stage():
    p = detect_patent(HEAD_A)
    assert p
    assert p["patent_no"] == "CN110346043A"
    assert p["app_no"] == "201910477986.4"
    assert p["filing_date"] == "2019-06-03"
    assert p["title"] == "一种快速评价颗粒肥料表面泛白程度的方法"
    assert p["inventors"] == "李淑华 阮自斌 吴初柱 陈凯"
    assert p["assignee"] == "湖北富邦科技股份有限公司"


def test_detect_granted_prefers_grant_number():
    """扉页同时印着授权公告号 B 和申请公布号 A，必须取 B——它才是当前出版阶段。"""
    p = detect_patent(HEAD_B)
    assert p
    assert p["patent_no"] == "CN112856361B"
    assert p["filing_date"] == "2021-03-08"
    assert p["title"] == "热法磷酸全热能回收系统"       # (54) 与名称同行的排版
    assert p["assignee"] == "昆明理工大学"              # 单位与地址粘连，须切干净


def test_no_false_positive_on_paper():
    """单个 'United States Patent' 不足以判定为专利，必须 ≥2 个 INID 标记。"""
    assert detect_patent(HEAD_NOT_PATENT) == {}
    assert detect_patent("") == {}
    assert detect_patent("一篇普通论文的摘要，讨论了防结块剂的性能。") == {}


def test_kind_code_and_stage():
    assert kind_code("CN110346043A") == "A"
    assert kind_code("CN112856361B") == "B"
    assert kind_code("CN201942519U") == "U"
    assert kind_code("US10123456B2") == "B2"
    assert patent_stage("CN110346043A") == "申请公布（审查中）"
    assert patent_stage("CN112856361B") == "发明专利已授权"
    assert patent_stage("CN201942519U") == "实用新型已授权"
    assert patent_stage("US10123456B2") == "已授权"
    assert patent_stage("US20200123456A1") == "申请公开（审查中）"
    assert patent_stage("") == "未知"


def test_expiry_is_arithmetic_not_lookup():
    """保护期：发明 20 年、实用新型 10 年，均自申请日起算。"""
    assert patent_expiry("CN112856361B", "2021-03-08") == "2041-03-08"
    assert patent_expiry("CN110346043A", "2019-06-03") == "2039-06-03"
    assert patent_expiry("CN201942519U", "2011-01-20") == "2021-01-20"
    assert patent_expiry("CN112856361B", "") == ""


def test_term_expired():
    assert is_term_expired("CN201942519U", "2011-01-20", today=date(2026, 8, 4))
    assert not is_term_expired("CN112856361B", "2021-03-08", today=date(2026, 8, 4))
    assert not is_term_expired("CN112856361B", "")      # 无申请日不敢断言过期


def test_describe_never_fakes_legal_status():
    """核心约束：没人工核实过，就必须明说「未核实」，不能拿出版阶段冒充法律状态。"""
    d = describe("CN112856361B", "2021-03-08")
    assert "法律状态 未核实" in d
    assert "发明专利已授权" in d          # 出版阶段照常显示，但两者分开呈现
    d2 = describe("CN112856361B", "2021-03-08", "驳回", "2026-08-04")
    assert "法律状态 驳回（2026-08-04 核实）" in d2


def test_extract_patent_no_tolerates_mineru_spacing():
    assert extract_patent_no("(10)申请公布号 CN 110346043 A") == "CN110346043A"
    assert extract_patent_no("授权公告号 CN 112856361 B") == "CN112856361B"
    assert extract_patent_no("没有任何专利号的文本") == ""
