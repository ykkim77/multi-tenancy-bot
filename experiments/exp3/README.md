# Experiment 3 — Hard Isolation under Noisy Neighbors (재설계)

이 실험은 **noisy-neighbor** 가 존재하는 multi-tenant 환경에서 victim 테넌트의
SLA(latency, jitter)가 어떻게 보호되는지를 정량 비교한다. 이전 실험의
한계(2 tenant + 수동 정책)를 정면으로 풀어, 3-mode × 3-scenario 매트릭스로
재설계되었다.

## ✨ 무엇이 바뀌었나

| 항목 | 이전 | **현재** |
|------|------|----------|
| `Operator` 모드 | 사람이 PriorityClass / Quota / NetPol 을 손으로 적용 | **실제 Go Operator 가** ChatSpace CR 만 받고 정책 자동 생성 + 30 s 마다 자율 재조정 |
| 비교 대상 | 2-way (baseline vs manual) | **3-way** (B1 baseline · B2 manual · B3 agentic) |
| 시나리오 | 1 victim + 1 aggressor | **A · B · C** (1+1, 1+3, 5+5) — 5+5 는 priority/standard tier 혼합 |
| 측정 지표 | latency, P95/P99, jitter | + **Operator 자동 대응 시간**, **재조정 횟수**, **agenticActions audit**, **tier 별 보호 차등** |
| Figure | Figure 9 (4-panel) | **Figure 9 (3×4)** + **Figure 10 (신규, 1×3)** |

리뷰어 설득 논리:
> 수동 정책 적용(현업 best practice)보다도 Agentic Operator 가 더 효과적이다.

## 비교 대상 (3 modes)

| ID | 이름 | 무엇을 적용 | 자동화 |
|----|------|------------|--------|
| **B1** | `baseline` | 모든 테넌트 동등 quota, NetPol 없음 | × (보호 없음) |
| **B2** | `manual` | PriorityClass + 차등 quota + LimitRange + NetPol 직접 apply | × (정적, 현업 방식) |
| **B3** | `agentic` | **`kubectl apply -f chatspace.yaml`** 만 | ○ (Operator 자동) |

## 시나리오 (3 scenarios)

| ID | victims | aggressors | idle background | 의도 |
|----|---------|------------|-----------------|------|
| **A** basic        | 1 (priority) | 1 | 0 | 가장 단순한 격리 효과 |
| **B** multi-attack | 1 (priority) | 3 | 2 | 다수 공격자 + idle peer 가 boost trigger |
| **C** multi-tenant | **3 priority + 2 standard** | 5 | 5 | 실 multi-tenant SLA + tier 별 보호 차등 검증 |

## 측정 흐름

1. `cleanup_exp3()` → 깨끗한 상태로 시작.
2. **Provisioning** — mode 별 분기.
   - `B3` 의 경우 `ChatSpace` CR 만 apply → `Phase=Ready` 까지 polling.
3. **Pre-noise baseline** — victim 별 ConfigMap CRUD round-trip × 15.
4. **Stress 주입** — aggressor namespace 마다 1 개의 stress pod
   (CPU spin loop + I/O burn). warm-up 5 s.
5. **측정창 (`--duration`, default 60 s)** — victim latency 를 round-robin 으로
   probe. **agentic mode 에서는 백그라운드 thread 가 3 s 마다 모든 ChatSpace 의
   `status.agenticActions` / `status.lastUpdated` 를 폴링**해 timeline 을 수집.
6. 통계 산출 (전체 + per-tier + agentic-only audit) → JSON 저장.

## agentic 전용 추가 지표

- **`first_response_s`** — stress 시작 이후 처음으로 `boosted` / `reclaimed`
  action 이 등장한 시각 (Operator 자동 대응 시간).
- **`new_actions_during_stress`** — 측정창 동안 새로 추가된 action 총 개수.
- **`action_counts`** — `{boosted: X, reclaimed: Y, no_rebal: Z, ...}`.
- **`per_cr.actions`** — ChatSpace 별 action 전체 ring-buffer.
- **`per_tier.{priority,standard}.{p95_ms,std_ms,...}`** — tier 별 victim
  통계 (Scenario C 에서만 의미 있음).

## 실행

```bash
# Operator 가 클러스터에 배포된 상태에서:
kubectl -n kcu-portal-system get deploy portal-operator
kubectl get crd chatspaces.portal.kcu.ac.kr

# 전체 실험 (3 시나리오 × 3 모드 × 1 run, ~15-25 분)
python3 experiments/exp3/run_experiment_isolation.py

# 빠른 dry-run (시나리오 A 만)
python3 experiments/exp3/run_experiment_isolation.py --scenarios A --duration 30

# Agentic 단독 분석
python3 experiments/exp3/run_experiment_isolation.py --modes agentic --scenarios C

# 결과만 다시 그리기
python3 experiments/exp3/run_experiment_isolation.py --skip-apply
python3 experiments/exp3/plot_isolation.py            # Figure 9
python3 experiments/exp3/plot_agentic_analysis.py     # Figure 10
```

운영 팁: `operator/main.go --rebalance-interval=10s` 로 배포해두면 30 s
대기 없이 빠르게 결정 timeline 을 채울 수 있다.

## 산출물

```
results/
├── exp3_isolation_results.json        # raw + per-CR snapshots + timeline
├── exp3_fig9_isolation.png/.pdf        # Figure 9 (3×4 grid)
└── exp3_fig10_agentic_analysis.png/.pdf  # Figure 10 (1×3, agentic-only)
```

### Figure 9 (3×4)
- 행: scenario A / B / C
- 열: (a) time-series (b) CDF (c) P95/P99 (d) jitter
- 색: `B1=blue dashed`, `B2=green dotted`, `B3=orange solid`
- 패널 (c)/(d) 에는 `B3 vs B1` 개선률 텍스트 박스

### Figure 10 (신규, 1×3)
- (a) Rebalance 발생 시점 vs victim latency 변화 (Scenario C, agentic)
- (b) AgenticActions Timeline — per ChatSpace 의 boosted/reclaimed/no-rebal 분포
- (c) Tier 별 보호 차등 — Scenario C 의 priority vs standard victim P95
       (3 mode 에서 각각 비교 → priority 가 더 강하게 보호받는지 검증)

## 가설 (검증 대상)

> **H1 (격리 효과)**: P95 latency 가 `B1 > B2 > B3` 순으로 단조 감소.
>
> **H2 (시나리오 강건성)**: aggressor 가 늘어나도(시나리오 B, C) `B3` 의
> P95 증가율이 `B1`/`B2` 대비 가장 작다.
>
> **H3 (Operator 자동 대응 속도)**: `first_response_s ≤ 30s` (Operator 의
> default rebalance cycle 안에 첫 결정).
>
> **H4 (Tier 차등)**: Scenario C 에서 `B3` 의 priority-tier victim P95 <
> standard-tier victim P95 (≥ 20 % 차이).
