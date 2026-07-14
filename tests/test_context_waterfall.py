"""Context waterfall 报告单测。"""

from src.repair.context_waterfall import (
    build_context_waterfall,
    waterfall_to_ascii,
)


def _make_agent_report(**sections: int) -> dict:
    """生成含 context_summary.sections 的 agent report。"""
    return {
        "total_tokens": sum(sections.values()),
        "context_summary": {
            "sections": dict(sections),
            "build_count": 1,
        },
    }


class TestBuildContextWaterfall:
    def test_empty_reports_returns_empty(self):
        wf = build_context_waterfall({})
        assert wf == {}

    def test_single_agent_waterfall(self):
        reports = {
            "localizer": _make_agent_report(system=200, tools=150, request=100),
        }
        wf = build_context_waterfall(reports)
        assert "localizer" in wf
        entries = wf["localizer"]
        assert len(entries) == 3
        # pct 和 ≈ 100
        pct_sum = sum(e["pct"] for e in entries)
        assert 99.0 <= pct_sum <= 101.0, f"pct sum={pct_sum}"

    def test_multi_agent_waterfall_pct_sum(self):
        reports = {
            "localizer": _make_agent_report(
                system=300, tools=200, skills=100, memory=80, history=400, request=150
            ),
            "retriever": _make_agent_report(
                system=250, tools=180, skills=90, history=300, request=120
            ),
        }
        wf = build_context_waterfall(reports)
        assert "localizer" in wf
        assert "retriever" in wf

        # 每个 agent 的 pct 和 ≈ 100
        for agent in ("localizer", "retriever"):
            entries = wf[agent]
            pct_sum = sum(e["pct"] for e in entries)
            assert 99.0 <= pct_sum <= 101.0, (
                f"{agent} pct sum={pct_sum}, entries={entries}"
            )

    def test_totals_aggregated(self):
        reports = {
            "agent_a": _make_agent_report(system=200, request=100),
            "agent_b": _make_agent_report(system=300, request=150),
        }
        wf = build_context_waterfall(reports)
        assert "_totals" in wf

        totals = {e["section"]: e["tokens"] for e in wf["_totals"]}
        assert totals["system"] == 500  # 200 + 300
        assert totals["request"] == 250  # 100 + 150

        pct_sum = sum(e["pct"] for e in wf["_totals"])
        assert 99.0 <= pct_sum <= 101.0

    def test_entries_sorted_by_tokens_desc(self):
        reports = {
            "agent": _make_agent_report(history=500, system=200, request=100, memory=50),
        }
        wf = build_context_waterfall(reports)
        tokens = [e["tokens"] for e in wf["agent"]]
        assert tokens == sorted(tokens, reverse=True)

    def test_sections_with_zero_tokens_excluded(self):
        reports = {
            "agent": _make_agent_report(system=200, knowledge=0, tools=0, request=100),
        }
        wf = build_context_waterfall(reports)
        section_names = {e["section"] for e in wf["agent"]}
        assert "knowledge" not in section_names
        assert "tools" not in section_names
        assert "system" in section_names

    def test_fallback_to_token_usage_sections(self):
        """无 context_summary 时回退到 token_usage.sections。"""
        report = {
            "token_usage": {
                "sections": {"system": 300, "request": 150},
            },
            "total_tokens": 450,
        }
        reports = {"agent": report}
        wf = build_context_waterfall(reports)
        assert len(wf["agent"]) == 2

    def test_waterfall_keys_are_present(self):
        """waterfall 每个 entry 含 section/tokens/pct 三键。"""
        reports = {"agent": _make_agent_report(system=100, request=50)}
        wf = build_context_waterfall(reports)
        for entry in wf["agent"]:
            assert "section" in entry
            assert "tokens" in entry
            assert "pct" in entry
            assert isinstance(entry["tokens"], int)
            assert isinstance(entry["pct"], float)


class TestWaterfallToAscii:
    def test_renders_agent_sections(self):
        reports = {"patcher": _make_agent_report(system=200, request=100)}
        wf = build_context_waterfall(reports)
        text = waterfall_to_ascii(wf)
        assert "[patcher]" in text
        assert "system" in text
        assert "request" in text
        assert "█" in text  # ASCII bar

    def test_empty_waterfall_returns_placeholder(self):
        text = waterfall_to_ascii({})
        assert "no context waterfall" in text

    def test_ascii_includes_totals(self):
        reports = {"agent": _make_agent_report(system=100)}
        wf = build_context_waterfall(reports)
        text = waterfall_to_ascii(wf)
        # 含 totals agent 时显示
        assert "TOTAL" in text
