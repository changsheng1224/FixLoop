"""SWE-bench Lite Adapter 单测（不依赖 HF / Docker / 官方 harness）。"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from src.benchmark.swebench.classify import classify_failure
from src.benchmark.swebench.convert import instance_to_issue
from src.benchmark.swebench.dataset import filter_instances, load_instances_from_jsonl
from src.benchmark.swebench.dev_instances import DEV_INSTANCE_IDS
from src.benchmark.swebench.fake import FakeGoldPatchOrchestrator
from src.benchmark.swebench.manifest import build_manifest, write_manifest
from src.benchmark.swebench.predictions import (
    read_predictions_jsonl,
    validate_prediction,
    write_predictions_jsonl,
)
from src.benchmark.swebench.repo_prep import preflight_repo
from src.benchmark.swebench.runner import AdapterConfig, SweBenchAdapter
from src.benchmark.swebench.types import FailureClass, InstanceResult

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "benchmark"
    / "swebench"
    / "fixtures"
    / "lite_dev5.jsonl"
)


@pytest.fixture
def work_dir():
    """独立临时目录（避免 Windows 上全局 .pytest-tmp 清理失败）。"""
    path = Path(tempfile.mkdtemp(prefix="swebench-adapter-"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


class TestConvertAndDataset:
    def test_fixture_has_five_dev_ids(self):
        instances = load_instances_from_jsonl(FIXTURE)
        assert len(instances) == 5
        assert [i.instance_id for i in instances] == list(DEV_INSTANCE_IDS)

    def test_issue_contains_problem_and_id(self):
        inst = load_instances_from_jsonl(FIXTURE)[0]
        issue = instance_to_issue(inst)
        assert inst.instance_id in issue
        assert "catalog indexing" in issue.lower() or "Bug:" in issue

    def test_filter_preserves_order(self):
        instances = load_instances_from_jsonl(FIXTURE)
        ids = [DEV_INSTANCE_IDS[2], DEV_INSTANCE_IDS[0]]
        got = filter_instances(instances, instance_ids=ids)
        assert [g.instance_id for g in got] == ids


class TestClassify:
    def test_env_over_agent(self):
        fc, _ = classify_failure(env_error="docker missing", agent_error="timeout")
        assert fc == FailureClass.ENV

    def test_empty_patch_is_agent(self):
        fc, detail = classify_failure(model_patch="")
        assert fc == FailureClass.AGENT
        assert detail == "empty_model_patch"

    def test_not_resolved_is_eval(self):
        fc, _ = classify_failure(model_patch="diff --git a/x", resolved=False)
        assert fc == FailureClass.EVAL

    def test_resolved_is_none(self):
        fc, _ = classify_failure(model_patch="diff", resolved=True)
        assert fc == FailureClass.NONE


class TestPredictionsAndManifest:
    def test_prediction_schema(self, work_dir):
        results = [
            InstanceResult(
                instance_id="x__y-1",
                model_name_or_path="fixloop",
                model_patch="diff --git a/a.py b/a.py\n",
            )
        ]
        path = write_predictions_jsonl(work_dir / "p.jsonl", results)
        preds = read_predictions_jsonl(path)
        assert validate_prediction(preds[0]) == []

    def test_manifest_locks_dev_ids(self, work_dir):
        m = build_manifest(
            instance_ids=list(DEV_INSTANCE_IDS),
            model="fake",
            extra={"baseline_policy": "strict_preflight_v1"},
        )
        path = write_manifest(work_dir / "manifest.json", m)
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["dataset"]["instance_ids"] == list(DEV_INSTANCE_IDS)
        assert data["versions"]["adapter"] == "swebench-adapter-v1"
        assert data["extra"]["baseline_policy"] == "strict_preflight_v1"

    def test_preflight_records_clean_repo(self, work_dir):
        repo = work_dir / "repo"
        _init_mini_repo(repo)
        report = preflight_repo(repo, base_commit=subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repo, text=True
        ).strip())
        assert report["ok"] is True
        assert report["reasons"] == []
        assert "core.autocrlf" in report["line_endings"]


def _init_mini_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "pkg.py").write_text("old\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "t@test.com"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "t"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=path,
        check=True,
        capture_output=True,
    )


class TestAdapterFakeE2E:
    def test_dry_run_five_instances(self, work_dir):
        out = work_dir / "out"
        work = work_dir / "work"
        cfg = AdapterConfig(
            output_dir=out,
            work_root=work,
            instances_jsonl=FIXTURE,
            instance_ids=list(DEV_INSTANCE_IDS),
            dry_run=True,
        )
        report = SweBenchAdapter(cfg, orchestrator_factory=None).run()
        assert report["ok"] is True
        assert (out / "manifest.json").is_file()
        assert (out / "predictions.jsonl").is_file()
        assert report["failure_summary"][FailureClass.AGENT.value] == 5

    def test_dirty_repo_is_baseline_dirty(self, work_dir):
        out = work_dir / "out"
        work = work_dir / "work"
        repo = work / DEV_INSTANCE_IDS[0].replace("/", "__")
        _init_mini_repo(repo)
        (repo / "dirty.py").write_text("x = 1\n", encoding="utf-8")

        cfg = AdapterConfig(
            output_dir=out,
            work_root=work,
            instances_jsonl=FIXTURE,
            instance_ids=[DEV_INSTANCE_IDS[0]],
            skip_clone=True,
            dry_run=False,
            run_harness=False,
            provider="anthropic_compat",
        )
        report = SweBenchAdapter(cfg, orchestrator_factory=None).run()
        result = report["results"][0]
        assert result["failure_class"] == FailureClass.BASELINE_DIRTY.value
        assert result["error"] == "baseline_dirty"
        assert result["baseline_preflight"]["ok"] is False
        assert "dirty_worktree" in result["baseline_preflight"]["reasons"]

    def test_fake_skip_clone_produces_patches(self, work_dir):
        out = work_dir / "out"
        work = work_dir / "work"
        rows = []
        for iid in DEV_INSTANCE_IDS:
            repo = work / iid.replace("/", "__")
            _init_mini_repo(repo)
            head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()
            rows.append({
                **json.loads(next(
                    line for line in FIXTURE.read_text(encoding="utf-8").splitlines()
                    if json.loads(line)["instance_id"] == iid
                )),
                "base_commit": head,
            })

        instances_jsonl = work_dir / "instances.jsonl"
        instances_jsonl.write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
            encoding="utf-8",
        )

        gold = (
            "diff --git a/pkg.py b/pkg.py\n"
            "--- a/pkg.py\n"
            "+++ b/pkg.py\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
        )

        def factory(repo: str):
            return FakeGoldPatchOrchestrator(repo, gold_patch=gold)

        cfg = AdapterConfig(
            output_dir=out,
            work_root=work,
            instances_jsonl=instances_jsonl,
            instance_ids=list(DEV_INSTANCE_IDS),
            skip_clone=True,
            dry_run=False,
            run_harness=False,
            allow_gold_patch_injection=True,
        )
        report = SweBenchAdapter(cfg, orchestrator_factory=factory).run()
        assert report["ok"] is True
        preds = read_predictions_jsonl(out / "predictions.jsonl")
        assert len(preds) == 5
        assert all(p.get("model_patch") for p in preds)
        assert report["failure_summary"].get(FailureClass.NONE.value) == 5
        manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["extra"]["gold_patch_visibility"] == "assisted"
        for iid in DEV_INSTANCE_IDS:
            assert (out / "instances" / iid / "model_patch.diff").is_file()


class TestSwebenchCliDefaults:
    def test_verify_enabled_by_default(self):
        from src.benchmark.swebench.__main__ import build_parser

        args = build_parser().parse_args([])
        assert args.skip_verify is False
        assert args.allow_unverified_harness is False

    def test_skip_verify_opt_in(self):
        from src.benchmark.swebench.__main__ import build_parser

        args = build_parser().parse_args(["--skip-verify"])
        assert args.skip_verify is True


class TestClassifyPostRepair:
    def test_verified_fixed_pending_harness(self):
        from src.benchmark.swebench.classify import classify_post_repair
        from src.benchmark.swebench.types import FailureClass

        patch = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new\n"
        fc, detail = classify_post_repair(
            model_patch=patch, repair_status="fixed", verified=True
        )
        assert fc == FailureClass.NONE
        assert detail == "pending_harness"

    def test_skip_verify_fixed_is_pending_verify(self):
        """E15: skip_verify + fixed → pending_verify（非 agent）。"""
        from src.benchmark.swebench.classify import classify_post_repair
        from src.benchmark.swebench.types import FailureClass

        patch = (
            "--- a/x.py\n"
            "+++ b/x.py\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
        )
        fc, detail = classify_post_repair(
            model_patch=patch, repair_status="fixed", verified=False, skip_verify=True
        )
        assert fc == FailureClass.NONE
        assert detail == "pending_verify"

    def test_timeout_with_unified_patch(self):
        from src.benchmark.swebench.classify import classify_post_repair
        from src.benchmark.swebench.types import FailureClass

        patch = "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-a\n+b\n"
        fc, detail = classify_post_repair(
            model_patch=patch, repair_status="timeout", verified=False, skip_verify=True
        )
        assert fc == FailureClass.NONE
        assert detail == "timeout_with_patch"

    def test_fragment_patch_invalid(self):
        from src.benchmark.swebench.classify import classify_post_repair
        from src.benchmark.swebench.types import FailureClass

        fc, detail = classify_post_repair(
            model_patch="-old\n+new\n", repair_status="fixed", verified=False, skip_verify=True
        )
        assert fc == FailureClass.AGENT
        assert detail == "invalid_patch_format"
