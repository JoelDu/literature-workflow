"""chunker 单元测试（无网络依赖）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from litreview.chunker import clean_markdown, split_markdown

ZH_DOC = """# 复合肥防结块剂研究

## 摘要

本文研究了复合肥防结块剂的制备方法。实验表明添加0.3%的防结块剂可使结块率降低85%。

![图1](images/fig1.jpg)

## 1 引言

复合肥在储存过程中容易结块，严重影响使用。""" + "结块机理包括晶桥形成、毛细吸附等多种因素。" * 60 + """

## 2 实验方法

采用喷涂法在颗粒表面形成包膜。<img src="images/fig2.jpg" alt="装置图">
包膜厚度控制在10-20微米。

## 参考文献

[1] 张三. 肥料学报, 2020.
[2] 李四. 磷肥与复肥, 2021.
"""

EN_DOC = """Abstract

This study investigates anti-caking agents for compound fertilizers. """ + \
    "The crystal bridge mechanism dominates under high humidity conditions. " * 80 + """

References

[1] Smith J. Fertilizer Research, 2019.
"""


def test_clean_removes_images():
    text = clean_markdown(ZH_DOC)
    assert "![" not in text
    assert "<img" not in text


def test_clean_truncates_references():
    text = clean_markdown(ZH_DOC)
    assert "张三" not in text
    assert "参考文献" not in text
    # 正文仍在
    assert "喷涂法" in text


def test_clean_keeps_early_reference_mention():
    # 参考文献标题出现在前 60% 时不截断
    doc = "## 参考文献\n\n正文开始" + "很长的正文。" * 500
    text = clean_markdown(doc)
    assert "正文开始" in text


def test_split_basic():
    chunks = split_markdown("d" * 64, ZH_DOC, chunk_size=1000, overlap=150)
    assert len(chunks) >= 3
    for c in chunks:
        assert 30 <= len(c.content) <= 1000 * 1.5
        assert c.doc_id == "d" * 64
    # chunk_index 连续
    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))


def test_split_section_titles():
    chunks = split_markdown("d" * 64, ZH_DOC, chunk_size=1000, overlap=150)
    titles = {c.section_title for c in chunks}
    assert "1 引言" in titles
    assert "2 实验方法" in titles


def test_split_overlap_within_section():
    chunks = split_markdown("d" * 64, ZH_DOC, chunk_size=500, overlap=100)
    # 同一小节内相邻块应有内容重叠
    for i in range(1, len(chunks)):
        if chunks[i].section_title == chunks[i - 1].section_title:
            tail = chunks[i - 1].content[-150:]
            head = chunks[i].content[:250]
            assert any(tail[j:j + 20] in head for j in range(0, len(tail) - 20, 10)), \
                f"块 {i-1}→{i} 无重叠"
            break


def test_split_english_no_headings():
    chunks = split_markdown("e" * 64, EN_DOC, chunk_size=1000, overlap=150)
    assert len(chunks) >= 2
    joined = " ".join(c.content for c in chunks)
    assert "crystal bridge" in joined
    assert "Smith" not in joined  # references 已截断


def test_empty_doc():
    assert split_markdown("x" * 64, "") == []
    assert split_markdown("x" * 64, "![img](a.jpg)") == []
