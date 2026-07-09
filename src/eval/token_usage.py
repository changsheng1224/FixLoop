"""评测 token 用量收集：合并 Agent run 报告与 ModelClient session 统计。"""

from __future__ import annotations

import json
from pathlib import Path

from agent_runtime.token_accounting import merge_session_snapshots, snapshot_from_session


def _empty_usage() -> dict:
    return {
        "total_tokens": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "cache_hit_rate": 0.0,
        "api_calls": 0,
        "estimated_total": 0,
        "estimated_sections": {},
        "run_count": 0,
    }


def _merge_sections(target: dict, source: dict) -> None:
    for key, value in source.items():
        if isinstance(value, int | float):
            target[key] = target.get(key, 0) + int(value)


def collect_repo_token_reports(repo: Path, since_ts: float | None = None) -> dict:
    """汇总 repo 内 `.agent/runs/*/report.json` 的估算 token。"""
    runs_dir = repo / ".agent" / "runs"
    if not runs_dir.is_dir():
        return {"estimated_total": 0, "estimated_sections": {}, "run_count": 0}

    estimated_total = 0
    estimated_sections: dict = {}
    run_count = 0
    for run_dir in sorted(runs_dir.iterdir()):
        if not run_dir.is_dir():
            continue
        report_path = run_dir / "report.json"
        if not report_path.is_file():
            continue
        if since_ts is not None and report_path.stat().st_mtime < since_ts:
            continue
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        run_count += 1
        estimated_total += int(report.get("total_tokens", 0) or 0)
        usage = report.get("token_usage")
        if isinstance(usage, dict):
            _merge_sections(estimated_sections, usage)

    return {
        "estimated_total": estimated_total,
        "estimated_sections": estimated_sections,
        "run_count": run_count,
    }


def get_client_session_usage(model_client) -> dict:
    """读取 model_client.session_usage 并归一化为 token 统计 dict。"""
    usage = getattr(model_client, "session_usage", None) or {}
    return snapshot_from_session(usage)


def resolve_model_clients(*agents) -> list:
    """从 Agent 列表去重收集 model_client 实例。"""
    clients = []
    seen: set[int] = set()
    for agent in agents:
        if agent is None:
            continue
        client = getattr(agent, "model_client", None)
        if client is None or id(client) in seen:
            continue
        seen.add(id(client))
        clients.append(client)
    return clients


def resolve_model_client(*agents):
    """返回第一个 Agent 绑定的 model_client，无则 None。"""
    clients = resolve_model_clients(*agents)
    return clients[0] if clients else None


def reset_clients_session_usage(*agents) -> None:
    """重置所有 Agent 关联 client 的 session 用量计数。"""
    for client in resolve_model_clients(*agents):
        reset_client_session_usage(client)


def collect_agent_reports_from_run(run_dir: Path) -> dict:
    """从共享 run 目录读取 agent_report.*.json，按 Agent 名汇总 token。"""
    if not run_dir.is_dir():
        return {}
    result: dict = {}
    for path in sorted(run_dir.glob("agent_report.*.json")):
        name = path.stem.removeprefix("agent_report.")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        result[name] = {
            "total_tokens": int(data.get("total_tokens", 0) or 0),
            "input_tokens": int(data.get("input_tokens", 0) or 0),
            "output_tokens": int(data.get("output_tokens", 0) or 0),
            "cache_read_tokens": int(data.get("cache_read_tokens", 0) or 0),
            "cache_creation_tokens": int(data.get("cache_creation_tokens", 0) or 0),
            "cache_hit_rate": float(data.get("cache_hit_rate", 0) or 0),
            "api_calls": int(data.get("api_calls", 0) or 0),
            "token_usage": data.get("token_usage") or {},
            "tool_steps": int(data.get("tool_steps", 0) or 0),
        }
    return result


def diff_client_usage(before: dict, after: dict) -> dict:
    """计算两次 client session 快照的增量。"""
    inp = max(0, after.get("input_tokens", 0) - before.get("input_tokens", 0))
    out = max(0, after.get("output_tokens", 0) - before.get("output_tokens", 0))
    calls = max(0, after.get("api_calls", 0) - before.get("api_calls", 0))
    return {
        "input_tokens": inp,
        "output_tokens": out,
        "total_tokens": inp + out,
        "api_calls": calls,
    }


def summarize_agent_tool_usage(by_agent: dict) -> dict:
    """从 token_usage_by_agent 提取各 Agent 工具调用次数。"""
    tool_by_agent = {
        name: int(info.get("tool_steps", 0) or 0) for name, info in by_agent.items()
    }
    return {
        "tool_usage_by_agent": tool_by_agent,
        "total_tool_steps": sum(tool_by_agent.values()),
    }


def build_repair_token_usage(
    model_clients: list,
    repo: Path,
    since_ts: float | None = None,
    repair_run_id: str | None = None,
) -> dict:
    """合并多个 Agent 共享/独立 client 的 API 用量与 run 报告。"""
    by_agent: dict = {}
    if repair_run_id:
        by_agent = collect_agent_reports_from_run(repo / ".agent" / "runs" / repair_run_id)

    reports = collect_repo_token_reports(repo, since_ts=since_ts)
    client_snaps = [get_client_session_usage(client) for client in model_clients]
    api_summary = merge_session_snapshots(*client_snaps) if client_snaps else _empty_usage()
    input_tokens = api_summary["input_tokens"]
    output_tokens = api_summary["output_tokens"]
    api_calls = api_summary["api_calls"]
    cache_read_tokens = api_summary["cache_read_tokens"]
    cache_creation_tokens = api_summary["cache_creation_tokens"]
    cache_hit_rate = api_summary["cache_hit_rate"]

    total_tokens = api_summary["total_tokens"]
    if total_tokens == 0 and by_agent:
        total_tokens = sum(v.get("total_tokens", 0) for v in by_agent.values())
    if total_tokens == 0:
        total_tokens = reports["estimated_total"]

    tool_summary = summarize_agent_tool_usage(by_agent)
    return {
        "total_tokens": total_tokens,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_creation_tokens": cache_creation_tokens,
        "cache_hit_rate": cache_hit_rate,
        "api_calls": api_calls,
        "estimated_total": reports["estimated_total"],
        "estimated_sections": reports["estimated_sections"],
        "run_count": reports["run_count"],
        "token_usage_by_agent": by_agent,
        **tool_summary,
    }


def build_token_usage_summary(model_client, repo: Path, since_ts: float | None = None) -> dict:
    """合并 API 实际用量（client.session_usage）与 Agent run 估算明细。"""
    return build_repair_token_usage([model_client], repo, since_ts=since_ts)


def reset_client_session_usage(model_client) -> None:
    """调用 client.reset_session_usage()（若存在）。"""
    reset = getattr(model_client, "reset_session_usage", None)
    if callable(reset):
        reset()
