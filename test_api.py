"""快速测试 MinerUClient 两步流程（使用 .env 配置路径）

用法:
    python test_api.py
    python test_api.py /path/to/your.pdf /path/to/output_dir
"""
import sys
import os
from pathlib import Path
from dotenv import load_dotenv
from mineru_client import MinerUClient

load_dotenv()

api_key = os.getenv("MINERU_API_KEY")
if not api_key:
    print("❌ 未找到 MINERU_API_KEY，请在 .env 文件中配置。")
    sys.exit(1)

# 支持命令行参数，也支持 .env 中的路径配置
if len(sys.argv) >= 3:
    pdf_path = sys.argv[1]
    output_dir = sys.argv[2]
else:
    pdf_path = os.getenv("TEST_PDF_PATH", "")
    output_dir = os.getenv("TEST_OUTPUT_DIR", "./test_output")

if not pdf_path or not Path(pdf_path).exists():
    print(f"❌ PDF 文件不存在: {pdf_path!r}")
    print("请通过以下任意方式指定 PDF 路径：")
    print("  1. 命令行参数: python test_api.py <pdf路径> <输出目录>")
    print("  2. .env 文件: TEST_PDF_PATH=<pdf路径>")
    sys.exit(1)

print(f"Starting MinerU test...")
print(f"  PDF   : {pdf_path}")
print(f"  Output: {output_dir}")

client = MinerUClient(api_key)
result = client.process_pdf(pdf_path, output_dir)

print("\n--- RESULT ---")
print("Status   :", result["status"])
print("Output   :", result["output_dir"])
print("Images   :", result["images_dir"])
print("Markdown (first 500 chars):")
print(result["markdown"][:500])
