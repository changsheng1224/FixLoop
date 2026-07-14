"""意图对抗 case_adv_ambiguous 单测：模糊 issue → exhausted。"""

import pytest
import yaml
from pathlib import Path


class TestAdvAmbiguousCase:
    @pytest.fixture
    def case_dir(self):
        return Path(__file__).parent.parent / "src" / "eval" / "cases" / "case_adv_ambiguous_001"

    def test_case_exists(self, case_dir):
        assert case_dir.is_dir()
        assert (case_dir / "issue.txt").is_file()
        assert (case_dir / "metadata.yaml").is_file()

    def test_metadata_expected_exhausted(self, case_dir):
        meta = yaml.safe_load((case_dir / "metadata.yaml").read_text(encoding="utf-8"))
        assert meta["expected_outcome"] == "exhausted"
        assert meta["issue_type"] == "unknown"

    def test_issue_is_ambiguous(self, case_dir):
        issue = (case_dir / "issue.txt").read_text(encoding="utf-8")
        # 不包含具体文件名、堆栈、错误类型
        assert "TypeError" not in issue
        assert "ImportError" not in issue
        assert 'File "' not in issue

    def test_case_is_loadable_by_eval(self, case_dir):
        from src.eval.case_io import load_case_metadata

        meta = load_case_metadata(case_dir)
        assert meta is not None
        assert meta.get("issue_type") == "unknown"
