#!/usr/bin/env bash
# Demo 1：完整修复流程（calculator TypeError）
#
# 步骤：项目结构 → issue → pytest 失败 → repair → diff → pytest 通过 → 恢复
#
# 用法（仓库根目录）：
#   bash demo/demo_1_repair.sh
#   SKIP_VERIFY=1 bash demo/demo_1_repair.sh   # 无 Docker

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# shellcheck disable=SC1091
source "$ROOT/demo/demo_lib.sh"

REPO="demo/calculator"
ISSUE_FILE="$REPO/issue.txt"

demo_check_prereqs "$ROOT"

demo_section "Demo 1 — 完整修复流程 (calculator TypeError)"

echo ""
echo "① 项目结构"
ls -la "$REPO"
echo ""
find "$REPO" -maxdepth 1 -type f | sort

echo ""
echo "② Issue 描述 ($ISSUE_FILE)"
cat "$ISSUE_FILE"

demo_pytest_allow_fail "$REPO" "修复前"

echo ""
echo "③ 运行 Multi-Agent repair --verbose"
read -r -a extra <<< "$(demo_repair_extra_args)"
log="$(mktemp)"
if ! python -m src.cli repair \
  --issue "$(cat "$ISSUE_FILE")" \
  --repo "$REPO" \
  --verbose \
  "${extra[@]}" 2>&1 | tee "$log"; then
  rm -f "$log"
  demo_restore_repo "$ROOT" "$REPO"
  exit 1
fi

if grep -q '❌ 修复未完成' "$log"; then
  rm -f "$log"
  echo "错误: 修复未完成" >&2
  demo_restore_repo "$ROOT" "$REPO"
  exit 1
fi
rm -f "$log"

echo ""
echo "④ 补丁 diff 已在上方 stdout 打印（--- calculator.py ---）"

if ! demo_pytest_must_pass "$REPO" "修复后"; then
  demo_restore_repo "$ROOT" "$REPO"
  exit 1
fi

demo_restore_repo "$ROOT" "$REPO"
demo_pytest_allow_fail "$REPO" "恢复后（应再次失败）"

echo ""
echo "✓ Demo 1 完成"
