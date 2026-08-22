"""同步生成调用的 UTC 配额账本。

Batch 由 batch_pipeline 单独登记；这里覆盖综述、元数据和教材等同步请求。
所有扣量按 API 的 total_tokens 校正，reasoning token 不会漏计。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from db_schema import ensure_llm_quota_tables
from .token_budget import estimate_tokens, usage_totals

MINI_MODEL = "gpt-5.4-mini"
REVIEW_MODEL = "gpt-5.4"


def _now() -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    return now.date().isoformat(), now.isoformat()


def _cap(settings, model: str) -> int:
    if model == REVIEW_MODEL:
        return settings.REVIEW_DAILY_TOKEN_CAP
    if model == MINI_MODEL:
        return settings.LLM_DAILY_TOKEN_CAP
    raise ValueError(f"禁止使用未固定的生成模型: {model}")


def reserve(settings, model: str, prompt: str, max_output_tokens: int) -> tuple[str, int]:
    """请求前保守预留。额度不足时失败，绝不靠 API 429 来限流。"""
    estimate = estimate_tokens(prompt) + max_output_tokens
    utc_date, now = _now()
    conn = sqlite3.connect(settings.DB_PATH)
    try:
        ensure_llm_quota_tables(conn)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT OR IGNORE INTO llm_usage_model_daily (utc_date, model, updated_at) VALUES (?, ?, ?)",
                     (utc_date, model, now))
        row = conn.execute("SELECT reserved_tokens, actual_total_tokens FROM llm_usage_model_daily WHERE utc_date=? AND model=?",
                           (utc_date, model)).fetchone()
        if row[0] + row[1] + estimate > _cap(settings, model):
            raise RuntimeError(f"{model} UTC 当日额度不足：已使用/预留 {row[0] + row[1]}，本次预留 {estimate}，上限 {_cap(settings, model)}")
        conn.execute("UPDATE llm_usage_model_daily SET reserved_tokens=reserved_tokens+?, updated_at=? WHERE utc_date=? AND model=?",
                     (estimate, now, utc_date, model))
        conn.commit()
        return utc_date, estimate
    finally:
        conn.close()


def reconcile(settings, model: str, reservation: tuple[str, int], usage) -> None:
    """成功返回即用真实 usage 替换预留。调用异常时不调用本函数，预留会保留为安全余量。"""
    utc_date, reserved = reservation
    _today, now = _now()
    inp, out, reasoning, total = usage_totals(usage)
    conn = sqlite3.connect(settings.DB_PATH)
    try:
        ensure_llm_quota_tables(conn)
        conn.execute("UPDATE llm_usage_model_daily SET reserved_tokens=MAX(0, reserved_tokens-?), actual_input_tokens=actual_input_tokens+?, actual_output_tokens=actual_output_tokens+?, actual_reasoning_tokens=actual_reasoning_tokens+?, actual_total_tokens=actual_total_tokens+?, updated_at=? WHERE utc_date=? AND model=?",
                     (reserved, inp, out, reasoning, total, now, utc_date, model))
        conn.commit()
    finally:
        conn.close()


def record_llm_call(settings, call_type: str, model: str, prompt: str,
                    response_text: str = "", stage: str = "", usage=None,
                    cost_seconds: float = 0.0, status: str = "SUCCESS",
                    error_message: str = "", meta: dict = None) -> None:
    """持久化记录每一次 LLM 调用的完整 Prompt、响应、Token 消耗与元数据至 SQLite 数据库。"""
    inp, out, reasoning, total = usage_totals(usage) if usage else (0, 0, 0, 0)
    meta_str = json.dumps(meta, ensure_ascii=False) if meta else "{}"
    conn = sqlite3.connect(settings.DB_PATH)
    try:
        ensure_llm_quota_tables(conn)
        conn.execute("""
            INSERT INTO llm_call_history 
            (call_type, model, stage, prompt, response_text, prompt_tokens, completion_tokens, total_tokens, cost_seconds, status, error_message, meta_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (call_type, model, stage, prompt, response_text, inp, out, total, cost_seconds, status, error_message, meta_str))
        conn.commit()
    except Exception as e:
        # 日志写入失败绝不阻断业务执行
        pass
    finally:
        conn.close()

