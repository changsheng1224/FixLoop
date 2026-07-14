"""CLI ablation 子命令测试。"""

import json
import sys
from pathlib import Path

CASES_DIR = Path(__file__).resolve().parents[1] / "src" / "eval" / "cases"


class TestCliAblation:
    def test_ablation_fake_single_case(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "src.cli",
                "ablation",
                "--fake",
                "--case",
                "case_001",
                "--repetitions",
                "1",
                "--output",
                str(tmp_path / "ablation.json"),
                "--verbose",
            ],
        )
        from src.cli import main

        assert main() == 0
        report_file = tmp_path / "ablation.json"
        assert report_file.is_file()
        data = json.loads(report_file.read_text(encoding="utf-8"))
        assert set(data["summary_by_variant"].keys()) == {"full", "single", "no_retriever", "naive"}
        assert data["summary_by_variant"]["full"]["total"] == 1
        assert len(data["runs"]) == 4
        err = capsys.readouterr().err
        assert "[full]" in err

    def test_ablation_requires_case_or_all(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["src.cli", "ablation"])
        from src.cli import main

        assert main() == 2
