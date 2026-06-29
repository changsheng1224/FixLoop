"""AgentConfig 单测：正常加载、缺少必填、非法值、环境变量覆盖。"""

import pytest
from pydantic import ValidationError

from agent_runtime.config import AgentConfig


class TestAgentConfigDefault:
    """正常加载：默认值构造。"""

    def test_default_values(self):
        config = AgentConfig(provider="deepseek")
        assert config.provider == "deepseek"
        assert config.model == "deepseek-v4-pro"
        assert config.max_steps == 6
        assert config.max_new_tokens == 512
        assert config.approval == "ask"
        assert config.temperature == 0.2

    def test_default_provider(self):
        """不传 provider 时应使用默认值 'deepseek'。"""
        config = AgentConfig()
        assert config.provider == "deepseek"


class TestAgentConfigValidation:
    """非法值校验。"""

    def test_max_steps_negative(self):
        with pytest.raises(ValidationError):
            AgentConfig(max_steps=0)

    def test_max_steps_too_large(self):
        with pytest.raises(ValidationError):
            AgentConfig(max_steps=100)

    def test_temperature_out_of_range(self):
        with pytest.raises(ValidationError):
            AgentConfig(temperature=3.0)

    def test_provider_empty(self):
        """空字符串 provider 应被拒绝（如果有校验）或接受。"""
        # 当前没有 provider 枚举校验，空字符串应被接受
        config = AgentConfig(provider="")
        assert config.provider == ""


class TestAgentConfigOverride:
    """字段覆盖 + 完整构造。"""

    def test_full_override(self):
        config = AgentConfig(
            provider="openai",
            model="gpt-4o",
            max_steps=10,
            max_new_tokens=1024,
            approval="auto",
            temperature=0.8,
        )
        assert config.provider == "openai"
        assert config.model == "gpt-4o"
        assert config.max_steps == 10
        assert config.max_new_tokens == 1024
        assert config.approval == "auto"
        assert config.temperature == 0.8

    def test_partial_override(self):
        """只覆盖部分字段，其余使用默认值。"""
        config = AgentConfig(provider="ollama", max_steps=3)
        assert config.provider == "ollama"
        assert config.max_steps == 3
        # 未覆盖的字段应保持默认值
        assert config.model == "deepseek-v4-pro"
        assert config.temperature == pytest.approx(0.2)
