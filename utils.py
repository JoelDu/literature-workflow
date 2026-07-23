"""utils.py — 共享工具函数
供 pipeline.py 和 batch_pipeline.py 共同使用，消除代码重复。
"""
import os
import re
import sys
import json
import hashlib
import shutil
from pathlib import Path
from datetime import datetime

import pandas as pd
from jinja2 import Template
from pydantic import BaseModel, Field, ValidationError


# ── 配置模型与启动强校验 (Pydantic) ──────────────────────────────────────────

class Settings(BaseModel):
    MINERU_API_KEY: str = Field(..., min_length=1, description="MinerU API Key is required")
    DEEPSEEK_API_KEY: str = Field(..., min_length=1, description="DeepSeek API Key is required")
    GEMINI_API_KEY: str = Field(..., min_length=1, description="Gemini API Key is required")
    
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
    RERANK_MODEL: str = "Qwen/Qwen3-Reranker-8B"
    RERANK_ENABLE: bool = True
    RERANK_CANDIDATES: int = 50              # 向量召回候选数（rerank 后取 REVIEW_TOP_K）
    REVIEW_MODEL: str = "deepseek-ai/DeepSeek-V4-Pro"   # 综述写作模型（SiliconFlow）
    REVIEW_ENRICH_LLM: bool = True           # enrich 时是否用 LLM 补缺元数据
    REVIEW_CHUNK_SIZE: int = 1000
    REVIEW_CHUNK_OVERLAP: int = 150
    # 教材/书籍单独用更大的块（书讲得散，大块保完整概念）。字符按原始 len 计，
    # 4800 原始字符≈3000 汉字（实测中文 1 汉字≈1.6 原始字符）；英文同口径。
    BOOK_CHUNK_SIZE: int = 4800
    BOOK_CHUNK_OVERLAP: int = 600
    REVIEW_TOP_K: int = 24
    REVIEW_MAX_CHUNKS_PER_DOC: int = 4
    REVIEW_MIN_SCORE: int = 6
    REVIEW_EVIDENCE_N: int = 10
    REVIEW_MAP_CONCURRENCY: int = 8
    REVIEW_OUTPUT_DIR: str = "./obsidian_vault/reviews"
    REVIEW_AUTO_INDEX: bool = True           # 导出后自动增量更新向量索引
    BOOK_SPLIT_PAGES: int = 180              # 教材入库时每份 PDF 最大页数（≤ MinerU 单任务上限）
    REVIEW_INSERT_FIGURES: bool = True       # 综述是否自动插入相关论文图表
    REVIEW_FIGURES_PER_SECTION: int = 2      # 每章节最多插图数
    REVIEW_FIGURE_MIN_SCORE: float = 0.2     # 图题 rerank 相关度阈值（低于不插）


_settings = None

def get_settings() -> Settings:
    """获取强校验后的全局配置（单例模式）。
    一旦发现 Key 缺失或格式不对，前置报错并优雅退出，适合长时守护进程。
    """
    global _settings
    if _settings is None:
        try:
            config_dict = {
                "MINERU_API_KEY": os.getenv("MINERU_API_KEY", ""),
                "DEEPSEEK_API_KEY": os.getenv("DEEPSEEK_API_KEY", ""),
                "GEMINI_API_KEY": os.getenv("GEMINI_API_KEY", ""),
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
                "RERANK_CANDIDATES": int(os.getenv("RERANK_CANDIDATES", "50")),
                "REVIEW_MODEL": os.getenv("REVIEW_MODEL", "deepseek-ai/DeepSeek-V4-Pro"),
                "REVIEW_ENRICH_LLM": os.getenv("REVIEW_ENRICH_LLM", "true").lower() == "true",
                "REVIEW_CHUNK_SIZE": int(os.getenv("REVIEW_CHUNK_SIZE", "1000")),
                "REVIEW_CHUNK_OVERLAP": int(os.getenv("REVIEW_CHUNK_OVERLAP", "150")),
                "BOOK_CHUNK_SIZE": int(os.getenv("BOOK_CHUNK_SIZE", "4800")),
                "BOOK_CHUNK_OVERLAP": int(os.getenv("BOOK_CHUNK_OVERLAP", "600")),
                "REVIEW_TOP_K": int(os.getenv("REVIEW_TOP_K", "24")),
                "REVIEW_MAX_CHUNKS_PER_DOC": int(os.getenv("REVIEW_MAX_CHUNKS_PER_DOC", "4")),
                "REVIEW_MIN_SCORE": int(os.getenv("REVIEW_MIN_SCORE", "6")),
                "REVIEW_EVIDENCE_N": int(os.getenv("REVIEW_EVIDENCE_N", "10")),
                "REVIEW_MAP_CONCURRENCY": int(os.getenv("REVIEW_MAP_CONCURRENCY", "8")),
                "REVIEW_OUTPUT_DIR": os.getenv("REVIEW_OUTPUT_DIR", "./obsidian_vault/reviews"),
                "REVIEW_AUTO_INDEX": os.getenv("REVIEW_AUTO_INDEX", "true").lower() == "true",
                "BOOK_SPLIT_PAGES": int(os.getenv("BOOK_SPLIT_PAGES", "180")),
                "REVIEW_INSERT_FIGURES": os.getenv("REVIEW_INSERT_FIGURES", "true").lower() == "true",
                "REVIEW_FIGURES_PER_SECTION": int(os.getenv("REVIEW_FIGURES_PER_SECTION", "2")),
                "REVIEW_FIGURE_MIN_SCORE": float(os.getenv("REVIEW_FIGURE_MIN_SCORE", "0.2")),
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

def generate_obsidian_note(
    paper_meta: dict,
    analysis: dict,
    images: list,
    obsidian_dir: str,
    doc_id: str,
    template_path: Path | None = None,
) -> str:
    """渲染 Obsidian Markdown 笔记，将提取的图片拷贝到 vault 的相对附件子目录下。
    用 '文献ID' 哈希对文件名防覆盖，图片链接采用相对路径跨系统自适应。
    """
    if template_path is None:
        template_path = get_template_path()

    with open(template_path, "r", encoding="utf-8") as f:
        template = Template(f.read())

    # 1. 建立相对附件目录 obsidian_vault/attachments/<doc_id>/
    attachments_rel_path = os.path.join("attachments", doc_id)
    attachments_dir = os.path.join(obsidian_dir, attachments_rel_path)
    os.makedirs(attachments_dir, exist_ok=True)

    # 2. 拷贝图片并转换为相对路径链接
    relative_image_links = []
    for img_path in images:
        if os.path.exists(img_path):
            img_name = os.path.basename(img_path)
            dest_path = os.path.join(attachments_dir, img_name)
            try:
                shutil.copy2(img_path, dest_path)
                # 使用相对路径，如 "attachments/a1b2c3d4/figure.png" 写入笔记
                relative_link = f"attachments/{doc_id}/{img_name}".replace("\\", "/")
                relative_image_links.append(relative_link)
            except Exception as e:
                print(f"[yellow]拷贝图片到附件目录失败 {img_path}: {e}")
        else:
            print(f"[yellow]未找到源图片文件: {img_path}")

    note_content = template.render(
        language=analysis.get("language", paper_meta.get("language", "en")),
        current_date=datetime.now().strftime("%Y-%m-%d"),
        title=paper_meta.get("title", "Unknown Title"),
        authors=analysis.get("authors") or paper_meta.get("authors") or "未提取",
        year=analysis.get("year") or datetime.now().strftime("%Y"),
        journal=analysis.get("journal") or "未提取",
        tldr=analysis.get("tldr", ""),
        abstract=analysis.get("abstract", "") or paper_meta.get("abstract", ""),
        background=analysis.get("background", ""),
        methods=analysis.get("methods", ""),
        results=analysis.get("results", ""),
        conclusion=analysis.get("conclusion", ""),
        images=relative_image_links,
    )

    raw_title = paper_meta.get("title", "Untitled")
    safe_title = "".join(c for c in raw_title if c.isalnum() or c in " -_").strip()
    if not safe_title:
        safe_title = "Untitled_Paper"

    # 文件名附带哈希前 8 位以保证唯一，支持同名论文共存
    note_filename = f"{safe_title}_{doc_id[:8]}.md"
    note_path = os.path.join(obsidian_dir, note_filename)
    
    with open(note_path, "w", encoding="utf-8") as f:
        f.write(note_content)
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


# ── 运行历史日志记录 ─────────────────────────────────────────────────────────

def log_run_event(mode: str, event: str, title: str = "", doc_id: str = "", status: str = "success", error: str = "", extra: dict = None) -> None:
    """在 data/pipeline_history.jsonl 中追加一条事件记录，方便 cli 查询运行历史与统计。
    """
    history_file = "./data/pipeline_history.jsonl"
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
    def __init__(self, filepath, original_stream):
        self.terminal = original_stream
        self.filepath = filepath
        try:
            os.makedirs(os.path.dirname(filepath), exist_ok=True)
        except Exception:
            pass

    def write(self, message):
        self.terminal.write(message)
        try:
            with open(self.filepath, "a", encoding="utf-8") as f:
                f.write(message)
        except Exception:
            pass

    def flush(self):
        try:
            self.terminal.flush()
        except Exception:
            pass

    def isatty(self):
        return hasattr(self.terminal, "isatty") and self.terminal.isatty()

# 自动重定向 stdout 和 stderr 到 data/app.log
sys.stdout = TeeLogger("./data/app.log", sys.stdout)
sys.stderr = TeeLogger("./data/app.log", sys.stderr)


