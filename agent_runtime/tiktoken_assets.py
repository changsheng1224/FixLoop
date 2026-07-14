"""Local tiktoken encoding assets bundled with FixLoop."""

from __future__ import annotations

from pathlib import Path

CL100K_BASE_HASH = "223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7"

PACKAGE_ROOT = Path(__file__).resolve().parent
LOCAL_TIKTOKEN_ROOT = PACKAGE_ROOT / "data" / "tiktoken"


def local_tiktoken_file(encoding_name: str) -> Path | None:
    """Return the packaged .tiktoken asset path for a supported encoding."""
    if encoding_name != "cl100k_base":
        return None
    path = LOCAL_TIKTOKEN_ROOT / "cl100k_base.tiktoken"
    return path if path.is_file() else None


def load_local_tiktoken_encoding(encoding_name: str):
    """Build a tiktoken.Encoding from a packaged asset, or return None."""
    path = local_tiktoken_file(encoding_name)
    if path is None:
        return None

    import tiktoken
    from tiktoken.load import load_tiktoken_bpe

    mergeable_ranks = load_tiktoken_bpe(str(path), expected_hash=CL100K_BASE_HASH)
    return tiktoken.Encoding(
        name=encoding_name,
        pat_str=(
            r"""'(?i:[sdmt]|ll|ve|re)|[^\r\n\p{L}\p{N}]?+\p{L}++|"""
            r"""\p{N}{1,3}+| ?[^\s\p{L}\p{N}]++[\r\n]*+|"""
            r"""\s++$|\s*[\r\n]|\s+(?!\S)|\s"""
        ),
        mergeable_ranks=mergeable_ranks,
        special_tokens={
            "<|endoftext|>": 100257,
            "<|fim_prefix|>": 100258,
            "<|fim_middle|>": 100259,
            "<|fim_suffix|>": 100260,
            "<|endofprompt|>": 100276,
        },
    )
