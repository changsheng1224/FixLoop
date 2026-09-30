"""Narrow, fixed Python/bwrap profile for the P1 WSL environment."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from agent_runtime.sensitive_paths import is_sensitive_path

from .models import SandboxRequest

LIBRARIES = (
    "libbz2.so.1.0",
    "libc.so.6",
    "libcrypto.so.3",
    "libdb-5.3.so",
    "libexpat.so.1",
    "libffi.so.8",
    "libgdbm.so.6",
    "liblzma.so.5",
    "libm.so.6",
    "libncursesw.so.6",
    "libpanelw.so.6",
    "libreadline.so.8",
    "libsqlite3.so.0",
    "libssl.so.3",
    "libtinfo.so.6",
    "libuuid.so.1",
    "libz.so.1",
    "libzstd.so.1",
)
_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}$")


@dataclass(frozen=True)
class SandboxPolicy:
    workspace: Path
    state_root: Path
    toolchain: Path
    helper: Path
    bwrap: Path = Path("/usr/bin/bwrap")
    python: Path = Path("/usr/bin/python3.14")
    lib_dir: Path = Path("/usr/lib/x86_64-linux-gnu")
    temp_limit_bytes: int = 67108864
    output_limit_bytes: int = 1048576
    max_timeout_s: int = 120

    def validate(self) -> None:
        if sys.platform != "linux":
            raise ValueError("wsl_unavailable")
        if "microsoft" not in os.uname().release.casefold():
            raise ValueError("wsl_unavailable: WSL2 kernel required")
        release = Path("/etc/os-release").read_text(encoding="utf-8")
        if "ID=ubuntu\n" not in release or 'VERSION_ID="26.04"' not in release:
            raise ValueError("distribution_mismatch")
        try:
            workspace, state, toolchain, helper = (
                p.resolve(strict=True)
                for p in (self.workspace, self.state_root, self.toolchain, self.helper)
            )
        except (OSError, RuntimeError) as exc:
            raise ValueError("workspace_mapping_rejected: missing trusted path") from exc
        if not workspace.is_dir() or not state.is_dir() or not toolchain.is_dir():
            raise ValueError("workspace_mapping_rejected")
        if any(p == workspace or workspace in p.parents for p in (state, toolchain, helper)):
            raise ValueError("workspace_mapping_rejected: trusted paths inside workspace")
        if str(workspace).startswith(("/mnt/", "/run/")):
            raise ValueError("workspace_mapping_rejected: non-native workspace")
        try:
            filesystem = subprocess.run(
                ["/usr/bin/stat", "-f", "-c", "%T", str(workspace)],
                capture_output=True,
                text=True,
                check=False,
                timeout=2,
                env={"PATH": "/usr/bin:/bin"},
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError("workspace_mapping_rejected: filesystem probe") from exc
        if filesystem.returncode or filesystem.stdout.strip() != "ext2/ext3":
            raise ValueError("workspace_mapping_rejected: expected WSL ext4")
        if not self.bwrap.is_file():
            raise ValueError("bwrap_unavailable")
        if not self.python.is_file():
            raise ValueError("toolchain_unavailable")
        if not (toolchain / "bin/python").exists() or not (toolchain / "bin/pytest").exists():
            raise ValueError("toolchain_unavailable")
        if (workspace / ".agent").exists() or (workspace / ".git").is_symlink():
            raise ValueError("workspace_mapping_rejected: control or git path")
        for path in workspace.rglob("*"):
            relative = path.relative_to(workspace)
            if ".git" in relative.parts:
                continue  # The entire .git mount is hidden from the target.
            if is_sensitive_path(relative):
                raise ValueError("workspace_mapping_rejected: credential-like asset")

    def digest(self) -> str:
        paths = [
            self.bwrap,
            self.python,
            self.lib_dir / "ld-linux-x86-64.so.2",
            self.toolchain / "pyvenv.cfg",
            self.helper,
            self.helper.parents[1] / "path_safety.py",
            self.helper.parents[1] / "sensitive_paths.py",
        ]
        paths.extend(sorted(self.helper.parent.glob("*.py")))
        paths.extend(self.lib_dir / name for name in LIBRARIES)
        identities = []
        for path in paths:
            stat = path.stat()
            identities.append(
                (str(path.resolve()), stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
            )
        # Both recursive read-only binds are part of the profile identity.
        for root in (Path("/usr/lib/python3.14"), self.toolchain):
            for path in sorted(root.rglob("*")):
                if path.is_symlink():
                    identities.append((str(path.relative_to(root)), "link", str(path.readlink())))
                elif path.is_file():
                    identities.append(
                        (str(path.relative_to(root)), hashlib.sha256(path.read_bytes()).hexdigest())
                    )
        value = (
            "p1-v1",
            str(self.workspace.resolve()),
            self.workspace.stat().st_dev,
            self.workspace.stat().st_ino,
            str(self.state_root.resolve()),
            str(self.toolchain.resolve()),
            bool((self.workspace / ".git").exists()),
            self.temp_limit_bytes,
            self.output_limit_bytes,
            self.max_timeout_s,
            identities,
            os.uname().release,
            Path("/etc/os-release").read_text(encoding="utf-8"),
            Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        )
        return hashlib.sha256(json.dumps(value).encode()).hexdigest()

    def validate_request(self, request: SandboxRequest) -> Path:
        for identifier in (request.workspace_id, request.task_id, request.run_id, request.call_id):
            if not isinstance(identifier, str) or not _ID.fullmatch(identifier):
                raise ValueError("policy_denied: invalid identity")
        if request.schema_version != "1" or request.operation not in ("command", "pytest"):
            raise ValueError("policy_denied: invalid operation")
        if (
            not isinstance(request.argv, tuple)
            or not request.argv
            or not all(isinstance(arg, str) and arg and "\x00" not in arg for arg in request.argv)
        ):
            raise ValueError("policy_denied: invalid argv")
        if request.argv[0] != "/toolchain/bin/python":
            raise ValueError("policy_denied: executable not in profile")
        if request.operation == "pytest" and request.argv[1:4] != ("-I", "-m", "pytest"):
            raise ValueError("policy_denied: pytest profile")
        if (
            not isinstance(request.timeout_s, int | float)
            or not 0 < request.timeout_s <= self.max_timeout_s
        ):
            raise ValueError("policy_denied: timeout")
        if (
            not isinstance(request.output_limit_bytes, int)
            or not 0 < request.output_limit_bytes <= self.output_limit_bytes
        ):
            raise ValueError("policy_denied: output limit")
        if not isinstance(request.cwd_relative, str) or not request.cwd_relative:
            raise ValueError("workspace_mapping_rejected: cwd")
        from agent_runtime.path_safety import resolve_under_root

        cwd = resolve_under_root(self.workspace, request.cwd_relative)
        if not cwd.is_dir():
            raise ValueError("workspace_mapping_rejected: cwd is not a directory")
        return cwd.relative_to(self.workspace.resolve())

    def argv(self, request: SandboxRequest) -> list[str]:
        cwd = self.validate_request(request)
        lib = self.lib_dir
        args = [
            str(self.bwrap),
            "--unshare-user",
            "--unshare-pid",
            "--unshare-net",
            "--unshare-ipc",
            "--unshare-uts",
            "--die-with-parent",
            "--new-session",
            "--clearenv",
            "--dir",
            "/usr",
            "--dir",
            "/usr/bin",
            "--dir",
            "/usr/lib",
            "--dir",
            str(lib),
            "--dir",
            "/lib64",
            "--ro-bind",
            str(self.python),
            str(self.python),
            "--symlink",
            "python3.14",
            "/usr/bin/python3",
            "--ro-bind",
            "/usr/lib/python3.14",
            "/usr/lib/python3.14",
            "--ro-bind",
            str(lib / "ld-linux-x86-64.so.2"),
            "/lib64/ld-linux-x86-64.so.2",
        ]
        for name in LIBRARIES:
            args += ["--ro-bind", str(lib / name), str(lib / name)]
        args += [
            "--ro-bind",
            str(self.toolchain),
            "/toolchain",
            "--bind",
            str(self.workspace),
            "/workspace",
        ]
        if (self.workspace / ".git").exists():
            args += ["--tmpfs", "/workspace/.git"]
        args += [
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--size",
            str(self.temp_limit_bytes),
            "--tmpfs",
            "/tmp",
            "--dir",
            "/home",
            "--symlink",
            "/tmp",
            "/home/sandbox",
            "--setenv",
            "PATH",
            "/toolchain/bin",
            "--setenv",
            "HOME",
            "/home/sandbox",
            "--setenv",
            "TMPDIR",
            "/tmp",
            "--setenv",
            "LANG",
            "C.UTF-8",
            "--setenv",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD",
            "1",
            "--chdir",
            "/workspace" + ("/" + cwd.as_posix() if cwd != Path(".") else ""),
            "--",
            *request.argv,
        ]
        return args
