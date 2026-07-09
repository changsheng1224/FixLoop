#!/usr/bin/env python3
"""下载 DeepSeek tokenizer 到 agent_runtime/data/tokenizers/（离线可用）。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_runtime.tokenizer_assets import (  # noqa: E402
    DEFAULT_DEEPSEEK_TOKENIZER_REPO,
    download_tokenizer,
    local_tokenizer_root,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="下载 DeepSeek HF tokenizer 到本地")
    parser.add_argument(
        "--repo",
        default=DEFAULT_DEEPSEEK_TOKENIZER_REPO,
        help=f"HuggingFace repo id（默认 {DEFAULT_DEEPSEEK_TOKENIZER_REPO}）",
    )
    parser.add_argument(
        "--output",
        default="",
        help="输出根目录（默认 agent_runtime/data/tokenizers）",
    )
    args = parser.parse_args()
    root = Path(args.output) if args.output else None
    target = download_tokenizer(args.repo, root=root)
    print(f"OK: {target}")
    print(f"根目录: {local_tokenizer_root() if root is None else root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
