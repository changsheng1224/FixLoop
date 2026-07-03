#!/usr/bin/env bash
# Demo 3：消融实验对比（full vs single vs no_retriever）
#
# 默认 --fake（无需 API，~10 秒）；真实 API 对比：USE_API=1 bash demo/demo_3_ablation.sh
#
# 用法：
#   bash demo/demo_3_ablation.sh
#   USE_API=1 bash demo/demo_3_ablation.sh

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# shellcheck disable=SC1091
source "$ROOT/demo/demo_lib.sh"

OUTPUT="${OUTPUT:-eval_results/demo_ablation}"
CASES=(case_001 case_002 case_003)

if [[ "${USE_API:-}" == "1" ]]; then
  demo_check_prereqs "$ROOT"
  FAKE_FLAG=()
  echo "模式: 真实 API（3 case × 3 变体 × 1 次，约数分钟）"
else
  ALLOW_FAKE=1
  SKIP_VERIFY=1
  demo_check_prereqs "$ROOT"
  FAKE_FLAG=(--fake --skip-verify)
  echo "模式: --fake + --skip-verify（无需 API / Docker，~10 秒）"
fi

demo_section "Demo 3 — 消融实验对比"

echo ""
echo "三种变体："
echo "  full         — Localizer + Retriever + Patcher + Verifier（Multi-Agent）"
echo "  single       — 单 Agent ReAct，持有全部工具（Baseline）"
echo "  no_retriever — Localizer + Patcher + Verifier（跳过 Retriever）"
echo ""
echo "Case: ${CASES[*]}  repetitions=1"

read -r -a extra <<< "$(demo_repair_extra_args)"
cmd=(
  python -m src.cli ablation
  --case "${CASES[0]}" --case "${CASES[1]}" --case "${CASES[2]}"
  --variant full --variant single --variant no_retriever
  --repetitions 1
  --output "$OUTPUT"
  --markdown
  --verbose
  "${FAKE_FLAG[@]}"
  "${extra[@]}"
)

echo ""
echo "运行: ${cmd[*]}"
"${cmd[@]}"

report_md="$OUTPUT/report.md"
if [[ -f "$report_md" ]]; then
  echo ""
  demo_section "Markdown 对比表"
  cat "$report_md"
else
  echo "提示: 未找到 $report_md" >&2
fi

echo ""
echo "✓ Demo 3 完成 — 完整 JSON: $OUTPUT/ablation_report.json"
