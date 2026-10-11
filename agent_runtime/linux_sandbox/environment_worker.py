"""Trusted provisioning/health worker for the dedicated distribution, never a tool worker."""

from __future__ import annotations

import hashlib
import io
import json
import os
import platform
import socket
import subprocess
import sys
import tarfile
from pathlib import Path, PurePosixPath

PREFIX = Path("/opt/fixloop")
RUNTIME = PREFIX / "runtime"
TOOLCHAIN = PREFIX / "toolchain"


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def run(argv: list[str], *, timeout: int = 120) -> str:
    proc = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd="/",
        env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
        check=False,
    )
    if proc.returncode:
        raise ValueError("environment_command_failed: " + argv[0])
    return proc.stdout.strip()


def install(raw: bytes) -> None:
    """The host verifies the complete payload before sending it over stdin."""
    if os.getuid() != 0:
        raise ValueError("environment_install_requires_root")
    with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
        members = archive.getmembers()
        names = [member.name for member in members]
        if len(set(names)) != len(names) or any(
            not member.isfile()
            or PurePosixPath(member.name).is_absolute()
            or ".." in PurePosixPath(member.name).parts
            or "\\" in member.name
            for member in members
        ):
            raise ValueError("environment_payload_invalid")
        manifest = json.loads(archive.extractfile("payload.json").read())
        if set(names) != {*manifest["files"], "payload.json"}:
            raise ValueError("environment_payload_invalid")
        contents = {}
        for member in members:
            data = archive.extractfile(member).read()
            if member.name != "payload.json" and digest(data) != manifest["files"][member.name]:
                raise ValueError("environment_payload_integrity")
            contents[member.name] = data
    RUNTIME.mkdir(parents=True, exist_ok=True, mode=0o755)
    for name, data in contents.items():
        target = RUNTIME / name
        if target.is_symlink():
            raise ValueError("environment_payload_alias")
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        temporary = target.with_name(target.name + ".installing")
        with temporary.open("wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o644)
        temporary.replace(target)
    run(["/usr/bin/python3.14", "-I", "-B", "-m", "venv", "--clear", str(TOOLCHAIN)])
    wheels = [str(RUNTIME / "wheels" / wheel["name"]) for wheel in manifest["release"]["wheels"]]
    run(
        [
            str(TOOLCHAIN / "bin/python"),
            "-I",
            "-B",
            "-m",
            "pip",
            "--isolated",
            "install",
            "--no-index",
            "--no-deps",
            "--no-compile",
            "--disable-pip-version-check",
            *wheels,
        ]
    )
    run([str(TOOLCHAIN / "bin/python"), "-I", "-B", "-m", "pip", "--isolated", "check"])
    user = subprocess.run(["/usr/bin/id", "-u", "fixloop"], capture_output=True, check=False)
    if user.returncode:
        run(
            [
                "/usr/sbin/useradd",
                "--uid",
                "1000",
                "--create-home",
                "--shell",
                "/usr/sbin/nologin",
                "fixloop",
            ]
        )
    elif user.stdout.strip() != b"1000":
        raise ValueError("environment_user_conflict")
    # Only this product-owned distro is configured; never touch the user's WSL distro.
    Path("/etc/wsl.conf").write_text(
        "[user]\ndefault=fixloop\n[interop]\nenabled=false\nappendWindowsPath=false\n"
        "[automount]\nenabled=false\nmountFsTab=false\n",
        encoding="utf-8",
    )


def measure() -> dict:
    """Measure the installed closure, including permissions and symlink targets."""
    manifest = json.loads((RUNTIME / "payload.json").read_text(encoding="utf-8"))
    for name, expected in manifest["files"].items():
        path = RUNTIME / name
        if path.is_symlink() or digest(path.read_bytes()) != expected:
            raise ValueError("environment_integrity_failed: payload")
    release = manifest["release"]
    os_release = Path("/etc/os-release").read_text(encoding="utf-8")
    if 'VERSION_ID="' + release["distribution_version"] + '"' not in os_release:
        raise ValueError("environment_version_mismatch: distribution")
    if (
        "microsoft" not in platform.release().casefold()
        or platform.machine() != release["architecture"]
    ):
        raise ValueError("environment_platform_mismatch")
    for package in release["packages"]:
        name, version = package.split("=", 1)
        actual = run(["/usr/bin/dpkg-query", "-W", "-f", "${Version}", name])
        if actual != version:
            raise ValueError("environment_version_mismatch: " + name)
    entries = {}
    roots = [RUNTIME, TOOLCHAIN, Path("/usr/lib/python3.14"), Path("/usr/lib/x86_64-linux-gnu")]
    files = [
        Path("/usr/bin/python3.14"),
        Path("/usr/bin/bwrap"),
        Path("/etc/wsl.conf"),
        Path("/etc/apt/sources.list.d/ubuntu.sources"),
        Path("/etc/ssl/certs/ca-certificates.crt"),
        Path("/etc/passwd"),
        Path("/etc/group"),
        Path("/lib"),
        Path("/lib64"),
        Path("/usr/lib64/ld-linux-x86-64.so.2"),
        Path("/var/lib/dpkg/status"),
    ]
    for root in roots:
        files.extend(sorted(root.rglob("*")))
    for path in files:
        stat = path.lstat()
        if stat.st_uid != 0 or (not path.is_symlink() and stat.st_mode & 0o022):
            raise ValueError("environment_integrity_failed: writable runtime")
        if path.is_symlink():
            entries[str(path)] = ["link", str(path.readlink()), stat.st_mode]
        elif path.is_file():
            entries[str(path)] = [digest(path.read_bytes()), stat.st_mode]
    return {
        "schema_version": 1,
        "release_id": manifest["release_id"],
        "runtime_digest": digest(json.dumps(entries, sort_keys=True).encode()),
        "distribution_version": release["distribution_version"],
        "runtime_version": release["version"],
        "python": run([str(TOOLCHAIN / "bin/python"), "-I", "-B", "--version"]),
        "pytest": run([str(TOOLCHAIN / "bin/python"), "-I", "-B", "-m", "pytest", "--version"]),
        "bwrap": run(["/usr/bin/bwrap", "--version"]),
        "kernel": platform.release(),
    }


def health() -> dict:
    if os.getuid() != 1000:
        raise ValueError("environment_health_requires_task_user")
    observed = measure()
    # An environment-only probe: no project/workspace is mounted or executed.
    # This wider system bind is never offered as a production execution profile.
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            pass
        code = (
            "import os,json,pathlib,socket; s=socket.socket(); s.settimeout(1); "
            "print(json.dumps(dict(pid=os.getpid(),tmp=os.statvfs('/tmp').f_blocks*"
            f"os.statvfs('/tmp').f_frsize,net=s.connect_ex(('127.0.0.1',{port})),"
            "mnt=pathlib.Path('/mnt').exists(),run=pathlib.Path('/run').exists(),"
            "state=pathlib.Path('/opt/fixloop').exists(),env=sorted(os.environ))))"
        )
        result = json.loads(
            run(
                [
                    "/usr/bin/bwrap",
                    "--unshare-all",
                    "--unshare-user",
                    "--disable-userns",
                    "--die-with-parent",
                    "--new-session",
                    "--cap-drop",
                    "ALL",
                    "--clearenv",
                    "--ro-bind",
                    "/usr",
                    "/usr",
                    "--symlink",
                    "usr/lib",
                    "/lib",
                    "--symlink",
                    "usr/lib64",
                    "/lib64",
                    "--proc",
                    "/proc",
                    "--dev",
                    "/dev",
                    "--size",
                    "67108864",
                    "--tmpfs",
                    "/tmp",
                    "--dir",
                    "/work",
                    "--chdir",
                    "/work",
                    "--setenv",
                    "LANG",
                    "C.UTF-8",
                    "--",
                    "/usr/bin/python3.14",
                    "-I",
                    "-B",
                    "-c",
                    code,
                ],
                timeout=15,
            )
        )
    if (
        result["pid"] != 2
        or result["tmp"] != 67108864
        or result["net"] == 0
        or result["mnt"]
        or result["run"]
        or result["state"]
        or set(result["env"]) - {"LANG", "LC_CTYPE", "PWD"}
    ):
        raise ValueError("environment_health_failed")
    descriptor = os.open("/dev/fuse", os.O_RDWR | os.O_CLOEXEC)
    os.close(descriptor)
    observed["checks"] = {
        "unprivileged_namespaces": True,
        "network_namespace": True,
        "tmpfs_limit": True,
        "clean_environment": True,
        "control_paths_hidden": True,
        "fuse_device": True,
    }
    return observed


if __name__ == "__main__":
    try:
        if sys.argv[1:] == ["install"]:
            install(sys.stdin.buffer.read(64 * 1024 * 1024 + 1))
            print(json.dumps({"installed": True}))
        elif sys.argv[1:] == ["measure"]:
            print(json.dumps(measure()))
        elif sys.argv[1:] == ["health"]:
            print(json.dumps(health()))
        else:
            raise ValueError("environment_operation_invalid")
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print(json.dumps({"error": str(exc).split(":", 1)[0]}))
        raise SystemExit(1) from exc
