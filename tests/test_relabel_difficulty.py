"""难度重标定脚本单测。"""

import json

import yaml


class TestRelabelDifficulty:
    def test_compute_difficulty(self):
        from scripts.relabel_case_difficulty import compute_difficulty

        assert compute_difficulty(1.0) == "easy"
        assert compute_difficulty(0.75) == "medium"
        assert compute_difficulty(0.3) == "hard"

    def test_relabel_dry_run(self, tmp_path):
        from scripts.relabel_case_difficulty import relabel

        report = tmp_path / "eval_report.json"
        report.write_text(
            json.dumps(
                {
                    "by_case": {
                        "case_001": {"fix_rate": 0.95},
                        "case_005": {"fix_rate": 0.55},
                    }
                }
            )
        )

        cases = tmp_path / "cases"
        for cid in ("case_001", "case_005"):
            d = cases / cid
            d.mkdir(parents=True)
            (d / "metadata.yaml").write_text(yaml.dump({"difficulty": "medium"}))

        changes = relabel(report, cases, dry_run=True)
        assert "case_001" in changes
        assert "easy" in changes["case_001"]
        assert "hard" in changes["case_005"]

        # dry_run 不修改文件
        meta = yaml.safe_load((cases / "case_001" / "metadata.yaml").read_text())
        assert meta["difficulty"] == "medium"
