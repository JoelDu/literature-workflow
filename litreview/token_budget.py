"""保守的 token 预算与 Batch 用量解析。

不依赖第三方 tokenizer：中文通常接近一字一 token，按 1.15 token/汉字保守预留；
ASCII 文本按 0.35 token/字符预留。实际账单始终以 API usage.total_tokens 为准。
"""
from __future__ import annotations

import re
from typing import Any

MODEL_CONTEXT_TOKENS = 400_000
MAX_INPUT_TOKENS = 200_000
BATCH_TOKEN_CAP = 600_000
DAILY_TOKEN_CAP = 2_000_000
REVIEW_DAILY_TOKEN_CAP = 220_000
MAX_OUTPUT_TOKENS = 12_000
_CJK = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")


def estimate_tokens(text: str) -> int:
    cjk = len(_CJK.findall(text))
    other = len(text) - cjk
    return max(1, int(cjk * 1.15 + other * 0.35) + 8)


def truncate_to_input_budget(text: str, max_input_tokens: int = MAX_INPUT_TOKENS) -> str:
    if estimate_tokens(text) <= max_input_tokens:
        return text
    # 二分查找而不是按字符比例切，确保最终估算不超过上限。
    low, high = 0, len(text)
    while low < high:
        mid = (low + high + 1) // 2
        if estimate_tokens(text[:mid]) <= max_input_tokens:
            low = mid
        else:
            high = mid - 1
    return text[:low] + "\n\n[正文因单篇输入 200k token 安全上限而截断]"


def request_reserved_tokens(request: dict) -> int:
    body = request["body"]
    prompt = "".join(str(m.get("content", "")) for m in body.get("messages", []))
    return estimate_tokens(prompt) + int(body.get("max_completion_tokens", body.get("max_tokens", MAX_OUTPUT_TOKENS)))


def usage_totals(usage: Any) -> tuple[int, int, int, int]:
    """返回 input, output, reasoning, total；兼容 dict 与 SDK 对象。"""
    if usage is None:
        return 0, 0, 0, 0
    get = (lambda name, default=0: usage.get(name, default)) if isinstance(usage, dict) else (lambda name, default=0: getattr(usage, name, default))
    inp = int(get("prompt_tokens", get("input_tokens", 0)) or 0)
    out = int(get("completion_tokens", get("output_tokens", 0)) or 0)
    total = int(get("total_tokens", 0) or 0)
    details = get("completion_tokens_details", get("output_tokens_details", {})) or {}
    reasoning = int(details.get("reasoning_tokens", 0) if isinstance(details, dict) else getattr(details, "reasoning_tokens", 0) or 0)
    return inp, out, reasoning, total or inp + out
