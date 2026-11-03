"""测试一律看不见本机真 DeepSeek key (backend/T-deepseek-model)。

舰队默认模型不再由 key 决定；显式测试 DeepSeek client 时仍须隔离真实 key，
避免本机凭证进入测试进程的数据结构。
pytest 先加载 conftest 再收集测试模块, 所以这里改 os.environ 赶得上导入期。
要测有 key 的路径: 用 monkeypatch 指 FLEET_DEEPSEEK_KEY_FILE 到临时文件 (见 test_deepseek_provider)。
"""
import os
import pytest

os.environ.pop("DEEPSEEK_API_KEY", None)
os.environ.pop("FLEET_DEFAULT_MODEL", None)
os.environ["FLEET_DEEPSEEK_KEY_FILE"] = "/nonexistent/fleet-tests/deepseek_api_key"


@pytest.fixture
def claude_default(monkeypatch):
    """Pin Claude for tests of its SDK and Claude-to-Codex transitions."""
    from feishu_bridge import fleet_bridge
    monkeypatch.setattr(fleet_bridge, "DEFAULT_MODEL", "claude-opus-5")
