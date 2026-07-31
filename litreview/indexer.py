"""增量索引编排：diff 待索引文档 → 分块 → 嵌入 → 落库。"""
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn

from .chunker import split_markdown, chunk_params_for
from .store import VectorStore, md_hash
from .embedder import Embedder


def build_index(settings, console, client, force: bool = False) -> dict:
    """幂等增量索引（走在线嵌入 API）。force=True 时全量重建。

    每文档独立事务，单篇失败不影响其余。

    REVIEW_EMBED_BACKEND=local 时本函数只报告待办、不做嵌入：本地 8B 嵌入
    单篇论文要跑约 24 分钟，绝不能让白天的 batch_fetch/教材入库当场触发。
    嵌入统一交给夜间任务 nightly_index.py（22:00–08:00，可断点续跑）。
    但 force 清库要照做——否则 local 后端下 `--force` 是个静默空操作，
    换嵌入模型后根本没有办法触发全量重建。
    """
    store = VectorStore(settings.DB_PATH, settings.EMBEDDING_DIM)

    if force:
        conn = store._conn()
        conn.execute("DELETE FROM review_index_meta")
        conn.execute("DELETE FROM review_chunks")
        conn.commit()
        conn.close()
        store._matrix = None

    # 默认值必须与 utils.Settings 一致（local）：这里若写 remote，任何字段不全的
    # settings 替身（测试桩、临时脚本）都会掉进昂贵的同步在线嵌入分支。
    if getattr(settings, "REVIEW_EMBED_BACKEND", "local") == "local":
        todo = store.docs_needing_index()
        console.print(f"[dim]嵌入后端=local：{len(todo)} 篇待索引留给夜间任务处理，此处跳过。"
                      + ("（--force 已清空索引，全部待重建）" if force else ""))
        return {"total": len(todo), "indexed": 0, "failed": 0, "chunks": 0,
                "deferred": len(todo)}

    embedder = Embedder(client, settings.EMBEDDING_MODEL, settings.EMBEDDING_DIM,
                        settings.EMBEDDING_BATCH_SIZE)

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
                csize, coverlap = chunk_params_for(
                    doc_type, settings.REVIEW_CHUNK_SIZE, settings.REVIEW_CHUNK_OVERLAP,
                    settings.BOOK_CHUNK_SIZE, settings.BOOK_CHUNK_OVERLAP)
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
