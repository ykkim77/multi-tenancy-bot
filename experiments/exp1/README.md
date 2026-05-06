# Experiment 1 — Provisioning Speed: Manual vs Helm vs Agentic Operator

이 실험은 KCU Knowledge Portal에서 **새 테넌트를 프로비저닝**하는 세 가지 방식의
**End-to-End 속도**를 정량적으로 비교한다.

| 모드      | 호출 방식                              | 의미 |
|-----------|----------------------------------------|------|
| `manual`  | `kubectl apply` 4개 리소스 순차 적용    | 사람이 직접 매니페스트를 던지는 baseline |
| `helm`    | `helm install kcu-tenant`             | 표준 패키지 매니저, 차트 1회 install |
| `agentic` | `kubectl apply -f chatspace.yaml`     | **실제 Operator**가 ChatSpace CR을 받아 모든 리소스 reconcile |

## ✨ 측정 방법론 (변경됨)

이전 버전과 비교한 핵심 차이:

| 항목 | 이전 | **현재** |
|------|------|----------|
| Agentic의 본질 | Python 스크립트가 Operator를 **모방** | `operator/` 의 **실제 Go Operator**가 ChatSpace CR을 reconcile |
| 측정 단위 | `time.time()` 으로 측정한 **스크립트 실행 시간** | **CR 제출 시각 → `status.conditions[Ready]` 시각** |
| 의미 | 클라이언트 처리 시간 (외란 多) | **API 서버가 관측한 Operator 응답 시간** (엄밀) |

세 모드 모두 클라이언트 측 `perf_counter` 와 함께 다음 4개 리소스가 Ready 가
되는 시점을 polling 으로 검증한다:

1. `Namespace`
2. `ResourceQuota`
3. `LimitRange`
4. `NetworkPolicy`

추가로 **Agentic 모드** 에 한해, `kubectl get chatspace -o json` 으로

- `metadata.creationTimestamp` (CR 제출 시각, **API server 기준**)
- `status.conditions[type=Ready, status=True].lastTransitionTime`

두 시각의 차이를 **server-side timing** 으로 별도 기록한다. 이는 클라이언트
지연/프로세스 jitter 가 완전히 배제된 가장 엄밀한 측정값이다.

## 사전 조건

```bash
# 1) Operator 가 클러스터에 배포되어 있어야 한다
kubectl apply -f operator/config/crd/bases/portal.kcu.ac.kr_chatspaces.yaml
kubectl apply -f operator/config/manager/manager.yaml
kubectl -n kcu-portal-system rollout status deploy/portal-operator

# 2) helm CLI 가 PATH 에 있어야 한다 (없으면 helm 모드 자동 제외)
helm version
```

## 실행

```bash
# 기본 — batch 5,10 / mode 3개 / run 3회
python3 experiments/exp1/run_experiment.py

# 더 빠르게: batch 5만, run 1회씩
python3 experiments/exp1/run_experiment.py --batch-sizes 5 --runs 1

# Helm 만 빼고
python3 experiments/exp1/run_experiment.py --modes manual,agentic

# 이미 결과 JSON 이 있으면 plot 만
python3 experiments/exp1/run_experiment.py --skip-apply
python3 experiments/exp1/plot_3way.py
```

## 산출물

- `results/exp1_results.json` — raw run records (배치, 모드, 누적 곡선, 서버 timing)
- `results/exp1_3way.png` — 4-패널 논문형 figure
  - (a) 누적 성공 곡선 (mode × batch)
  - (b) 클라이언트 ready latency boxplot
  - (c) **Agentic server-side timing** boxplot
  - (d) Mann-Whitney U 통계 박스 (Agentic vs Manual / vs Helm)

## 가설 (검증 대상)

> H1: **Agentic Operator** 의 ready latency 는 manual / helm 대비
> 통계적으로 유의하게 작다 (p < 0.05).
>
> H2: 배치 크기가 커질수록 Agentic 의 우위가 커진다
> (Operator 의 병렬 reconcile 효과).
