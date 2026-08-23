"""utils.py — 共享工具函数
供 pipeline.py 和 batch_pipeline.py 共同使用，消除代码重复。
"""
import os
import re
import sys
import json
import time
import hashlib
import shutil
import sqlite3
from pathlib import Path
from datetime import datetime
from typing import Any

import pandas as pd
from jinja2 import Template
from pydantic import BaseModel, ValidationError


# ── 配置模型与启动强校验 (Pydantic) ──────────────────────────────────────────

class Settings(BaseModel):
    # 凭据在真正创建对应客户端时校验。status/history/types 等纯本地命令不应因为
    # 没有 API Key 而无法查看；DEEPSEEK_API_KEY 也只是 SiliconFlow 未配置时的回退。
    MINERU_API_KEY: str = ""
    OPENAI_API_KEY: str = ""
    OPENAI_API_BASE: str = "https://api.openai.com/v1"
    ANALYSIS_MODEL: str = "gpt-5.4-mini"                # 论文分析、元数据、证据筛选
    DEEPSEEK_API_KEY: str = ""
    GEMINI_API_KEY: str = ""  # 仅兼容历史 Gemini Batch 任务，新任务不使用
    
    # 路径配置默认全部集中到安全的 data/ 目录下
    INPUT_PDF_DIR: str = "./input_pdfs"
    MINERU_OUTPUT_DIR: str = "./mineru_output"
    OBSIDIAN_VAULT_DIR: str = "./obsidian_vault"
    EXCEL_OUTPUT_PATH: str = "./data/knowledge_base.xlsx"
    PROCESSED_PDF_DIR: str = "./processed_pdfs"
    FAILED_PDF_DIR: str = "./failed_pdfs"
    DB_PATH: str = "./data/batch_tracking.db"
    
    RUN_MODE: str = "realtime"
    SCAN_INTERVAL_MINUTES: int = 10
    BATCH_PREPARE_TIME: str = "01:00"
    BATCH_FETCH_TIME_1: str = "07:30"
    BATCH_FETCH_TIME_2: str = "13:30"
    BATCH_SIZE_LIMIT: int = 30
    WEBHOOK_URL: str = ""

    # ── 及时入库调度（间隔驱动，替代固定时刻） ──
    BATCH_SCAN_INTERVAL_MINUTES: int = 30    # 扫描 input_pdfs 并提交 Batch 的间隔
    BATCH_FETCH_INTERVAL_MINUTES: int = 120  # 拉取 Batch 结果的间隔

    # ── 综述生成器 (Review Generator) ──
    EMBEDDING_MODEL: str = "Qwen/Qwen3-Embedding-8B"
    EMBEDDING_DIM: int = 4096
    EMBEDDING_BATCH_SIZE: int = 32
    EMBEDDING_QUERY_INSTRUCTION: str = "给定一个中文或英文的检索问题，找出能回答该问题的文献段落"
    # remote=文档嵌入走在线 API；local=交给 nightly_index.py 用本地权重跑。
    # 默认使用在线 API，避免新部署意外加载大型本地模型。
    REVIEW_EMBED_BACKEND: str = "remote"
    LOCAL_EMBEDDING_MODEL_PATH: str = ""
    NIGHTLY_INDEX_DEADLINE: str = "08:00"     # 仅本地嵌入任务使用
    RERANK_MODEL: str = "Qwen/Qwen3-Reranker-8B"
    RERANK_ENABLE: bool = True
    RERANK_CANDIDATES: int = 80              # 向量召回候选数（rerank 后取 REVIEW_TOP_K）
    REVIEW_MODEL: str = "gpt-5.4"                       # 仅综述写作使用；论文分析固定 mini
    REVIEW_ENRICH_LLM: bool = True           # enrich 时是否用 LLM 补缺元数据
    REVIEW_CHUNK_SIZE: int = 1000
    REVIEW_CHUNK_OVERLAP: int = 150
    # 教材/书籍单独用更大的块（书讲得散，大块保完整概念）。字符按原始 len 计，
    # 4800 原始字符≈3000 汉字（实测中文 1 汉字≈1.6 原始字符）；英文同口径。
    BOOK_CHUNK_SIZE: int = 4800
    BOOK_CHUNK_OVERLAP: int = 600
    REVIEW_TOP_K: int = 60                # 每章进入 GPT 评估前的重排候选数
    REVIEW_MAX_CHUNKS_PER_DOC: int = 2     # 单篇文献在初筛中最多保留 2 块，强制文献多样性
    REVIEW_MIN_SCORE: int = 6
    REVIEW_EVIDENCE_N: int = 32            # 每章最终保留的高分多样化证据数（覆盖 12-18 篇独立文献）
    REVIEW_MAP_CONCURRENCY: int = 8
    REVIEW_OUTPUT_DIR: str = "./obsidian_vault/reviews"
    REVIEW_AUTO_INDEX: bool = True           # 导出后自动增量更新向量索引
    BOOK_SPLIT_PAGES: int = 180              # 教材入库时每份 PDF 最大页数（≤ MinerU 单任务上限）
    BOOK_INPUT_DIR: str = "./input_books"    # add-book 不传路径时默认扫描此目录（与 INPUT_PDF_DIR 平行）
    BOOK_OUTPUT_DIR: str = "./book_output"   # 教材解析结果根目录，每本书一个子文件夹（与 MINERU_OUTPUT_DIR 平行、互不混放）
    BOOK_PROCESSED_DIR: str = "./processed_books"  # 已入库的教材原件归档（与 PROCESSED_PDF_DIR 平行）
    BOOK_FAILED_DIR: str = "./failed_books"        # 文件本身损坏、重试也没意义的教材（与 FAILED_PDF_DIR 平行）
    BOOK_INTAKE_TIME: str = "03:00"           # 每日教材定时入库任务启动时间（HH:MM，本地时区）
    BOOK_DAILY_PAGE_BUDGET: int = 2000        # 每晚最多处理的 PDF 总页数，用完留到下一晚（EPUB 走 pandoc 不占此预算）
    REVIEW_INSERT_FIGURES: bool = True       # 综述是否自动插入相关论文图表
    REVIEW_FIGURES_PER_SECTION: int = 2      # 每章节最多插图数
    REVIEW_FIGURE_MIN_SCORE: float = 0.2     # 图题 rerank 相关度阈值（低于不插）
    LLM_DAILY_TOKEN_CAP: int = 2_000_000
    LLM_BATCH_TOKEN_CAP: int = 600_000
    LLM_MAX_INPUT_TOKENS: int = 200_000
    REVIEW_DAILY_TOKEN_CAP: int = 400_000                # GPT-5.4 独立日额度 (400k Tokens)


_settings = None

def get_settings() -> Settings:
    """获取结构与类型校验后的全局配置（单例模式）。

    API 凭据由对应客户端在真正发生网络操作时校验，使纯本地运维命令能在没有
    `.env` 的全新安装上正常运行。
    """
    global _settings
    if _settings is None:
        try:
            config_dict = {
                "MINERU_API_KEY": os.getenv("MINERU_API_KEY", ""),
                "OPENAI_API_KEY": os.getenv("OPENAI_API_KEY", ""),
                "OPENAI_API_BASE": os.getenv("OPENAI_API_BASE", "https://api.openai.com/v1"),
                "ANALYSIS_MODEL": "gpt-5.4-mini",
                "INPUT_PDF_DIR": os.getenv("INPUT_PDF_DIR", "./input_pdfs"),
                "MINERU_OUTPUT_DIR": os.getenv("MINERU_OUTPUT_DIR", "./mineru_output"),
                "OBSIDIAN_VAULT_DIR": os.getenv("OBSIDIAN_VAULT_DIR", "./obsidian_vault"),
                "EXCEL_OUTPUT_PATH": os.getenv("EXCEL_OUTPUT_PATH", "./data/knowledge_base.xlsx"),
                "PROCESSED_PDF_DIR": os.getenv("PROCESSED_PDF_DIR", "./processed_pdfs"),
                "FAILED_PDF_DIR": os.getenv("FAILED_PDF_DIR", "./failed_pdfs"),
                "DB_PATH": os.getenv("DB_PATH", "./data/batch_tracking.db"),
                "RUN_MODE": os.getenv("RUN_MODE", "realtime"),
                "SCAN_INTERVAL_MINUTES": int(os.getenv("SCAN_INTERVAL_MINUTES", "10")),
                "BATCH_PREPARE_TIME": os.getenv("BATCH_PREPARE_TIME", "01:00"),
                "BATCH_FETCH_TIME_1": os.getenv("BATCH_FETCH_TIME_1", "07:30"),
                "BATCH_FETCH_TIME_2": os.getenv("BATCH_FETCH_TIME_2", "13:30"),
                "BATCH_SIZE_LIMIT": int(os.getenv("BATCH_SIZE_LIMIT", "30")),
                "WEBHOOK_URL": os.getenv("WEBHOOK_URL", ""),
                "BATCH_SCAN_INTERVAL_MINUTES": int(os.getenv("BATCH_SCAN_INTERVAL_MINUTES", "30")),
                "BATCH_FETCH_INTERVAL_MINUTES": int(os.getenv("BATCH_FETCH_INTERVAL_MINUTES", "120")),
                "EMBEDDING_MODEL": os.getenv("EMBEDDING_MODEL", "Qwen/Qwen3-Embedding-8B"),
                "EMBEDDING_DIM": int(os.getenv("EMBEDDING_DIM", "4096")),
                "EMBEDDING_BATCH_SIZE": int(os.getenv("EMBEDDING_BATCH_SIZE", "32")),
                "EMBEDDING_QUERY_INSTRUCTION": os.getenv("EMBEDDING_QUERY_INSTRUCTION",
                                                         "给定一个中文或英文的检索问题，找出能回答该问题的文献段落"),
                "RERANK_MODEL": os.getenv("RERANK_MODEL", "Qwen/Qwen3-Reranker-8B"),
                "RERANK_ENABLE": os.getenv("RERANK_ENABLE", "true").lower() == "true",
                "RERANK_CANDIDATES": int(os.getenv("RERANK_CANDIDATES", "80")),
                # 固定值，故意不读取环境变量，避免综述写作被意外换模型。
                "REVIEW_MODEL": "gpt-5.4",
                "REVIEW_ENRICH_LLM": os.getenv("REVIEW_ENRICH_LLM", "true").lower() == "true",
                "REVIEW_CHUNK_SIZE": int(os.getenv("REVIEW_CHUNK_SIZE", "1000")),
                "REVIEW_CHUNK_OVERLAP": int(os.getenv("REVIEW_CHUNK_OVERLAP", "150")),
                "BOOK_CHUNK_SIZE": int(os.getenv("BOOK_CHUNK_SIZE", "4800")),
                "BOOK_CHUNK_OVERLAP": int(os.getenv("BOOK_CHUNK_OVERLAP", "600")),
                "REVIEW_TOP_K": int(os.getenv("REVIEW_TOP_K", "60")),
                "REVIEW_MAX_CHUNKS_PER_DOC": int(os.getenv("REVIEW_MAX_CHUNKS_PER_DOC", "2")),
                "REVIEW_MIN_SCORE": int(os.getenv("REVIEW_MIN_SCORE", "6")),
                "REVIEW_EVIDENCE_N": int(os.getenv("REVIEW_EVIDENCE_N", "32")),
                "REVIEW_EMBED_BACKEND": os.getenv("REVIEW_EMBED_BACKEND", "remote"),
                "LOCAL_EMBEDDING_MODEL_PATH": os.getenv("LOCAL_EMBEDDING_MODEL_PATH", ""),
                "NIGHTLY_INDEX_DEADLINE": os.getenv("NIGHTLY_INDEX_DEADLINE", "08:00"),
                "REVIEW_MAP_CONCURRENCY": int(os.getenv("REVIEW_MAP_CONCURRENCY", "8")),
                "REVIEW_OUTPUT_DIR": os.getenv("REVIEW_OUTPUT_DIR", "./obsidian_vault/reviews"),
                "REVIEW_AUTO_INDEX": os.getenv("REVIEW_AUTO_INDEX", "true").lower() == "true",
                "BOOK_SPLIT_PAGES": int(os.getenv("BOOK_SPLIT_PAGES", "180")),
                "BOOK_INPUT_DIR": os.getenv("BOOK_INPUT_DIR", "./input_books"),
                "BOOK_OUTPUT_DIR": os.getenv("BOOK_OUTPUT_DIR", "./book_output"),
                "BOOK_PROCESSED_DIR": os.getenv("BOOK_PROCESSED_DIR", "./processed_books"),
                "BOOK_FAILED_DIR": os.getenv("BOOK_FAILED_DIR", "./failed_books"),
                "BOOK_INTAKE_TIME": os.getenv("BOOK_INTAKE_TIME", "03:00"),
                "BOOK_DAILY_PAGE_BUDGET": int(os.getenv("BOOK_DAILY_PAGE_BUDGET", "2000")),
                "REVIEW_INSERT_FIGURES": os.getenv("REVIEW_INSERT_FIGURES", "true").lower() == "true",
                "REVIEW_FIGURES_PER_SECTION": int(os.getenv("REVIEW_FIGURES_PER_SECTION", "2")),
                "REVIEW_FIGURE_MIN_SCORE": float(os.getenv("REVIEW_FIGURE_MIN_SCORE", "0.2")),
                # 项目免费额度硬阀：不读环境变量，避免部署配置误改后进入付费区间。
                "LLM_DAILY_TOKEN_CAP": 2_000_000,
                "LLM_BATCH_TOKEN_CAP": 600_000,
                "LLM_MAX_INPUT_TOKENS": 200_000,
                "REVIEW_DAILY_TOKEN_CAP": 200_000,
            }
            _settings = Settings(**config_dict)
        except ValidationError as e:
            from rich import print as rprint
            rprint("\n[bold red]❌ 配置校验失败，请检查 .env 或 api_keys.env 环境配置！[/bold red]")
            for error in e.errors():
                loc = " -> ".join(str(x) for x in error["loc"])
                rprint(f"  - [red]{loc}[/red]: {error['msg']}")
            sys.exit(1)
    return _settings


# ── SHA-256 主键唯一标识 ──────────────────────────────────────────────────────

def calculate_pdf_hash(pdf_path: str) -> str:
    """计算 PDF 文件的 SHA-256 哈希值，用于文献去重与防同名论文静默覆盖。
    如果文件操作异常，回退采用路径及最后修改时间混淆的稳定哈希。
    """
    sha256 = hashlib.sha256()
    try:
        with open(pdf_path, "rb") as f:
            while chunk := f.read(65536):
                sha256.update(chunk)
        return sha256.hexdigest()
    except Exception as e:
        print(f"[Warning] 无法读取文件计算内容哈希，回退至元数据哈希 ({pdf_path}): {e}", file=sys.stderr)
        try:
            stat = os.stat(pdf_path)
            fallback_str = f"{pdf_path}_{stat.st_size}_{stat.st_mtime}"
        except Exception as e2:
            # 注意：不能用 datetime.now() 兜底，那样每次调用哈希都不同，会彻底击穿去重机制
            print(f"[Warning] 文件元数据也读取失败，回退至纯路径哈希，去重能力下降 ({pdf_path}): {e2}", file=sys.stderr)
            fallback_str = pdf_path
        return hashlib.sha256(fallback_str.encode("utf-8")).hexdigest()


# ── 已处理文献哈希去重 ────────────────────────────────────────────────────────

def _get_hashes_path(db_path: str) -> str:
    """返回 processed_hashes.json 的路径，与 db_path 同目录。"""
    return os.path.join(os.path.dirname(db_path), "processed_hashes.json")


def load_processed_hashes(db_path: str) -> set[str]:
    """读取已处理文献的 SHA-256 哈希集合，用于跳过重复文件。
    文件不存在或格式异常时返回空集合，保证管道不因此中断。
    """
    hashes_path = _get_hashes_path(db_path)
    if not os.path.exists(hashes_path):
        return set()
    try:
        with open(hashes_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return set(data) if isinstance(data, list) else set()
    except Exception:
        return set()


def save_processed_hash(db_path: str, doc_id: str) -> None:
    """将新的文献哈希追加到持久化集合中。
    采用读-合并-写策略，确保并发安全性（单进程场景）。
    """
    hashes_path = _get_hashes_path(db_path)
    existing = load_processed_hashes(db_path)
    existing.add(doc_id)
    os.makedirs(os.path.dirname(hashes_path), exist_ok=True)
    with open(hashes_path, "w", encoding="utf-8") as f:
        json.dump(sorted(existing), f, ensure_ascii=False, indent=2)


# ── 路径工具 ─────────────────────────────────────────────────────────────────

def get_template_path() -> Path:
    """返回 obsidian_template.md 的绝对路径（基于脚本位置，与 CWD 无关）。"""
    return Path(__file__).parent / "obsidian_template.md"


def init_dirs(*dirs: str) -> None:
    """批量创建目录，若已存在则忽略。"""
    for d in dirs:
        os.makedirs(d, exist_ok=True)


# ── 文本处理 ─────────────────────────────────────────────────────────────────

def extract_original_abstract(text: str) -> str:
    """从 Markdown 文本中提取原始的摘要内容，采用极其鲁棒的多种模式匹配。"""
    # 模式 1: Markdown 标题格式
    patterns = [
        r"(?:^|\n)#{1,3}\s*(?:abstract|摘要|📌\s*摘要|🎯\s*摘要).{0,20}\n(.*?)(?=\n#{1,3}\s|\Z)",
        # 模式 2: 中文常见格式 ［摘 要］ xxx 或 [摘  要] xxx 或 摘要：xxx
        r"(?:^|\n)(?:［|\[)\s*摘\s*要\s*(?:］|\])\s*(.*?)(?=\n|$)",
        r"(?:^|\n)摘\s*要\s*[:：]\s*(.*?)(?=\n|$)",
        # 模式 3: 英文常见格式 Abstract: xxx 或 Abstract：xxx
        r"(?:^|\n)Abstract\s*[:：]\s*(.*?)(?=\n|$)",
    ]
    for pattern in patterns:
        m = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
        if m:
            content = m.group(1).strip()
            if content:
                return content
    return ""


# MinerU 偶尔会「解析成功」却吐出空的 full.md（扫描件糊了、PDF 只有图层、页面全是
# 不可提取的矢量图都会这样）。这种正文送进 LLM，模型只能凭标题瞎编，等于往知识库里
# 灌污染数据；走 Batch 时更糟——错误要等几小时后拉结果才暴露。所以在解析出口直接拦掉。
# 200 这个阈值取得很松：真实论文正文普遍 8000 字符以上，实测出问题的那篇是 0 字符，
# 中间隔着两个数量级，不会误伤短文。
MIN_MARKDOWN_CHARS = 200


def extract_key_sections(text: str, max_chars: int = 40000) -> str:
    """
    智能提取论文关键章节（摘要、引言、方法、结果、结论），
    优于直接字符截断，避免丢失论文尾部的结论区。
    若未识别到章节，回退到"头部 2/3 + 尾部 1/3"策略。
    """
    patterns = [
        r"(?:^|\n)#{1,3}\s*(?:abstract|摘要).{0,20}\n(.*?)(?=\n#{1,3}\s|\Z)",
        r"(?:^|\n)#{1,3}\s*(?:introduction|引言|背景|research background).{0,20}\n(.*?)(?=\n#{1,3}\s|\Z)",
        r"(?:^|\n)#{1,3}\s*(?:method|方法|methodology|approach).{0,20}\n(.*?)(?=\n#{1,3}\s|\Z)",
        r"(?:^|\n)#{1,3}\s*(?:result|结果|experiment|实验).{0,20}\n(.*?)(?=\n#{1,3}\s|\Z)",
        r"(?:^|\n)#{1,3}\s*(?:conclusion|结论|discussion|讨论).{0,20}\n(.*?)(?=\n#{1,3}\s|\Z)",
    ]
    extracted = []
    for pattern in patterns:
        m = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
        if m:
            extracted.append(m.group(0).strip())

    if extracted:
        combined = "\n\n".join(extracted)
        return combined[:max_chars]

    # 回退：保留头部 + 尾部（防止截断结论）
    if len(text) <= max_chars:
        return text
    head = max_chars * 2 // 3
    tail = max_chars - head
    return text[:head] + "\n\n[... 中间内容已省略 ...]\n\n" + text[-tail:]


# ── Obsidian 相对附件路径生成 ────────────────────────────────────────────────

def build_note_metadata(title: str, markdown: str = "", language: str = "") -> dict:
    """在笔记导出前判定可可靠识别的文献类型并整理模板元数据。

    专利/标准复用 enrich 的同一识别器；其余类型没有可靠封面格式，继续按论文处理，
    等人工 ``set-type``，避免为了标签正确而污染数据库分类。
    """
    from litreview import doctype as dt
    from litreview.enrich import detect_doc_type, fields_for

    doc_type, info = detect_doc_type(markdown or "")
    doc_type = doc_type or dt.DEFAULT_TYPE
    metadata = fields_for(doc_type, info) if info else {}
    metadata.update({"title": title, "language": language, "doc_type": doc_type})
    return metadata


def generate_obsidian_note(
    paper_meta: dict,
    analysis: dict | Any,
    images: list,
    obsidian_dir: str,
    doc_id: str,
    template_path: Path | None = None,
) -> str:
    """渲染 Obsidian Markdown 笔记，将提取的图片拷贝到 vault 的相对附件子目录下。
    用 '文献ID' 哈希对文件名防覆盖，图片链接采用相对路径跨系统自适应。
    """
    from litreview.analysis_record import PaperAnalysisRecord
    if isinstance(analysis, PaperAnalysisRecord):
        rec = PaperAnalysisRecord.from_llm_json(
            analysis.model_dump() if hasattr(analysis, "model_dump") else analysis.dict(),
            paper_meta=paper_meta,
        )
    else:
        rec = PaperAnalysisRecord.from_llm_json(analysis, paper_meta=paper_meta)
    
    note_path, _ = rec.publish_obsidian_note(
        obsidian_dir=obsidian_dir,
        title=paper_meta.get("title", "Unknown Title"),
        doc_id=doc_id,
        raw_images=images,
        template_path=str(template_path) if template_path else None,
    )
    return note_path


# ── Excel 导出（批量写入，文献ID去重）─────────────────────────────────────────

def export_to_excel(rows: list[dict], excel_path: str) -> None:
    """
    将多行数据批量写入 Excel，并与现有数据合并。
    优先根据"文献ID"去重，保留最新记录，完美保留同标题的多版本论文。
    """
    if not rows:
        return

    # 自动创建父目录存放数据文件
    os.makedirs(os.path.dirname(excel_path), exist_ok=True)

    new_df = pd.DataFrame(rows)
    if os.path.exists(excel_path):
        existing_df = pd.read_excel(excel_path, engine="openpyxl")
        combined = pd.concat([existing_df, new_df], ignore_index=True)
        
        # 优先用唯一主键"文献ID"去重
        if "文献ID" in combined.columns:
            combined = combined.drop_duplicates(subset=["文献ID"], keep="last")
        elif "标题" in combined.columns:
            combined = combined.drop_duplicates(subset=["标题"], keep="last")
            
        combined.to_excel(excel_path, index=False, engine="openpyxl")
    else:
        new_df.to_excel(excel_path, index=False, engine="openpyxl")


def remove_from_excel(doc_ids: list | set, excel_path: str) -> int:
    """从 Excel 汇总表中删除指定文献 ID 对应的行，返回删除的行数。"""
    if not os.path.exists(excel_path) or not doc_ids:
        return 0
    id_set = set(doc_ids)
    try:
        df = pd.read_excel(excel_path, engine="openpyxl")
        if "文献ID" not in df.columns:
            return 0
        initial_len = len(df)
        df = df[~df["文献ID"].isin(id_set)]
        removed_count = initial_len - len(df)
        if removed_count > 0:
            df.to_excel(excel_path, index=False, engine="openpyxl")
        return removed_count
    except Exception as e:
        print(f"[Warning] 更新 Excel 异常: {e}")
        return 0


def sync_excel_with_db(db_path: str, excel_path: str) -> int:
    """按数据库中实际 EXPORTED 状态的文献全量重建/重对齐 Excel 总表，清除历史孤儿行。"""
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute("""
        SELECT p.id, p.title, COALESCE(d.authors, ''), COALESCE(d.year, ''),
               COALESCE(d.journal, ''), p.pdf_path, p.language, p.status,
               p.batch_provider, p.batch_job_id, p.processed_at, p.result_json,
               COALESCE(d.doc_type, 'paper')
        FROM papers p
        LEFT JOIN paper_details d ON d.doc_id = p.id
        WHERE p.status = 'EXPORTED'
    """)
    rows = c.fetchall()
    conn.close()

    excel_rows = []
    for (doc_id, title, authors, year, journal, pdf_path, lang, status,
         provider, job_id, processed_at, result_json, doc_type) in rows:
        tldr = ""
        fertilizer_implications = ""
        keywords = ""
        try:
            if result_json:
                data = json.loads(result_json)
                tldr = data.get("tldr", "")
                fertilizer_implications = data.get("fertilizer_implications", "")
                keywords = data.get("keywords", "")
        except Exception:
            pass
        safe = "".join(c for c in title if c.isalnum() or c in " -_").strip() or "Untitled"
        note_name = f"{safe}_{doc_id[:8]}"
        excel_rows.append({
            "文献ID": doc_id,
            "标题": title,
            "关键词": keywords,
            "肥料开发借鉴": fertilizer_implications,
            "TLDR": tldr,
            "作者": authors,
            "年份": year,
            "期刊": journal,
            "文件路径": pdf_path,
            "Obsidian链接": f"[[{note_name}]]",
            "语言": lang or "zh",
            "状态": status,
            "服务商": provider or "",
            "批量任务ID": job_id or "",
            "解析时间": processed_at or "",
        })
    os.makedirs(os.path.dirname(excel_path), exist_ok=True)
    df = pd.DataFrame(excel_rows)
    df.to_excel(excel_path, index=False, engine="openpyxl")
    return len(excel_rows)


# ── 运行历史日志记录 ─────────────────────────────────────────────────────────

def _data_dir() -> str:
    """运行日志/历史的落盘目录，跟 DB_PATH 同一个根，不单独硬编码相对路径
    （否则裸跑 CLI 时会散落到 CWD 而不是 DB_PATH 实际所在的数据盘）。
    """
    return os.path.dirname(os.getenv("DB_PATH", "./data/batch_tracking.db")) or "."


def log_run_event(mode: str, event: str, title: str = "", doc_id: str = "", status: str = "success", error: str = "", extra: dict = None) -> None:
    """在 <DB_PATH 所在目录>/pipeline_history.jsonl 中追加一条事件记录，方便 cli 查询运行历史与统计。
    """
    history_file = os.path.join(_data_dir(), "pipeline_history.jsonl")
    os.makedirs(os.path.dirname(history_file), exist_ok=True)
    
    log_entry = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "mode": mode,
        "event": event,
        "title": title,
        "doc_id": doc_id,
        "status": status,
        "error": error,
    }
    if extra:
        log_entry.update(extra)
        
    try:
        with open(history_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[Warning] 写入运行历史日志失败: {e}", file=sys.stderr)


# ── 运行日志本地重定向 (TeeLogger) ─────────────────────────────────────────────

class TeeLogger:
    """把 stdout/stderr 原样转发到终端，同时写一份 <log_dir>/<prefix>-YYYY-MM-DD.log。

    日期必须在**每次写入时**现算，不能在 __init__ 里定死：daemon 是常驻进程，一跑
    就是好几天，定死的话整整一周的日志会全堆进它启动那天的文件里，等于没分天。
    """

    def __init__(self, log_dir, original_stream, prefix="app", retention_days=30):
        self.terminal = original_stream
        self.log_dir = log_dir
        self.prefix = prefix
        self.retention_days = retention_days
        self._day = None        # 当前打开的是哪一天的文件
        self._fh = None
        try:
            os.makedirs(log_dir, exist_ok=True)
        except Exception:
            pass

    def current_path(self) -> str:
        return os.path.join(self.log_dir, f"{self.prefix}-{datetime.now():%Y-%m-%d}.log")

    def _handle(self):
        """返回今天那份文件的句柄；跨天时自动换新文件并清理过期日志。"""
        day = datetime.now().strftime("%Y-%m-%d")
        if day != self._day or self._fh is None:
            if self._fh is not None:
                try:
                    self._fh.close()
                except Exception:
                    pass
                self._fh = None
            # 追加模式：daemon 与手敲的 CLI 会同时写同一个文件，靠 O_APPEND 保证互不截断
            self._fh = open(self.current_path(), "a", encoding="utf-8")
            self._day = day
            self._prune()
        return self._fh

    def _prune(self):
        """删掉超过保留期的日志。跨天换文件时顺带做一次，不另起线程/定时器。"""
        if self.retention_days <= 0:      # 配 0 或负数 = 永久保留
            return
        cutoff = time.time() - self.retention_days * 86400
        try:
            for name in os.listdir(self.log_dir):
                if not (name.startswith(f"{self.prefix}-") and name.endswith(".log")):
                    continue
                path = os.path.join(self.log_dir, name)
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
        except Exception:
            pass

    def write(self, message):
        try:
            self.terminal.write(message)
        except UnicodeEncodeError:
            # Windows 的非 UTF-8 控制台（常见为 GBK）无法表示 emoji。终端显示时仅
            # 替换不可编码字符，避免整条 CLI/daemon 崩溃；UTF-8 日志仍写入原文。
            encoding = getattr(self.terminal, "encoding", None) or "ascii"
            safe_message = message.encode(encoding, errors="replace").decode(encoding)
            self.terminal.write(safe_message)
        try:
            fh = self._handle()
            fh.write(message)
            # 常驻进程不显式 flush 的话，tail 出来的日志会滞后好几个钟头
            fh.flush()
        except Exception:
            pass

    def flush(self):
        try:
            self.terminal.flush()
        except Exception:
            pass
        if self._fh is not None:
            try:
                self._fh.flush()
            except Exception:
                pass

    def isatty(self):
        return hasattr(self.terminal, "isatty") and self.terminal.isatty()


def _log_retention_days() -> int:
    """坏值一律退回默认 30 天：这段在 import 期执行，抛异常会让整个项目起不来。"""
    try:
        return int(os.getenv("LOG_RETENTION_DAYS", "30"))
    except (TypeError, ValueError):
        return 30


# 自动重定向 stdout 和 stderr 到 <DB_PATH 所在目录>/app-YYYY-MM-DD.log（按天一份）
_retention = _log_retention_days()
sys.stdout = TeeLogger(_data_dir(), sys.stdout, retention_days=_retention)
sys.stderr = TeeLogger(_data_dir(), sys.stderr, retention_days=_retention)
