#!/usr/bin/env bash
# 在 WSL 中预构建 SWE-bench 评测镜像，供后续 harness 复用。
# 已存在的 instance/env/base 镜像会跳过（除非 --force）。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# WSL 下若从 /mnt/c 调用，ROOT 可能是 Windows 路径的挂载点
PY="${FIXLOOP_WSL_PYTHON:-$HOME/.venvs/swebench/bin/python}"
if [[ ! -x "$PY" ]]; then
  PY="python3"
fi

DATASET="${SWEBENCH_DATASET:-princeton-nlp/SWE-bench_Lite}"
SPLIT="${SWEBENCH_SPLIT:-test}"
MAX_WORKERS="${SWEBENCH_IMAGE_WORKERS:-1}"
FORCE=false
IDS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --force) FORCE=true; shift ;;
    --dataset) DATASET="$2"; shift 2 ;;
    --split) SPLIT="$2"; shift 2 ;;
    --max-workers) MAX_WORKERS="$2"; shift 2 ;;
    --django-only)
      IDS=(django__django-11099)
      shift
      ;;
    --dev5)
      IDS=(
        astropy__astropy-12907
        django__django-11099
        matplotlib__matplotlib-23964
        pylint-dev__pylint-6506
        sympy__sympy-20590
      )
      shift
      ;;
    --)
      shift
      IDS+=("$@")
      break
      ;;
    -*)
      echo "unknown flag: $1" >&2
      exit 2
      ;;
    *)
      IDS+=("$1")
      shift
      ;;
  esac
done

if [[ ${#IDS[@]} -eq 0 ]]; then
  IDS=(django__django-11099)
fi

export HF_HOME="${HF_HOME:-/mnt/c/Users/${USERNAME:-$USER}/.cache/huggingface}"
# Windows 用户名在 WSL 常不可用；优先已有 HF_HOME
if [[ ! -d "$HF_HOME" && -d /mnt/c/Users/haoyu/.cache/huggingface ]]; then
  export HF_HOME=/mnt/c/Users/haoyu/.cache/huggingface
fi
export HUGGINGFACE_HUB_CACHE="${HUGGINGFACE_HUB_CACHE:-$HF_HOME/hub}"
export HF_HUB_OFFLINE="${HF_HUB_OFFLINE:-1}"

# 复用 Clash（若可连）；镜像层主要走 Docker daemon，不一定走该代理
if [[ -z "${HTTPS_PROXY:-}" && -f /etc/resolv.conf ]]; then
  NS=$(sed -n 's/^nameserver //p' /etc/resolv.conf | head -1)
  if [[ -n "$NS" ]]; then
    export HTTP_PROXY="http://${NS}:7897"
    export HTTPS_PROXY="http://${NS}:7897"
  fi
fi

echo "[prepare-images] python=$PY dataset=$DATASET split=$SPLIT force=$FORCE"
echo "[prepare-images] instances=${IDS[*]}"
echo "[prepare-images] HF_HOME=$HF_HOME HF_HUB_OFFLINE=$HF_HUB_OFFLINE"

FORCE_ARG="False"
if [[ "$FORCE" == "true" ]]; then
  FORCE_ARG="True"
fi

set -x
"$PY" -m swebench.harness.prepare_images \
  --dataset_name "$DATASET" \
  --split "$SPLIT" \
  --instance_ids "${IDS[@]}" \
  --max_workers "$MAX_WORKERS" \
  --force_rebuild "$FORCE_ARG" \
  --tag latest \
  --env_image_tag latest

echo "[prepare-images] done. Existing images are reused on next harness run."
echo "[prepare-images] Do NOT run: docker system prune -a  (会删评测镜像)"
docker images --format 'table {{.Repository}}\t{{.Tag}}\t{{.Size}}' | head -40 || true
