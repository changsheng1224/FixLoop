"""sandbox_health 探针单测（mock docker-py）。"""

from unittest.mock import MagicMock

import pytest

from src.harness.sandbox_health import probe_sandbox_health


def _healthy_client(*, smoke_raises: Exception | None = None):
    client = MagicMock()
    client.ping.return_value = True
    client.info.return_value = {"ServerVersion": "24.0.0"}
    client.images.get.return_value = {"Id": "img-1"}
    if smoke_raises:

        def _run(**_kwargs):
            raise smoke_raises

        client.containers.run.side_effect = _run
    else:
        client.containers.run.return_value = MagicMock()
    return client


class TestProbeSandboxHealth:
    def test_all_checks_pass(self):
        report = probe_sandbox_health(client=_healthy_client())
        assert report.ready is True
        assert report.docker_ping == "ok"
        assert report.docker_info == "ok"
        assert report.image == "ok"
        assert report.network_smoke == "ok"
        assert report.image_ref

    def test_ping_failure(self):
        client = MagicMock()
        client.ping.side_effect = RuntimeError("daemon down")
        report = probe_sandbox_health(client=client)
        assert not report.ready
        assert report.docker_ping == "error"
        assert report.network_smoke == "skipped"

    def test_missing_image(self):
        from docker.errors import ImageNotFound

        client = _healthy_client()
        client.images.get.side_effect = ImageNotFound("not found")
        report = probe_sandbox_health(client=client, run_smoke=False)
        assert not report.ready
        assert report.image.startswith("missing:")

    def test_smoke_failure(self):
        report = probe_sandbox_health(
            client=_healthy_client(smoke_raises=RuntimeError("smoke failed")),
        )
        assert not report.ready
        assert report.network_smoke.startswith("error:")

    def test_skip_smoke_when_disabled(self):
        report = probe_sandbox_health(client=_healthy_client(), run_smoke=False)
        assert report.ready
        assert report.network_smoke == "skipped"
        client = _healthy_client()
        probe_sandbox_health(client=client, run_smoke=False)
        client.containers.run.assert_not_called()

    def test_to_dict_for_health_cli(self):
        report = probe_sandbox_health(client=_healthy_client(), run_smoke=False)
        data = report.to_dict()
        assert "ready" in data
        assert "docker_ping" in data
        assert "checks_ms" in data


class TestTryCreateVerifier:
    def test_returns_none_when_probe_not_ready(self, monkeypatch, workspace):
        from src.repair_factory import try_create_verifier

        class FakeReport:
            ready = False
            errors = ["image: missing:repair-agent/python-repair"]

        monkeypatch.setattr(
            "src.harness.sandbox_health.probe_sandbox_health",
            lambda: FakeReport(),
        )
        assert try_create_verifier(None, workspace, workspace.repo_root) is None

    def test_creates_verifier_when_probe_ready(self, monkeypatch, workspace):
        from src.repair_factory import try_create_verifier

        class FakeReport:
            ready = True
            errors = []

        fake_verifier = object()
        monkeypatch.setattr(
            "src.harness.sandbox_health.probe_sandbox_health",
            lambda: FakeReport(),
        )
        monkeypatch.setattr(
            "src.repair_factory.create_verifier",
            lambda *_a, **_k: fake_verifier,
        )
        assert try_create_verifier(None, workspace, workspace.repo_root) is fake_verifier
