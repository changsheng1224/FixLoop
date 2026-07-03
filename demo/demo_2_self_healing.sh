#!/usr/bin/env bash
# Demo 2：Verifier 自愈循环（case_006 off-by-one）
#
# 展示：第 1 次 patch → pytest 失败 → 回滚 + feedback → 再次 patch → 通过
# Orchestrator stderr 会打印 Patcher 开始 (retry=N)。
#
# 用法：
#   bash demo/demo_2_self_healing.sh
#   SKIP_VERIFY=1 bash demo/demo_2_self_healing.sh   # 无自愈演示，不推荐

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# shellcheck disable=SC1091
source "$ROOT/demo/demo_lib.sh"

REPO="src/eval/cases/case_006/repo"
ISSUE_FILE="src/eval/cases/case_006/issue.txt"

demo_check_prereqs "$ROOT"

if [[ "${SKIP_VERIFY:-}" == "1" ]]; then
  echo "警告: SKIP_VERIFY=1 时不会进入 Verifier 重试循环，自愈演示不完整。" >&2
fi

demo_section "Demo 2 — Verifier 自愈循环 (case_006 logic_error)"

echo ""
echo "① Bug 说明"
cat "$ISSUE_FILE"

echo ""
echo "② 嫌疑代码 (ranges.py)"
sed -n '1,8p' "$REPO/ranges.py"

demo_pytest_allow_fail "$REPO" "修复前"

echo ""
echo "③ repair --verbose（关注 stderr 中的 retry= 与 Verifier）"
read -r -a extra <<< "$(demo_repair_extra_args)"
log="$(mktemp)"
if ! python -m src.cli repair \
  --issue "$(tr '\n' ' ' < "$ISSUE_FILE" | sed 's/  */ /g' | sed 's/^ //;s/ $//')" \
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

echo ""
echo "④ 自愈过程摘要（从日志提取）"
grep -E 'Patcher 开始 \(retry=|Verifier|feedback|回滚|restore|retry_count' "$log" || true
retry_lines="$(grep -c 'Patcher 开始 (retry=' "$log" || true)"
echo ""
echo "Patcher 轮次（含 retry= 行）: ${retry_lines} 次"
if [[ "${SKIP_VERIFY:-}" != "1" && "$retry_lines" -lt 2 ]]; then
  echo "提示: 本次一次 patch 即通过，仍算修复成功；复杂 bug 常见 retry>=1。" >&2
fi

rm -f "$log"

if ! demo_pytest_must_pass "$REPO" "修复后"; then
  demo_restore_repo "$ROOT" "$REPO"
  exit 1
fi

demo_restore_repo "$ROOT" "$REPO"
demo_pytest_allow_fail "$REPO" "恢复后"

echo ""
echo "✓ Demo 2 完成"
