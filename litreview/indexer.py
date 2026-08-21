"""增量向量索引编排（深模块设计）：封装 Markdown 切块、SiliconFlow 4096维嵌入、SQLite 事务落库与并发加速。"""
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn

from .chunker import split_markdown, chunk_params_for
from .store import VectorStore, md_hash
from .embedder import Embedder


def _get_default_embed_client():
    """内部自洽构造 SiliconFlow 嵌入客户端，隔离外部调用方依赖。"""
    from openai import OpenAI
    sf_key = os.getenv("SILICONFLOW_API_KEY")
    sf_base = os.getenv("SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1")
    if not sf_key:
        raise ValueError("未配置 SILICONFLOW_API_KEY，无法进行 4096 维 Qwen3 向量化。")
    return OpenAI(api_key=sf_key, base_url=sf_base)


class LiteratureIndexer:
    """文献向量索引深模块：隐藏分块、批处理调度、重试网络交互与 SQLite 事务细节。"""

    def __init__(self, settings=None, console=None, client=None):
        if settings is None:
            from utils import get_settings
            settings = get_settings()
        self.settings = settings
        self.console = console or Console()
        self.client = client
        self.store = VectorStore(self.settings.DB_PATH, self.settings.EMBEDDING_DIM)

    def _ensure_embedder(self) -> Embedder:
        if not self.client:
            self.client = _get_default_embed_client()
        return Embedder(self.client, self.settings.EMBEDDING_MODEL, self.settings.EMBEDDING_DIM,
                        self.settings.EMBEDDING_BATCH_SIZE)

    def index_single(self, doc_id: str, md: str, doc_type: str, embedder: Embedder) -> tuple[int, bool, str]:
        """索引单篇文献（单篇事务隔离）：返回 (n_chunks, success, error_msg)。"""
        try:
            csize, coverlap = chunk_params_for(
                doc_type, self.settings.REVIEW_CHUNK_SIZE, self.settings.REVIEW_CHUNK_OVERLAP,
                self.settings.BOOK_CHUNK_SIZE, self.settings.BOOK_CHUNK_OVERLAP)
            chunks = split_markdown(doc_id, md, csize, coverlap)
            if not chunks:
                self.store.replace_doc_chunks(doc_id, md_hash(md), [])
                self.store.mark_embedded(doc_id)
                return 0, True, ""
            chunk_ids = self.store.replace_doc_chunks(doc_id, md_hash(md), chunks)
            vecs = embedder.embed_texts([c.content for c in chunks])
            self.store.save_embeddings(chunk_ids, vecs, self.settings.EMBEDDING_MODEL)
            self.store.mark_embedded(doc_id)
            return len(chunks), True, ""
        except Exception as e:
            return 0, False, str(e)

    def run(self, doc_ids: list = None, force: bool = False, workers: int = 4) -> dict:
        """主入口：支持全库扫差分、指定文档直建，以及多线程并发加速。"""
        if force:
            conn = self.store._conn()
            conn.execute("DELETE FROM review_index_meta")
            conn.execute("DELETE FROM review_chunks")
            conn.commit()
            conn.close()
            self.store._matrix = None

        if getattr(self.settings, "REVIEW_EMBED_BACKEND", "remote") == "local":
            todo = self.store.docs_needing_index(doc_ids=doc_ids)
            self.console.print(f"[dim]嵌入后端=local：{len(todo)} 篇待索引留给夜间任务处理，此处跳过。"
                               + ("（--force 已清空索引，全部待重建）" if force else ""))
            return {"total": len(todo), "indexed": 0, "failed": 0, "chunks": 0, "deferred": len(todo)}

        embedder = self._ensure_embedder()
        todo = self.store.docs_needing_index(doc_ids=doc_ids)
        report = {"total": len(todo), "indexed": 0, "failed": 0, "chunks": 0}

        if not todo:
            self.console.print("[green]✔ 向量索引已是最新，0 篇待索引。")
            return report

        self.console.print(f"[cyan]共 {len(todo)} 篇文献待建立 4096 维 Qwen3 向量索引（并发: {min(workers, len(todo))}）...")
        
        lock = threading.Lock()
        with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
                      BarColumn(), console=self.console) as progress:
            task = progress.add_task("[cyan]索引构建中...", total=len(todo))

            def _worker(item):
                doc_id, md, doc_type = item
                n_chunks, success, err = self.index_single(doc_id, md, doc_type, embedder)
                with lock:
                    if success:
                        report["indexed"] += 1
                        report["chunks"] += n_chunks
                    else:
                        report["failed"] += 1
                        self.console.print(f"[red]✖ 索引失败 {doc_id[:8]}: {err}")
                    progress.advance(task)

            max_w = max(1, min(workers, len(todo)))
            with ThreadPoolExecutor(max_workers=max_w) as executor:
                futures = [executor.submit(_worker, item) for item in todo]
                for f in as_completed(futures):
                    f.result()

        self.console.print(f"[bold green]✔ 索引构建完成：成功 {report['indexed']} 篇 "
                           f"({report['chunks']} 块)，失败 {report['failed']} 篇。")
        return report


def build_index(settings=None, console=None, client=None, force: bool = False,
                doc_ids: list = None, workers: int = 4) -> dict:
    """极简统一门面函数（兼容历史调用与新深模块接口）：
    - 允许零参数调用：build_index()
    - 支持指定单篇或批次：build_index(doc_ids=['d773f081'])
    - 内部自动并发加速与自洽初始化
    """
    indexer = LiteratureIndexer(settings=settings, console=console, client=client)
    return indexer.run(doc_ids=doc_ids, force=force, workers=workers)
