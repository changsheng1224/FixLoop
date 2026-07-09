"""统一 token 会计：Provider usage 归一化、session 快照、report.json 字段。"""

from __future__ import annotations


def empty_session_usage() -> dict:
    """ModelClient.session_usage 的零值模板。"""
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "calls": 0,
    }


def parse_provider_usage(usage: dict | None) -> dict:
    """将 Provider 原始 usage 归一化为统一字段。"""
    if not usage:
        return {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
        }
    cache_read = int(
        usage.get("cache_read_input_tokens", 0)
        or usage.get("cache_read_tokens", 0)
        or usage.get("prompt_cache_hit_tokens", 0)
        or 0
    )
    cache_creation = int(
        usage.get("cache_creation_input_tokens", 0)
        or usage.get("cache_creation_tokens", 0)
        or usage.get("prompt_cache_miss_tokens", 0)
        or 0
    )
    return {
        "input_tokens": int(usage.get("input_tokens", 0) or 0),
        "output_tokens": int(usage.get("output_tokens", 0) or 0),
        "cache_read_tokens": cache_read,
        "cache_creation_tokens": cache_creation,
    }


def compute_cache_hit_rate(cache_read: int, cache_creation: int) -> float:
    """Prompt cache 命中率。"""
    denom = int(cache_read or 0) + int(cache_creation or 0)
    if denom <= 0:
        return 0.0
    return round(int(cache_read or 0) / denom, 4)


def snapshot_from_session(session_usage: dict | None) -> dict:
    """从 ModelClient.session_usage 生成可写入 report 的快照。"""
    usage = session_usage or {}
    inp = int(usage.get("input_tokens", 0) or 0)
    out = int(usage.get("output_tokens", 0) or 0)
    cache_read = int(usage.get("cache_read_tokens", 0) or 0)
    cache_creation = int(usage.get("cache_creation_tokens", 0) or 0)
    return {
        "input_tokens": inp,
        "output_tokens": out,
        "total_tokens": inp + out,
        "cache_read_tokens": cache_read,
        "cache_creation_tokens": cache_creation,
        "cache_hit_rate": compute_cache_hit_rate(cache_read, cache_creation),
        "api_calls": int(usage.get("calls", 0) or 0),
    }


def merge_session_snapshots(*snapshots: dict) -> dict:
    """合并多个 client / Agent 的 session 快照。"""
    inp = out = cache_read = cache_creation = api_calls = 0
    for snap in snapshots:
        inp += int(snap.get("input_tokens", 0) or 0)
        out += int(snap.get("output_tokens", 0) or 0)
        cache_read += int(snap.get("cache_read_tokens", 0) or 0)
        cache_creation += int(snap.get("cache_creation_tokens", 0) or 0)
        api_calls += int(snap.get("api_calls", 0) or 0)
    return {
        "input_tokens": inp,
        "output_tokens": out,
        "total_tokens": inp + out,
        "cache_read_tokens": cache_read,
        "cache_creation_tokens": cache_creation,
        "cache_hit_rate": compute_cache_hit_rate(cache_read, cache_creation),
        "api_calls": api_calls,
    }


def build_report_token_fields(
    session_usage: dict | None,
    context_meta: dict | None = None,
) -> dict:
    """合并 API session 累计与 Context 投影元数据，供 report.json 写入。"""
    api = snapshot_from_session(session_usage)
    meta = context_meta or {}
    sections = dict(meta.get("sections") or {})
    estimated_total = int(meta.get("total_tokens", 0) or 0)
    total_tokens = api["total_tokens"] or estimated_total
    return {
        "total_tokens": total_tokens,
        "input_tokens": api["input_tokens"],
        "output_tokens": api["output_tokens"],
        "cache_read_tokens": api["cache_read_tokens"],
        "cache_creation_tokens": api["cache_creation_tokens"],
        "cache_hit_rate": api["cache_hit_rate"],
        "api_calls": api["api_calls"],
        "token_usage": sections,
        "prompt_budget": meta.get("prompt_budget"),
        "budget_cuts": list(meta.get("cuts") or []),
    }
