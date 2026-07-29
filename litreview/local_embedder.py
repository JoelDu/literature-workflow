"""本地 Qwen3-Embedding 推理，接口与 embedder.Embedder 对齐（文档侧 / 查询侧）。

只给夜间入库任务 nightly_index.py 用。依赖 torch + sentence-transformers，这两个装在
系统 python(/usr/bin/python3)里；.venv_lit 里没有也不必装——这台机器没显卡，装 CUDA 版
torch 白占 3G 系统盘。

已实证（2026-07-29）：本地推理产出的向量与 SiliconFlow 线上同名模型**是同一个向量空间**
（同文本余弦最小 0.9998，16/16 top-1 命中自己），可与库里存量向量自由混用，因此
embedding_model 仍写同一个标识。验证脚本 /mnt/ripe/models/bench/vec_compat.py，
**换模型或换服务商后必须重跑**——向量空间不兼容是静默失效，检索质量会悄悄退化而不报错。

性能（Xeon E5-2678 v3，无 AVX-512/显卡）：冷启动约 195 秒，热态约 60 秒/千字 chunk。
batch 从 1 加到 8 只快 9%，已贴着算力屋顶，别指望调批量能提速。
"""
import numpy as np

_MAX_CHARS = 7000          # 与 embedder.Embedder 保持一致


class LocalEmbedder:
    """懒加载：构造时不碰权重，第一次 embed 才把 15G 读进来。"""

    def __init__(self, model_path: str, dim: int, batch_size: int = 4,
                 threads: int = 12, query_instruction: str = ""):
        self.model_path = model_path
        self.dim = dim
        self.batch_size = batch_size
        self.threads = threads          # 12 = 物理核数，实测最优（24 是超线程后的逻辑核）
        self.query_instruction = query_instruction
        self._model = None

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def _ensure_loaded(self):
        if self._model is not None:
            return
        import torch
        from sentence_transformers import SentenceTransformer

        torch.set_num_threads(self.threads)
        try:
            self._model = SentenceTransformer(
                self.model_path, model_kwargs={"dtype": torch.bfloat16})
        except TypeError:               # sentence-transformers < 5 用的是 torch_dtype
            self._model = SentenceTransformer(
                self.model_path, model_kwargs={"torch_dtype": torch.bfloat16})

    def embed_texts(self, texts, progress_cb=None) -> np.ndarray:
        """文档侧嵌入：不加 instruction 前缀（与线上 embed_texts 一致）。"""
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        self._ensure_loaded()
        texts = [t[:_MAX_CHARS] for t in texts]
        mat = np.asarray(
            self._model.encode(texts, batch_size=self.batch_size,
                               show_progress_bar=False),
            dtype=np.float32)
        if mat.shape != (len(texts), self.dim):
            raise ValueError(f"本地嵌入维度不符：期望 {(len(texts), self.dim)}，实得 {mat.shape}")
        norms = np.linalg.norm(mat, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        if progress_cb:
            progress_cb(len(texts))
        return mat / norms

    def embed_query(self, text: str) -> np.ndarray:
        """查询侧嵌入：按官方规范加 Instruct 前缀（仅查询侧，文档侧不加）。"""
        if self.query_instruction:
            text = f"Instruct: {self.query_instruction}\nQuery: {text}"
        return self.embed_texts([text])[0]

    def close(self):
        """放掉权重。15G 常驻会把 page cache 挤爆，跑完就还回去。"""
        self._model = None
        import gc
        gc.collect()
