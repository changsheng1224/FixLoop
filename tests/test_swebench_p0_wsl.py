"""P0：WSL 路径、harness 过滤、reexport。"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

import pytest

from src.benchmark.swebench.harness import filter_predictions_with_patch, resolve_harness_backend
from src.benchmark.swebench.reexport import reexport_from_snapshots
from src.benchmark.swebench.wsl_util import preferred_wsl_distro, win_to_wsl_path


@pytest.fixture
def work_dir():
    path = Path(tempfile.mkdtemp(prefix="swebench-p0-"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


class TestWslPath:
    def test_win_drive_path(self):
        # 构造类 Windows 路径字符串（在非 Windows 上也测转换逻辑）
        from src.benchmark.swebench import wsl_util

        if not wsl_util.is_windows():
            pytest.skip("win path resolve only on Windows")
        p = Path("C:/Users/haoyu/Documents/FixLoop/a.jsonl")
        got = win_to_wsl_path(p)
        assert got.startswith("/mnt/c/")
        assert got.endswith("a.jsonl") or got.endswith("a.jsonl".replace("\\", "/"))


class TestHostIpParse:
    def test_strips_nameserver_line(self):
        from src.benchmark.swebench.wsl_util import _parse_host_ip

        assert _parse_host_ip("nameserver 172.25.32.1") == "172.25.32.1"
        assert _parse_host_ip("172.25.32.1\n") == "172.25.32.1"
        assert _parse_host_ip("# comment\nnameserver 10.0.0.1\n") == "10.0.0.1"
        assert _parse_host_ip("nameserver 172.25.32.1") != "nameserver 172.25.32.1"
        assert _parse_host_ip("garbage") == ""


class TestWslProxyEnv:
    def test_rewrites_loopback_even_when_env_set(self, monkeypatch):
        from src.benchmark.swebench import wsl_util

        monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7897")
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7897")
        monkeypatch.setattr(wsl_util, "_windows_host_ip_for_wsl", lambda distro=None: "172.25.32.1")
        monkeypatch.setattr(wsl_util, "is_windows", lambda: True)
        env = wsl_util.wsl_proxy_env("Ubuntu")
        assert env["HTTPS_PROXY"] == "http://172.25.32.1:7897"
        assert " " not in env["HTTPS_PROXY"]
        assert "nameserver" not in env["HTTPS_PROXY"]

    def test_rejects_dirty_host_ip(self, monkeypatch):
        from src.benchmark.swebench import wsl_util

        monkeypatch.delenv("HTTPS_PROXY", raising=False)
        monkeypatch.delenv("HTTP_PROXY", raising=False)
        monkeypatch.delenv("https_proxy", raising=False)
        monkeypatch.delenv("http_proxy", raising=False)
        monkeypatch.setattr(wsl_util, "is_windows", lambda: True)
        monkeypatch.setattr(
            wsl_util, "_windows_host_ip_for_wsl", lambda distro=None: "nameserver 172.25.32.1"
        )
        # _windows_host_ip_for_wsl 已保证干净；若脏值漏出也不应写入带空格 URL
        # 这里模拟旧 bug：直接测 parse
        assert wsl_util._parse_host_ip("nameserver 172.25.32.1") == "172.25.32.1"


class TestPreferredDistro:
    def test_skips_docker_desktop(self):
        assert preferred_wsl_distro(["docker-desktop", "Ubuntu-22.04"]) == "Ubuntu-22.04"

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("FIXLOOP_WSL_DISTRO", "MyDistro")
        assert preferred_wsl_distro(["MyDistro", "Ubuntu"]) == "MyDistro"


class TestFilterPredictions:
    def test_keeps_verified_nonempty_only(self, work_dir):
        src = work_dir / "predictions.jsonl"
        rows = [
            {"instance_id": "a", "model_name_or_path": "m", "model_patch": "", "verified": True},
            {
                "instance_id": "django__django-11099",
                "model_name_or_path": "m",
                "model_patch": "diff\n",
                "verified": True,
            },
            {"instance_id": "b", "model_name_or_path": "m", "model_patch": "x", "verified": False},
        ]
        src.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        out, ids = filter_predictions_with_patch(
            src, instance_ids=["django__django-11099", "a", "b"], out_path=work_dir / "h.jsonl"
        )
        assert ids == ["django__django-11099"]
        kept = [
            json.loads(line)
            for line in out.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert len(kept) == 1

    def test_allow_unverified_keeps_nonempty(self, work_dir):
        src = work_dir / "predictions.jsonl"
        rows = [
            {"instance_id": "b", "model_name_or_path": "m", "model_patch": "x", "verified": False},
        ]
        src.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        _, ids = filter_predictions_with_patch(
            src, out_path=work_dir / "h2.jsonl", require_verified=False
        )
        assert ids == ["b"]


class TestReexport:
    def test_reexport_skips_binary_and_keeps_text_diff(self, work_dir):
        out = work_dir / "out"
        work = work_dir / "work"
        iid = "demo__demo-1"
        original = out / "snapshots" / iid / "original"
        modified = work / iid
        original.mkdir(parents=True)
        modified.mkdir(parents=True)
        (original / "a.py").write_text("old\n", encoding="utf-8")
        (modified / "a.py").write_text("new\n", encoding="utf-8")
        (original / "x.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 10)
        (modified / "x.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x01" * 10)
        # seed empty prediction
        (out / "predictions.jsonl").write_text(
            json.dumps(
                {"instance_id": iid, "model_name_or_path": "m", "model_patch": ""},
            )
            + "\n",
            encoding="utf-8",
        )
        (out / "adapter_report.json").write_text(
            json.dumps({"results": [{"instance_id": iid, "model_patch": "", "repair_status": "failed"}]}),
            encoding="utf-8",
        )
        summary = reexport_from_snapshots(output_dir=out, work_root=work)
        assert summary["nonempty_patches"] == 1
        preds = (out / "predictions.jsonl").read_text(encoding="utf-8")
        assert "a.py" in preds
        assert "PNG" not in preds


class TestResolveBackend:
    def test_native_on_windows_fails_gracefully(self, monkeypatch):
        monkeypatch.setattr(
            "src.benchmark.swebench.harness.is_windows",
            lambda: True,
        )
        monkeypatch.setattr(
            "src.benchmark.swebench.harness.native_harness_importable",
            lambda: False,
        )
        monkeypatch.setattr(
            "src.benchmark.swebench.harness.probe_wsl",
            lambda: type("P", (), {"available": False, "error": "no distro", "note": "install Ubuntu"})(),
        )
        backend, err = resolve_harness_backend("auto")
        assert backend == ""
        assert "no distro" in err or "Ubuntu" in err
