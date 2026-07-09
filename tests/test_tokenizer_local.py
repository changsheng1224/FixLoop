"""DeepSeek 本地 tokenizer 与加载路径单测。"""

from pathlib import Path

import pytest

from agent_runtime.tokenizer_assets import (
    DEFAULT_DEEPSEEK_TOKENIZER_REPO,
    download_tokenizer,
    is_tokenizer_cached_locally,
    local_tokenizer_dir,
    local_tokenizer_json,
    local_tokenizer_root,
    repo_id_to_dirname,
)
from agent_runtime.tokenizers import clear_token_counter_cache, resolve_token_counter


@pytest.fixture(autouse=True)
def _clear_counter_cache():
    clear_token_counter_cache()
    yield
    clear_token_counter_cache()


class TestTokenizerAssetsPaths:
    def test_repo_id_to_dirname(self):
        assert repo_id_to_dirname("deepseek-ai/deepseek-llm-7b-chat") == (
            "deepseek-ai--deepseek-llm-7b-chat"
        )

    def test_local_paths_under_package_data(self):
        root = local_tokenizer_root()
        assert root.name == "tokenizers"
        assert root.parent.name == "data"
        assert root.parent.parent.name == "agent_runtime"


class TestLocalDeepseekTokenizer:
    def test_default_tokenizer_cached_in_repo(self):
        """仓库应自带默认 DeepSeek tokenizer（scripts/download_deepseek_tokenizer.py）。"""
        assert is_tokenizer_cached_locally(DEFAULT_DEEPSEEK_TOKENIZER_REPO), (
            "缺少本地 tokenizer；请运行: python scripts/download_deepseek_tokenizer.py"
        )
        path = local_tokenizer_json(DEFAULT_DEEPSEEK_TOKENIZER_REPO)
        assert path is not None
        assert path.stat().st_size > 1000

    def test_resolve_counter_uses_local_backend(self):
        counter = resolve_token_counter("deepseek-v4-pro", "deepseek")
        assert counter.backend.startswith("huggingface-local:")
        assert "deepseek-llm-7b-chat" in counter.backend

    def test_local_deepseek_counts_without_network(self, monkeypatch):
        monkeypatch.setenv("HF_HUB_OFFLINE", "1")
        counter = resolve_token_counter("deepseek-v4-pro", "deepseek")
        assert counter.count("你好世界") > 0

    def test_download_tokenizer_to_tmp(self, tmp_path, monkeypatch):
        """下载逻辑：写入 tmp 目录且含 tokenizer.json。"""

        def _fake_download(repo_id: str, filename: str, dest: Path) -> None:
            if filename == "tokenizer.json":
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text('{"version":"1.0","truncation":null,"padding":null,"added_tokens":[]}', encoding="utf-8")

        monkeypatch.setattr(
            "agent_runtime.tokenizer_assets._download_file",
            _fake_download,
        )
        target = download_tokenizer("fake/deepseek-test", root=tmp_path)
        assert (target / "tokenizer.json").is_file()
        assert (target / "manifest.json").is_file()
