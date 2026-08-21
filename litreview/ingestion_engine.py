"""Unified Document Ingestion Engine & Strategies (Phase B Architecture Deepening).

Standardizes the universal 5-stage ingestion lifecycle:
  [1. Acquire/Scan] -> [2. Verify (Magic + Size + SHA256)] -> [3. Parse (MinerU / PyPDF / Pandoc)] -> [4. Vectorize (Qwen3 Embeddings)] -> [5. Persist (DB, Vault, Excel, Graph)]
"""
import os
import io
import re
import json
import time
import shutil
import hashlib
import logging
import sqlite3
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, List, Any, Optional, Tuple, Set

import pypdf
import numpy as np

from litreview.chunker import split_markdown
from litreview.ref_parser import parse_reference_mention, normalize_title, extract_first_author_surname

logger = logging.getLogger("literature_analyzer.ingestion_engine")


@dataclass
class VerificationResult:
    is_valid: bool
    sha256: str
    file_size: int
    page_count: int
    magic_valid: bool
    error_reason: Optional[str] = None


@dataclass
class IngestionResult:
    success: bool
    doc_id: str
    stage: str
    metadata: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None


def verify_document_file(
    file_path: str,
    min_size: int = 1024,
    max_size: int = 300 * 1024 * 1024
) -> VerificationResult:
    """通用物理文件验真与哈希计算 (魔数校验、大小校验、PDF页数解析、SHA-256计算)。"""
    if not os.path.exists(file_path):
        return VerificationResult(
            is_valid=False, sha256="", file_size=0, page_count=0, magic_valid=False,
            error_reason=f"文件不存在: {file_path}"
        )

    file_size = os.path.getsize(file_path)
    if file_size < min_size:
        return VerificationResult(
            is_valid=False, sha256="", file_size=file_size, page_count=0, magic_valid=False,
            error_reason=f"文件过小 ({file_size} 字节 < {min_size} 字节)，可能是截断或空文件"
        )
    if file_size > max_size:
        return VerificationResult(
            is_valid=False, sha256="", file_size=file_size, page_count=0, magic_valid=False,
            error_reason=f"文件超出大小限制 ({file_size} 字节 > {max_size} 字节)"
        )

    # SHA-256 计算
    hasher = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            hasher.update(chunk)
    sha256_hash = hasher.hexdigest()

    # 魔数校验
    ext = os.path.splitext(file_path)[1].lower()
    magic_valid = False
    page_count = 1

    with open(file_path, "rb") as f:
        header = f.read(16)

    if ext == ".pdf":
        if header.startswith(b"%PDF-"):
            magic_valid = True
            try:
                reader = pypdf.PdfReader(file_path)
                page_count = len(reader.pages)
                if page_count == 0:
                    return VerificationResult(
                        is_valid=False, sha256=sha256_hash, file_size=file_size,
                        page_count=0, magic_valid=True, error_reason="PDF 页数为 0"
                    )
            except Exception as e:
                return VerificationResult(
                    is_valid=False, sha256=sha256_hash, file_size=file_size,
                    page_count=0, magic_valid=False, error_reason=f"PDF 结构损坏无法解析: {e}"
                )
        else:
            return VerificationResult(
                is_valid=False, sha256=sha256_hash, file_size=file_size,
                page_count=0, magic_valid=False, error_reason="文件头缺少 %PDF- 标志，非合法 PDF"
            )
    elif ext == ".epub":
        if header.startswith(b"PK\x03\x04"):
            magic_valid = True
            page_count = 1
        else:
            return VerificationResult(
                is_valid=False, sha256=sha256_hash, file_size=file_size,
                page_count=0, magic_valid=False, error_reason="文件头缺少 ZIP 标志，非合法 EPUB"
            )
    else:
        # 其他类型宽松放行
        magic_valid = True

    return VerificationResult(
        is_valid=True,
        sha256=sha256_hash,
        file_size=file_size,
        page_count=page_count,
        magic_valid=magic_valid
    )


class IngestionStrategy(ABC):
    """入库策略抽象基类。"""

    @abstractmethod
    def acquire(self, source: Any) -> str:
        """获取或定位物理文献文件。"""
        pass

    @abstractmethod
    def verify(self, file_path: str) -> VerificationResult:
        """验真与哈希计算。"""
        pass

    @abstractmethod
    def parse(self, file_path: str, verification: VerificationResult) -> Dict[str, Any]:
        """正文解析与切块。"""
        pass

    @abstractmethod
    def vectorize(self, parsed_data: Dict[str, Any]) -> Dict[str, Any]:
        """文本向量化。"""
        pass

    @abstractmethod
    def persist(self, file_path: str, parsed_data: Dict[str, Any], vectorized_data: Dict[str, Any]) -> IngestionResult:
        """持久化至数据库、图谱与知识库。"""
        pass


class OpenAccessCandidateStrategy(IngestionStrategy):
    """外部 OA 候选文献入库策略。"""

    def __init__(self, candidate_id: str, staging_dir: str, cache_db_path: str, test_citation_db_path: str):
        self.candidate_id = candidate_id
        self.staging_dir = staging_dir
        self.cache_db_path = cache_db_path
        self.test_citation_db_path = test_citation_db_path
        self.candidate_info: Dict[str, Any] = {}

    def acquire(self, source: Any) -> str:
        """从 OpenAlex OA 链接下载 PDF 原文至暂存区。"""
        conn = sqlite3.connect(f"file:{self.cache_db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM discovered_candidates WHERE candidate_id = ?", (self.candidate_id,)).fetchone()
        conn.close()

        if not row:
            raise ValueError(f"候选文献不存在: {self.candidate_id}")

        self.candidate_info = dict(row)
        target_url = self.candidate_info.get("oa_url") or self.candidate_info.get("landing_page_url")
        if not target_url:
            raise ValueError(f"候选文献缺少有效下载 URL: {self.candidate_id}")

        os.makedirs(self.staging_dir, exist_ok=True)
        local_path = os.path.join(self.staging_dir, f"{self.candidate_id}.pdf")

        if not os.path.exists(local_path):
            headers = {"User-Agent": "LiteratureAnalyzer/2.5 (research workstation; mailto:contact@example.com)"}
            req = urllib.request.Request(target_url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as resp:
                with open(local_path, "wb") as out_f:
                    shutil.copyfileobj(resp, out_f)

        return local_path

    def verify(self, file_path: str) -> VerificationResult:
        """魔数与哈希校验。"""
        return verify_document_file(file_path)

    def parse(self, file_path: str, verification: VerificationResult) -> Dict[str, Any]:
        """使用 PyPDF 提取正文并切块。"""
        reader = pypdf.PdfReader(file_path)
        full_text = ""
        for page in reader.pages:
            full_text += (page.extract_text() or "") + "\n\n"

        chunks = split_markdown(full_text, chunk_size=1000, overlap=100)
        return {
            "candidate_id": self.candidate_id,
            "verification": verification,
            "full_text": full_text,
            "chunks": chunks,
            "candidate_info": self.candidate_info
        }

    def vectorize(self, parsed_data: Dict[str, Any]) -> Dict[str, Any]:
        """调用 Qwen3 密集向量模型生成嵌入。"""
        from litreview.pipeline import get_qwen3_embeddings
        chunks = parsed_data["chunks"]
        if not chunks:
            return {"embeddings": []}

        texts = [c["content"] for c in chunks]
        embeddings = get_qwen3_embeddings(texts)
        return {"embeddings": embeddings}

    def persist(self, file_path: str, parsed_data: Dict[str, Any], vectorized_data: Dict[str, Any]) -> IngestionResult:
        """入库至 test_citation_network.db 并将候选状态推进至 VECTOR_INDEXED。"""
        from litreview.pipeline import ingest_acquired_candidate_to_graph
        res = ingest_acquired_candidate_to_graph(
            self.test_citation_db_path,
            self.cache_db_path,
            self.candidate_id,
            file_path,
            parsed_data["verification"].sha256,
            parsed_data["verification"].file_size,
            parsed_data["verification"].page_count,
            parsed_data["chunks"],
            vectorized_data["embeddings"]
        )
        return IngestionResult(
            success="error" not in res,
            doc_id=res.get("work_id", self.candidate_id),
            stage="VECTOR_INDEXED",
            metadata=res,
            error=res.get("error")
        )


class DocumentIngestionEngine:
    """统一文献入库调度引擎 (Universal Ingestion Orchestrator)。"""

    def __init__(self, strategy: IngestionStrategy):
        self.strategy = strategy

    def execute(self, source: Any) -> IngestionResult:
        """执行标准 5 步入库流水线。"""
        try:
            # 1. Acquire
            local_path = self.strategy.acquire(source)
            logger.info(f"[IngestionEngine] Acquired local file: {local_path}")

            # 2. Verify
            verification = self.strategy.verify(local_path)
            if not verification.is_valid:
                logger.error(f"[IngestionEngine] Verification failed: {verification.error_reason}")
                return IngestionResult(
                    success=False,
                    doc_id=verification.sha256 or "",
                    stage="VERIFY",
                    error=verification.error_reason
                )

            # 3. Parse
            parsed_data = self.strategy.parse(local_path, verification)
            logger.info(f"[IngestionEngine] Parsed {len(parsed_data.get('chunks', []))} chunks")

            # 4. Vectorize
            vectorized_data = self.strategy.vectorize(parsed_data)
            logger.info(f"[IngestionEngine] Vectorized {len(vectorized_data.get('embeddings', []))} embeddings")

            # 5. Persist
            result = self.strategy.persist(local_path, parsed_data, vectorized_data)
            logger.info(f"[IngestionEngine] Persist completed. Success: {result.success}")
            return result

        except Exception as e:
            logger.error(f"[IngestionEngine] Execution failed: {e}", exc_info=True)
            return IngestionResult(
                success=False,
                doc_id="",
                stage="EXECUTION_ERROR",
                error=str(e)
            )
