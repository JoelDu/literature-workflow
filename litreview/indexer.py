"""增量索引编排：diff 待索引文档 → 分块 → 嵌入 → 落库。"""
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn

from .chunker import split_markdown
from .store import VectorStore, md_hash
from .embedder import Embedder


def build_index(settings, console, client, force: bool = False) -> dict:
    """幂等增量索引。force=True 时全量重建。每文档独立事务，单篇失败不影响其余。"""
    store = VectorStore(settings.DB_PATH, settings.EMBEDDING_DIM)
    embedder = Embedder(client, settings.EMBEDDING_MODEL, settings.EMBEDDING_DIM,
                        settings.EMBEDDING_BATCH_SIZE)

    if force:
        conn = store._conn()
        conn.execute("DELETE FROM review_index_meta")
        conn.execute("DELETE FROM review_chunks")
        conn.commit()
        conn.close()
        store._matrix = None

    todo = store.docs_needing_index()
    report = {"total": len(todo), "indexed": 0, "failed": 0, "chunks": 0}
    if not todo:
        console.print("[green]✔ 向量索引已是最新，0 篇待索引。")
        return report

    console.print(f"[cyan]共 {len(todo)} 篇文献待索引（模型: {settings.EMBEDDING_MODEL}）...")
    with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
                  BarColumn(), console=console) as progress:
        task = progress.add_task("[cyan]索引中...", total=len(todo))
        for doc_id, md, doc_type in todo:
            progress.update(task, description=f"[cyan]索引 {doc_id[:8]}...")
            try:
                # 书籍用更大的块（BOOK_CHUNK_SIZE），论文用默认块
                if doc_type == "book":
                    csize, coverlap = settings.BOOK_CHUNK_SIZE, settings.BOOK_CHUNK_OVERLAP
                else:
                    csize, coverlap = settings.REVIEW_CHUNK_SIZE, settings.REVIEW_CHUNK_OVERLAP
                chunks = split_markdown(doc_id, md, csize, coverlap)
                if not chunks:
                    console.print(f"[yellow]⚠️ {doc_id[:8]} 清洗后无有效内容，跳过。")
                    store.replace_doc_chunks(doc_id, md_hash(md), [])
                    store.mark_embedded(doc_id)
                    progress.advance(task)
                    continue
                chunk_ids = store.replace_doc_chunks(doc_id, md_hash(md), chunks)
                vecs = embedder.embed_texts([c.content for c in chunks])
                store.save_embeddings(chunk_ids, vecs, settings.EMBEDDING_MODEL)
                store.mark_embedded(doc_id)
                report["indexed"] += 1
                report["chunks"] += len(chunks)
            except Exception as e:
                report["failed"] += 1
                console.print(f"[red]✖ 索引失败 {doc_id[:8]}: {e}")
                try:
                    from utils import log_run_event
                    log_run_event(mode="review", event="index_doc", doc_id=doc_id,
                                  status="failed", error=str(e))
                except Exception:
                    pass
            progress.advance(task)

    console.print(f"[bold green]✔ 索引完成：成功 {report['indexed']} 篇 "
                  f"({report['chunks']} 块)，失败 {report['failed']} 篇。")
    return report
