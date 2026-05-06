#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
실험 1 (배치 버전): 에이전틱 vs 수동 프로비저닝 - 다중 테넌트 배치 배포

- Manual Baseline: 테넌트 N개를 순차적으로 kubectl apply (최적화된 Bash)
- Agentic: 테넌트 N개를 병렬로 프로비저닝 (Operator 시뮬레이션)
- 배치 크기: 5개, 10개 테넌트
- 시각화: 누적 성공 곡선 (X: 시간, Y: 누적 성공률 %)
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Literal, Tuple

import numpy as np

SCRIPT_DIR = Path(__file__).resolve().parent
MANIFESTS_DIR = SCRIPT_DIR / "manifests_batch"
RESULTS_DIR = SCRIPT_DIR / "results_batch"
EXPERIMENT_NS_PREFIX = "exp1b"
POLL_INTERVAL = 0.1  # 초
MAX_WAIT = 300.0  # 최대 대기(초) - 배치 배포는 더 오래 걸릴 수 있음


def run_cmd(cmd: List[str], capture: bool = True, check: bool = True, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        capture_output=capture,
        text=capture,
        check=check,
        timeout=kwargs.get("timeout", 120),
        **kwargs,
    )


def total_resources_per_tenant(complexity: int) -> int:
    """테넌트 1개당 리소스 수: Namespace 1 + NetworkPolicy k + ResourceQuota k"""
    return 1 + 2 * complexity


def ns_name(batch_id: int, tenant_idx: int, complexity: int) -> str:
    return f"{EXPERIMENT_NS_PREFIX}-b{batch_id}-t{tenant_idx}-c{complexity}"


def namespace_yaml(batch_id: int, tenant_idx: int, complexity: int) -> str:
    name = ns_name(batch_id, tenant_idx, complexity)
    return f"""apiVersion: v1
kind: Namespace
metadata:
  name: {name}
  labels:
    tenant: exp1-batch
    batch_id: "{batch_id}"
    tenant_idx: "{tenant_idx}"
    complexity: "{complexity}"
"""


def network_policy_yaml(batch_id: int, tenant_idx: int, complexity: int, index: int) -> str:
    name = ns_name(batch_id, tenant_idx, complexity)
    np_name = f"tenant-isolation-{index}"
    return f"""apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: {np_name}
  namespace: {name}
spec:
  podSelector: {{}}
  policyTypes:
  - Ingress
  - Egress
  ingress:
  - from:
    - podSelector: {{}}
  egress:
  - to:
    - podSelector: {{}}
  - to:
    - namespaceSelector:
        matchLabels:
          name: kube-system
    ports:
    - protocol: UDP
      port: 53
"""


def resource_quota_yaml(batch_id: int, tenant_idx: int, complexity: int, index: int) -> str:
    name = ns_name(batch_id, tenant_idx, complexity)
    rq_name = f"tenant-quota-{index}"
    return f"""apiVersion: v1
kind: ResourceQuota
metadata:
  name: {rq_name}
  namespace: {name}
spec:
  hard:
    requests.cpu: "2"
    requests.memory: 4Gi
    limits.cpu: "4"
    limits.memory: 8Gi
    count/pods: "20"
"""


def generate_tenant_manifests(batch_id: int, tenant_idx: int, complexity: int) -> List[Path]:
    """단일 테넌트에 대한 매니페스트 생성"""
    base = MANIFESTS_DIR / f"batch_{batch_id}" / f"tenant_{tenant_idx}"
    base.mkdir(parents=True, exist_ok=True)
    paths = []

    # 1. Namespace
    ns_path = base / "00-namespace.yaml"
    with open(ns_path, "w") as f:
        f.write(namespace_yaml(batch_id, tenant_idx, complexity))
    paths.append(ns_path)

    # 2. NetworkPolicies
    for i in range(1, complexity + 1):
        p = base / f"10-networkpolicy-{i}.yaml"
        with open(p, "w") as f:
            f.write(network_policy_yaml(batch_id, tenant_idx, complexity, i))
        paths.append(p)

    # 3. ResourceQuotas
    for i in range(1, complexity + 1):
        p = base / f"20-resourcequota-{i}.yaml"
        with open(p, "w") as f:
            f.write(resource_quota_yaml(batch_id, tenant_idx, complexity, i))
        paths.append(p)

    return paths


def generate_batch_manifests(batch_id: int, batch_size: int, complexity: int) -> Dict[int, List[Path]]:
    """배치 내 모든 테넌트 매니페스트 생성. 반환: {tenant_idx: [paths]}"""
    result = {}
    for tenant_idx in range(batch_size):
        result[tenant_idx] = generate_tenant_manifests(batch_id, tenant_idx, complexity)
    return result


def validate_tenant(batch_id: int, tenant_idx: int, complexity: int) -> bool:
    """테넌트의 모든 리소스가 검증 통과했는지 확인"""
    ns = ns_name(batch_id, tenant_idx, complexity)
    
    # Namespace 확인
    r = run_cmd(["kubectl", "get", "namespace", ns, "-o", "json"], capture=True, check=False)
    if r.returncode != 0:
        return False
    
    # NetworkPolicy 확인
    for i in range(1, complexity + 1):
        r = run_cmd(
            ["kubectl", "get", "networkpolicy", f"tenant-isolation-{i}", "-n", ns, "-o", "json"],
            capture=True, check=False
        )
        if r.returncode != 0 or "spec" not in (r.stdout or ""):
            return False
    
    # ResourceQuota 확인
    for i in range(1, complexity + 1):
        r = run_cmd(
            ["kubectl", "get", "resourcequota", f"tenant-quota-{i}", "-n", ns, "-o", "json"],
            capture=True, check=False
        )
        if r.returncode != 0 or "spec" not in (r.stdout or ""):
            return False
    
    return True


def apply_tenant_sequential(paths: List[Path]) -> None:
    """테넌트 1개의 리소스를 순차 적용"""
    for p in sorted(paths):
        run_cmd(["kubectl", "apply", "-f", str(p)])


def apply_tenant_parallel(paths: List[Path]) -> None:
    """테넌트 1개의 리소스를 병렬 적용 (namespace 먼저)"""
    ns_file = next(p for p in paths if "00-namespace" in p.name)
    rest = [p for p in paths if p != ns_file]
    run_cmd(["kubectl", "apply", "-f", str(ns_file)])
    
    def apply_one(path: Path) -> None:
        run_cmd(["kubectl", "apply", "-f", str(path)])
    
    with ThreadPoolExecutor(max_workers=min(16, len(rest))) as ex:
        list(ex.map(apply_one, rest))


def apply_batch_manual(batch_id: int, batch_size: int, complexity: int) -> None:
    """Manual: 테넌트들을 순차적으로, 각 테넌트 내 리소스도 순차적으로 적용"""
    manifests = generate_batch_manifests(batch_id, batch_size, complexity)
    for tenant_idx in range(batch_size):
        apply_tenant_sequential(manifests[tenant_idx])


def apply_batch_agentic(batch_id: int, batch_size: int, complexity: int) -> None:
    """Agentic: 테넌트들을 병렬로, 각 테넌트 내 리소스도 병렬로 적용"""
    manifests = generate_batch_manifests(batch_id, batch_size, complexity)
    
    def apply_tenant(tenant_idx: int) -> None:
        apply_tenant_parallel(manifests[tenant_idx])
    
    with ThreadPoolExecutor(max_workers=min(batch_size, 16)) as ex:
        list(ex.map(apply_tenant, range(batch_size)))


def sample_cumulative_success_curve(
    batch_id: int,
    batch_size: int,
    complexity: int,
    mode: Literal["manual", "agentic"],
) -> Tuple[List[float], List[float], float]:
    """
    배포 실행 및 누적 성공 곡선 샘플링.
    반환: (times, cumulative_success_rates), apply_time
    """
    print(f"    [{mode.upper()}] Generating manifests for {batch_size} tenants...")
    _ = generate_batch_manifests(batch_id, batch_size, complexity)
    
    print(f"    [{mode.upper()}] Starting deployment...")
    start_time = time.perf_counter()
    
    if mode == "manual":
        apply_batch_manual(batch_id, batch_size, complexity)
    else:
        apply_batch_agentic(batch_id, batch_size, complexity)
    
    apply_done = time.perf_counter() - start_time
    print(f"    [{mode.upper()}] Apply completed in {apply_done:.2f}s")
    
    # 누적 성공 곡선 샘플링
    print(f"    [{mode.upper()}] Polling for validation...")
    times: List[float] = [0.0]
    success_rates: List[float] = [0.0]
    
    poll_start = time.perf_counter()
    deadline = poll_start + MAX_WAIT
    poll_count = 0
    
    while time.perf_counter() < deadline:
        t = time.perf_counter() - start_time  # 전체 시작 시점 기준
        
        # 각 테넌트별 검증
        passed_tenants = 0
        for tenant_idx in range(batch_size):
            if validate_tenant(batch_id, tenant_idx, complexity):
                passed_tenants += 1
        
        success_rate = (passed_tenants / batch_size) * 100.0
        times.append(t)
        success_rates.append(success_rate)
        
        poll_count += 1
        if poll_count % 10 == 0:
            print(f"      Poll {poll_count}: {passed_tenants}/{batch_size} tenants validated ({success_rate:.1f}%)")
        
        if passed_tenants >= batch_size:
            print(f"    [{mode.upper()}] All {batch_size} tenants validated! ✓ ({t:.2f}s)")
            break
        
        time.sleep(POLL_INTERVAL)
    
    return times, success_rates, apply_done


def cleanup_batch(batch_id: int, batch_size: int, complexity: int) -> None:
    """배치 내 모든 테넌트 네임스페이스 삭제"""
    print(f"    Cleaning up batch {batch_id} ({batch_size} namespaces)...")
    for tenant_idx in range(batch_size):
        ns = ns_name(batch_id, tenant_idx, complexity)
        run_cmd(["kubectl", "delete", "namespace", ns, "--ignore-not-found", "--timeout=30s"], check=False)


def compute_auc(times: List[float], rates: List[float]) -> float:
    """AUC 계산 (시간 x 성공률)"""
    if len(times) < 2:
        return 0.0
    return float(np.trapz(rates, times))


def statistical_test(aucs_manual: List[float], aucs_agentic: List[float]) -> Dict[str, Any]:
    """AUC 차이에 대한 통계 검정"""
    try:
        from scipy import stats
    except ImportError:
        return {"p_value": None, "message": "scipy not installed", "significant": None}
    if len(aucs_manual) < 2 or len(aucs_agentic) < 2:
        return {"p_value": None, "message": "Need at least 2 runs per group", "significant": None}
    stat, p_value = stats.mannwhitneyu(aucs_agentic, aucs_manual, alternative="two-sided")
    return {
        "test": "Mann-Whitney U",
        "p_value": float(p_value),
        "significant": bool(p_value < 0.05),
        "mean_auc_manual": float(np.mean(aucs_manual)),
        "mean_auc_agentic": float(np.mean(aucs_agentic)),
    }


def plot_cumulative_success_curves(all_results: List[Dict[str, Any]], out_dir: Path) -> None:
    """누적 성공 곡선 시각화"""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    
    # 배치 크기별로 그래프 생성
    batch_sizes = sorted(set(r["batch_size"] for r in all_results))
    complexities = sorted(set(r["complexity"] for r in all_results))
    
    for batch_size in batch_sizes:
        for complexity in complexities:
            manual_curves = [r for r in all_results 
                           if r["mode"] == "manual" 
                           and r["batch_size"] == batch_size 
                           and r["complexity"] == complexity]
            agentic_curves = [r for r in all_results 
                            if r["mode"] == "agentic" 
                            and r["batch_size"] == batch_size 
                            and r["complexity"] == complexity]
            
            if not manual_curves or not agentic_curves:
                continue
            
            fig, ax = plt.subplots(figsize=(10, 6))
            
            # 개별 곡선 (투명하게)
            for rec in manual_curves:
                ax.plot(rec["times"], rec["success_rates"], color="C0", alpha=0.3, linewidth=1)
            for rec in agentic_curves:
                ax.plot(rec["times"], rec["success_rates"], color="C1", alpha=0.3, linewidth=1)
            
            # 평균 곡선 계산
            def mean_curve(records: List[Dict]) -> Tuple[np.ndarray, np.ndarray]:
                t_max = max(max(r["times"]) for r in records)
                t_grid = np.linspace(0, t_max, 200)
                vals = []
                for r in records:
                    v = np.interp(t_grid, r["times"], r["success_rates"])
                    vals.append(v)
                return t_grid, np.mean(vals, axis=0)
            
            t_m, y_m = mean_curve(manual_curves)
            t_a, y_a = mean_curve(agentic_curves)
            
            ax.plot(t_m, y_m, color="C0", lw=2.5, label="Manual Script (Optimized)")
            ax.plot(t_a, y_a, color="C1", lw=2.5, label="Agentic (Operator)")
            
            # 100% 도달 시간 표시
            manual_100_time = None
            agentic_100_time = None
            for i, v in enumerate(y_m):
                if v >= 100:
                    manual_100_time = t_m[i]
                    break
            for i, v in enumerate(y_a):
                if v >= 100:
                    agentic_100_time = t_a[i]
                    break
            
            # 그래프 설정
            ax.set_xlabel("Time (seconds)", fontsize=12)
            ax.set_ylabel("Cumulative Success Rate (%)", fontsize=12)
            ax.set_title(f"Cumulative Success Curve\nBatch Size: {batch_size} tenants, Complexity: {complexity}", fontsize=14)
            ax.legend(loc="lower right", fontsize=11)
            ax.grid(True, alpha=0.3)
            ax.set_ylim(-5, 105)
            ax.set_xlim(0, max(t_m.max(), t_a.max()) * 1.05)
            
            # 100% 도달 시간 주석
            if manual_100_time:
                ax.axvline(x=manual_100_time, color="C0", linestyle="--", alpha=0.5)
                ax.annotate(f"Manual: {manual_100_time:.1f}s", 
                           xy=(manual_100_time, 100), xytext=(manual_100_time + 0.5, 90),
                           fontsize=9, color="C0")
            if agentic_100_time:
                ax.axvline(x=agentic_100_time, color="C1", linestyle="--", alpha=0.5)
                ax.annotate(f"Agentic: {agentic_100_time:.1f}s", 
                           xy=(agentic_100_time, 100), xytext=(agentic_100_time + 0.5, 80),
                           fontsize=9, color="C1")
            
            fig.tight_layout()
            fig.savefig(out_dir / f"cumulative_success_batch{batch_size}_c{complexity}.png", dpi=150)
            plt.close(fig)
    
    # 배치 크기별 평균 완료 시간 비교 그래프
    fig2, ax2 = plt.subplots(figsize=(10, 6))
    
    for batch_size in batch_sizes:
        manual_times = []
        agentic_times = []
        for complexity in complexities:
            m_recs = [r for r in all_results 
                     if r["mode"] == "manual" and r["batch_size"] == batch_size and r["complexity"] == complexity]
            a_recs = [r for r in all_results 
                     if r["mode"] == "agentic" and r["batch_size"] == batch_size and r["complexity"] == complexity]
            if m_recs:
                manual_times.append(np.mean([r["total_time"] for r in m_recs]))
            if a_recs:
                agentic_times.append(np.mean([r["total_time"] for r in a_recs]))
        
        ax2.plot(complexities[:len(manual_times)], manual_times, "o--", 
                label=f"Manual (Batch={batch_size})", alpha=0.8)
        ax2.plot(complexities[:len(agentic_times)], agentic_times, "s-", 
                label=f"Agentic (Batch={batch_size})", alpha=0.8)
    
    ax2.set_xlabel("Complexity (resources per tenant)", fontsize=12)
    ax2.set_ylabel("Average Time to 100% Success (seconds)", fontsize=12)
    ax2.set_title("Deployment Time Comparison by Batch Size", fontsize=14)
    ax2.legend(loc="upper left", fontsize=10)
    ax2.grid(True, alpha=0.3)
    fig2.tight_layout()
    fig2.savefig(out_dir / "time_comparison_by_batch.png", dpi=150)
    plt.close(fig2)
    
    # Speedup 비율 그래프
    fig3, ax3 = plt.subplots(figsize=(10, 6))
    
    for batch_size in batch_sizes:
        speedups = []
        for complexity in complexities:
            m_recs = [r for r in all_results 
                     if r["mode"] == "manual" and r["batch_size"] == batch_size and r["complexity"] == complexity]
            a_recs = [r for r in all_results 
                     if r["mode"] == "agentic" and r["batch_size"] == batch_size and r["complexity"] == complexity]
            if m_recs and a_recs:
                m_time = np.mean([r["total_time"] for r in m_recs])
                a_time = np.mean([r["total_time"] for r in a_recs])
                if a_time > 0:
                    speedups.append(m_time / a_time)
        
        if speedups:
            ax3.plot(complexities[:len(speedups)], speedups, "o-", 
                    label=f"Batch Size = {batch_size}", lw=2, markersize=8)
    
    ax3.axhline(y=1.0, color="gray", linestyle="--", alpha=0.5, label="No speedup (1x)")
    ax3.set_xlabel("Complexity (resources per tenant)", fontsize=12)
    ax3.set_ylabel("Speedup Ratio (Manual Time / Agentic Time)", fontsize=12)
    ax3.set_title("Agentic Speedup over Manual Script", fontsize=14)
    ax3.legend(loc="upper left", fontsize=10)
    ax3.grid(True, alpha=0.3)
    fig3.tight_layout()
    fig3.savefig(out_dir / "speedup_ratio.png", dpi=150)
    plt.close(fig3)


def main():
    parser = argparse.ArgumentParser(description="Experiment 1 (Batch): Agentic vs Manual provisioning")
    parser.add_argument("--batch-sizes", type=str, default="5,10", help="Comma-separated batch sizes (e.g., 5,10)")
    parser.add_argument("--complexity-min", type=int, default=1)
    parser.add_argument("--complexity-max", type=int, default=5)
    parser.add_argument("--runs", type=int, default=3, help="Runs per (batch_size, complexity, mode)")
    parser.add_argument("--skip-apply", action="store_true", help="Only plot from existing results")
    parser.add_argument("--out", type=str, default="", help="Results JSON path")
    args = parser.parse_args()

    batch_sizes = [int(x) for x in args.batch_sizes.split(",")]
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    MANIFESTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = Path(args.out or str(RESULTS_DIR / "exp1_batch_results.json"))

    if not args.skip_apply:
        print("=" * 70)
        print("EXPERIMENT 1 (BATCH): Agentic vs Manual Multi-Tenant Provisioning")
        print("=" * 70)
        print(f"Batch sizes: {batch_sizes}")
        print(f"Complexity range: {args.complexity_min} - {args.complexity_max}")
        print(f"Runs per configuration: {args.runs}")
        
        all_results: List[Dict[str, Any]] = []
        total_configs = len(batch_sizes) * (args.complexity_max - args.complexity_min + 1) * 2 * args.runs
        current = 0
        batch_id = 0
        
        for batch_size in batch_sizes:
            print(f"\n{'='*70}")
            print(f"BATCH SIZE: {batch_size} tenants")
            print(f"{'='*70}")
            
            for complexity in range(args.complexity_min, args.complexity_max + 1):
                resources_per_tenant = total_resources_per_tenant(complexity)
                total_resources = batch_size * resources_per_tenant
                print(f"\n--- Complexity {complexity} ({resources_per_tenant} resources/tenant, {total_resources} total) ---")
                
                for run_idx in range(args.runs):
                    for mode in ("manual", "agentic"):
                        current += 1
                        batch_id += 1
                        print(f"\n[{current}/{total_configs}] {mode.upper()} - Batch {batch_size}, Complexity {complexity}, Run {run_idx + 1}")
                        
                        try:
                            times, rates, apply_time = sample_cumulative_success_curve(
                                batch_id, batch_size, complexity, mode
                            )
                            
                            # 100% 도달 시간
                            total_time = times[-1] if rates[-1] >= 100 else MAX_WAIT
                            for i, r in enumerate(rates):
                                if r >= 100:
                                    total_time = times[i]
                                    break
                            
                            auc = compute_auc(times, rates)
                            
                            rec = {
                                "mode": mode,
                                "batch_size": batch_size,
                                "complexity": complexity,
                                "run_idx": run_idx,
                                "batch_id": batch_id,
                                "times": times,
                                "success_rates": rates,
                                "apply_time": apply_time,
                                "total_time": total_time,
                                "auc": auc,
                            }
                            all_results.append(rec)
                            print(f"  → Total time: {total_time:.2f}s, AUC: {auc:.2f}")
                            
                        finally:
                            cleanup_batch(batch_id, batch_size, complexity)
        
        with open(out_path, "w") as f:
            json.dump(all_results, f, indent=2)
        print(f"\n✓ Results written to {out_path}")
    
    else:
        print(f"Loading existing results from {out_path}...")
        with open(out_path) as f:
            all_results = json.load(f)
        print(f"✓ Loaded {len(all_results)} experiment records")
    
    # 시각화
    print("\n" + "=" * 70)
    print("PHASE 3: Visualization")
    print("=" * 70)
    
    try:
        import matplotlib
        print("Generating cumulative success curve plots...")
        plot_cumulative_success_curves(all_results, RESULTS_DIR)
        print(f"✓ Plots saved under {RESULTS_DIR}")
    except ImportError:
        print("⚠ matplotlib not installed; skipping plots.")
    
    # 통계 검정
    print("\n" + "=" * 70)
    print("PHASE 4: Statistical Analysis")
    print("=" * 70)
    
    by_mode = {"manual": [], "agentic": []}
    for r in all_results:
        by_mode[r["mode"]].append(r["total_time"])
    
    print(f"  Manual runs: {len(by_mode['manual'])} (mean time: {np.mean(by_mode['manual']):.2f}s)")
    print(f"  Agentic runs: {len(by_mode['agentic'])} (mean time: {np.mean(by_mode['agentic']):.2f}s)")
    
    if by_mode["manual"] and by_mode["agentic"]:
        speedup = np.mean(by_mode["manual"]) / np.mean(by_mode["agentic"])
        print(f"  Average Speedup: {speedup:.2f}x")
    
    stats_out = statistical_test(
        [r["auc"] for r in all_results if r["mode"] == "manual"],
        [r["auc"] for r in all_results if r["mode"] == "agentic"]
    )
    
    print(f"\n✓ Statistical test (AUC):")
    print(f"  Test: {stats_out.get('test', 'N/A')}")
    print(f"  P-value: {stats_out.get('p_value', 'N/A')}")
    print(f"  Significant (p < 0.05): {stats_out.get('significant', 'N/A')}")
    
    stats_path = RESULTS_DIR / "exp1_batch_stats.json"
    with open(stats_path, "w") as f:
        json.dump(stats_out, f, indent=2)
    print(f"\n✓ Statistics saved to {stats_path}")
    
    print("\n" + "=" * 70)
    print("EXPERIMENT COMPLETE!")
    print("=" * 70)
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
