"""DeepSeek 等 HuggingFace tokenizer 本地资源路径与下载。"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

from agent_runtime.logging_setup import get_logger

log = get_logger("tokenizer_assets")

PACKAGE_ROOT = Path(__file__).resolve().parent
DEFAULT_LOCAL_TOKENIZER_ROOT = PACKAGE_ROOT / "data" / "tokenizers"
LOCAL_TOKENIZER_ROOT_ENV = "FIXLOOP_TOKENIZER_DIR"

# resolve_deepseek_tokenizer_id 默认映射
DEFAULT_DEEPSEEK_TOKENIZER_REPO = "deepseek-ai/deepseek-llm-7b-chat"
DEEPSEEK_TOKENIZER_FILES = ("tokenizer.json", "tokenizer_config.json")

DEFAULT_HF_ENDPOINT = "https://huggingface.co"


def local_tokenizer_root() -> Path:
    override = os.environ.get(LOCAL_TOKENIZER_ROOT_ENV, "").strip()
    if override:
        return Path(override)
    return DEFAULT_LOCAL_TOKENIZER_ROOT


def repo_id_to_dirname(repo_id: str) -> str:
    return repo_id.replace("/", "--")


def local_tokenizer_dir(repo_id: str) -> Path:
    return local_tokenizer_root() / repo_id_to_dirname(repo_id)


def local_tokenizer_json(repo_id: str) -> Path | None:
    """本地 tokenizer.json 路径；不存在则返回 None。"""
    path = local_tokenizer_dir(repo_id) / "tokenizer.json"
    return path if path.is_file() else None


def is_tokenizer_cached_locally(repo_id: str) -> bool:
    return local_tokenizer_json(repo_id) is not None


def _hf_endpoint() -> str:
    return os.environ.get("HF_ENDPOINT", DEFAULT_HF_ENDPOINT).rstrip("/")


def _download_file(repo_id: str, filename: str, dest: Path) -> None:
    url = f"{_hf_endpoint()}/{repo_id}/resolve/main/{filename}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "FixLoop-tokenizer-download/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            dest.write_bytes(resp.read())
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            log.warning("跳过缺失文件 %s/%s", repo_id, filename)
            return
        raise


def download_tokenizer(repo_id: str, *, root: Path | None = None) -> Path:
    """下载 tokenizer.json（及可选 tokenizer_config.json）到本地目录。"""
    target = (root or local_tokenizer_root()) / repo_id_to_dirname(repo_id)
    target.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, str] = {"repo_id": repo_id, "files": {}}

    for name in DEEPSEEK_TOKENIZER_FILES:
        dest = target / name
        _download_file(repo_id, name, dest)
        if dest.is_file():
            manifest["files"][name] = str(dest.relative_to(target))

    (target / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if not (target / "tokenizer.json").is_file():
        raise RuntimeError(f"未能下载 {repo_id} 的 tokenizer.json（请检查网络或 HF_ENDPOINT）")
    log.info("已缓存 tokenizer: %s -> %s", repo_id, target)
    return target
