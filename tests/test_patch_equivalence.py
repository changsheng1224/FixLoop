"""patch_equivalence 进 eval_report 单测。"""


class TestPatchEquivalence:
    def test_full_equivalence_same_files(self):
        from src.eval.patch_utils import patch_equivalence

        actual = "--- a/calc.py\n+++ b/calc.py\n@@ -1 +1 @@\n-return a+b\n+return int(a)+int(b)\n"
        expected = "--- a/calc.py\n+++ b/calc.py\n@@ -1 +1 @@\n-return a+b\n+return int(a)+int(b)\n"
        assert patch_equivalence(actual, expected) == "full"

    def test_none_no_common_files(self):
        from src.eval.patch_utils import patch_equivalence

        assert patch_equivalence(
            "--- a/calc.py\n+++ b/calc.py\n", "--- a/other.py\n+++ b/other.py\n"
        ) == "none"

    def test_partial_overlapping_files(self):
        from src.eval.patch_utils import patch_equivalence

        assert patch_equivalence(
            "--- a/a.py\n+++ b/a.py\n--- a/b.py\n+++ b/b.py\n",
            "--- a/a.py\n+++ b/a.py\n"
        ) == "partial"

    def test_empty_diffs_none(self):
        from src.eval.patch_utils import patch_equivalence

        assert patch_equivalence("", "") == "none"


class TestCaseResultEquivalence:
    def test_equivalence_field_default(self):
        from src.eval.models import CaseResult

        cr = CaseResult(case_id="test")
        assert cr.equivalence == ""

    def test_equivalence_in_to_dict(self):
        from src.eval.models import CaseResult

        cr = CaseResult(case_id="test", equivalence="full")
        d = cr.to_dict()
        assert d["equivalence"] == "full"


class TestEquivalenceInMetrics:
    def test_summary_includes_equivalence(self):
        from src.eval.metrics import _summary_metrics
        from src.eval.models import CaseResult

        results = [
            CaseResult(case_id="c1", fixed=True, equivalence="full"),
            CaseResult(case_id="c2", fixed=True, equivalence="partial"),
            CaseResult(case_id="c3", fixed=False, equivalence="none"),
        ]
        summary = _summary_metrics(results)
        eq = summary["equivalence_by_type"]
        assert eq["full"] == 1
        assert eq["partial"] == 1
        assert eq["none"] == 1
        assert summary["avg_equivalence_full_rate"] == round(1 / 3, 4)
