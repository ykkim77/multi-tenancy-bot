# 실험 1: 에이전틱 프로비저닝 능력 검증

## 실험 버전

### 버전 1: 단일 테넌트 복잡도 변화 (`run_experiment.py`)
- 테넌트 1개씩, 복잡도(1~10) 변화에 따른 비교

### 버전 2: 배치 테넌트 배포 (`run_experiment_batch.py`) ⭐ 권장
- 테넌트 5개, 10개씩 묶어서 동시 배포
- **누적 성공 곡선(Cumulative Success Curve)** 시각화
  - X축: 시간(sec)
  - Y축: 누적 성공률(%)

## 목적
- **Manual Baseline**: 사람이 직접 치는 대신, `kubectl apply -f`를 순차 실행 (수동 방식의 이론적 한계 속도).
- **Agentic**: CRD 감지 후 Operator가 병렬로 프로비저닝 (namespace 적용 후 나머지 리소스 병렬 적용).

## 사용법

### 배치 실험 (권장)

```bash
# 1. 의존성 설치
pip3 install -r requirements.txt

# 2. 배치 실험 실행 (테넌트 5개, 10개씩 배포)
python3 run_experiment_batch.py --batch-sizes 5,10 --complexity-min 1 --complexity-max 5 --runs 3

# 3. 기존 결과로만 시각화
python3 run_experiment_batch.py --skip-apply --out results_batch/exp1_batch_results.json
```

### 단일 테넌트 실험 (이전 버전)

```bash
python3 generate_manifests.py
python3 run_experiment.py --complexity-min 1 --complexity-max 10 --runs 3
```

## 출력 (배치 실험)

| 파일 | 설명 |
|------|------|
| `results_batch/cumulative_success_batch5_c*.png` | 배치 5개, 복잡도별 누적 성공 곡선 |
| `results_batch/cumulative_success_batch10_c*.png` | 배치 10개, 복잡도별 누적 성공 곡선 |
| `results_batch/time_comparison_by_batch.png` | 배치 크기별 완료 시간 비교 |
| `results_batch/speedup_ratio.png` | Agentic vs Manual 속도 향상 비율 |
| `results_batch/exp1_batch_results.json` | 원시 데이터 |
| `results_batch/exp1_batch_stats.json` | 통계 검정 결과 |

## 시각화 설명

### 누적 성공 곡선 (Cumulative Success Curve)
- **X축**: 배포 시작 후 경과 시간 (초)
- **Y축**: 전체 테넌트 중 검증 완료된 테넌트의 누적 비율 (%)
- Manual은 순차 배포, Agentic은 병렬 배포
- 100% 도달 시간이 빠를수록 좋음

### Speedup 비율
- `Manual Time / Agentic Time`
- 1.0 이상이면 Agentic이 더 빠름
- 값이 클수록 Agentic의 우위가 큼

## 출력
- `results/exp1_results.json`: run별 시계열 및 AUC
- `results/exp1_stats.json`: AUC 통계 검정 결과 (p_value, significant)
- `results/exp1_complexity_*.png`: 복잡도별 Manual vs Agentic 곡선
- `results/exp1_auc_by_complexity.png`: 복잡도별 평균 AUC 비교
