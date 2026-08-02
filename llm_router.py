import json
import os

from langdetect import detect, DetectorFactory
from openai import OpenAI
from google import genai
from google.genai import types
from pydantic import BaseModel, Field
from tenacity import retry, stop_after_attempt, wait_exponential

from utils import extract_key_sections

# 固定随机种子，确保 langdetect 结果可重现
DetectorFactory.seed = 0


class PaperAnalysis(BaseModel):
    tldr: str = Field(description="一两句话总结文章核心做了什么，结果如何。")
    abstract: str = Field(description="从原文中完整提取的原始摘要（一字不漏，保持原文语言，不要有任何缩写或概括）。")
    background: str = Field(description="研究背景与动机。请提供详尽的分析，说明该研究针对的具体工业/学术痛点，以及核心动机。避免简短总结。")
    methods: str = Field(description="核心方法与公式。深入、详细地剖析论文的关键技术、公式算法以及主要创新点。")
    results: str = Field(description="实验数据与结果。详尽地列出关键的实验结果、对比数据指标和核心结论（如果原文包含数据，请在此处具体列出）。")
    conclusion: str = Field(description="结论与个人启发。分析文章的局限性、个人反思以及后续的研究/工业应用启发。")
    language: str = Field(description="文章主要语言，例如 'zh' 或 'en'")
    authors: str = Field(description="作者列表，多位作者用英文逗号分隔；无法提取时返回空字符串")
    journal: str = Field(description="期刊或会议全名；无法提取时返回空字符串")
    year: str = Field(description="发表年份（4位数字）；无法提取时返回空字符串")


def make_deepseek_client(deepseek_api_key: str = None, quiet: bool = False):
    """构建 DeepSeek 兼容 client：优先 SiliconFlow（同 client 也可调 /v1/embeddings），否则官方 DeepSeek。
    返回 (client, model)。供 LLMRouter 与 litreview 综述模块共用，保证两处解析逻辑完全一致。
    """
    sf_key = os.getenv("SILICONFLOW_API_KEY")
    sf_base = os.getenv("SILICONFLOW_API_BASE")

    if sf_key and sf_base:
        client = OpenAI(api_key=sf_key, base_url=sf_base, timeout=180)
        # 默认值曾是 deepseek-ai/DeepSeek-V3，2026-07-24 之后它在硅基流动上任何请求都返回
        # 429「System is too busy now」（同步、Batch 都一样，Pro/ 前缀版同症状），等于彻底不可用。
        # 更阴的是走 Batch 时这个错只会以一句 "Request failed: Unknown error." 出现在错误文件里，
        # 从日志上根本看不出是模型的问题。
        # 换 V3.1-Terminus 的原因：整个账户里只有 V3、V3.1-Terminus、R1 三个模型支持 batch 推理
        # （其余包括 V3.2 / V4-Pro / V4-Flash 提交时就报 20088 not support batch inference），
        # 而 V3 已废、R1 是推理模型不适合做结构化抽取，Terminus 是唯一实测跑通的。
        # ⚠️ 换模型前务必先确认它支持 batch，否则阶段 1 会在提交时直接 400。
        model = os.getenv("DEEPSEEK_MODEL", "deepseek-ai/DeepSeek-V3.1-Terminus")
        if not quiet:
            print(f"[LLM Router] 正在使用 硅基流动 (SiliconFlow) {model} 进行无损文献分析。")
    else:
        client = OpenAI(
            api_key=deepseek_api_key or os.getenv("DEEPSEEK_API_KEY", ""),
            base_url=os.getenv("DEEPSEEK_API_BASE", "https://api.deepseek.com/v1"),
            timeout=180,
        )
        model = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
        if not quiet:
            print(f"[LLM Router] 正在使用 官方 DeepSeek {model} 进行文献分析。")
    return client, model


class LLMRouter:
    def __init__(self, deepseek_api_key: str, gemini_api_key: str):
        self.deepseek_client, self.deepseek_model = make_deepseek_client(deepseek_api_key)
        self.gemini_client = genai.Client(api_key=gemini_api_key)

        self.system_prompt = """\
你是一个极其专业、细致的学术论文分析助手。请阅读以下由 PDF 转换而来的 Markdown 格式的论文内容，提取并总结出结构化信息。
我们要求生成的分析高度专业、详尽且深入（拒绝简短敷衍的一两句话总结），并且必须完整提取原文的原始摘要。

请输出符合 JSON 格式，包含以下字段：
- tldr: 💡 核心简评（一两句话高屋建瓴地总结文章的核心贡献和结果）
- abstract: 📄 原始摘要（一字不漏地从论文中提取完整的原始摘要内容，保持其原文语言）
- background: 🎯 研究背景与动机（详尽、丰富地分析该研究的起点，解决了什么工业/技术痛点，研究动机是什么，通常需要 3-5 句以上的深度表述）
- methods: 🧪 核心方法与公式（详细、系统地剖析其所采用的核心方法、关键公式或核心算法，若有重要创新点或实验步骤请详细列出）
- results: 📊 实验数据与结果（极其详尽地提炼实验数据、测试指标、对比参数与核心发现，力求还原原文的关键数据支撑）
- conclusion: 📝 结论与个人启发（深刻提炼研究局限性、个人启发以及未来拓展/工业落地的可行性方向）
- language: 文章的主要语言（'zh' 或 'en' 等）
- authors: 作者列表，多位作者用英文逗号分隔（从论文头部提取，无法提取时输出空字符串）
- journal: 期刊或会议全名（从论文头部或页脚提取，无法提取时输出空字符串）
- year: 发表年份，4位数字（从论文头部提取，无法提取时输出空字符串）"""

    def detect_language(self, text: str) -> str:
        """基于前 2000 字符检测语言，随机种子已固定，结果可重现。"""
        try:
            return detect(text[:2000])
        except Exception:
            return "en"

    def analyze_paper(self, markdown_text: str) -> dict:
        """根据语言自动路由给不同模型进行分析。
        注意：当前 Gemini API Key 已暂停，统一使用 DeepSeek V3 处理中英文。
        利用大模型超长上下文，采用 100% 原始文本，不进行任何截断。
        """
        # 直接使用全文本进行无损分析！
        return self._call_deepseek(markdown_text)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10), reraise=True)
    def _call_deepseek(self, text: str) -> dict:
        """调用 DeepSeek V3 (基于硅基流动或官方)。"""
        response = self.deepseek_client.chat.completions.create(
            model=self.deepseek_model,
            messages=[
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": f"请分析以下论文内容：\n\n{text}"},
            ],
            response_format={"type": "json_object"},
            temperature=0.3,
        )
        try:
            return json.loads(response.choices[0].message.content)
        except Exception as e:
            return {"error": str(e), "raw": response.choices[0].message.content}

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10), reraise=True)
    def _call_gemini(self, text: str) -> dict:
        """调用 Gemini 2.5 Pro，使用 Pydantic schema 确保结构化输出。"""
        response = self.gemini_client.models.generate_content(
            model="gemini-2.5-pro",
            contents=f"{self.system_prompt}\n\n请分析以下论文内容：\n{text}",
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=PaperAnalysis,
                temperature=0.3,
            ),
        )
        try:
            return json.loads(response.text)
        except Exception as e:
            return {"error": str(e), "raw": response.text}
