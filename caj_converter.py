#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import subprocess
import shutil

from utils import get_settings

# 路径统一走 settings，兼容容器内 (/app/...) 与宿主机两种运行环境
# 之前硬编码宿主机路径 /mnt/ripe/literature_analyzer_data，导致在容器内调用时目录不存在、静默什么都不做
_settings = get_settings()
INPUT_DIR = _settings.INPUT_PDF_DIR
TEMP_DIR = os.path.join(os.path.dirname(_settings.DB_PATH), "caj_originals")

def check_dependencies():
    """检查并安装必要依赖"""
    print("正在检查系统依赖...")
    
    # 检查并安装 python 依赖
    try:
        import caj2pdf
        print("caj2pdf 已安装。")
    except ImportError:
        print("未检测到 caj2pdf，尝试通过 github 源码安装...")
        try:
            subprocess.run([sys.executable, "-m", "pip", "install", "git+https://github.com/caj2pdf/caj2pdf.git", "--break-system-packages"], check=True)
            print("caj2pdf 安装成功！")
        except subprocess.CalledProcessError:
            try:
                # 尝试普通安装
                subprocess.run([sys.executable, "-m", "pip", "install", "git+https://github.com/caj2pdf/caj2pdf.git"], check=True)
                print("caj2pdf 安装成功！")
            except Exception as e:
                print(f"安装 caj2pdf 失败，请手动执行: pip install git+https://github.com/caj2pdf/caj2pdf.git。错误: {e}")
                sys.exit(1)

    # 检查 mutool (caj2pdf 核心依赖，用于转换)
    if not shutil.which("mutool"):
        print("⚠️  警告: 系统未检测到 'mutool'。对于部分 CAJ 文件，caj2pdf 需要 mutool 进行 PDF 页面重构。")
        print("正在尝试自动安装 mutool (仅限 Debian/Ubuntu)...")
        try:
            subprocess.run(["sudo", "apt-get", "update"], check=True)
            subprocess.run(["sudo", "apt-get", "install", "-y", "mupdf-tools"], check=True)
            print("mutool 安装成功！")
        except Exception as e:
            print(f"自动安装 mutool 失败。如果转换报错，请手动执行: sudo apt-get install mupdf-tools")

def convert_caj_files():
    """扫描并转换 CAJ 文件"""
    if not os.path.exists(INPUT_DIR):
        print(f"错误: 输入目录不存在: {INPUT_DIR}")
        return

    caj_files = [f for f in os.listdir(INPUT_DIR) if f.lower().endswith('.caj')]
    if not caj_files:
        print("没有在输入目录中找到任何 .caj 文件。")
        return

    print(f"共发现 {len(caj_files)} 个 .caj 文件，开始转换...")
    os.makedirs(TEMP_DIR, exist_ok=True)
    
    success_count = 0
    fail_count = 0

    for filename in caj_files:
        caj_path = os.path.join(INPUT_DIR, filename)
        pdf_name = os.path.splitext(filename)[0] + ".pdf"
        pdf_path = os.path.join(INPUT_DIR, pdf_name)

        print(f"\n正在转换: {filename} -> {pdf_name}")
        
        # 尝试调用 caj2pdf convert 命令
        try:
            # 命令行直接运行 caj2pdf showinfo 校验，或者直接 convert
            result = subprocess.run(
                ["caj2pdf", "convert", caj_path, pdf_path],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            
            if result.returncode == 0 and os.path.exists(pdf_path) and os.path.getsize(pdf_path) > 0:
                print(f"✅ 转换成功: {pdf_name}")
                # 将原 .caj 移动到备份文件夹，防止重复处理
                shutil.move(caj_path, os.path.join(TEMP_DIR, filename))
                success_count += 1
            else:
                print(f"❌ 转换失败: {filename}")
                if result.stderr:
                    print(f"  错误详情: {result.stderr.strip()}")
                fail_count += 1
        except Exception as e:
            print(f"❌ 运行转换脚本时发生异常: {e}")
            fail_count += 1

    print("\n====================================")
    print(f"转换任务结束！")
    print(f"成功: {success_count} 篇")
    print(f"失败: {fail_count} 篇")
    if success_count > 0:
        print(f"原 .caj 文件已安全备份至: {TEMP_DIR}")
        print("新生成的 PDF 文件已放入输入目录，容器会自动捡起进行解析！")
    print("====================================")

if __name__ == "__main__":
    check_dependencies()
    convert_caj_files()
