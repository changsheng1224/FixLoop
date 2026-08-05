"""Windows ↔ WSL 路径与发行版探测（P0 harness 用）。"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass
class WslProbe:
    available: bool
    distros: list[str]
    preferred: str | None
    error: str = ""
    note: str = ""


_SKIP_DISTROS = frozenset(
    {
        "docker-desktop",
        "docker-desktop-data",
        "rancher-desktop",
        "podman-machine-default",
    }
)


def is_windows() -> bool:
    return sys.platform.startswith("win")


def win_to_wsl_path(path: Path | str) -> str:
    """``C:\\Users\\a\\b`` → ``/mnt/c/Users/a/b``。"""
    p = Path(path).resolve()
    s = str(p)
    # pathlib on Windows: C:\...
    m = re.match(r"^([A-Za-z]):[\\/](.*)$", s)
    if not m:
        # already posix-ish
        return s.replace("\\", "/")
    drive, rest = m.group(1).lower(), m.group(2).replace("\\", "/")
    return f"/mnt/{drive}/{rest}"


def list_wsl_distros() -> list[str]:
    """返回 WSL 发行版名（去掉 docker-desktop 等仍列入，由 preferred 过滤）。"""
    if not is_windows():
        return []
    try:
        proc = subprocess.run(
            ["wsl", "--list", "--quiet"],
            capture_output=True,
            check=False,
        )
    except OSError:
        return []
    # wsl 常输出 UTF-16LE
    raw = proc.stdout or b""
    text = ""
    for enc in ("utf-16-le", "utf-8", "gbk"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    names: list[str] = []
    for line in text.splitlines():
        name = line.strip().strip("\x00").strip()
        if name:
            names.append(name)
    return names


def preferred_wsl_distro(distros: list[str] | None = None) -> str | None:
    env = os.environ.get("FIXLOOP_WSL_DISTRO", "").strip()
    names = distros if distros is not None else list_wsl_distros()
    if env:
        return env if env in names or not names else env
    usable = [n for n in names if n.lower() not in _SKIP_DISTROS]
    # 优先 Ubuntu / Debian
    for key in ("Ubuntu", "Debian", "kali"):
        for n in usable:
            if key.lower() in n.lower():
                return n
    return usable[0] if usable else None


def resolve_wsl_python(explicit: str | None = None) -> str:
    """优先显式路径 / 环境变量 / ``~/.venvs/swebench/bin/python`` / ``python3``。"""
    if explicit:
        return explicit
    env = os.environ.get("FIXLOOP_WSL_PYTHON", "").strip()
    if env:
        return env
    # 约定路径（setup 脚本安装位置）
    candidate = "$HOME/.venvs/swebench/bin/python"
    # 在 WSL 里展开检测
    distro = preferred_wsl_distro()
    if distro:
        check = run_wsl(
            ["bash", "-lc", "test -x \"$HOME/.venvs/swebench/bin/python\" && echo yes"],
            distro=distro,
            timeout_s=15,
        )
        if check.returncode == 0 and "yes" in (check.stdout or ""):
            # 用 login 展开后的绝对路径更稳
            which = run_wsl(
                ["bash", "-lc", "echo \"$HOME/.venvs/swebench/bin/python\""],
                distro=distro,
                timeout_s=15,
            )
            path = (which.stdout or "").strip()
            if path:
                return path
    return "python3"


def probe_wsl() -> WslProbe:
    if not is_windows():
        return WslProbe(available=False, distros=[], preferred=None, error="not Windows")
    if shutil_which_wsl() is None:
        return WslProbe(
            available=False,
            distros=[],
            preferred=None,
            error="wsl.exe not found",
            note="Install WSL2: wsl --install -d Ubuntu",
        )
    distros = list_wsl_distros()
    pref = preferred_wsl_distro(distros)
    if not pref:
        return WslProbe(
            available=False,
            distros=distros,
            preferred=None,
            error="no suitable WSL distro (only docker-desktop?)",
            note="Install a Linux distro: wsl --install -d Ubuntu",
        )
    py = resolve_wsl_python()
    check = run_wsl(
        [py, "-c", "import resource; print('ok')"],
        distro=pref,
        timeout_s=30,
    )
    if check.returncode != 0:
        return WslProbe(
            available=False,
            distros=distros,
            preferred=pref,
            error=f"python/resource unavailable in {pref}: {(check.stderr or check.stdout)[:300]}",
            note=(
                f"In WSL ({pref}): bash /mnt/c/.../scripts/swebench_wsl_venv_install.sh "
                f"or set FIXLOOP_WSL_PYTHON"
            ),
        )
    # swebench 可选提示
    sb = run_wsl(
        [py, "-c", "import swebench; print('swebench-ok')"],
        distro=pref,
        timeout_s=30,
    )
    note = f"python={py}"
    if sb.returncode != 0:
        note += "; swebench missing — run scripts/swebench_wsl_venv_install.sh"
    return WslProbe(available=True, distros=distros, preferred=pref, note=note)


def shutil_which_wsl() -> str | None:
    from shutil import which

    return which("wsl")


_IP_RE = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}$")
_NAMESERVER_RE = re.compile(r"(?m)^nameserver\s+(\S+)")


def _parse_host_ip(text: str) -> str:
    """从 resolv.conf / 命令输出中抽出合法 IPv4；拒绝带空格的脏值。"""
    raw = (text or "").strip()
    if _IP_RE.match(raw):
        return raw
    m = _NAMESERVER_RE.search(text or "")
    if m and _IP_RE.match(m.group(1)):
        return m.group(1)
    # 容忍 ``nameserver 1.2.3.4`` 整行被当成输出
    parts = raw.split()
    for p in parts:
        if _IP_RE.match(p):
            return p
    return ""


def _windows_host_ip_for_wsl(distro: str | None = None) -> str:
    """WSL2 访问 Windows 主机（代理）常用 nameserver IP。

    注意：经 ``wsl.exe`` 传递时 ``awk '{print $2}'`` 里的 ``$2`` 会被吞掉，
    因此改用 sed/cut，并在 Python 侧再校验。
    """
    r = run_wsl(
        ["bash", "-lc", "sed -n 's/^nameserver //p' /etc/resolv.conf | head -1"],
        distro=distro,
        timeout_s=15,
    )
    ip = _parse_host_ip(r.stdout or "")
    if ip:
        return ip
    # 回退：读整文件再解析
    r2 = run_wsl(
        ["bash", "-lc", "cat /etc/resolv.conf"],
        distro=distro,
        timeout_s=15,
    )
    return _parse_host_ip(r2.stdout or "")


def _rewrite_loopback_proxy(url: str, host_ip: str) -> str:
    if not url or not host_ip:
        return url
    return url.replace("127.0.0.1", host_ip).replace("localhost", host_ip)


def wsl_proxy_env(distro: str | None = None) -> dict[str, str]:
    """把 Windows 侧代理 / HF 缓存映射到 WSL。"""
    out: dict[str, str] = {}
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "HF_ENDPOINT"):
        val = os.environ.get(key, "").strip()
        if val:
            out[key] = val

    user = os.environ.get("USERNAME") or os.environ.get("USER") or ""
    if user:
        hf = f"/mnt/c/Users/{user}/.cache/huggingface"
        out.setdefault("HF_HOME", hf)
        out.setdefault("HUGGINGFACE_HUB_CACHE", f"{hf}/hub")
        # 优先离线读 Windows 已缓存的 Lite（无外网时）
        if os.environ.get("HF_HUB_OFFLINE", "").strip() or os.path.isdir(
            os.path.join(os.path.expanduser("~"), ".cache", "huggingface", "hub",
                         "datasets--princeton-nlp--SWE-bench_Lite")
        ):
            out.setdefault("HF_HUB_OFFLINE", "1")

    # Windows 默认 Clash 端口；WSL 内 127.0.0.1 不是宿主机
    if not (out.get("HTTPS_PROXY") or out.get("https_proxy") or out.get("HTTP_PROXY")):
        if is_windows():
            out["HTTP_PROXY"] = "http://127.0.0.1:7897"
            out["HTTPS_PROXY"] = "http://127.0.0.1:7897"
            out["http_proxy"] = out["HTTP_PROXY"]
            out["https_proxy"] = out["HTTPS_PROXY"]

    needs_map = any(
        v and ("127.0.0.1" in v or "localhost" in v)
        for k, v in out.items()
        if k.lower() in ("http_proxy", "https_proxy", "all_proxy")
    )
    if needs_map:
        host_ip = _windows_host_ip_for_wsl(distro)
        if host_ip:
            for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY"):
                if key in out:
                    out[key] = _rewrite_loopback_proxy(out[key], host_ip)
    return out


@dataclass
class WslCmdResult:
    returncode: int
    stdout: str
    stderr: str


def run_wsl(
    argv: list[str],
    *,
    distro: str | None = None,
    cwd_wsl: str | None = None,
    timeout_s: int = 3600,
    env_exports: dict[str, str] | None = None,
) -> WslCmdResult:
    """在 WSL 中执行命令（非 login shell）。"""
    distro = distro or preferred_wsl_distro()
    if not distro:
        return WslCmdResult(127, "", "no WSL distro")
    prefix = ["wsl", "-d", distro, "--"]
    # 可选 cd + env
    if cwd_wsl or env_exports:
        parts = []
        if env_exports:
            for k, v in env_exports.items():
                parts.append(f"export {k}={_shell_quote(v)}")
        if cwd_wsl:
            parts.append(f"cd {_shell_quote(cwd_wsl)}")
        parts.append(" ".join(_shell_quote(a) for a in argv))
        cmd = prefix + ["bash", "-lc", " && ".join(parts)]
    else:
        cmd = prefix + argv
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout_s,
        check=False,
    )
    return WslCmdResult(proc.returncode, proc.stdout or "", proc.stderr or "")


def _shell_quote(s: str) -> str:
    return "'" + s.replace("'", "'\"'\"'") + "'"
