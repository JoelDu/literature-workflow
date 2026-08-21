"""Literature Review Domain Facade & Unified Job Registry (Phase C Architecture Deepening).

Provides a single, first-class Python interface for all domain operations across:
- CLI terminal commands (`review.py` / `cli.py`)
- MCP Server tools (`mcp_server.py`)
- Web REST APIs (`web_server.py`)
"""
import os
import time
import queue
import logging
import threading
from typing import Dict, List, Any, Optional, Callable

from litreview.library_store import (
    get_dashboard_stratified_stats,
    list_library_documents,
    get_document_full_detail,
    list_discovery_candidates_paginated,
    enrich_document_metadata
)
from litreview.citation_network import (
    query_citation_network,
    get_full_citation_graph,
    get_seed_subgraph
)
from litreview.ingestion_engine import (
    DocumentIngestionEngine,
    OpenAccessCandidateStrategy,
    verify_document_file,
    IngestionResult
)

logger = logging.getLogger("literature_analyzer.facade")


class JobRegistry:
    """进程内统一异步任务注册与生命周期管理器。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._jobs: Dict[str, Dict[str, Any]] = {}

    def create_job(self, job_type: str, title: str, meta: Optional[Dict[str, Any]] = None) -> str:
        job_id = f"job_{job_type[:4]}_{int(time.time())}_{os.urandom(3).hex()}"
        with self._lock:
            self._jobs[job_id] = {
                "job_id": job_id,
                "type": job_type,
                "title": title,
                "status": "RUNNING",
                "progress": 0,
                "current_step": "初始化任务...",
                "logs": [],
                "meta": meta or {},
                "created_at": time.time(),
                "updated_at": time.time(),
                "result": None,
                "error": None
            }
        return job_id

    def update_job(
        self,
        job_id: str,
        progress: Optional[int] = None,
        step: Optional[str] = None,
        log: Optional[str] = None,
        status: Optional[str] = None,
        result: Optional[Any] = None,
        error: Optional[str] = None
    ):
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            if progress is not None:
                job["progress"] = max(0, min(100, progress))
            if step is not None:
                job["current_step"] = step
            if log is not None:
                job["logs"].append(f"[{time.strftime('%H:%M:%S')}] {log}")
            if status is not None:
                job["status"] = status
            if result is not None:
                job["result"] = result
            if error is not None:
                job["error"] = error
            job["updated_at"] = time.time()

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            job = self._jobs.get(job_id)
            return dict(job) if job else None

    def list_jobs(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            sorted_jobs = sorted(self._jobs.values(), key=lambda j: j["created_at"], reverse=True)
            return [dict(j) for j in sorted_jobs[:limit]]


class LiteratureReviewFacade:
    """统一领域外观 (Unified Domain Facade)。"""

    def __init__(
        self,
        prod_db_path: Optional[str] = None,
        test_citation_db_path: Optional[str] = None,
        cache_db_path: Optional[str] = None,
        staging_dir: Optional[str] = None
    ):
        self.prod_db_path = prod_db_path or os.getenv(
            "DB_PATH",
            "/mnt/ripe/literature_analyzer_data/batch_tracking.db"
        )
        base_dir = os.path.dirname(os.path.abspath(__file__))
        self.test_citation_db_path = test_citation_db_path or os.getenv(
            "TEST_CITATION_DB_PATH",
            os.path.join(base_dir, "..", "data", "test_citation_network.db")
        )
        self.cache_db_path = cache_db_path or os.getenv(
            "EXTERNAL_DISCOVERY_CACHE_DB_PATH",
            os.path.join(base_dir, "..", "data", "external_discovery_cache.db")
        )
        self.staging_dir = staging_dir or os.getenv(
            "STAGING_PDF_DIR",
            os.path.join(base_dir, "..", "data", "staging_pdf")
        )
        self.jobs = JobRegistry()

    # ── 仪表盘与馆藏概况 ──

    def get_dashboard_stats(self) -> Dict[str, Any]:
        """获取全局 5 级闭合分区统计指标。"""
        return get_dashboard_stratified_stats(
            self.prod_db_path,
            self.test_citation_db_path,
            self.cache_db_path
        )

    def list_documents(
        self,
        page: int = 1,
        page_size: int = 20,
        query: str = "",
        doc_type: str = "all",
        indexed: str = "all"
    ) -> Dict[str, Any]:
        """多维分页查询馆藏文献目录。"""
        return list_library_documents(
            self.prod_db_path,
            self.test_citation_db_path,
            page=page,
            page_size=page_size,
            query=query,
            doc_type=doc_type,
            indexed=indexed
        )

    def get_document_detail(self, doc_id: str) -> Dict[str, Any]:
        """获取单篇文献完整详情。"""
        return get_document_full_detail(
            doc_id,
            self.prod_db_path,
            self.test_citation_db_path
        )

    def enrich_document(self, doc_id: str) -> Dict[str, Any]:
        """通过 OpenAlex & CrossRef 权威学术图谱反查对齐并深度补全单篇文献元数据。"""
        return enrich_document_metadata(
            doc_id,
            self.prod_db_path,
            self.test_citation_db_path,
            self.cache_db_path
        )

    # ── 引文网络图谱 ──

    def get_citation_graph(self, include_unresolved: bool = False, limit: int = 10000) -> Dict[str, Any]:
        """获取引文网络图谱数据 (默认骨干模式以保证 60 FPS 流畅交互)。"""
        return get_full_citation_graph(
            self.test_citation_db_path,
            include_unresolved=include_unresolved,
            limit=limit
        )

    def get_document_citation_network(self, doc_id: str, include_candidates: bool = True) -> Dict[str, Any]:
        """获取单篇文献的 1 阶出入向引文拓扑。"""
        return query_citation_network(
            self.test_citation_db_path,
            doc_id,
            include_candidates=include_candidates
        )

    def get_document_subgraph(self, doc_id: str) -> Dict[str, Any]:
        """获取单篇文献的力导向聚焦子图。"""
        return get_seed_subgraph(self.test_citation_db_path, doc_id)

    # ── 滚雪球发现与候选池 ──

    def list_candidates(
        self,
        page: int = 1,
        page_size: int = 20,
        query: str = "",
        track: str = "ALL",
        is_oa: Optional[bool] = None,
        status: str = "ALL"
    ) -> Dict[str, Any]:
        """分页查询发现候选池文献。"""
        return list_discovery_candidates_paginated(
            self.cache_db_path,
            page=page,
            page_size=page_size,
            query=query,
            track=track,
            is_oa=is_oa,
            status=status
        )

    def approve_candidates(
        self,
        candidate_ids: List[str],
        reason: Optional[str] = None,
        operator: str = "agent_operator"
    ) -> Dict[str, Any]:
        """批量审批通过候选文献 (进入 APPROVED 状态并记录审计账本)。"""
        from litreview.pipeline import approve_candidates_batch
        return approve_candidates_batch(
            self.cache_db_path,
            candidate_ids,
            decision_source="USER",
            reason=reason or "批量人工核准入库",
            operator=operator
        )

    # ── 异步长耗时流水线 ──

    def submit_ingest_candidate_job(self, candidate_id: str) -> str:
        """提交单个 OA 候选文献的端到端入库异步任务。"""
        job_id = self.jobs.create_job(
            job_type="ingest",
            title=f"入库候选文献 {candidate_id[:8]}",
            meta={"candidate_id": candidate_id}
        )

        def _worker():
            try:
                self.jobs.update_job(job_id, progress=10, step="创建入库策略与下载原文...", log=f"开始下载 {candidate_id}")
                strategy = OpenAccessCandidateStrategy(
                    candidate_id=candidate_id,
                    staging_dir=self.staging_dir,
                    cache_db_path=self.cache_db_path,
                    test_citation_db_path=self.test_citation_db_path
                )
                engine = DocumentIngestionEngine(strategy)

                self.jobs.update_job(job_id, progress=30, step="执行 5 步入库流水线...", log="正在执行物理验真与正文切块...")
                result = engine.execute(candidate_id)

                if result.success:
                    self.jobs.update_job(
                        job_id, progress=100, step="入库完成",
                        status="COMPLETED", log="文献已成功入库并回填引文网络！",
                        result=result.metadata
                    )
                else:
                    self.jobs.update_job(
                        job_id, progress=100, step="入库失败",
                        status="FAILED", error=result.error,
                        log=f"入库失败: {result.error}"
                    )
            except Exception as e:
                logger.error(f"Ingest worker exception: {e}", exc_info=True)
                self.jobs.update_job(job_id, status="FAILED", error=str(e), log=f"执行异常: {e}")

        threading.Thread(target=_worker, daemon=True).start()
        return job_id

    def get_job_status(self, job_id: str) -> Optional[Dict[str, Any]]:
        """查询任务状态。"""
        return self.jobs.get_job(job_id)


_default_facade: Optional[LiteratureReviewFacade] = None
_facade_lock = threading.Lock()


def get_facade() -> LiteratureReviewFacade:
    """获取单例领域外观实例。"""
    global _default_facade
    if _default_facade is None:
        with _facade_lock:
            if _default_facade is None:
                _default_facade = LiteratureReviewFacade()
    return _default_facade
