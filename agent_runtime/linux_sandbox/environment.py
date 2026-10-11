"""Resumable preparation of a product-owned WSL2 runtime, outside every workspace."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import platform
import re
import subprocess
import tarfile
import urllib.error
import urllib.request
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path

from agent_runtime.state_root import native_io_path, trusted_state_root

from .task_context import SandboxEnvironmentIdentity

WORKER = "/opt/fixloop/runtime/agent_runtime/linux_sandbox/environment_worker.py"
PYTHON = "/usr/bin/python3.14"
HEALTH_CHECKS = {
    "unprivileged_namespaces",
    "network_namespace",
    "tmpfs_limit",
    "clean_environment",
    "control_paths_hidden",
    "fuse_device",
}
SETUP_GUIDANCE = (
    "请以管理员身份运行 wsl --install --no-distribution，按系统要求重启后重试。",
    "同时确认固件虚拟化和 Virtual Machine Platform 已启用；FixLoop 不自动提升管理员权限。",
)


class EnvironmentError(RuntimeError):
    def __init__(self, code: str, guidance: tuple[str, ...] = ()):
        self.code, self.guidance = code, guidance
        super().__init__(code)


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def canonical(raw: dict) -> bytes:
    return json.dumps(raw, sort_keys=True, ensure_ascii=False).encode()


def load_release() -> dict:
    release = json.loads(Path(__file__).with_name("environment_release.json").read_text("utf-8"))
    validate_release(release)
    return release


def validate_release(release: dict) -> None:
    if (
        not isinstance(release, dict)
        or set(release)
        != {
            "schema_version",
            "version",
            "architecture",
            "distribution_version",
            "apt_snapshot",
            "rootfs",
            "packages",
            "wheels",
        }
        or type(release["schema_version"]) is not int
        or release["schema_version"] != 1
        or not isinstance(release["version"], str)
        or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,30}", release["version"])
        or release["architecture"] != "x86_64"
        or release["distribution_version"] != "26.04"
        or not isinstance(release["apt_snapshot"], str)
        or not re.fullmatch(
            r"https://snapshot\.ubuntu\.com/ubuntu/[0-9]{8}T[0-9]{6}Z/", release["apt_snapshot"]
        )
        or not isinstance(release["packages"], list)
        or not isinstance(release["wheels"], list)
        or not release["packages"]
        or not release["wheels"]
    ):
        raise EnvironmentError("environment_release_invalid")
    for package in release["packages"]:
        if not isinstance(package, str) or not re.fullmatch(
            r"[a-z0-9.+-]+=[a-zA-Z0-9.+:~_-]+", package
        ):
            raise EnvironmentError("environment_release_invalid")
    for artifact in [release["rootfs"], *release["wheels"]]:
        if (
            not isinstance(artifact, dict)
            or set(artifact) != {"name", "url", "sha256"}
            or not isinstance(artifact["name"], str)
            or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]+", artifact["name"])
            or not isinstance(artifact["url"], str)
            or not artifact["url"].startswith("https://")
            or not isinstance(artifact["sha256"], str)
            or not re.fullmatch(r"[a-f0-9]{64}", artifact["sha256"])
        ):
            raise EnvironmentError("environment_release_invalid")


def file_digest(path: Path) -> str:
    value = hashlib.sha256()
    with native_io_path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def fetch_artifact(artifact: dict, cache: Path) -> Path:
    """Range resume is opportunistic; a full hash is mandatory before promotion."""
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / artifact["sha256"]
    if target.exists():
        if target.is_symlink() or file_digest(target) != artifact["sha256"]:
            raise EnvironmentError("environment_artifact_corrupt")
        return target
    partial = target.with_suffix(".partial")
    if partial.is_symlink():
        raise EnvironmentError("environment_artifact_corrupt")
    offset = partial.stat().st_size if partial.exists() else 0
    request = urllib.request.Request(
        artifact["url"], headers={"Range": f"bytes={offset}-"} if offset else {}
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            if not response.geturl().startswith("https://"):
                raise EnvironmentError("environment_download_insecure")
            append = offset > 0 and response.status == 206
            if append and not response.headers.get("Content-Range", "").startswith(
                f"bytes {offset}-"
            ):
                raise EnvironmentError("environment_download_range_invalid")
            length = offset if append else 0
            with native_io_path(partial).open("ab" if append else "wb") as stream:
                while chunk := response.read(1024 * 1024):
                    length += len(chunk)
                    if length > 2 * 1024**3:
                        raise EnvironmentError("environment_artifact_too_large")
                    stream.write(chunk)
                stream.flush()
                os.fsync(stream.fileno())
    except (OSError, urllib.error.URLError) as exc:
        raise EnvironmentError(
            "environment_download_interrupted", ("重试会继续下载并重新校验完整文件。",)
        ) from exc
    if file_digest(partial) != artifact["sha256"]:
        partial.unlink()  # Only our untrusted download fragment, never an installed distro.
        raise EnvironmentError("environment_artifact_corrupt")
    partial.replace(target)
    return target


def runtime_files() -> dict[str, bytes]:
    root = Path(__file__).resolve().parents[1]
    # Package-owned source only. Never include project .env, homes, Git or task state.
    files = {
        "agent_runtime/" + path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*.py"))
        if "__pycache__" not in path.parts
    }
    return files


def payload_manifest(release: dict, sources: dict[str, bytes]) -> dict:
    files = {name: sha256(raw) for name, raw in sources.items()}
    files.update({"wheels/" + item["name"]: item["sha256"] for item in release["wheels"]})
    release_id = sha256(canonical({"release": release, "files": files}))
    return {"release_id": release_id, "release": release, "files": files}


def build_payload(manifest: dict, sources: dict[str, bytes], wheels: dict[str, Path]) -> bytes:
    contents = {**sources, **{name: path.read_bytes() for name, path in wheels.items()}}
    if set(contents) != set(manifest["files"]) or any(
        sha256(raw) != manifest["files"][name] for name, raw in contents.items()
    ):
        raise EnvironmentError("environment_payload_integrity")
    contents["payload.json"] = canonical(manifest)
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        for name, raw in sorted(contents.items()):
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(raw), 0o644, 0
            archive.addfile(info, io.BytesIO(raw))
    return stream.getvalue()


def certificate_bundle(wheels: dict[str, Path]) -> bytes:
    names = [name for name in wheels if name.startswith("wheels/certifi-")]
    if len(names) != 1:
        raise EnvironmentError("environment_certificates_invalid")
    try:
        with zipfile.ZipFile(wheels[names[0]]) as archive:
            info = archive.getinfo("certifi/cacert.pem")
            if info.file_size > 1024 * 1024:
                raise ValueError("size")
            return archive.read(info)
    except (ValueError, KeyError, OSError, zipfile.BadZipFile) as exc:
        raise EnvironmentError("environment_certificates_invalid") from exc


def load_image_bundle(path: str | Path) -> dict:
    """Read a trusted release sidecar; its image still requires a complete hash check."""
    try:
        path = Path(path).resolve(strict=True)
        raw = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise EnvironmentError("environment_bundle_invalid") from exc
    if (
        not isinstance(raw, dict)
        or set(raw) != {"schema_version", "release_id", "runtime_digest", "image", "sha256"}
        or type(raw["schema_version"]) is not int
        or raw["schema_version"] != 1
        or any(
            not isinstance(raw[key], str) or not re.fullmatch(r"[a-f0-9]{64}", raw[key])
            for key in (
                "release_id",
                "runtime_digest",
                "sha256",
            )
        )
        or not isinstance(raw["image"], str)
        or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]+", raw["image"])
    ):
        raise EnvironmentError("environment_bundle_invalid")
    image = path.parent / raw["image"]
    if not image.is_file() or image.is_symlink() or file_digest(image) != raw["sha256"]:
        raise EnvironmentError("environment_artifact_corrupt")
    return {**raw, "path": image}


def decode_wsl(raw: bytes) -> str:
    return raw.decode("utf-16-le" if b"\x00" in raw else "utf-8", errors="replace").lstrip("\ufeff")


class WslEnvironmentTransport:
    """Fixed argument vectors; no host shell, default distro or user-controlled commands."""

    def call(self, argv: list[str], *, data: bytes | None = None, timeout: int = 60) -> bytes:
        try:
            proc = subprocess.run(
                ["wsl.exe", *argv],
                input=data,
                capture_output=True,
                timeout=timeout,
                check=False,
                env={
                    key: value
                    for key, value in os.environ.items()
                    if key.upper()
                    not in {
                        "WSLENV",
                        "WSL_INTEROP",
                        "PYTHONPATH",
                        "PYTHONHOME",
                    }
                },
                cwd=os.environ.get("SystemRoot", "C:\\Windows"),
            )
        except FileNotFoundError as exc:
            raise EnvironmentError("wsl_missing", SETUP_GUIDANCE) from exc
        except subprocess.TimeoutExpired as exc:
            raise EnvironmentError(
                "environment_operation_interrupted",
                ("再次准备会核对已有安装阶段，不覆盖已有发行版。",),
            ) from exc
        except OSError as exc:
            raise EnvironmentError("wsl_unavailable", SETUP_GUIDANCE) from exc
        if proc.returncode:
            # Keep worker error codes, never its traceback, command output or arbitrary text.
            try:
                failure = json.loads(proc.stdout)
                if (
                    isinstance(failure, dict)
                    and set(failure) == {"error"}
                    and isinstance(failure["error"], str)
                    and re.fullmatch(r"environment_[a-z_]{1,60}", failure["error"])
                ):
                    raise EnvironmentError(failure["error"])
            except (ValueError, TypeError):
                pass
            diagnostic = decode_wsl(proc.stdout + proc.stderr).casefold()
            if any(
                token in diagnostic
                for token in (
                    "0x80370102",
                    "optional_component",
                    "virtualization",
                    "wsl_e_wsl",
                    "0x8007019e",
                    "reboot",
                )
            ):
                raise EnvironmentError("wsl_system_setup_required", SETUP_GUIDANCE)
            if "lock" in diagnostic:
                raise EnvironmentError("environment_busy")
            raise EnvironmentError("environment_command_failed")
        return proc.stdout

    def inventory(self) -> dict[str, int]:
        text = decode_wsl(self.call(["--list", "--verbose"]))
        inventory = {}
        for line in text.splitlines():
            match = re.fullmatch(r"\s*\*?\s*(.+?)\s+\S+\s+([12])\s*", line)
            if match:
                inventory[match[1]] = int(match[2])
        return inventory

    def registration_path(self, distribution: str) -> Path | None:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Lxss"
        ) as root:
            index = 0
            while True:
                try:
                    name = winreg.EnumKey(root, index)
                except OSError:
                    break
                index += 1
                with winreg.OpenKey(root, name) as child:
                    if (
                        winreg.QueryValueEx(child, "DistributionName")[0].casefold()
                        == distribution.casefold()
                    ):
                        return Path(winreg.QueryValueEx(child, "BasePath")[0]).resolve()
        return None

    def execute(
        self, distribution: str, argv: list[str], *, user: str = "root", data=None, timeout=60
    ) -> bytes:
        return self.call(
            ["--distribution", distribution, "--user", user, "--exec", *argv],
            data=data,
            timeout=timeout,
        )


@dataclass(frozen=True)
class PreparedEnvironment:
    identity: SandboxEnvironmentIdentity
    release_id: str
    install_path: str
    health: dict

    @property
    def launcher_paths(self) -> dict:
        return {
            "distribution": self.identity.distribution,
            "python": PYTHON,
            "controller": "/opt/fixloop/runtime/agent_runtime/linux_sandbox/controller_entry.py",
            "user": "fixloop",
        }


@contextlib.contextmanager
def preparation_lock(path: Path):
    with path.open("a+b") as stream:
        stream.seek(0)
        stream.write(b"0")
        stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise EnvironmentError("environment_busy") from exc
        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


class SandboxEnvironmentManager:
    def __init__(
        self,
        workspace: str | Path,
        *,
        home=None,
        release=None,
        transport=None,
        fetcher=fetch_artifact,
        sources=None,
        progress=None,
        image_bundle=None,
    ):
        parent = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
        self.home = trusted_state_root(workspace, home or parent / "FixLoop/sandbox")
        self.release = load_release() if release is None else release
        validate_release(self.release)
        self.sources = runtime_files() if sources is None else sources
        self.manifest = payload_manifest(self.release, self.sources)
        self.release_id = self.manifest["release_id"]
        self.image = load_image_bundle(image_bundle) if image_bundle is not None else None
        if self.image is not None and self.image["release_id"] != self.release_id:
            raise EnvironmentError("environment_bundle_version_mismatch")
        self.distribution = (
            "FixLoop-Sandbox-" + self.release["version"] + "-" + self.release_id[:12]
        )
        self.directory = self.home / "environments" / self.release_id
        self.directory.mkdir(parents=True, exist_ok=True)
        self.journal = self.directory / "installation.json"
        self.transport = transport or WslEnvironmentTransport()
        self.fetcher, self.progress = fetcher, progress or (lambda stage: None)

    def _save(self, record: dict, stage: str) -> None:
        record["stage"] = stage
        raw = {**record, "checksum": sha256(canonical(record))}
        temporary = self.journal.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(raw, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(self.journal)
        self.progress(stage)

    def _load(self) -> dict | None:
        if not self.journal.exists():
            return None
        try:
            raw = json.loads(self.journal.read_text("utf-8"))
            checksum = raw.pop("checksum")
            if (
                checksum != sha256(canonical(raw))
                or raw["release_id"] != self.release_id
                or raw["distribution"] != self.distribution
                or not Path(raw["install_path"]).resolve().is_relative_to(self.directory.resolve())
                or Path(raw["install_path"]).resolve() == self.directory.resolve()
                or raw["stage"]
                not in {
                    "downloading",
                    "importing",
                    "imported",
                    "packages",
                    "installed",
                    "ready",
                    "retiring",
                    "retired",
                }
            ):
                raise ValueError("identity")
            return raw
        except (ValueError, KeyError, TypeError, OSError) as exc:
            raise EnvironmentError("environment_journal_invalid") from exc

    def _assert_registration(self, inventory: dict, record: dict) -> None:
        matches = [name for name in inventory if name.casefold() == self.distribution.casefold()]
        if len(matches) != 1 or inventory[matches[0]] != 2:
            raise EnvironmentError("environment_distribution_missing")
        actual = self.transport.registration_path(matches[0])
        if actual is None or Path(os.path.normcase(str(actual.resolve()))) != Path(
            os.path.normcase(str(Path(record["install_path"]).resolve()))
        ):
            raise EnvironmentError("environment_distribution_conflict")

    def _json_command(self, record: dict, operation: str, *, user="root") -> dict:
        raw = self.transport.execute(
            self.distribution, [PYTHON, "-I", "-B", WORKER, operation], user=user
        )
        try:
            result = json.loads(raw)
            if not isinstance(result, dict) or result.get("release_id") != record["release_id"]:
                raise ValueError("identity")
            return result
        except (ValueError, TypeError) as exc:
            raise EnvironmentError("environment_health_invalid") from exc

    def ensure(self, *, package_proxy: str = "") -> PreparedEnvironment:
        if platform.machine().casefold() not in {"amd64", "x86_64"}:
            raise EnvironmentError("environment_architecture_unsupported")
        if package_proxy and (not re.fullmatch(r"https?://[a-zA-Z0-9.\[\]:-]+/?", package_proxy)):
            raise EnvironmentError("environment_proxy_invalid")
        with preparation_lock(self.directory / "prepare.lock"):
            existing = self._load()
            if existing and existing["stage"] in {"retiring", "retired"}:
                raise EnvironmentError("environment_retired")
            self.transport.call(["--status"])  # Works even when no distribution exists yet.
            inventory = self.transport.inventory()
            record = self._load()
            if record is None:
                if any(name.casefold() == self.distribution.casefold() for name in inventory):
                    raise EnvironmentError("environment_distribution_conflict")
                record = {
                    "schema_version": 1,
                    "release_id": self.release_id,
                    "distribution": self.distribution,
                    "install_path": str(self.directory / ("distro-" + uuid.uuid4().hex)),
                }
                self._save(record, "downloading")
            present = any(name.casefold() == self.distribution.casefold() for name in inventory)
            if present:
                self._assert_registration(inventory, record)
            elif record["stage"] not in {"downloading", "importing"}:
                raise EnvironmentError("environment_distribution_missing")
            if record["stage"] not in {"installed", "ready"}:
                cache = self.home / "downloads"
                if self.image is not None:
                    if file_digest(self.image["path"]) != self.image["sha256"]:
                        raise EnvironmentError("environment_artifact_corrupt")
                    rootfs, payload = self.image["path"], None
                else:
                    rootfs = self.fetcher(self.release["rootfs"], cache)
                    wheels = {
                        "wheels/" + item["name"]: self.fetcher(item, cache)
                        for item in self.release["wheels"]
                    }
                    payload = build_payload(self.manifest, self.sources, wheels)
                if not present:
                    # Interrupted/unregistered images are retained. A new attempt uses a fresh path.
                    install_path = Path(record["install_path"])
                    if install_path.exists():
                        record.setdefault("retained_attempts", []).append(str(install_path))
                        record["install_path"] = str(
                            self.directory / ("distro-" + uuid.uuid4().hex)
                        )
                    self._save(record, "importing")
                    self.transport.call(
                        [
                            "--import",
                            self.distribution,
                            record["install_path"],
                            str(rootfs),
                            "--version",
                            "2",
                        ],
                        timeout=300,
                    )
                    self._assert_registration(self.transport.inventory(), record)
                self._save(record, "imported")
                if self.image is not None:
                    observed = self._json_command(record, "measure")
                    if observed.get("runtime_digest") != self.image["runtime_digest"]:
                        raise EnvironmentError("environment_integrity_failed")
                    record["runtime_digest"] = self.image["runtime_digest"]
                    self._save(record, "installed")
                    return self._finish(record)
                self.transport.execute(
                    self.distribution, ["/usr/bin/dpkg", "--configure", "-a"], timeout=120
                )
                # The bare image has no CA bundle; bootstrap from the verified pinned wheel.
                self.transport.execute(
                    self.distribution, ["/usr/bin/mkdir", "-p", "/etc/ssl/certs"]
                )
                self.transport.execute(
                    self.distribution,
                    ["/usr/bin/tee", "/etc/ssl/certs/ca-certificates.crt"],
                    data=certificate_bundle(wheels),
                )
                sources = (
                    "Types: deb\nURIs: " + self.release["apt_snapshot"] + "\n"
                    "Suites: resolute resolute-updates resolute-security\n"
                    "Components: main universe\n"
                    "Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\n"
                    "Check-Valid-Until: no\n"
                )
                self.transport.execute(
                    self.distribution,
                    ["/usr/bin/tee", "/etc/apt/sources.list.d/ubuntu.sources"],
                    data=sources.encode(),
                )
                apt = [
                    "/usr/bin/env",
                    "-i",
                    "PATH=/usr/bin:/bin",
                    "LANG=C.UTF-8",
                    "DEBIAN_FRONTEND=noninteractive",
                    "/usr/bin/apt-get",
                ]
                if package_proxy:
                    apt += [
                        "-o",
                        "Acquire::http::Proxy=" + package_proxy,
                        "-o",
                        "Acquire::https::Proxy=" + package_proxy,
                    ]
                apt += [
                    "-o",
                    "Acquire::Retries=2",
                    "-o",
                    "Acquire::http::Timeout=20",
                    "-o",
                    "APT::Update::Error-Mode=any",
                    "-o",
                    "Acquire::https::CaInfo=/etc/ssl/certs/ca-certificates.crt",
                ]
                self.progress("updating_packages")
                self.transport.execute(self.distribution, [*apt, "update"], timeout=300)
                self.progress("installing_packages")
                self.transport.execute(
                    self.distribution,
                    [*apt, "install", "-y", "--no-install-recommends", *self.release["packages"]],
                    timeout=600,
                )
                self._save(record, "packages")
                # Bootstrap source is bundled product code, not a command supplied by the Agent.
                worker = self.sources["agent_runtime/linux_sandbox/environment_worker.py"].decode(
                    "utf-8"
                )
                answer = self.transport.execute(
                    self.distribution,
                    [PYTHON, "-I", "-B", "-c", worker, "install"],
                    data=payload,
                    timeout=180,
                )
                if json.loads(answer) != {"installed": True}:
                    raise EnvironmentError("environment_install_failed")
                observed = self._json_command(record, "measure")
                record["runtime_digest"] = observed["runtime_digest"]
                self._save(record, "installed")
                self.transport.call(["--terminate", self.distribution])
            return self._finish(record)

    def _finish(self, record: dict) -> PreparedEnvironment:
        if record["stage"] == "installed":
            # Also reload wsl.conf when interruption happened before the first terminate.
            self.transport.call(["--terminate", self.distribution])
        self.progress("checking_health")
        observed = self._json_command(record, "health", user="fixloop")
        if observed.get("runtime_digest") != record.get("runtime_digest"):
            raise EnvironmentError("environment_integrity_failed")
        checks = observed.get("checks")
        if (
            not isinstance(checks, dict)
            or set(checks) != HEALTH_CHECKS
            or any(value is not True for value in checks.values())
        ):
            raise EnvironmentError("environment_health_failed")
        identity = SandboxEnvironmentIdentity(
            self.distribution,
            self.release["distribution_version"],
            self.release["version"],
            observed["runtime_digest"],
        )
        self._save(record, "ready")
        return PreparedEnvironment(identity, self.release_id, record["install_path"], observed)

    def retire(self) -> dict:
        """Explicitly retire this exact managed release, never an arbitrary distro.

        Pins retain idle sessions and pending approvals across host restarts.
        User workspaces, worktrees and task recovery journals are never collected.
        """
        from .receipts import _read

        with preparation_lock(self.directory / "prepare.lock"):
            record = self._load()
            if not record or record["stage"] not in {"ready", "retiring", "retired"}:
                raise EnvironmentError("environment_retirement_invalid")
            if record["stage"] == "retired":
                return record
            pins = self.directory / "task-pins"
            if pins.exists():
                for path in pins.iterdir():
                    pin = _read(path)
                    if not pin:
                        raise EnvironmentError("environment_pin_invalid")
                    lifecycle = _read(Path(pin["lifecycle"]))
                    if (
                        not lifecycle
                        or lifecycle.get("identity") != pin["identity"]
                        or lifecycle.get("state") != "retired"
                    ):
                        raise EnvironmentError("environment_in_use")
            inventory = self.transport.inventory()
            present = any(name.casefold() == self.distribution.casefold() for name in inventory)
            if present:
                self._assert_registration(inventory, record)
                self._save(record, "retiring")
                # WSL owns VHD disposal. No computed recursive host deletion.
                self.transport.call(["--unregister", self.distribution], timeout=120)
            elif record["stage"] != "retiring":
                raise EnvironmentError("environment_distribution_missing")
            self._save(record, "retired")
            return record
