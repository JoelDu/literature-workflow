#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import json
import sqlite3
import shutil
from pathlib import Path

# 配置路径
BASE_DIR = "./data"
DB_PATH = os.path.join(BASE_DIR, "batch_tracking.db")
MINERU_OUT_DIR = os.path.join(BASE_DIR, "mineru_output")
PROCESSED_PDF_DIR = os.path.join(BASE_DIR, "processed_pdfs")

# 将当前工作目录添加到 sys.path 以便导入项目模块
sys.path.append(os.path.abspath(os.path.dirname(__file__)))
from utils import get_settings
from mineru_client import MinerUClient

def recover_all():
    settings = get_settings()
    os.makedirs(MINERU_OUT_DIR, exist_ok=True)
    
    if not os.path.exists(DB_PATH):
        print(f"错误: 数据库不存在: {DB_PATH}")
        return

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # 获取所有 EXPORTED 状态的文献记录
    c.execute("SELECT id, title, pdf_path, mineru_md, images_dir FROM papers WHERE status='EXPORTED'")
    papers = c.fetchall()
    
    print(f"📊 数据库中共有 {len(papers)} 篇已导出文献记录。开始恢复中间产物...")
    
    mineru_client = MinerUClient(settings.MINERU_API_KEY)
    
    success_count = 0
    skip_count = 0
    fail_count = 0

    for idx, paper in enumerate(papers, 1):
        doc_id = paper['id']
        title = paper['title']
        pdf_path = paper['pdf_path']
        mineru_md = paper['mineru_md']
        images_dir_db = paper['images_dir']
        
        # 统一规范输出目录名称: {title}_{doc_id[:8]}
        folder_name = f"{title}_{doc_id[:8]}"
        # 去除非法字符（MinerU 客户端生成逻辑）
        for char in ['/', '\\', '?', '*', ':', '|', '"', '<', '>']:
            folder_name = folder_name.replace(char, '_')
        
        paper_output_dir = os.path.join(MINERU_OUT_DIR, folder_name)
        images_dir = os.path.join(paper_output_dir, "images")
        
        print(f"\n[{idx}/{len(papers)}] 处理: {title}")
        
        # 如果对应的物理 images 文件夹及主 Markdown 文件已存在，跳过
        md_file_path = os.path.join(paper_output_dir, f"{title}.md")
        if os.path.exists(paper_output_dir) and os.path.exists(images_dir) and os.listdir(images_dir):
            print(f"  ✅ 物理目录和图片已存在，跳过。")
            skip_count += 1
            continue

        # 如果本地 PDF 不存在，报错
        if not os.path.exists(pdf_path):
            # 尝试修复宿主机与容器相对路径
            filename = os.path.basename(pdf_path)
            alt_path = os.path.join(PROCESSED_PDF_DIR, filename)
            if os.path.exists(alt_path):
                pdf_path = alt_path
            else:
                print(f"  ❌ 找不到归档 PDF 源文件: {pdf_path} (跳过此篇)")
                fail_count += 1
                continue
                
        print(f"  🔄 正在通过 MinerU API 重新解析并恢复完整 zip 包及切片图片...")
        os.makedirs(paper_output_dir, exist_ok=True)
        
        try:
            # 调用 MinerUClient 重新抓取结果包并解包
            res = mineru_client.process_pdf(pdf_path, paper_output_dir)
            if res.get("status") == "success":
                # 如果解压后的主 md 文件与数据库中不一致，以数据库为准回填
                if mineru_md:
                    md_files = list(Path(paper_output_dir).rglob("*.md"))
                    if md_files:
                        primary_md = max(md_files, key=lambda f: f.stat().st_size)
                        primary_md.write_text(mineru_md, encoding="utf-8")
                print(f"  ✅ 恢复成功！")
                success_count += 1
            else:
                print(f"  ❌ MinerU 接口返回失败: {res}")
                fail_count += 1
        except Exception as e:
            # 如果接口失败，但有 md，则至少还原 md 文本
            if mineru_md:
                print(f"  ⚠️ MinerU API 失败 ({e})，正在退而仅恢复 Markdown 文本...")
                os.makedirs(images_dir, exist_ok=True)
                with open(os.path.join(paper_output_dir, f"{title}.md"), "w", encoding="utf-8") as f:
                    f.write(mineru_md)
                success_count += 1
            else:
                print(f"  ❌ 恢复失败: {e}")
                fail_count += 1

    print("\n====================================")
    print(f"🎉 恢复任务结束！")
    print(f"成功恢复/重建: {success_count} 篇")
    print(f"无损保留: {skip_count} 篇")
    print(f"失败/缺失: {fail_count} 篇")
    print("====================================")

    conn.close()

if __name__ == "__main__":
    recover_all()
