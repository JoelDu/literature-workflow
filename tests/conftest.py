import os
import pytest

# Ensure safe dummy environment variables for running tests on clean installs
os.environ.setdefault("MINERU_API_KEY", "mock_mineru_api_key_12345")
os.environ.setdefault("OPENAI_API_KEY", "mock_openai_api_key_12345")
os.environ.setdefault("SILICONFLOW_API_KEY", "mock_siliconflow_api_key_12345")
os.environ.setdefault("SILICONFLOW_API_BASE", "https://api.siliconflow.cn/v1")
