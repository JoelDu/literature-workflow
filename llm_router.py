"""唯一的 OpenAI 生成客户端入口。

这里故意没有环境变量模型覆盖或失败回退。论文分析/补全固定 mini，综述正文固定
GPT-5.4；两者共享客户端但分别进入各自的 UTC 额度账本。
"""
import json
import os

import httpx
from langdetect import detect, DetectorFactory
from openai import OpenAI
from tenacity import retry, retry_if_not_exception_type, stop_after_attempt, wait_exponential

from litreview.analysis_prompt import build_adaptive_analysis_prompt
from litreview.token_budget import truncate_to_input_budget
from litreview.quota import QuotaExceededError

DetectorFactory.seed = 0
OPENAI_MODEL = "gpt-5.4-mini"


def make_openai_client(api_key: str | None = None, quiet: bool = False):
    from utils import get_settings
    settings = get_settings()
    key = api_key or settings.OPENAI_API_KEY
    if not key:
        raise ValueError("未配置 OPENAI_API_KEY，无法创建论文分析与综述生成客户端")
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
        self.system_prompt = build_adaptive_analysis_prompt("", doc_type="paper")

    def detect_language(self, text: str) -> str:
        from litreview.analysis_prompt import detect_language as _detect
        return _detect(text)

    def build_request(self, doc_id: str, markdown_text: str, doc_type: str = "paper") -> dict:
        from litreview.analysis_prompt import build_adaptive_analysis_prompt
        text = truncate_to_input_budget(markdown_text)
        sys_prompt = build_adaptive_analysis_prompt(text, doc_type=doc_type)
        return {
            "custom_id": doc_id,
            "method": "POST",
            "url": "/v1/chat/completions",
            "body": {
                "model": self.openai_model,
                "messages": [
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": f"请分析以下论文内容：\n\n{text}"},
                ],
                "response_format": {"type": "json_object"},
                "temperature": 0.3,
                "max_completion_tokens": 12000,
            },
        }

    def analyze_paper(self, markdown_text: str, doc_type: str = "paper"):
        return self._call_openai(markdown_text, doc_type=doc_type)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10),
           retry=retry_if_not_exception_type(QuotaExceededError), reraise=True)
    def _call_openai(self, text: str, doc_type: str = "paper"):
        import time
        # 实时模式同样消耗额度，必须和 Batch/元数据共用 UTC 账本。
        from litreview.quota import reconcile, reserve, record_llm_call
        from litreview.analysis_record import PaperAnalysisRecord
        from utils import get_settings
        settings = get_settings()
        req = self.build_request("realtime", text, doc_type=doc_type)["body"]
        prompt = req["messages"][0]["content"] + "\n" + req["messages"][1]["content"]
        reservation = reserve(settings, self.openai_model, prompt, req["max_completion_tokens"])
        t0 = time.time()
        try:
            response = self.openai_client.chat.completions.create(**req)
            reconcile(settings, self.openai_model, reservation, response.usage)
            content = response.choices[0].message.content
            cost_sec = time.time() - t0
            record_llm_call(settings, call_type="analyze_paper", model=self.openai_model,
                            prompt=prompt, response_text=content, stage="realtime_analysis",
                            usage=response.usage, cost_seconds=cost_sec, status="SUCCESS")
            return PaperAnalysisRecord.from_llm_json(content, markdown_text=text)
        except Exception as e:
            cost_sec = time.time() - t0
            record_llm_call(settings, call_type="analyze_paper", model=self.openai_model,
                            prompt=prompt, response_text="", stage="realtime_analysis",
                            usage=None, cost_seconds=cost_sec, status="FAILED", error_message=str(e))
            raise
