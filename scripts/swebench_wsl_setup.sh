#!/usr/bin/env bash
# WSL Ubuntu: install SWE-bench harness deps.
# Usage from Windows:
#   wsl -d Ubuntu -- bash /mnt/c/Users/haoyu/Documents/FixLoop/scripts/swebench_wsl_setup.sh
set -euo pipefail

echo "[swebench-wsl] apt packages..."
sudo apt-get update -y
sudo apt-get install -y python3 python3-pip python3-venv git curl

echo "[swebench-wsl] pip swebench + datasets..."
python3 -m pip install -U pip
python3 -m pip install -U 'swebench>=3.0' 'datasets>=2.14'

echo "[swebench-wsl] verify..."
python3 -c "import resource, swebench; print('ok', getattr(swebench, '__version__', ''))"

if command -v docker >/dev/null 2>&1; then
  docker version --format 'docker {{.Server.Version}}' || true
else
  echo "[swebench-wsl] WARN: docker not in PATH; enable Docker Desktop WSL integration"
fi

echo "[swebench-wsl] done"
