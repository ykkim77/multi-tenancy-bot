# 실험 3: Hard Isolation — 커널 수준 자원 간섭 차단

## 목적
자원 제약 환경에서 **Noise 테넌트(CPU + I/O 집약적 부하)**가 **Victim 테넌트(보호 대상)**에게 미치는 간섭을 측정하고, Operator의 Hard Isolation 메커니즘이 이를 얼마나 효과적으로 차단하는지 검증합니다.

## 실험 설계

### Noise 생성
- **테넌트 1-3**: CPU burn + Disk I/O stress (대량의 벡터 검색 시뮬레이션)
- 각 Noise 테넌트에 2개의 stress pod 배포 (총 6개 pod)

### Victim 보호
- **테넌트 4**: 중요 테넌트 (보호 대상)
- P95 Latency를 시계열로 측정하여 간섭 영향 분석

### 비교 시나리오

#### Baseline (기본 네임스페이스)
- 모든 테넌트에 동등한 ResourceQuota
- PriorityClass 없음
- 자원 간섭 차단 메커니즘 없음

#### Operator (Hard Isolation)
- **PriorityClass**: Victim에 높은 우선순위 부여 (10000 vs 100)
- **차등 ResourceQuota**: Noise는 제한적, Victim은 보장된 자원
- **LimitRange**: 컨테이너별 최대 자원 제한
- **NetworkPolicy**: 테넌트 간 네트워크 격리

## 측정 지표

1. **P95 Latency**: Victim 테넌트의 95 백분위수 응답 시간
2. **P99 Latency**: Victim 테넌트의 99 백분위수 응답 시간
3. **Jitter (σ)**: 응답 시간의 표준편차 (시스템 안정성 지표)
4. **Time-Series**: 스트레스 기간 동안의 지연 시간 변화 추이

## 사용법

```bash
# 1. 의존성 설치 (실험 1과 동일)
pip3 install -r ../exp1/requirements.txt

# 2. 실험 실행 (40초 스트레스 측정)
python3 run_experiment_isolation.py --duration 40

# 3. 시각화
python3 plot_isolation.py

# 4. 기존 결과로만 시각화
python3 plot_isolation.py
```

## 출력

| 파일 | 설명 |
|------|------|
| `results/exp3_isolation_integrated.png` | 2x2 통합 그래프 (고해상도 300dpi) |
| `results/exp3_isolation_integrated.pdf` | 논문용 벡터 그래프 |
| `results/exp3_isolation_results.json` | 원시 데이터 (시계열, 통계) |

## 시각화 설명

### (a) Victim Tenant Latency Time-Series
- 스트레스 기간 동안 Victim 테넌트의 응답 시간 변화
- Moving average로 트렌드 표시
- P95 수평선으로 임계값 표시
- **범례**: 그래프 내부 우측 상단에 배치

### (b) Latency Distribution (CDF)
- 누적 분포 함수로 지연 시간 분포 비교
- P95, P99 마커 표시

### (c) P95 / P99 Latency Comparison
- 막대 그래프로 백분위수 비교
- P95 개선율 텍스트 상자 표시

### (d) Response Stability (Jitter)
- 표준편차(σ)로 시스템 안정성 비교
- **색상 통일**: Baseline=파란색, Operator=주황색
- Jitter 감소율 텍스트 상자 표시

## 기대 결과

- **P95 Improvement**: Operator가 Victim의 P95 지연 시간을 얼마나 개선했는지
- **Jitter Reduction**: 응답 시간의 변동성(불안정성)을 얼마나 줄였는지
- **Hard Isolation 효과**: 커널 수준의 자원 간섭을 차단하여 중요 테넌트의 성능을 보호함을 증명
