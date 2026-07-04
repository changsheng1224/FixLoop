"""评测 token 用量收集：合并 Agent run 报告与 ModelClient session 统计。"""

from __future__ import annotations

import json
from pathlib import Path


def _empty_usage() -> dict:
    return {
        "total_tokens": 0,
        "input_tokens": 0,
        "output_tokens": 0,
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
    inp = int(usage.get("input_tokens", 0) or 0)
    out = int(usage.get("output_tokens", 0) or 0)
    return {
        "input_tokens": inp,
        "output_tokens": out,
        "total_tokens": inp + out,
        "api_calls": int(usage.get("calls", 0) or 0),
    }


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


def build_repair_token_usage(
    model_clients: list,
    repo: Path,
    since_ts: float | None = None,
) -> dict:
    """合并多个 Agent 共享/独立 client 的 API 用量与 run 报告。"""
    reports = collect_repo_token_reports(repo, since_ts=since_ts)
    input_tokens = 0
    output_tokens = 0
    api_calls = 0
    for client in model_clients:
        api = get_client_session_usage(client)
        input_tokens += api["input_tokens"]
        output_tokens += api["output_tokens"]
        api_calls += api["api_calls"]

    total_tokens = input_tokens + output_tokens
    if total_tokens == 0:
        total_tokens = reports["estimated_total"]

    return {
        "total_tokens": total_tokens,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "api_calls": api_calls,
        "estimated_total": reports["estimated_total"],
        "estimated_sections": reports["estimated_sections"],
        "run_count": reports["run_count"],
    }


def build_token_usage_summary(model_client, repo: Path, since_ts: float | None = None) -> dict:
    """合并 API 实际用量（client.session_usage）与 Agent run 估算明细。"""
    return build_repair_token_usage([model_client], repo, since_ts=since_ts)


def reset_client_session_usage(model_client) -> None:
    """调用 client.reset_session_usage()（若存在）。"""
    reset = getattr(model_client, "reset_session_usage", None)
    if callable(reset):
        reset()
