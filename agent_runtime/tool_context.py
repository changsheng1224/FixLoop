"""工具上下文：提供路径解析、逃逸检测、Shell 环境等能力。

所有工具函数通过 ToolContext 获取 workspace 信息，不直接访问 Agent 内部状态。
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from agent_runtime.path_safety import resolve_under_root


@dataclass
class ToolContext:
    """工具执行上下文。

    Attributes:
        root: workspace 根目录（绝对路径），所有文件访问必须在此目录内。
        path_resolver: 将相对路径解析为绝对路径的函数。
        shell_env_provider: 返回安全环境变量 dict 的可调用对象（M2 使用）。
    """

    root: str
    path_resolver: Callable[[str], Path] = field(default=None)
    shell_env_provider: Callable[[], dict] | None = None

    def __post_init__(self):
        if self.path_resolver is None:
            self.path_resolver = self._default_resolve

    def resolve(self, raw_path: str) -> Path:
        """将用户提供的路径解析为绝对路径，并进行逃逸检测。

        Args:
            raw_path: 用户/模型提供的原始路径（可为相对或绝对路径）。

        Returns:
            解析后的绝对 Path 对象，保证在 workspace root 内。

        Raises:
            ValueError: 路径逃逸到 workspace root 之外时抛出。
        """
        return self.path_resolver(raw_path)

    def _default_resolve(self, raw_path: str) -> Path:
        """默认路径解析器：分量遍历 + symlink 校验 + canonical 边界检测。"""
        return resolve_under_root(self.root, raw_path)
