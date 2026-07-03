#!/usr/bin/env bash
# 共享函数：M8D3 demo 脚本 source 此文件。
# shellcheck disable=SC2034  # ROOT 由调用方使用

demo_root() {
  local here
  here="$(cd "$(dirname "${BASH_SOURCE[1]}")/.." && pwd)"
  echo "$here"
}

demo_load_env() {
  local root="$1"
  if [[ -f "$root/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "$root/.env"
    set +a
  fi
}

demo_check_prereqs() {
  local root="$1"
  demo_load_env "$root"
  if [[ -z "${DEEPSEEK_API_KEY:-}" && "${ALLOW_FAKE:-}" != "1" ]]; then
    echo "错误: 未设置 DEEPSEEK_API_KEY。请 cp .env.example .env 并填入 API key。" >&2
    echo "提示: 消融演示可设 ALLOW_FAKE=1（仅 demo_3）。" >&2
    exit 1
  fi
  if ! python -c "import src.cli" 2>/dev/null; then
    echo "错误: 无法 import src.cli。请执行: pip install -e \".[dev]\"" >&2
    exit 1
  fi
  if [[ "${SKIP_VERIFY:-}" != "1" && "${ALLOW_FAKE:-}" != "1" ]]; then
    if ! docker info >/dev/null 2>&1; then
      echo "错误: Docker 不可用。请启动 Docker，或 SKIP_VERIFY=1。" >&2
      exit 1
    fi
    if ! docker image inspect repair-agent/python-repair:latest >/dev/null 2>&1; then
      echo "提示: 构建镜像 repair-agent/python-repair:latest ..." >&2
      docker build -t repair-agent/python-repair:latest -f sandbox/Dockerfile.python sandbox/
    fi
  fi
}

demo_section() {
  echo ""
  echo "============================================================"
  echo " $*"
  echo "============================================================"
}

demo_restore_repo() {
  local root="$1"
  local repo="$2"
  echo ""
  echo "--- 恢复 $repo 至 bug 状态 ---"
  git -C "$root" checkout -- "$repo"
}

demo_pytest_allow_fail() {
  local repo="$1"
  local label="$2"
  echo ""
  echo "--- pytest $repo ($label) ---"
  if pytest "$repo" -q; then
    echo "(全部通过)"
  else
    echo "(存在失败用例 — 符合演示预期)"
  fi
}

demo_pytest_must_pass() {
  local repo="$1"
  local label="$2"
  echo ""
  echo "--- pytest $repo ($label) ---"
  if ! pytest "$repo" -q; then
    echo "错误: $label 时 pytest 未全部通过" >&2
    return 1
  fi
  echo "(全部通过)"
  return 0
}

demo_repair_extra_args() {
  if [[ "${SKIP_VERIFY:-}" == "1" ]]; then
    echo --skip-verify
  fi
}
