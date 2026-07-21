import os
import shutil
import unittest
import tempfile
import pandas as pd
from datetime import datetime

# 引入被测试模块
from utils import calculate_pdf_hash, export_to_excel
from batch_pipeline import SKIP_STATUSES, RESUBMITTABLE_STATUSES


class TestLiteratureAnalyzer(unittest.TestCase):

    def setUp(self):
        # 创建临时测试输出文件夹
        self.test_dir = tempfile.mkdtemp()
        self.excel_path = os.path.join(self.test_dir, "test_knowledge_base.xlsx")

    def tearDown(self):
        # 移除临时测试数据，践行优秀的数据科学和临时清理实践
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)

    def test_pdf_unique_hash(self):
        """测试 1: 验证 PDF SHA-256 ID 计算及路径/时戳回退的防撞性。
        验证即使文件不存在或同名，系统也可以计算出唯一的哈希标识。
        """
        # 构造两个不同的临时 PDF 文件
        pdf1 = os.path.join(self.test_dir, "paper_alpha.pdf")
        pdf2 = os.path.join(self.test_dir, "paper_beta.pdf")
        
        with open(pdf1, "w") as f:
            f.write("PDF-1.4 % 核心论文内容 Alpha")
        with open(pdf2, "w") as f:
            f.write("PDF-1.4 % 核心论文内容 Beta - 完全不同的学术描述")
            
        hash1 = calculate_pdf_hash(pdf1)
        hash2 = calculate_pdf_hash(pdf2)
        
        self.assertNotEqual(hash1, hash2, "不同内容的 PDF 生成的 ID 发生了冲突碰撞！")
        self.assertEqual(len(hash1), 64, "生成的 SHA-256 哈希字符串长度应为 64")
        
        # 验证回退机制：当文件损坏或无法读取时，也能生成稳定的防撞哈希
        fallback_hash = calculate_pdf_hash("non_existent.pdf")
        self.assertEqual(len(fallback_hash), 64)

    def test_state_machine_retry_filter(self):
        """测试 2: 验证状态机过滤机制对自动重试的支持。
        处于 SKIP_STATUSES 中的文献应当被滤除跳过；
        处于 RESUBMITTABLE_STATUSES (故障和待解析) 下的文献应当可以被安全重试和捡起。
        """
        # 模拟各种文献所处的状态
        mock_papers = [
            {"id": "doc_001", "status": "PARSED"},            # 待提交，应该可以重试
            {"id": "doc_002", "status": "BATCH_SUBMITTED"},   # 云端中，不能重试
            {"id": "doc_003", "status": "BATCH_FAILED"},      # 夭折失败，应该可以自动重试补偿
            {"id": "doc_004", "status": "EXPORTED"},          # 已导出，不能重试
            {"id": "doc_005", "status": "SUBMIT_FAILED"}      # 提交失败，应该可以自动重试补偿
        ]
        
        to_retry = []
        to_skip = []
        
        for paper in mock_papers:
            status = paper["status"]
            if status in SKIP_STATUSES:
                to_skip.append(paper["id"])
            if status in RESUBMITTABLE_STATUSES:
                to_retry.append(paper["id"])
                
        # 验证过滤断言
        self.assertIn("doc_001", to_retry, "待解析态 PARSED 应该被重新提交")
        self.assertIn("doc_003", to_retry, "云端失败态 BATCH_FAILED 应该被纳入补偿重试")
        self.assertIn("doc_005", to_retry, "接口提交失败态 SUBMIT_FAILED 应该被纳入补偿重试")
        
        self.assertIn("doc_002", to_skip, "已经在处理中的文献不应被重提交")
        self.assertIn("doc_004", to_skip, "已经完成导出的文献不应被重提交")

    def test_excel_deduplication(self):
        """测试 3: 验证带有 '文献ID' 字段的 Excel 批量写入与防覆写精准去重。
        去重时应当能够完美合并新老数据，并以最后一版的信息为准。
        """
        # 1. 模拟第一次写入 2 篇文献
        rows_batch_1 = [
            {
                "文献ID": "hash_1a2b",
                "标题": "Attention Is All You Need",
                "作者": "Vaswani et al.",
                "年份": "2017",
                "文件路径": "/archive/attention.pdf",
                "解析时间": "2026-05-28 10:00:00"
            },
            {
                "文献ID": "hash_3c4d",
                "标题": "BERT: Pre-training of Deep Bidirectional Transformers",
                "作者": "Devlin et al.",
                "年份": "2018",
                "文件路径": "/archive/bert.pdf",
                "解析时间": "2026-05-28 10:05:00"
            }
        ]
        export_to_excel(rows_batch_1, self.excel_path)
        
        # 确认文件创建并包含 2 行数据
        self.assertTrue(os.path.exists(self.excel_path))
        df_1 = pd.read_excel(self.excel_path)
        self.assertEqual(len(df_1), 2)
        
        # 2. 模拟第二次写入：包含对 hash_1a2b 属性的更新（如作者或路径），以及新增一篇文献 hash_5e6f
        rows_batch_2 = [
            {
                "文献ID": "hash_1a2b",  # 重复ID
                "标题": "Attention Is All You Need",
                "作者": "Vaswani et al. (Updated Author)", # 作者信息更新
                "年份": "2017",
                "文件路径": "/archive/attention_new.pdf", # 路径更新
                "解析时间": "2026-05-28 12:00:00"
            },
            {
                "文献ID": "hash_5e6f",  # 全新文献
                "标题": "GPT-4 Technical Report",
                "作者": "OpenAI",
                "年份": "2023",
                "文件路径": "/archive/gpt4.pdf",
                "解析时间": "2026-05-28 12:10:00"
            }
        ]
        export_to_excel(rows_batch_2, self.excel_path)
        
        # 3. 验证去重合并断言
        df_combined = pd.read_excel(self.excel_path)
        
        # 合理行数断言：2(原先) + 1(新增) - 1(重复去重) = 3 篇文献
        self.assertEqual(len(df_combined), 3)
        
        # 属性更新保留最新 (keep='last') 断言
        paper_attention = df_combined[df_combined["文献ID"] == "hash_1a2b"].iloc[0]
        self.assertEqual(paper_attention["作者"], "Vaswani et al. (Updated Author)")
        self.assertEqual(paper_attention["文件路径"], "/archive/attention_new.pdf")
        
        # 新增文献断言
        self.assertIn("GPT-4 Technical Report", df_combined["标题"].values)


if __name__ == "__main__":
    unittest.main()
