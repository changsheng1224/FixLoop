#!/usr/bin/env bash
set -euo pipefail
VENV="$HOME/.venvs/swebench"
mkdir -p "$HOME/.venvs"
python3 -m venv "$VENV"
"$VENV/bin/pip" install -U pip
"$VENV/bin/pip" install -U 'swebench>=3.0' 'datasets>=2.14'
"$VENV/bin/python" -c 'import resource, swebench; print("ok", getattr(swebench, "__version__", ""))'
echo "VENV=$VENV"
