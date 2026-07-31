#!/usr/bin/env python3
"""夜间本地嵌入入库：22:05 起跑，08:00 收工（10 小时窗口），可跨夜断点续跑。

**必须用系统 python 跑**（torch + sentence-transformers 装在那儿，.venv_lit 没有）：

    /usr/bin/python3 nightly_index.py                # 跑到 NIGHTLY_INDEX_DEADLINE 收工
    /usr/bin/python3 nightly_index.py --dry-run      # 只报待办，不加载模型
    /usr/bin/python3 nightly_index.py --no-deadline  # 手动全量跑，不设收工时间

为什么单独一个脚本、而不是塞进 daemon.py：
  daemon 跑在 .venv_lit 里（无 torch），且 build_index 挂在白天的 batch_fetch 上，
  每 120 分钟就可能触发一次。本地 8B 嵌一篇论文要约 24 分钟、驻留 15G 内存，
  白天当场跑等于把机器占死。所以 REVIEW_EMBED_BACKEND=local 时 build_index 只报待办
  不干活（见 litreview/indexer.py），嵌入全部落到这里。

四条保命设计（缺一不可）：
  1. 没有待办就立刻退出，绝不加载那 15G 权重。
  2. md_hash 未变时**不**调 replace_doc_chunks——它头一句是
     `DELETE FROM review_chunks WHERE doc_id=?`，会连同上一晚算好的向量一起删掉。
     一本 300 页教材要约 9.2 小时 > 8 小时窗口，不续跑就永远入不了库，
     且每晚白烧 8 小时不报任何错。
  3. 每 CHUNK_BATCH 个 chunk 落一次库，被 kill 最多丢这么多。
  4. 一篇文档所有 chunk 都拿到向量了才 mark_embedded，半截的下次自动接着跑。

跑批期间别开 Chrome：15G 权重全靠 page cache 撑着，被换出去就要按 110 MB/s
从机械盘重读，那 195 秒冷启动的代价会反复付。
"""
import argparse
import os
import signal
import sys
import time
import traceback
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 权重加载进度条在 cron 日志里是几千字符的乱码，关掉
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")

CHUNK_BATCH = 4            # 实测最优；再大也只快个位数百分比，反而拉长丢失窗口
# 兜底上限，防手动误跑跑通宵。**必须 ≥ 夜间窗口长度**：窗口是 22:00–08:00 共 10 小时，
# 这里若填 8，22:05 起跑的那次会被 min(次日08:00, 起跑+8h) 砍到 06:05 收工，白少两小时。
MAX_HOURS = 10

_stop = False


def _on_signal(signum, frame):
    global _stop
    _stop = True
    print(f"\n收到信号 {signum}，做完当前这批就存盘退出...", flush=True)


def load_env(path: str):
    """极简 .env 解析。不用 utils.settings——它拉 pandas，系统 python 里没有。"""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def deadline_at(hhmm: str) -> datetime:
    """下一个 hhmm 时刻，但最多 MAX_HOURS 小时后。

    cron 只在 22–23 点和 0–7 点拉起，正常总是次日/当天 08:00。加上限是防手动在白天误跑：
    那样 deadline 会滚到第二天 08:00，机器被占十几个小时。
    """
    now = datetime.now()
    h, m = (int(x) for x in hhmm.split(":"))
    d = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if d <= now:
        d += timedelta(days=1)
    return min(d, now + timedelta(hours=MAX_HOURS))


def fmt(sec: float) -> str:
    sec = int(max(sec, 0))
    return f"{sec // 3600}h{sec % 3600 // 60:02d}m" if sec >= 3600 else f"{sec // 60}m{sec % 60:02d}s"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只报待办，不加载模型")
    ap.add_argument("--no-deadline", action="store_true", help="不设收工时间，跑到做完为止")
    args = ap.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    load_env(os.path.join(here, ".env"))

    # 默认值必须与 utils.Settings 的 "./data/batch_tracking.db" 指同一个文件：
    # 早先这里默认的是 <repo>/batch_tracking.db，.env 里一旦漏了 DB_PATH，本脚本会
    # 静默新建一个空库、打印"无待索引文档"再以退出码 0 收工，看不出任何异常。
    db_path = os.getenv("DB_PATH") or os.path.join(here, "data", "batch_tracking.db")
    if not os.path.exists(db_path):
        print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 数据库不存在：{db_path}\n"
              f"       （检查 .env 的 DB_PATH；本脚本不新建空库，免得静默空跑）", flush=True)
        return 2
    dim = int(os.getenv("EMBEDDING_DIM", "4096"))
    # 标识与线上保持一致：已实证两者同属一个向量空间，混用无碍（见 local_embedder 文档串）
    model_label = os.getenv("EMBEDDING_MODEL", "Qwen/Qwen3-Embedding-8B")
    model_path = os.getenv("LOCAL_EMBEDDING_MODEL_PATH", "/mnt/ripe/models/Qwen3-Embedding-8B")
    review_size = int(os.getenv("REVIEW_CHUNK_SIZE", "1000"))
    review_overlap = int(os.getenv("REVIEW_CHUNK_OVERLAP", "150"))
    book_size = int(os.getenv("BOOK_CHUNK_SIZE", "4800"))
    book_overlap = int(os.getenv("BOOK_CHUNK_OVERLAP", "600"))

    from litreview.store import VectorStore, md_hash
    from litreview.chunker import split_markdown, chunk_params_for

    store = VectorStore(db_path, dim)
    todo = store.docs_needing_index()

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if not todo:
        print(f"[{stamp}] 无待索引文档，未加载模型，退出。", flush=True)
        return 0

    # 待办里已经嵌了一半的（上一晚没跑完的）单独标出来
    resumable = sum(1 for d, md, _ in todo if (st := store.doc_index_state(d)) and st[0] == md_hash(md))
    print(f"[{stamp}] 待索引 {len(todo)} 篇（其中 {resumable} 篇是上次未跑完、可续跑）", flush=True)

    if args.dry_run:
        for doc_id, md, doc_type in todo:
            st = store.doc_index_state(doc_id)
            if st and st[0] == md_hash(md):
                left = len(store.chunks_needing_embedding(doc_id))
                print(f"  {doc_id[:8]} [{doc_type}] 续跑：还差 {left}/{st[1]} 块", flush=True)
            else:
                print(f"  {doc_id[:8]} [{doc_type}] 新文档：{len(md)} 字符待分块", flush=True)
        return 0

    dl = None if args.no_deadline else deadline_at(os.getenv("NIGHTLY_INDEX_DEADLINE", "08:00"))
    if dl:
        print(f"       收工时间 {dl:%m-%d %H:%M}（还有 {fmt((dl - datetime.now()).total_seconds())}）",
              flush=True)

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    from litreview.local_embedder import LocalEmbedder
    emb = LocalEmbedder(model_path, dim, batch_size=CHUNK_BATCH,
                        threads=int(os.getenv("EMBED_THREADS", "12")))

    done_docs = done_chunks = done_chars = failed_docs = 0
    t_start = time.time()
    out_of_time = False

    try:
        for doc_id, md, doc_type in todo:
            if _stop or (dl and datetime.now() >= dl):
                out_of_time = True
                break

            # 单篇出错只跳过这一篇：这个循环没有排序，哪篇会炸每晚都不一样，
            # 让异常冒出去等于一个坏文档就报销掉整晚（其余待办一块都不嵌，也不打汇总）。
            try:
                state = store.doc_index_state(doc_id)
                if state and state[0] == md_hash(md):
                    # ── 续跑：分块结果仍然有效，绝不能重新 replace（会删掉已算好的向量）
                    pending = store.chunks_needing_embedding(doc_id)
                    print(f"  ▶ {doc_id[:8]} [{doc_type}] 续跑 {len(pending)}/{state[1]} 块",
                          flush=True)
                else:
                    # ── 新文档或正文变了：重新分块，旧向量本来就作废了
                    csize, coverlap = chunk_params_for(doc_type, review_size, review_overlap,
                                                       book_size, book_overlap)
                    chunks = split_markdown(doc_id, md, csize, coverlap)
                    if not chunks:
                        print(f"  ⚠ {doc_id[:8]} 清洗后无有效内容，跳过", flush=True)
                        store.replace_doc_chunks(doc_id, md_hash(md), [])
                        store.mark_embedded(doc_id)
                        continue
                    ids = store.replace_doc_chunks(doc_id, md_hash(md), chunks)
                    pending = list(zip(ids, [c.content for c in chunks]))
                    print(f"  ▶ {doc_id[:8]} [{doc_type}] 新索引 {len(pending)} 块", flush=True)

                for i in range(0, len(pending), CHUNK_BATCH):
                    if _stop or (dl and datetime.now() >= dl):
                        out_of_time = True
                        break
                    sub = pending[i:i + CHUNK_BATCH]
                    t0 = time.time()
                    vecs = emb.embed_texts([c for _, c in sub])
                    store.save_embeddings([cid for cid, _ in sub], vecs, model_label)
                    chars = sum(len(c) for _, c in sub)
                    done_chunks += len(sub)
                    done_chars += chars
                    # 成本单位是 chunk 不是字符：实测约 60 秒/块且对块长不敏感，
                    # 短块的"字符/小时"会难看好几倍（678 字符也要 272 秒），拿它估时会严重跑偏
                    sec_per_chunk = (time.time() - t_start) / done_chunks
                    left = len(pending) - i - len(sub)
                    eta = f"，本篇剩余约 {fmt(left * sec_per_chunk)}" if left else ""
                    print(f"    {i + len(sub):>4}/{len(pending)} 块  {chars:>5} 字符  "
                          f"{time.time() - t0:5.1f}s  均 {sec_per_chunk:.0f}s/块{eta}", flush=True)

                # 只有全部 chunk 都拿到向量才算这篇完成，半截的下次自动接着跑
                if not store.chunks_needing_embedding(doc_id):
                    store.mark_embedded(doc_id)
                    done_docs += 1
                    print(f"  ✔ {doc_id[:8]} 完成", flush=True)
                else:
                    left = len(store.chunks_needing_embedding(doc_id))
                    print(f"  ⏸ {doc_id[:8]} 未跑完，还差 {left} 块，明晚接着跑", flush=True)
            except Exception as e:
                failed_docs += 1
                print(f"  ✖ {doc_id[:8]} 出错，跳过这一篇（其余继续）: "
                      f"{type(e).__name__}: {e}", flush=True)
                traceback.print_exc()
                continue

            if out_of_time:
                break
    finally:
        emb.close()

    used = time.time() - t_start
    why = "到点收工" if out_of_time and not _stop else ("被中断" if _stop else "全部做完")
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {why}：完成 {done_docs} 篇 / {done_chunks} 块 / "
          f"{done_chars} 字符" + (f"，出错跳过 {failed_docs} 篇" if failed_docs else "")
          + f"，耗时 {fmt(used)}"
          f"（{used / max(done_chunks, 1):.0f} 秒/块，"
          f"{done_chars / max(used, 1e-6) * 3600 / 10000:.1f} 万字符/小时）", flush=True)
    left = len(store.docs_needing_index())
    if left:
        print(f"       仍有 {left} 篇待索引，下次启动自动接着跑（已完成的块不会重算）", flush=True)
    return 1 if failed_docs else 0      # 有跳过的篇目就带非零退出码，别让 cron 看着像全绿


if __name__ == "__main__":
    sys.exit(main())
