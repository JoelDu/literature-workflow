"""SiliconFlow /v1/rerank 封装（Qwen3-Reranker）。失败时优雅降级为按原顺序返回，并完整记录审计日志。"""
import json
import os
import time

import requests
from tenacity import retry, stop_after_attempt, wait_exponential

_MAX_DOC_CHARS = 5000  # 每条候选文档送入 reranker 的截断长度（放宽以容纳更大的书籍块）


class Reranker:
    def __init__(self, model: str, api_key: str = None, base_url: str = None):
        self.model = model
        self.api_key = api_key or os.getenv("SILICONFLOW_API_KEY", "")
        self.base_url = (base_url or os.getenv("SILICONFLOW_API_BASE", "")).rstrip("/")

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=15))
    def _call(self, query: str, documents: list, top_n: int) -> list:
        resp = requests.post(
            f"{self.base_url}/rerank",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={"model": self.model, "query": query,
                  "documents": [d[:_MAX_DOC_CHARS] for d in documents],
                  "top_n": top_n, "return_documents": False},
            timeout=60,
        )
        resp.raise_for_status()
        results = resp.json()["results"]
        return [(r["index"], float(r["relevance_score"])) for r in results]

    def rerank(self, query: str, documents: list, top_n: int = None, stage: str = "stage_B_rerank") -> list:
        """返回 [(原始下标, 相关分)]，按分数降序。同时异步记录至 SQLite 审计数据库。"""
        if not documents:
            return []
        top_n = top_n or len(documents)
        t0 = time.time()
        
        # 组装用于审计的 Prompt 摘要
        prompt_text = (
            f"【重排检索 Query】: {query}\n"
            f"【候选段落总数】: {len(documents)} 块\n"
            f"【候选段落预览 (前 5 块)】:\n"
            + "\n---\n".join([f"[{i}] {d[:300]}..." for i, d in enumerate(documents[:5])])
        )
        
        settings = None
        try:
            from utils import get_settings
            settings = get_settings()
        except Exception:
            pass

        try:
            results = self._call(query, documents, top_n)
            cost_sec = time.time() - t0
            if settings:
                from litreview.quota import record_llm_call
                resp_text = json.dumps(
                    [{"index": idx, "score": score, "preview": documents[idx][:150]} for idx, score in results[:10]],
                    ensure_ascii=False, indent=2
                )
                tot_chars = sum(len(d) for d in documents) + len(query)
                tot_tok = int(tot_chars / 3.5)
                import types
                usage_obj = types.SimpleNamespace(prompt_tokens=tot_tok, completion_tokens=0, total_tokens=tot_tok)
                record_llm_call(
                    settings, call_type="rerank", model=self.model,
                    prompt=prompt_text, response_text=resp_text, stage=stage,
                    usage=usage_obj, cost_seconds=cost_sec, status="SUCCESS",
                    meta={"query": query, "total_docs": len(documents), "top_n": top_n, "top_score": results[0][1] if results else 0.0}
                )
            return results
        except Exception as e:
            cost_sec = time.time() - t0
            if settings:
                from litreview.quota import record_llm_call
                record_llm_call(
                    settings, call_type="rerank", model=self.model,
                    prompt=prompt_text, response_text="", stage=stage,
                    usage=None, cost_seconds=cost_sec, status="FAILED",
                    error_message=str(e), meta={"query": query, "total_docs": len(documents)}
                )
            return [(i, 0.0) for i in range(min(top_n, len(documents)))]
