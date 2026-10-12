"""Installed and zip-imported distributions must serve every advertised asset."""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from src.workbench.assets import ASSETS, read_asset

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def distribution(tmp_path_factory):
    root = tmp_path_factory.mktemp("distribution")
    built = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            str(ROOT),
            "--no-deps",
            "--no-build-isolation",
            "--no-index",
            "--wheel-dir",
            str(root),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert built.returncode == 0, built.stdout + built.stderr
    (wheel,) = root.glob("*.whl")
    target = root / "installed"
    installed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--no-index",
            "--target",
            str(target),
            str(wheel),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert installed.returncode == 0, installed.stdout + installed.stderr
    return root, wheel, target


@pytest.mark.parametrize("layout", ["installed", "zip"])
def test_distribution_serves_assets_without_checkout(distribution, layout):
    root, wheel, installed = distribution
    entry = installed if layout == "installed" else wheel
    script = r"""
import hashlib, http.client, json, sys, threading
sys.path.insert(0, sys.argv[1])
from src.workbench import assets
from src.workbench.http import WorkbenchHTTPServer
assert str(assets.__file__).startswith(sys.argv[1]), assets.__file__
server = WorkbenchHTTPServer(('127.0.0.1', 0), object(), token='packaging-test-token-000000')
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
result = {}
try:
    for path in assets.ASSETS:
        conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
        conn.request('GET', path)
        response = conn.getresponse()
        assert response.status == 200, (path, response.status)
        result[path] = [hashlib.sha256(response.read()).hexdigest(), response.getheader('Content-Type')]
        conn.close()
finally:
    server.shutdown()
    server.server_close()
    thread.join(5)
print(json.dumps(result))
"""
    child = subprocess.run(
        [sys.executable, "-I", "-c", script, str(entry)],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert child.returncode == 0, child.stdout + child.stderr
    expected = {
        path: [hashlib.sha256(read_asset(path)[0]).hexdigest(), ASSETS[path][1]] for path in ASSETS
    }
    assert json.loads(child.stdout) == expected
