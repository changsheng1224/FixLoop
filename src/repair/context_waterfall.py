"""Context waterfall 报告：从 agent report 提取 section token 分布。

每个 Agent 最近一次 context_built 的 section 分布 → 汇总为 waterfall。
CLI --verbose 可打印 ASCII 条。
"""

from __future__ import annotations


def build_context_waterfall(
    agent_reports: dict[str, dict],
) -> dict[str, list[dict]]:
    """从多 Agent 的 report 构建 context waterfall。

    Returns:
        {"by_agent": {"localizer": [{section, tokens, pct}, ...], ...},
         "totals": {"section_name": total_tokens, ...}}
    """
    by_agent: dict[str, list[dict]] = {}
    totals: dict[str, int] = {}

    for agent_name, report in agent_reports.items():
        sections = _extract_sections(report)
        if not sections:
            continue

        total = sum(sections.values())
        if total <= 0:
            continue

        entries: list[dict] = []
        for name, tokens in sections.items():
            entries.append(
                {
                    "section": name,
                    "tokens": tokens,
                    "pct": round(tokens / total * 100, 1),
                }
            )
        entries.sort(key=lambda e: e["tokens"], reverse=True)
        by_agent[agent_name] = entries

        for name, tokens in sections.items():
            totals[name] = totals.get(name, 0) + tokens

    # 总计排序
    if totals:
        grand_total = sum(totals.values())
        totals_entry = []
        for name, tokens in sorted(totals.items(), key=lambda x: x[1], reverse=True):
            totals_entry.append(
                {
                    "section": name,
                    "tokens": tokens,
                    "pct": round(tokens / grand_total * 100, 1),
                }
            )
        by_agent["_totals"] = totals_entry

    return by_agent


def _extract_sections(report: dict) -> dict[str, int]:
    """从单个 agent report 提取 section → token 映射。"""
    # 优先从 context_summary.sections
    ctx = report.get("context_summary") or {}
    sections = ctx.get("sections")
    if isinstance(sections, dict) and sections:
        return {k: int(v) for k, v in sections.items() if int(v) > 0}

    # fallback: token_usage.sections
    tu = report.get("token_usage") or {}
    sections = tu.get("sections")
    if isinstance(sections, dict) and sections:
        return {k: int(v) for k, v in sections.items() if int(v) > 0}

    # fallback: sections 直接在 report 顶层
    sections = report.get("sections")
    if isinstance(sections, dict) and sections:
        return {k: int(v) for k, v in sections.items() if int(v) > 0}

    return {}


def waterfall_to_ascii(waterfall: dict[str, list[dict]]) -> str:
    """将 build_context_waterfall() 输出渲染为 ASCII 条形图。"""
    if not waterfall:
        return "(no context waterfall data)"

    lines = ["Context Waterfall (tokens per section)", "=" * 50]
    max_bar = 40

    for agent_name, entries in waterfall.items():
        if agent_name.startswith("_"):
            continue
        if not entries:
            continue
        total = sum(e["tokens"] for e in entries)
        lines.append(f"\n  [{agent_name}]  total={total} tokens")
        for entry in entries:
            bar_len = max(1, int(entry["pct"] / 100 * max_bar))
            bar = "█" * bar_len
            lines.append(
                f"    {entry['section']:12s} {entry['tokens']:>5d} tok ({entry['pct']:5.1f}%) {bar}"
            )

    # 总计
    totals = waterfall.get("_totals")
    if totals:
        grand = sum(e["tokens"] for e in totals)
        lines.append(f"\n  [TOTAL]  {grand} tokens across all agents")
        for entry in totals:
            bar_len = max(1, int(entry["pct"] / 100 * max_bar))
            bar = "█" * bar_len
            lines.append(
                f"    {entry['section']:12s} {entry['tokens']:>5d} tok ({entry['pct']:5.1f}%) {bar}"
            )

    return "\n".join(lines)
