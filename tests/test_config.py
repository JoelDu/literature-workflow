"""配置与凭据校验边界：纯本地命令可启动，网络客户端在使用时才校验。"""

import pytest

import utils
from llm_router import LLMRouter, make_deepseek_client
from mineru_client import MinerUClient


_CREDENTIAL_VARS = (
    "MINERU_API_KEY",
    "DEEPSEEK_API_KEY",
    "GEMINI_API_KEY",
    "SILICONFLOW_API_KEY",
    "SILICONFLOW_API_BASE",
)


def _clear_credentials(monkeypatch):
    for name in _CREDENTIAL_VARS:
        monkeypatch.delenv(name, raising=False)


def test_settings_load_without_api_credentials(monkeypatch):
    """status/history/types 只读本地数据，不应被外部服务凭据挡住。"""
    _clear_credentials(monkeypatch)
    monkeypatch.setattr(utils, "_settings", None)

    settings = utils.get_settings()

    assert settings.MINERU_API_KEY == ""
    assert settings.DEEPSEEK_API_KEY == ""
    assert settings.GEMINI_API_KEY == ""
    assert settings.LOCAL_EMBEDDING_MODEL_PATH == ""


def test_network_clients_validate_credentials_when_created(monkeypatch):
    _clear_credentials(monkeypatch)

    with pytest.raises(ValueError, match="MINERU_API_KEY"):
        MinerUClient("")

    with pytest.raises(ValueError, match="SILICONFLOW_API_KEY"):
        make_deepseek_client(quiet=True)


def test_gemini_is_optional_for_current_llm_route(monkeypatch):
    _clear_credentials(monkeypatch)

    router = LLMRouter(deepseek_api_key="test-only-deepseek-key")

    assert router.gemini_client is None
