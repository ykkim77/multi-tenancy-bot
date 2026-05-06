# Manual Baseline (Optimized): 리소스 순서에 맞게 딜레이 없이 연속 kubectl apply -f 실행.
# 사용법: ./apply_manual_baseline.sh <complexity> [run_id]
# 예: ./apply_manual_baseline.sh 5
#     ./apply_manual_baseline.sh 5 run1
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MANIFESTS_DIR="${SCRIPT_DIR}/manifests"
COMPLEXITY="${1:?Usage: $0 <complexity> [run_id]}"
RUN_ID="${2:-}"
DIR="${MANIFESTS_DIR}/c${COMPLEXITY}"
if [[ -n "${RUN_ID}" ]]; then
  DIR="${DIR}/run_${RUN_ID}"
fi
if [[ ! -d "${DIR}" ]]; then
  echo "Run generate_manifests.py first or use run_experiment.py to generate."
  exit 1
fi
# 순서대로 딜레이 없이 연속 적용 (이론적 한계 속도)
for f in $(find "${DIR}" -name "*.yaml" | sort); do
  kubectl apply -f "$f"
done
