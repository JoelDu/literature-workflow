import os
import json
import tempfile
import pytest
from litreview.analysis_record import PaperAnalysisRecord


def test_analysis_record_from_llm_json_and_reconcile():
    raw_json = json.dumps({
        "tldr": "开发了一种新型聚氨酯包膜肥料。",
        "keywords": "包膜控释; 水性聚氨酯; 释放动力学",
        "fertilizer_implications": "利用大豆油多元醇降低包膜材料成本，调控交联度延长控释期至90天。",
        "abstract": "LLM 提取的摘要",
        "background": "传统控释肥包膜材料降解难、成本高。",
        "methods": "两步水性乳化聚合，流化床喷雾包膜。",
        "results": "在25度水中释放周期达到90天，养分利用率提升32%。",
        "conclusion": "该包膜材料具备良好的降解性与农用经济性。",
        "language": "zh",
        "authors": "张三; 李四",
        "journal": "农业工程学报",
        "year": "2026",
    })

    sample_md = """# 一种水性聚氨酯包膜控释肥料的研究

## 摘要
这是论文全文开头正文里的原始摘要段落，用于验证去幻觉双向融合机制。

## 引言
正文内容...
"""

    record = PaperAnalysisRecord.from_llm_json(raw_json, markdown_text=sample_md)
    assert record.tldr == "开发了一种新型聚氨酯包膜肥料。"
    assert "包膜控释" in record.keywords
    assert "大豆油多元醇" in record.fertilizer_implications
    # 验证原始摘要成功被正则抽取并覆盖 LLM 提取的内容
    assert "这是论文全文开头正文里的原始摘要段落" in record.abstract
    assert record.year == "2026"
    assert record.authors == "张三; 李四"


def test_analysis_record_to_excel_row():
    record = PaperAnalysisRecord(
        tldr="一句话总结",
        keywords="磷化工; 湿法磷酸",
        fertilizer_implications="工艺脱镁脱铁关键参数",
        authors="王五",
        year="2025",
        journal="化工学报",
        language="zh",
    )

    row = record.to_excel_row(
        doc_id="a1b2c3d4e5f6",
        title="湿法磷酸净化新工艺",
        pdf_path="/path/to/paper.pdf",
        note_name="湿法磷酸净化新工艺_a1b2c3d4",
        status="EXPORTED",
        provider="openai",
        job_id="batch_123",
        processed_at="2026-08-22 22:00:00",
    )

    assert row["文献ID"] == "a1b2c3d4e5f6"
    assert row["标题"] == "湿法磷酸净化新工艺"
    assert row["关键词"] == "磷化工; 湿法磷酸"
    assert row["肥料开发借鉴"] == "工艺脱镁脱铁关键参数"
    assert row["TLDR"] == "一句话总结"
    assert row["Obsidian链接"] == "[[湿法磷酸净化新工艺_a1b2c3d4]]"
    assert row["状态"] == "EXPORTED"
    assert row["服务商"] == "openai"


def test_analysis_record_publish_obsidian_note():
    with tempfile.TemporaryDirectory() as tmp_vault:
        record = PaperAnalysisRecord(
            tldr="控释肥新配方",
            keywords="控释肥; 生物质膜",
            fertilizer_implications="利用木质素接枝改性降低成本",
            abstract="测试摘要",
            background="背景描述",
            methods="实验方法",
            results="测试结果",
            conclusion="测试结论",
            authors="赵六",
            year="2026",
            journal="植物营养与肥料学报",
        )

        note_path, note_name = record.publish_obsidian_note(
            obsidian_dir=tmp_vault,
            title="生物质膜控释肥料",
            doc_id="112233445566",
            raw_images=[],
        )

        assert os.path.exists(note_path)
        with open(note_path, "r", encoding="utf-8") as f:
            content = f.read()

        assert "## 💡 核心摘要 / TL;DR" in content
        assert "控释肥新配方" in content
        assert "## 🌾 肥料开发借鉴与工程启示 / Fertilizer Implications" in content
        assert "利用木质素接枝改性降低成本" in content
        assert "## 🏷️ 核心领域与关键词 / Keywords" in content
        assert "生物质膜" in content


def test_analysis_record_fallback_on_corrupt_json():
    corrupt_data = "{ invalid_json: true, unterminated string"
    record = PaperAnalysisRecord.from_llm_json(corrupt_data)
    assert record.tldr == "结果解析 JSON 失败"
    assert record.error is not None
    assert "JSONDecodeError" in record.error
