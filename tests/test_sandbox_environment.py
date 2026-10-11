"""Environment provisioning boundaries: no user distro mutation or host fallback."""

import io
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_runtime.linux_sandbox import WslLauncherConfig
from agent_runtime.linux_sandbox import environment as env


class Transport:
    def __init__(self):
        self.registered = {"Ubuntu": (2, Path("/user/ubuntu"))}
        self.calls = []
        self.failure = None
        self.digest = "a" * 64
        self.checks = dict.fromkeys(env.HEALTH_CHECKS, True)

    def call(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if argv[0] == "--import":
            self.registered[argv[1]] = (2, Path(argv[2]))
            Path(argv[2]).mkdir()
        if self.failure == argv[0]:
            self.failure = None
            raise env.EnvironmentError("environment_operation_interrupted")
        if argv[0] == "--unregister":
            del self.registered[argv[1]]
        return b""

    def inventory(self):
        return {name: record[0] for name, record in self.registered.items()}

    def registration_path(self, distribution):
        return self.registered.get(distribution, (0, None))[1]

    def execute(self, distribution, argv, **kwargs):
        assert distribution != "Ubuntu"
        self.calls.append((argv, {"distribution": distribution, **kwargs}))
        if self.failure == argv[-1]:
            self.failure = None
            raise env.EnvironmentError("environment_operation_interrupted")
        if argv[-1] == "install":
            return b'{"installed": true}'
        if argv[-1] in {"measure", "health"}:
            return json.dumps(
                {
                    "release_id": self.release_id,
                    "runtime_digest": self.digest,
                    "checks": self.checks,
                }
            ).encode()
        return b""


@pytest.fixture
def manager(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    release = env.load_release()
    blobs = {}
    for item in [release["rootfs"], *release["wheels"]]:
        data = ("fixture:" + item["name"]).encode()
        if item["name"].startswith("certifi-"):
            stream = io.BytesIO()
            with zipfile.ZipFile(stream, "w") as archive:
                archive.writestr("certifi/cacert.pem", "fixture signed certificates")
            data = stream.getvalue()
        item["sha256"] = env.sha256(data)
        blobs[item["sha256"]] = data

    def fetch(item, cache):
        cache.mkdir(parents=True, exist_ok=True)
        target = cache / item["sha256"]
        target.write_bytes(blobs[item["sha256"]])
        return target

    transport = Transport()
    result = env.SandboxEnvironmentManager(
        workspace,
        home=tmp_path / "external",
        release=release,
        transport=transport,
        fetcher=fetch,
        sources={"agent_runtime/linux_sandbox/environment_worker.py": b"# fixture"},
    )
    transport.release_id = result.release_id
    return result


def test_prepare_and_recheck_never_use_default_or_user_distro(manager):
    prepared = manager.ensure()
    assert prepared.identity.runtime_digest == "a" * 64
    assert manager._load()["stage"] == "ready"
    imports = [argv for argv, _ in manager.transport.calls if argv[0] == "--import"]
    assert len(imports) == 1 and imports[0][-2:] == ["--version", "2"]
    before = len(manager.transport.calls)
    manager.ensure()
    later = manager.transport.calls[before:]
    assert not any(argv[0] == "--import" or "/usr/bin/apt-get" in argv for argv, _ in later)
    assert later[-1][1]["user"] == "fixloop"
    assert manager.transport.registered["Ubuntu"] == (2, Path("/user/ubuntu"))
    launcher = WslLauncherConfig.from_environment(prepared, "/opt/fixloop/runtime/config.json")
    assert launcher.argv()[3:5] == ["--user", "fixloop"]


def test_retire_waits_for_durable_idle_task_pin_and_preserves_user_distro(manager, tmp_path):
    from agent_runtime.linux_sandbox.receipts import _write

    manager.ensure()
    lifecycle = tmp_path / "task" / "sandbox-lifecycle.json"
    identity = {"task_id": "idle-task"}
    _write(
        manager.directory / "task-pins" / "task.json",
        {"identity": identity, "lifecycle": str(lifecycle)},
    )
    with pytest.raises(env.EnvironmentError, match="environment_in_use"):
        manager.retire()
    assert manager._load()["stage"] == "ready"
    _write(lifecycle, {"identity": identity, "state": "retired"})
    result = manager.retire()
    assert result["stage"] == "retired"
    assert manager.retire() == result
    assert manager.transport.inventory() == {"Ubuntu": 2}
    assert lifecycle.exists()
    with pytest.raises(env.EnvironmentError, match="environment_retired"):
        manager.ensure()


def test_retire_revalidates_registration_and_retries_interruption(manager):
    manager.ensure()
    owned = manager.transport.registered[manager.distribution]
    manager.transport.registered[manager.distribution] = (2, Path("/user/replacement"))
    with pytest.raises(env.EnvironmentError, match="distribution_conflict"):
        manager.retire()
    manager.transport.registered[manager.distribution] = owned
    manager.transport.failure = "--unregister"
    with pytest.raises(env.EnvironmentError, match="interrupted"):
        manager.retire()
    assert manager._load()["stage"] == "retiring"
    with pytest.raises(env.EnvironmentError, match="environment_retired"):
        manager.ensure()
    assert manager.retire()["stage"] == "retired"


def test_backend_pin_is_durable_and_rejects_environment_retirement(manager, tmp_path, monkeypatch):
    from agent_runtime.linux_sandbox.lifecycle import pin_environment
    from agent_runtime.linux_sandbox.receipts import _read, _write

    prepared = manager.ensure()
    monkeypatch.setattr(env, "WslEnvironmentTransport", lambda: manager.transport)
    binding = SimpleNamespace(
        environment=prepared.identity,
        state_root=str(tmp_path / "task"),
        identity={"task_id": "idle"},
    )
    pin_environment(binding)
    pins = list((manager.directory / "task-pins").iterdir())
    assert len(pins) == 1 and _read(pins[0])["identity"] == binding.identity
    with pytest.raises(env.EnvironmentError, match="environment_in_use"):
        manager.retire()
    _write(
        Path(binding.state_root) / "sandbox-lifecycle.json",
        {"identity": binding.identity, "state": "retired"},
    )
    manager.retire()
    with pytest.raises(ValueError, match="environment_distribution_missing"):
        pin_environment(binding)


@pytest.mark.parametrize("stage", ["--import", "update", "install", "--terminate", "health"])
def test_interrupted_stages_continue_without_reimporting_or_deleting(manager, stage):
    manager.transport.failure = stage
    with pytest.raises(env.EnvironmentError, match="interrupted"):
        manager.ensure()
    manager.ensure()
    calls = [argv for argv, _ in manager.transport.calls]
    assert len([argv for argv in calls if argv[0] == "--import"]) == 1
    assert not any("--unregister" in argv or "--set-default" in argv for argv in calls)
    assert manager._load()["stage"] == "ready"


def test_failure_before_registration_retains_orphan_in_separate_attempt(manager):
    record = {
        "schema_version": 1,
        "release_id": manager.release_id,
        "distribution": manager.distribution,
        "install_path": str(manager.directory / "partial"),
    }
    Path(record["install_path"]).mkdir()
    marker = Path(record["install_path"]) / "unconfirmed.vhdx"
    marker.write_text("preserve")
    manager._save(record, "importing")
    prepared = manager.ensure()
    assert prepared.install_path != record["install_path"]
    assert marker.read_text() == "preserve"
    assert manager._load()["retained_attempts"] == [record["install_path"]]


def test_distribution_name_collision_stops_before_download_or_mutation(manager):
    manager.transport.registered[manager.distribution] = (2, Path("/user/distro"))
    with pytest.raises(env.EnvironmentError, match="distribution_conflict"):
        manager.ensure()
    assert manager.transport.calls == [(["--status"], {})]
    assert not manager.journal.exists()


@pytest.mark.parametrize("replacement", ["path", "wsl1", "missing"])
def test_registered_environment_must_match_owned_path_and_wsl2(manager, replacement):
    manager.ensure()
    if replacement == "path":
        manager.transport.registered[manager.distribution] = (2, Path("/replacement"))
    elif replacement == "wsl1":
        manager.transport.registered[manager.distribution] = (
            1,
            Path(manager._load()["install_path"]),
        )
    else:
        del manager.transport.registered[manager.distribution]
    with pytest.raises(env.EnvironmentError, match="distribution_"):
        manager.ensure()
    assert len([argv for argv, _ in manager.transport.calls if argv[0] == "--import"]) == 1


def test_integrity_drift_never_reinstalls_over_existing_runtime(manager):
    manager.ensure()
    before = len(manager.transport.calls)
    manager.transport.digest = "b" * 64
    with pytest.raises(env.EnvironmentError, match="integrity_failed"):
        manager.ensure()
    assert not any("/usr/bin/apt-get" in argv for argv, _ in manager.transport.calls[before:])


def test_package_sources_are_signed_fixed_snapshot_in_owned_distro(manager):
    manager.ensure()
    sources = [
        kw["data"].decode()
        for argv, kw in manager.transport.calls
        if argv == ["/usr/bin/tee", "/etc/apt/sources.list.d/ubuntu.sources"]
    ]
    assert len(sources) == 1
    assert manager.release["apt_snapshot"] in sources[0]
    assert "Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg" in sources[0]
    assert all(
        kw["distribution"] == manager.distribution
        for argv, kw in manager.transport.calls
        if argv[0] == "/usr/bin/tee"
    )


@pytest.mark.parametrize("bad", [False, 1, "true"])
def test_health_evidence_requires_each_actual_boolean_pass(manager, bad):
    manager.transport.checks["fuse_device"] = bad
    with pytest.raises(env.EnvironmentError, match="health_failed"):
        manager.ensure()
    assert manager._load()["stage"] == "installed"


def test_journal_corruption_stops_before_import(manager):
    manager.ensure()
    raw = json.loads(manager.journal.read_text())
    raw["install_path"] = "/somebody/else"
    manager.journal.write_text(json.dumps(raw))
    with pytest.raises(env.EnvironmentError, match="journal_invalid"):
        manager.ensure()


def test_checksummed_journal_still_rejects_parent_escape(manager):
    manager.ensure()
    record = manager._load()
    record["install_path"] = str(manager.directory / ".." / "other-release")
    manager._save(record, "ready")
    with pytest.raises(env.EnvironmentError, match="journal_invalid"):
        manager._load()


def test_nested_prepare_uses_actual_os_lock(manager):
    with env.preparation_lock(manager.directory / "prepare.lock"):
        with pytest.raises(env.EnvironmentError, match="environment_busy"):
            manager.ensure()


def test_external_environment_storage_cannot_be_in_workspace(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with pytest.raises(ValueError, match="state_root inside workspace"):
        env.SandboxEnvironmentManager(workspace, home=workspace / ".fixloop")


def test_missing_wsl_provides_enable_and_restart_instructions(monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError()

    monkeypatch.setattr(env.subprocess, "run", missing)
    with pytest.raises(env.EnvironmentError) as caught:
        env.WslEnvironmentTransport().inventory()
    assert caught.value.code == "wsl_missing"
    assert "--no-distribution" in " ".join(caught.value.guidance)


def test_disabled_system_feature_provides_actionable_guidance(monkeypatch):
    monkeypatch.setattr(
        env.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(
            returncode=1, stdout="Wsl/Service/0x80370102".encode("utf-16-le"), stderr=b""
        ),
    )
    with pytest.raises(env.EnvironmentError) as caught:
        env.WslEnvironmentTransport().inventory()
    assert caught.value.code == "wsl_system_setup_required" and caught.value.guidance


def test_localized_inventory_and_no_ambient_wsl_environment(monkeypatch):
    captured = {}

    def listing(argv, **kwargs):
        captured.update(argv=argv, **kwargs)
        return SimpleNamespace(
            returncode=0,
            stdout=("  名称 状态 版本\n* Ubuntu 已停止 2\n  Other 正在运行 1\n").encode(
                "utf-16-le"
            ),
            stderr=b"",
        )

    monkeypatch.setenv("WSLENV", "SECRET/u")
    monkeypatch.setattr(env.subprocess, "run", listing)
    assert env.WslEnvironmentTransport().inventory() == {"Ubuntu": 2, "Other": 1}
    assert "WSLENV" not in captured["env"] and "shell" not in captured


class Response(io.BytesIO):
    def __init__(self, data, status=200, content_range=""):
        super().__init__(data)
        self.status, self.headers = status, {"Content-Range": content_range}

    def geturl(self):
        return "https://vendor.example/artifact"


@pytest.mark.parametrize("supports_range", [True, False])
def test_download_resume_verifies_full_content(tmp_path, monkeypatch, supports_range):
    data = b"complete verified artifact"
    item = {"url": "https://vendor.example/artifact", "sha256": env.sha256(data)}
    (tmp_path / (item["sha256"] + ".partial")).write_bytes(data[:5])

    def download(request, **kwargs):
        assert request.headers["Range"] == "bytes=5-"
        return Response(
            data[5:] if supports_range else data, 206 if supports_range else 200, "bytes 5-24/25"
        )

    monkeypatch.setattr(env.urllib.request, "urlopen", download)
    target = env.fetch_artifact(item, tmp_path)
    assert target.read_bytes() == data


def test_corrupt_download_not_promoted_or_executed(tmp_path, monkeypatch):
    item = {"url": "https://vendor.example/artifact", "sha256": env.sha256(b"expected")}
    monkeypatch.setattr(env.urllib.request, "urlopen", lambda *a, **kw: Response(b"tampered"))
    with pytest.raises(env.EnvironmentError, match="artifact_corrupt"):
        env.fetch_artifact(item, tmp_path)
    assert not (tmp_path / item["sha256"]).exists()


def test_corrupt_cached_artifact_is_rejected_without_fetch(tmp_path):
    item = {"url": "https://vendor.example/artifact", "sha256": env.sha256(b"expected")}
    (tmp_path / item["sha256"]).write_bytes(b"tampered")
    with pytest.raises(env.EnvironmentError, match="artifact_corrupt"):
        env.fetch_artifact(item, tmp_path)


def test_payload_is_deterministic_and_covers_executor_code(manager):
    wheels = {
        "wheels/" + item["name"]: manager.fetcher(item, manager.home / "downloads")
        for item in manager.release["wheels"]
    }
    first = env.build_payload(manager.manifest, manager.sources, wheels)
    assert first == env.build_payload(manager.manifest, manager.sources, wheels)
    wheels[next(iter(wheels))].write_bytes(b"tampered wheel")
    with pytest.raises(env.EnvironmentError, match="payload_integrity"):
        env.build_payload(manager.manifest, manager.sources, wheels)


def test_prepared_offline_image_skips_all_download_and_install(manager, tmp_path):
    image = tmp_path / "prepared.tar.gz"
    image.write_bytes(b"fixture prepared image")
    sidecar = tmp_path / "bundle.json"
    sidecar.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "release_id": manager.release_id,
                "runtime_digest": manager.transport.digest,
                "image": image.name,
                "sha256": env.file_digest(image),
            }
        )
    )
    manager.image = env.load_image_bundle(sidecar)
    manager.fetcher = lambda *a: pytest.fail("offline bundle must not download")
    manager.ensure()
    assert not any("/usr/bin/apt-get" in argv for argv, _ in manager.transport.calls)


@pytest.mark.parametrize(
    "distribution,user",
    [
        ("Ubuntu;evil", ""),
        ("FixLoop-Sandbox-s2-v1-abcdefabcdef", "root"),
        ("FixLoop-Sandbox-s2-v1-abcdefabcdef", ""),
    ],
)
def test_launcher_rejects_name_injection_and_privileged_task_user(distribution, user):
    with pytest.raises(ValueError):
        WslLauncherConfig(distribution, "/python", "/controller", "/config", user).argv()


@pytest.mark.parametrize(
    "field,value", [("version", None), ("packages", {}), ("wheels", "bad"), ("rootfs", None)]
)
def test_malformed_release_is_reported_as_typed_error(field, value):
    release = env.load_release()
    release[field] = value
    with pytest.raises(env.EnvironmentError, match="release_invalid"):
        env.validate_release(release)


def test_worker_failure_keeps_safe_error_code(monkeypatch):
    monkeypatch.setattr(
        env.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(
            returncode=1,
            stdout=b'{"error": "environment_integrity_failed"}',
            stderr=b"private traceback contents",
        ),
    )
    with pytest.raises(env.EnvironmentError, match="^environment_integrity_failed$"):
        env.WslEnvironmentTransport().call(["--distribution", "FixLoop-Sandbox-s2-v1-abcdefabcdef"])


@pytest.mark.parametrize("bad", [None, {}, {"schema_version": True}])
def test_malformed_offline_bundle_has_safe_error(tmp_path, bad):
    sidecar = tmp_path / "bundle.json"
    sidecar.write_text(json.dumps(bad))
    with pytest.raises(env.EnvironmentError, match="bundle_invalid"):
        env.load_image_bundle(sidecar)


def test_bundle_constructor_rejects_different_executor_release(manager, tmp_path):
    image = tmp_path / "prepared.tar.gz"
    image.write_bytes(b"verified image")
    sidecar = tmp_path / "bundle.json"
    sidecar.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "release_id": "b" * 64,
                "runtime_digest": "c" * 64,
                "image": image.name,
                "sha256": env.file_digest(image),
            }
        )
    )
    with pytest.raises(env.EnvironmentError, match="bundle_version_mismatch"):
        env.SandboxEnvironmentManager(
            tmp_path / "workspace",
            home=manager.home,
            release=manager.release,
            sources=manager.sources,
            image_bundle=sidecar,
        )


def test_download_interruption_retains_bytes_for_retry(tmp_path, monkeypatch):
    data = b"verified complete content"
    item = {"url": "https://vendor.example/artifact", "sha256": env.sha256(data)}

    class InterruptedResponse(Response):
        def read(self, size):
            if self.tell():
                raise OSError("connection interrupted")
            return super().read(5)

    monkeypatch.setattr(env.urllib.request, "urlopen", lambda *a, **kw: InterruptedResponse(data))
    with pytest.raises(env.EnvironmentError, match="download_interrupted"):
        env.fetch_artifact(item, tmp_path)
    assert (tmp_path / (item["sha256"] + ".partial")).read_bytes() == data[:5]
    monkeypatch.setattr(
        env.urllib.request, "urlopen", lambda *a, **kw: Response(data[5:], 206, "bytes 5-24/25")
    )
    assert env.fetch_artifact(item, tmp_path).read_bytes() == data


def test_ca_bootstrap_uses_verified_certificate_wheel_and_explicit_tls_path(manager):
    manager.ensure()
    certificate_writes = [
        kw["data"]
        for argv, kw in manager.transport.calls
        if argv == ["/usr/bin/tee", "/etc/ssl/certs/ca-certificates.crt"]
    ]
    assert certificate_writes == [b"fixture signed certificates"]
    apt_calls = [argv for argv, _ in manager.transport.calls if "/usr/bin/apt-get" in argv]
    assert all(
        "Acquire::https::CaInfo=/etc/ssl/certs/ca-certificates.crt" in argv for argv in apt_calls
    )
    assert "APT::Update::Error-Mode=any" in apt_calls[0]
    assert not any(
        "Verify-Peer=false" in part or "--allow-unauthenticated" in part
        for argv in apt_calls
        for part in argv
    )


def test_ca_bootstrap_rejects_missing_or_malformed_archive(tmp_path):
    with pytest.raises(env.EnvironmentError, match="certificates_invalid"):
        env.certificate_bundle({})
    invalid = tmp_path / "certifi.whl"
    invalid.write_bytes(b"bad archive")
    with pytest.raises(env.EnvironmentError, match="certificates_invalid"):
        env.certificate_bundle({"wheels/certifi-fixture.whl": invalid})
