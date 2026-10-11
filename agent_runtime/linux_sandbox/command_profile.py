"""S4 declarative Python profile. No shell, network installer or source builds."""

from __future__ import annotations

from .workspace_contract import MAX_REQUEST_BYTES, relative_path


def test_target(raw: str) -> str:
    file, sep, selector = raw.partition("::")
    file = relative_path(file)
    if (
        not file.endswith(".py")
        or (sep and not selector)
        or any(
            not part or part.startswith("-") or any(ord(c) < 32 for c in part)
            for part in selector.split("::")
            if sep
        )
    ):
        raise ValueError("policy_denied: invalid pytest target")
    return file + (sep + selector if sep else "")


def command_spec(request) -> tuple[str, tuple[str, ...]]:
    """Validate again at the trusted controller; paths are checked inside bwrap."""
    if (
        request.operation not in {"command", "pytest"}
        or request.argv[:1] != ("/toolchain/bin/python",)
        or not 0 < request.timeout_s <= 120
        or not 0 < request.output_limit_bytes <= 65536
        or sum(len(s.encode()) for s in request.argv) > MAX_REQUEST_BYTES // 2
    ):
        raise ValueError("policy_denied: restricted Python command profile")
    relative_path(request.cwd_relative, allow_root=True)
    args = list(request.argv[1:])
    if args[:1] == ["-I"]:
        args.pop(0)
    if args[:1] == ["-B"]:
        args.pop(0)
    if args[:2] == ["-m", "pytest"]:
        args = args[2:]
        targets = []
        for arg in args:
            if arg in {"-q", "-v", "--tb=line", "--tb=short", "--maxfail=3"}:
                continue
            targets.append(test_target(arg))
        return "pytest", tuple(targets)
    if request.operation == "pytest":
        raise ValueError("policy_denied: pytest profile required")
    if args[:2] == ["-m", "pip"]:
        wheels = args[3:] if args[2:3] == ["install"] else []
        if not wheels or len(wheels) > 32:
            raise ValueError("policy_denied: offline wheel install required")
        paths = tuple(relative_path(wheel) for wheel in wheels)
        if any(not path.endswith(".whl") for path in paths):
            raise ValueError("policy_denied: only local wheels are supported")
        return "install", paths
    if len(args) == 2 and args[0] == "-c":
        return "code", (args[1],)
    if args and not args[0].startswith("-"):
        return "script", (relative_path(args[0]), *args[1:])
    raise ValueError("policy_denied: use Python script, -c, pytest or offline pip install")


def install_inputs(request):
    """Workspace-relative inputs actually opened from the command's cwd."""
    mode, parameters = command_spec(request)
    if mode != "install":
        return ()
    cwd = relative_path(request.cwd_relative, allow_root=True)
    return tuple(relative_path(p if cwd == "." else cwd + "/" + p) for p in parameters)


def bootstrap(mode: str, args: tuple[str, ...], cwd: str) -> str:
    """Fixed import paths; ignore PYTHONPATH, site hooks and repository executables."""
    import json

    return (
        "import sys,os,runpy; from pathlib import Path; "
        "sys.path[:0]=['/task/deps','/toolchain/lib/python3.14/site-packages',"
        "'/workspace','/workspace/src']; "
        f"mode,args,cwd={json.dumps([mode, list(args), cwd])}; "
        "root=Path('/workspace'); os.chdir(root/cwd); "
        "\nif mode=='pytest':\n"
        " for target in args:\n"
        "  if not Path(target.split('::',1)[0]).is_file():\n"
        "   raise ValueError('pytest target missing')\n"
        " import pytest\n"
        " raise SystemExit(pytest.main([*args,'-q','--tb=line','--maxfail=3',"
        "'-p','no:cacheprovider','--basetemp=/tmp/pytest']))\n"
        "elif mode=='install':\n"
        " for p in ('/task/deps','/workspace','/workspace/src'): sys.path.remove(p)\n"
        " sys.argv=['pip','--isolated','install','--no-index','--no-deps','--only-binary=:all:',"
        "'--no-compile','--no-cache-dir','--disable-pip-version-check','--target=/task/deps',"
        "*[str(Path.cwd()/p) for p in args]]\n"
        " runpy.run_module('pip',run_name='__main__')\n"
        "elif mode=='code':\n"
        " sys.argv=['-c']; exec(compile(args[0],'<command>','exec'),{'__name__':'__main__'})\n"
        "else:\n"
        " path=Path.cwd()/args[0]; sys.argv=list(args); sys.path.insert(0,str(path.parent))\n"
        " runpy.run_path(str(path),run_name='__main__')\n"
    )
