"""SiliconFlow /v1/embeddings 封装（Qwen3-Embedding / bge-m3），复用现有 OpenAI 兼容 client。"""
import numpy as np
from tenacity import retry, stop_after_attempt, wait_exponential

# Qwen3-Embedding 上下文 32k、bge-m3 8192；统一留安全余量
_MAX_CHARS = 7000


class Embedder:
    def __init__(self, client, model: str, dim: int, batch_size: int = 32,
                 query_instruction: str = ""):
        self.client = client
        self.model = model
        self.dim = dim
        self.batch_size = batch_size
        self.query_instruction = query_instruction
        # Qwen3-Embedding 支持 MRL 自定义维度（dimensions 参数）；bge 系列不支持
        self._supports_dimensions = "Qwen3-Embedding" in model

    @retry(stop=stop_after_attempt(4), wait=wait_exponential(min=2, max=30))
    def _embed_batch(self, texts: list) -> np.ndarray:
        texts = [t[:_MAX_CHARS] for t in texts]
        kwargs = {"model": self.model, "input": texts}
        if self._supports_dimensions:
            kwargs["dimensions"] = self.dim
        resp = self.client.embeddings.create(**kwargs)
        # 按 index 排序，防服务端乱序返回
        data = sorted(resp.data, key=lambda d: d.index)
        mat = np.array([d.embedding for d in data], dtype=np.float32)
        if mat.shape != (len(texts), self.dim):
            raise ValueError(f"embedding 维度不符: got {mat.shape}, expect ({len(texts)}, {self.dim})")
        # L2 归一化，检索时点积即余弦
        norms = np.linalg.norm(mat, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return mat / norms

    def embed_texts(self, texts: list, progress_cb=None) -> np.ndarray:
        """文档侧嵌入：不加 instruction 前缀。"""
        parts = []
        for i in range(0, len(texts), self.batch_size):
            parts.append(self._embed_batch(texts[i : i + self.batch_size]))
            if progress_cb:
                progress_cb(min(i + self.batch_size, len(texts)))
        return np.vstack(parts) if parts else np.zeros((0, self.dim), dtype=np.float32)

    def embed_query(self, text: str) -> np.ndarray:
        """查询侧嵌入：Qwen3-Embedding 按官方规范加 Instruct 前缀（仅查询侧，文档侧不加）。"""
        if self._supports_dimensions and self.query_instruction:
            text = f"Instruct: {self.query_instruction}\nQuery: {text}"
        return self._embed_batch([text])[0]
