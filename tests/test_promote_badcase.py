"""badcase 晋升脚本单测：fixture run → case 骨架生成。"""

import json
import tempfile
from pathlib import Path


class TestPromoteBadcase:
    def test_generate_skeleton_from_fake_run(self, tmp_path):
        from scripts.promote_badcase import generate_case_skeleton, next_case_id

        # 创建 fake run
        run_dir = tmp_path / "runs" / "fake-001"
        run_dir.mkdir(parents=True)
        (run_dir / "task_state.json").write_text(
            json.dumps({"user_request": "TypeError at calc.py:42", "status": "failed"}),
            encoding="utf-8",
        )
        (run_dir / "report.json").write_text(
            json.dumps({"status": "failed"}),
            encoding="utf-8",
        )

        output_dir = tmp_path / "cases"
        output_dir.mkdir()
        result = generate_case_skeleton(run_dir, output_dir, dry_run=False)

        assert result is not None
        assert (result / "issue.txt").is_file()
        assert (result / "metadata.yaml").is_file()
        assert (result / "repo").is_dir()

        # 检查 metadata
        import yaml
        meta = yaml.safe_load((result / "metadata.yaml").read_text(encoding="utf-8"))
        assert meta["issue_type"] == "type_error"

    def test_dry_run_does_not_create(self, tmp_path):
        from scripts.promote_badcase import generate_case_skeleton

        run_dir = tmp_path / "runs" / "dry-001"
        run_dir.mkdir(parents=True)
        (run_dir / "task_state.json").write_text(
            json.dumps({"user_request": "error", "status": "failed"}),
            encoding="utf-8",
        )

        output_dir = tmp_path / "cases"
        result = generate_case_skeleton(run_dir, output_dir, dry_run=True)
        assert result is None
        assert not list(output_dir.glob("case_*"))
