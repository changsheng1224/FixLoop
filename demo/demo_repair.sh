#!/usr/bin/env bash
# FixLoop E2E 演示：对 demo 项目跑 repair --verbose，结束后恢复 bug 状态。
#
# 前置条件（仓库根目录）：
#   1. cp .env.example .env 并填入 DEEPSEEK_API_KEY
#   2. Docker 运行中，镜像 repair-agent/python-repair:latest 已构建：
#        docker build -t repair-agent/python-repair:latest -f sandbox/Dockerfile.python sandbox/
#   3. pip install -e ".[dev]"  （或已安装项目依赖）
#
# 用法：
#   bash demo/demo_repair.sh              # 默认 calculator
#   bash demo/demo_repair.sh importer
#   bash demo/demo_repair.sh logic_bug
#   bash demo/demo_repair.sh all          # 依次跑 3 个 case
#   SKIP_VERIFY=1 bash demo/demo_repair.sh   # 无 Docker 时跳过验证

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

DEMO="${1:-calculator}"

load_env() {
  if [[ -f "$ROOT/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "$ROOT/.env"
    set +a
  fi
}

check_prereqs() {
  load_env
  if [[ -z "${DEEPSEEK_API_KEY:-}" ]]; then
    echo "错误: 未设置 DEEPSEEK_API_KEY。请 cp .env.example .env 并填入 API key。" >&2
    exit 1
  fi
  if ! python -c "import src.cli" 2>/dev/null; then
    echo "错误: 无法 import src.cli。请在仓库根目录执行: pip install -e ." >&2
    exit 1
  fi
  if [[ "${SKIP_VERIFY:-}" != "1" ]]; then
    if ! docker info >/dev/null 2>&1; then
      echo "错误: Docker 不可用。请启动 Docker，或设置 SKIP_VERIFY=1 跳过容器验证。" >&2
      exit 1
    fi
    if ! docker image inspect repair-agent/python-repair:latest >/dev/null 2>&1; then
      echo "提示: 未找到镜像 repair-agent/python-repair:latest，正在构建..." >&2
      docker build -t repair-agent/python-repair:latest -f sandbox/Dockerfile.python sandbox/
    fi
  fi
}

restore_repo() {
  local repo="$1"
  echo ""
  echo "--- 恢复 $repo 至 bug 状态 (git checkout) ---"
  git checkout -- "$repo"
}

run_pytest() {
  local repo="$1"
  local label="$2"
  echo ""
  echo "--- pytest $repo ($label) ---"
  pytest "$repo" -q
}

run_pytest_or_fail() {
  local repo="$1"
  local label="$2"
  echo ""
  echo "--- pytest $repo ($label) ---"
  if ! pytest "$repo" -q; then
    echo "错误: $label 时 pytest 未全部通过" >&2
    return 1
  fi
  return 0
}

run_pytest_allow_fail() {
  local repo="$1"
  local label="$2"
  echo ""
  echo "--- pytest $repo ($label) ---"
  if pytest "$repo" -q; then
    echo "(全部通过)"
  else
    echo "(存在失败用例)"
  fi
}

run_repair() {
  local name="$1"
  local repo="$2"
  local issue="$3"

  echo ""
  echo "============================================================"
  echo " Demo: $name"
  echo " Repo: $repo"
  echo "============================================================"

  run_pytest_allow_fail "$repo" "修复前"

  local extra=()
  if [[ "${SKIP_VERIFY:-}" == "1" ]]; then
    extra+=(--skip-verify)
  fi

  echo ""
  echo "--- python -m src.cli repair --verbose ---"
  local log
  log="$(mktemp)"
  if ! python -m src.cli repair \
    --issue "$issue" \
    --repo "$repo" \
    --verbose \
    "${extra[@]}" 2>&1 | tee "$log"; then
    rm -f "$log"
    echo "错误: repair 命令异常退出" >&2
    restore_repo "$repo"
    exit 1
  fi

  if grep -q '❌ 修复未完成' "$log"; then
    rm -f "$log"
    echo "错误: $name 修复失败" >&2
    restore_repo "$repo"
    exit 1
  fi
  rm -f "$log"

  if ! run_pytest_or_fail "$repo" "修复后（应全部通过）"; then
    restore_repo "$repo"
    exit 1
  fi
  restore_repo "$repo"
  run_pytest_allow_fail "$repo" "恢复后（应再次失败）"

  echo ""
  echo "✓ $name 演示完成"
}

run_calculator() {
  run_repair \
    "calculator (TypeError)" \
    "demo/calculator" \
    "TypeError: can only concatenate str (not 'int') to str at calculator.py:6 in add()"
}

run_importer() {
  run_repair \
    "importer (ImportError)" \
    "demo/importer" \
    "ModuleNotFoundError: No module named 'utils.helper' at app.py:3"
}

run_logic_bug() {
  run_repair \
    "logic_bug (off-by-one)" \
    "demo/logic_bug" \
    "AssertionError: assert [1, 2] == [1, 2, 3] at sequence.py:8 in iota()"
}

usage() {
  cat <<'EOF'
用法: bash demo/demo_repair.sh [calculator|importer|logic_bug|all]

在 demo 项目上运行完整 repair 流水线（含 --verbose），结束后 git checkout 恢复 bug 状态。

环境变量:
  SKIP_VERIFY=1   跳过 Docker 验证（仅生成补丁，status=patched）
EOF
}

main() {
  case "$DEMO" in
    -h|--help|help)
      usage
      exit 0
      ;;
    calculator)
      check_prereqs
      run_calculator
      ;;
    importer)
      check_prereqs
      run_importer
      ;;
    logic_bug)
      check_prereqs
      run_logic_bug
      ;;
    all)
      check_prereqs
      run_calculator
      run_importer
      run_logic_bug
      echo ""
      echo "============================================================"
      echo " 全部 3 个 demo 演示完成"
      echo "============================================================"
      ;;
    *)
      echo "未知 demo: $DEMO" >&2
      usage >&2
      exit 1
      ;;
  esac
}

main
