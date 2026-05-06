#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
실험 1: 에이전틱 vs 수동(최적화 스크립트) 프로비저닝 검증 및 시각화.

- Manual Baseline: kubectl apply -f 를 리소스 순서에 맞게 딜레이 없이 연속 실행 (최적화된 Bash).
- Agentic: 동일 리소스를 namespace 적용 후 NetworkPolicy/ResourceQuota를 병렬 적용 (Operator 시뮬레이션).
- 복잡도 1~10, 각 지점에서 완료 시간 및 검증 통과 누적 비율 측정.
- AU-ROC 스타일 시각화 (X: 시간, Y: 검증 100% 통과한 리소스 누적 비율).
- AUC 차이에 대한 통계적 유의성 (p < 0.05) 계산.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Literal

# 의존성: pip install matplotlib scipy numpy
import numpy as np

SCRIPT_DIR = Path(__file__).resolve().parent
MANIFESTS_DIR = SCRIPT_DIR / "manifests"
RESULTS_DIR = SCRIPT_DIR / "results"
EXPERIMENT_NS_PREFIX = "exp1"
POLL_INTERVAL = 0.05  # 초
MAX_WAIT = 120.0  # 최대 대기(초)


def run(cmd: List[str], capture: bool = True, check: bool = True, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        capture_output=capture,
        text=capture,
        check=check,
        timeout=kwargs.get("timeout", 60),
        **kwargs,
    )


def total_resources(complexity: int) -> int:
    return 1 + 2 * complexity


def ns_name(complexity: int, run_id: str = "") -> str:
    suffix = f"-run{run_id}" if run_id else ""
    return f"{EXPERIMENT_NS_PREFIX}-c{complexity}{suffix}"


def validate_resource(kind: str, name: str, namespace: str) -> bool:
    """보안/정책 검증: 리소스 존재 및 spec 유효성 (kubectl get -o json 간단 체크)."""
    try:
        if kind == "Namespace":
            r = run(
                ["kubectl", "get", "namespace", name, "-o", "json"],
                capture=True,
                check=False,
            )
            if r.returncode != 0:
                return False
            return "metadata" in (r.stdout or "")
        if kind == "NetworkPolicy":
            r = run(
                ["kubectl", "get", "networkpolicy", name, "-n", namespace, "-o", "json"],
                capture=True,
                check=False,
            )
            if r.returncode != 0:
                return False
            out = r.stdout or ""
            return "spec" in out and "policyTypes" in out
        if kind == "ResourceQuota":
            r = run(
                ["kubectl", "get", "resourcequota", name, "-n", namespace, "-o", "json"],
                capture=True,
                check=False,
            )
            if r.returncode != 0:
                return False
            return "spec" in (r.stdout or "") and "hard" in (r.stdout or "")
        return False
    except Exception:
        return False


def list_expected_resources(complexity: int, run_id: str) -> List[Tuple[str, str, str]]:
    """(kind, name, namespace) 리스트. 검증 시 사용."""
    ns = ns_name(complexity, run_id)
    out = [( "Namespace", ns, "" )]
    for i in range(1, complexity + 1):
        out.append(( "NetworkPolicy", f"tenant-isolation-{i}", ns ))
    for i in range(1, complexity + 1):
        out.append(( "ResourceQuota", f"tenant-quota-{i}", ns ))
    return out


def validate_all(complexity: int, run_id: str) -> int:
    """통과한 리소스 개수."""
    expected = list_expected_resources(complexity, run_id)
    passed = 0
    for kind, name, namespace in expected:
        if kind == "Namespace":
            ok = validate_resource(kind, name, "")
        else:
            ok = validate_resource(kind, name, namespace)
        if ok:
            passed += 1
    return passed


def generate_manifests_for_run(complexity: int, run_id: str) -> List[Path]:
    sys.path.insert(0, str(SCRIPT_DIR))
    from generate_manifests import generate_for_complexity  # type: ignore
    out_dir = str(MANIFESTS_DIR / f"c{complexity}")
    paths = generate_for_complexity(complexity, run_id=run_id, out_dir=out_dir)
    return [Path(p) for p in paths]


def apply_manual_baseline(complexity: int, run_id: str) -> float:
    """Manual: Bash 스크립트로 순차 적용. 반환: 소요 시간(초)."""
    print(f"  [Manual] Complexity {complexity}, Run {run_id}: Generating manifests...")
    paths = generate_manifests_for_run(complexity, run_id)
    print(f"  [Manual] Generated {len(paths)} manifest files")
    dir_path = paths[0].parent
    print(f"  [Manual] Applying manifests sequentially...")
    start = time.perf_counter()
    run(["bash", str(SCRIPT_DIR / "apply_manual_baseline.sh"), str(complexity), run_id], cwd=str(SCRIPT_DIR))
    elapsed = time.perf_counter() - start
    print(f"  [Manual] Apply completed in {elapsed:.2f}s")
    return elapsed


def apply_agentic(complexity: int, run_id: str) -> float:
    """Agentic: namespace 적용 후 나머지 리소스 병렬 적용. 반환: 소요 시간(초)."""
    print(f"  [Agentic] Complexity {complexity}, Run {run_id}: Generating manifests...")
    paths = generate_manifests_for_run(complexity, run_id)
    ns_file = next(p for p in paths if "00-namespace" in p.name)
    rest = [p for p in paths if p != ns_file]
    print(f"  [Agentic] Applying namespace first...")
    start = time.perf_counter()
    run(["kubectl", "apply", "-f", str(ns_file)])
    print(f"  [Agentic] Applying {len(rest)} remaining resources in parallel...")
    def apply_one(path: Path) -> None:
        run(["kubectl", "apply", "-f", str(path)])
    with ThreadPoolExecutor(max_workers=min(32, len(rest))) as ex:
        list(ex.map(lambda p: apply_one(p), rest))
    elapsed = time.perf_counter() - start
    print(f"  [Agentic] Apply completed in {elapsed:.2f}s")
    return elapsed


def sample_validation_curve(
    complexity: int,
    run_id: str,
    mode: Literal["manual", "agentic"],
    apply_fn,
) -> Tuple[List[float], List[float], float]:
    """
    적용 함수를 실행하고, 적용 완료 후 폴링으로 (시간, 누적 비율) 시계열 수집.
    반환: (times, ratios), total_apply_time.
    """
    n_total = total_resources(complexity)
    # 적용은 백그라운드에서 시간을 재며 실행하고, 주기적으로 검증 개수 확인
    start_wall = time.perf_counter()
    print(f"    [{mode.upper()}] Running apply function...")
    apply_fn(complexity, run_id)
    apply_done = time.perf_counter() - start_wall

    times: List[float] = [0.0]
    ratios: List[float] = [0.0]
    print(f"    [{mode.upper()}] Polling for resource validation...")
    start_wall = time.perf_counter()
    t0 = start_wall
    deadline = start_wall + MAX_WAIT
    poll_count = 0
    while time.perf_counter() < deadline:
        t = time.perf_counter() - start_wall
        passed = validate_all(complexity, run_id)
        r = passed / n_total if n_total else 0.0
        times.append(t)
        ratios.append(r)
        poll_count += 1
        if poll_count % 20 == 0:  # 20번마다 한 번 출력
            print(f"      Poll {poll_count}: {passed}/{n_total} resources validated ({r*100:.1f}%)")
        if passed >= n_total:
            print(f"    [{mode.upper()}] All {n_total} resources validated! ✓")
            break
        time.sleep(POLL_INTERVAL)
    return times, ratios, apply_done




def compute_auc(times: List[float], ratios: List[float]) -> float:
    if len(times) < 2:
        return 0.0
    return float(np.trapz(ratios, times))


def run_single(
    mode: Literal["manual", "agentic"],
    complexity: int,
    run_id: str,
    apply_manual_fn,
    apply_agentic_fn,
):
    if mode == "manual":
        apply_fn = apply_manual_fn
    else:
        apply_fn = apply_agentic_fn
    times, ratios, apply_time = sample_validation_curve(complexity, run_id, mode, apply_fn)
    auc = compute_auc(times, ratios)
    return {
        "mode": mode,
        "complexity": complexity,
        "run_id": run_id,
        "apply_time_sec": apply_time,
        "times": times,
        "ratios": ratios,
        "auc": auc,
    }


def cleanup_run(complexity: int, run_id: str) -> None:
    ns = ns_name(complexity, run_id)
    print(f"    Cleaning up namespace {ns}...")
    run(["kubectl", "delete", "namespace", ns, "--ignore-not-found", "--timeout=30s"], check=False)


def statistical_test(aucs_manual: List[float], aucs_agentic: List[float]) -> Dict[str, Any]:
    """AUC 차이에 대한 통계 검정 (독립 표본, p-value < 0.05)."""
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
        "significant": bool(p_value < 0.05),  # numpy bool -> Python bool
        "mean_auc_manual": float(np.mean(aucs_manual)),
        "mean_auc_agentic": float(np.mean(aucs_agentic)),
    }


def main():
    parser = argparse.ArgumentParser(description="Experiment 1: Agentic vs Manual provisioning")
    parser.add_argument("--complexity-min", type=int, default=1)
    parser.add_argument("--complexity-max", type=int, default=10)
    parser.add_argument("--runs", type=int, default=3, help="Runs per (complexity, mode)")
    parser.add_argument("--skip-apply", action="store_true", help="Only plot from existing results")
    parser.add_argument("--out", type=str, default="", help="Results JSON path")
    args = parser.parse_args()

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = Path(args.out or str(RESULTS_DIR / "exp1_results.json"))

    if not args.skip_apply:
        # 1) 매니페스트 생성 (c1..c10)
        from generate_manifests import generate_for_complexity  # noqa: F401
        print("=" * 60)
        print("PHASE 1: Generating manifests for complexity 1-10")
        print("=" * 60)
        for c in range(1, 11):
            generate_for_complexity(c)
            print(f"✓ Complexity {c}: {total_resources(c)} resources")

        all_results: List[Dict[str, Any]] = []
        total_runs = (args.complexity_max - args.complexity_min + 1) * 2 * args.runs
        current_run = 0
        
        print("\n" + "=" * 60)
        print(f"PHASE 2: Running experiments ({args.runs} runs × 2 modes × {args.complexity_max - args.complexity_min + 1} complexities)")
        print("=" * 60)
        
        for complexity in range(args.complexity_min, args.complexity_max + 1):
            print(f"\n=== Complexity {complexity} (Total: {total_resources(complexity)} resources) ===")
            for run_idx in range(args.runs):
                run_id = f"r{run_idx}"
                for mode in ("manual", "agentic"):
                    current_run += 1
                    print(f"\n[{current_run}/{total_runs}] {mode.upper()} - Run {run_idx + 1}/{args.runs}")
                    try:
                        rec = run_single(
                            mode,
                            complexity,
                            run_id,
                            apply_manual_baseline,
                            apply_agentic,
                        )
                        all_results.append(rec)
                        print(f"  → AUC: {rec['auc']:.4f}, Apply time: {rec['apply_time_sec']:.2f}s")
                    finally:
                        cleanup_run(complexity, run_id)

        with open(out_path, "w") as f:
            json.dump(all_results, f, indent=2)
        print(f"\n✓ Results written to {out_path}")
    else:
        print(f"Loading existing results from {out_path}...")
        with open(out_path) as f:
            all_results = json.load(f)
        print(f"✓ Loaded {len(all_results)} experiment records")

    # 시각화 및 통계
    print("\n" + "=" * 60)
    print("PHASE 3: Visualization and Statistical Analysis")
    print("=" * 60)
    
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        HAS_MATPLOTLIB = True
    except ImportError:
        print("⚠ matplotlib not installed; skipping plot.")
        HAS_MATPLOTLIB = False
        plt = None

    if HAS_MATPLOTLIB and all_results:
        print("Generating AU-ROC style plots...")
        plot_auroc_style(all_results, RESULTS_DIR)
        print(f"✓ Plots saved under {RESULTS_DIR}")

    # AUC 집계 및 검정
    print("\nComputing AUC statistics...")
    by_mode = {"manual": [], "agentic": []}
    for r in all_results:
        by_mode[r["mode"]].append(r["auc"])
    
    print(f"  Manual runs: {len(by_mode['manual'])} (mean AUC: {np.mean(by_mode['manual']):.4f})")
    print(f"  Agentic runs: {len(by_mode['agentic'])} (mean AUC: {np.mean(by_mode['agentic']):.4f})")
    
    stats_out = statistical_test(by_mode["manual"], by_mode["agentic"])
    print(f"\n✓ Statistical test (AUC):")
    print(f"  Test: {stats_out.get('test', 'N/A')}")
    print(f"  P-value: {stats_out.get('p_value', 'N/A')}")
    print(f"  Significant (p < 0.05): {stats_out.get('significant', 'N/A')}")
    print(f"  Mean AUC Manual: {stats_out.get('mean_auc_manual', 'N/A'):.4f}")
    print(f"  Mean AUC Agentic: {stats_out.get('mean_auc_agentic', 'N/A'):.4f}")
    
    stats_path = RESULTS_DIR / "exp1_stats.json"
    with open(stats_path, "w") as f:
        json.dump(stats_out, f, indent=2)
    print(f"\n✓ Statistics saved to {stats_path}")
    print("\n" + "=" * 60)
    print("EXPERIMENT COMPLETE!")
    print("=" * 60)
    return 0


def plot_auroc_style(all_results: List[Dict[str, Any]], out_dir: Path) -> None:
    """X: 시간(또는 리소스 사용량), Y: 검증 100% 통과 누적 비율. Manual Script (Optimized) 라벨."""
    import matplotlib.pyplot as plt

    # 복잡도별로 한 그래프에 Manual vs Agentic (평균 곡선)
    for complexity in sorted({r["complexity"] for r in all_results}):
        manual_curves = [r for r in all_results if r["mode"] == "manual" and r["complexity"] == complexity]
        agentic_curves = [r for r in all_results if r["mode"] == "agentic" and r["complexity"] == complexity]
        if not manual_curves or not agentic_curves:
            continue

        fig, ax = plt.subplots()
        for rec in manual_curves:
            ax.plot(rec["times"], rec["ratios"], color="C0", alpha=0.4)
        for rec in agentic_curves:
            ax.plot(rec["times"], rec["ratios"], color="C1", alpha=0.4)

        # 평균 곡선 (보간해서 동일 시간 그리드에서 평균)
        def mean_curve(records: List[Dict[str, Any]]) -> Tuple[np.ndarray, np.ndarray]:
            t_max = max(max(r["times"]) for r in records)
            t_grid = np.linspace(0, t_max, 200)
            vals = []
            for r in records:
                v = np.interp(t_grid, r["times"], r["ratios"])
                vals.append(v)
            return t_grid, np.mean(vals, axis=0)

        t_m, y_m = mean_curve(manual_curves)
        t_a, y_a = mean_curve(agentic_curves)
        ax.plot(t_m, y_m, color="C0", lw=2, label="Manual Script (Optimized)")
        ax.plot(t_a, y_a, color="C1", lw=2, label="Agentic (Operator)")
        ax.set_xlabel("Time (seconds)")
        ax.set_ylabel("Cumulative proportion of resources passing validation")
        ax.set_title(f"Experiment 1 — Complexity {complexity}")
        ax.legend()
        ax.grid(True, alpha=0.3)
        ax.set_ylim(-0.05, 1.05)
        fig.savefig(out_dir / f"exp1_complexity_{complexity}.png", dpi=150, bbox_inches="tight")
        plt.close(fig)

    # 전체 AUC 비교 (복잡도별 평균)
    fig2, ax2 = plt.subplots()
    complexities = sorted({r["complexity"] for r in all_results})
    manual_aucs = [np.mean([r["auc"] for r in all_results if r["mode"] == "manual" and r["complexity"] == c]) for c in complexities]
    agentic_aucs = [np.mean([r["auc"] for r in all_results if r["mode"] == "agentic" and r["complexity"] == c]) for c in complexities]
    ax2.plot(complexities, manual_aucs, "o-", label="Manual Script (Optimized)", color="C0")
    ax2.plot(complexities, agentic_aucs, "s-", label="Agentic (Operator)", color="C1")
    ax2.set_xlabel("Complexity")
    ax2.set_ylabel("Mean AUC")
    ax2.legend()
    ax2.grid(True, alpha=0.3)
    fig2.savefig(out_dir / "exp1_auc_by_complexity.png", dpi=150, bbox_inches="tight")
    plt.close(fig2)


if __name__ == "__main__":
    sys.exit(main())
