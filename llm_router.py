"""唯一的 OpenAI 生成客户端入口。

这里故意没有环境变量模型覆盖或失败回退。论文分析/补全固定 mini，综述正文固定
GPT-5.4；两者共享客户端但分别进入各自的 UTC 额度账本。
"""
import json
import os

import httpx
from langdetect import detect, DetectorFactory
from openai import OpenAI
from tenacity import retry, stop_after_attempt, wait_exponential

from litreview.token_budget import truncate_to_input_budget

DetectorFactory.seed = 0
OPENAI_MODEL = "gpt-5.4-mini"


def make_openai_client(api_key: str | None = None, quiet: bool = False):
    from utils import get_settings
    settings = get_settings()
    key = api_key or settings.OPENAI_API_KEY
    model = getattr(settings, "ANALYSIS_MODEL", "gpt-5.4-mini")
    kwargs = {"api_key": key, "base_url": settings.OPENAI_API_BASE, "timeout": 180}
    # 宿主机 MCP 会清掉共享文件里的失效系统代理；项目专用 LIT 代理必须显式传给
    # OpenAI 客户端。嵌入/重排客户端独立创建，因此不会被这个代理配置牵连。
    proxy = os.getenv("LIT_HTTPS_PROXY") or os.getenv("LIT_HTTP_PROXY")
    if proxy:
        kwargs["http_client"] = httpx.Client(proxy=proxy, timeout=180)
    client = OpenAI(**kwargs)
    if not quiet:
        print(f"[LLM Router] 使用模型 {model}。")
    return client, model


def make_legacy_siliconflow_client():
    """仅用于拉取迁移前已经提交的硅基流动 Batch，绝不用于创建新生成任务。"""
    key = os.getenv("SILICONFLOW_API_KEY", "")
    base = os.getenv("SILICONFLOW_API_BASE", "")
    if not key or not base:
        raise RuntimeError("存在历史 SiliconFlow Batch，但未配置 SILICONFLOW_API_KEY/API_BASE，无法拉取旧结果")
    return OpenAI(api_key=key, base_url=base, timeout=180)


class LLMRouter:
    def __init__(self, _legacy_key: str | None = None, _legacy_gemini_key: str | None = None):
        self.openai_client, self.openai_model = make_openai_client(_legacy_key)
        self.system_prompt = """你是一个精通化学工程、材料科学与磷化工/新型肥料研发的顶尖学术分析专家。请阅读以下由 PDF 转换而来的 Markdown 格式的论文内容，深入提取并总结出结构化信息。\n
请输出严格合法的 JSON 对象，包含以下字段：
1. tldr: 1~2 句话精炼概括论文核心贡献与一句话结论。
2. keywords: 3~5 个高概括度的中英文核心关键词/研究领域标签（分号隔开，如：包膜控释; 水性聚氨酯; 养分释放动力学; 膜孔隙率）。
3. fertilizer_implications: 【重点】这篇论文的思路、方法、改性手段、反应机理或结论，对磷化工生产、新型肥料产品开发（如缓释/控释肥、水溶肥、功能性生物肥、磷石膏综合利用等）有哪些具体的借鉴价值和技术抓手？严禁假大空套话（如“提供了理论参考”），必须直击配方调控、机理借鉴、工艺降本或工业放大的具体技术点。
4. abstract: 尽量逐字提取并保留作者原始摘要。
5. background: 研究背景、行业痛点与研究动机。
6. methods: 核心实验路线、反应方程式、表征手段与工艺参数。
7. results: 关键量化数据、对照实验结果与对比性能。
8. conclusion: 主要结论、实际应用价值与作者启示。
9. language / authors / journal / year: 基础学术著录元数据。无法提取则使用空字符串。"""

    def detect_language(self, text: str) -> str:
        try:
            return detect(text[:2000])
        except Exception:
            return "en"

    def build_request(self, doc_id: str, markdown_text: str) -> dict:
        text = truncate_to_input_budget(markdown_text)
        return {
            "custom_id": doc_id,
            "method": "POST",
            "url": "/v1/chat/completions",
            "body": {
                "model": self.openai_model,
                "messages": [
                    {"role": "system", "content": self.system_prompt},
                    {"role": "user", "content": f"请分析以下论文内容：\n\n{text}"},
                ],
                "response_format": {"type": "json_object"},
                "temperature": 0.3,
                "max_completion_tokens": 12000,
            },
        }

    def analyze_paper(self, markdown_text: str) -> dict:
        return self._call_openai(markdown_text)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10), reraise=True)
    def _call_openai(self, text: str) -> dict:
        # 实时模式同样消耗额度，必须和 Batch/元数据共用 UTC 账本。
        from litreview.quota import reconcile, reserve
        from utils import get_settings
        req = self.build_request("realtime", text)["body"]
        prompt = req["messages"][0]["content"] + "\n" + req["messages"][1]["content"]
        reservation = reserve(get_settings(), self.openai_model, prompt, req["max_completion_tokens"])
        response = self.openai_client.chat.completions.create(**req)
        reconcile(get_settings(), self.openai_model, reservation, response.usage)
        content = response.choices[0].message.content
        try:
            return json.loads(content)
        except Exception as exc:
            return {"error": str(exc), "raw": content}
