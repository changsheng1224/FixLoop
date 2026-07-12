"""CLI eval 子命令测试。"""

import json
import sys
from pathlib import Path

CASES_DIR = Path(__file__).resolve().parents[1] / "src" / "eval" / "cases"


class TestCliEval:
    def test_eval_fake_single_case(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "src.cli",
                "eval",
                "--fake",
                "--case",
                "case_001",
                "--output",
                str(tmp_path / "report.json"),
                "--verbose",
            ],
        )
        from src.cli import main

        assert main() in (0, 1)
        report_file = tmp_path / "report.json"
        assert report_file.is_file()
        data = json.loads(report_file.read_text(encoding="utf-8"))
        assert data["summary"]["total"] == 1
        assert data["summary"]["fixed"] == 1
        assert data["cases"][0]["case_id"] == "case_001"
        err = capsys.readouterr().err
        assert "[OK] case_001" in err

    def test_eval_fake_all(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "src.cli",
                "eval",
                "--fake",
                "--all",
                "--output",
                str(tmp_path),
            ],
        )
        from src.cli import main

        assert main() in (0, 1)
        data = json.loads((tmp_path / "eval_report.json").read_text(encoding="utf-8"))
        assert data["summary"]["total"] == 18
        assert data["summary"]["fix_rate"] >= 0.9

    def test_eval_requires_case_or_all(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["src.cli", "eval"])
        from src.cli import main

        assert main() == 2

    def test_eval_run_subcommand_fake_all(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "src.cli",
                "eval",
                "run",
                "--fake",
                "--all",
                "--output",
                str(tmp_path),
            ],
        )
        from src.cli import main

        assert main() in (0, 1)
        data = json.loads((tmp_path / "eval_report.json").read_text(encoding="utf-8"))
        assert data["summary"]["total"] == 18

    def test_eval_module_runner_fake(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "src.eval.runner",
                "--fake",
                "--case",
                "case_002",
                "--output",
                str(tmp_path),
            ],
        )
        from src.eval.__main__ import main

        assert main() in (0, 1)
        assert (tmp_path / "eval_report.json").is_file()
