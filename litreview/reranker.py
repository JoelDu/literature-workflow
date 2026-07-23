"""SiliconFlow /v1/rerank 封装（Qwen3-Reranker）。失败时优雅降级为按原顺序返回。"""
import os

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

    def rerank(self, query: str, documents: list, top_n: int = None) -> list:
        """返回 [(原始下标, 相关分)]，按分数降序。失败时降级为原顺序、分数置 0（不中断上层流程）。"""
        if not documents:
            return []
        top_n = top_n or len(documents)
        try:
            return self._call(query, documents, top_n)
        except Exception:
            return [(i, 0.0) for i in range(min(top_n, len(documents)))]
