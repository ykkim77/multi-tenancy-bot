#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
실험 2: 부하 분산 및 밀도 최적화 (Tenant Density)

자원 제약 환경에서 최대 수용 밀도(Saturation Point)를 측정한다.
테넌트를 1~20개까지 늘려가며:
  - Control Plane 지연 시간 (API 응답 속도)
  - 각 테넌트의 평균 프로비저닝 응답 속도
  - K8s 오버헤드 증가 추이

비교:
  - Static:  모든 테넌트에 고정 ResourceQuota (총 자원 / N 균등 분배)
  - Dynamic: Operator가 테넌트 사용 패턴을 모니터링하고
             유휴 테넌트의 자원을 활성 테넌트로 재분배 (Dynamic Re-balancing)
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

os.environ["PYTHONUNBUFFERED"] = "1"

import numpy as np


def log(msg: str) -> None:
    print(msg, flush=True)

SCRIPT_DIR = Path(__file__).resolve().parent
RESULTS_DIR = SCRIPT_DIR / "results"
NS_PREFIX = "exp2-density"

CLUSTER_CPU_TOTAL = 8       # 클러스터 총 CPU (cores)
CLUSTER_MEM_TOTAL_GI = 16   # 클러스터 총 메모리 (Gi)


def run_cmd(cmd: List[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=check, timeout=60)


def ns_name(mode: str, idx: int) -> str:
    return f"{NS_PREFIX}-{mode}-{idx}"


# ── Manifest generators ──────────────────────────────────────────────

def gen_namespace_yaml(name: str) -> str:
    return (
        f"apiVersion: v1\nkind: Namespace\nmetadata:\n"
        f"  name: {name}\n  labels:\n    experiment: exp2\n"
    )


def gen_quota_yaml(ns: str, cpu_req: str, mem_req: str, cpu_lim: str, mem_lim: str) -> str:
    return (
        f"apiVersion: v1\nkind: ResourceQuota\nmetadata:\n"
        f"  name: tenant-quota\n  namespace: {ns}\n"
        f"spec:\n  hard:\n"
        f"    requests.cpu: \"{cpu_req}\"\n"
        f"    requests.memory: \"{mem_req}\"\n"
        f"    limits.cpu: \"{cpu_lim}\"\n"
        f"    limits.memory: \"{mem_lim}\"\n"
        f"    count/pods: \"20\"\n"
    )


def gen_netpol_yaml(ns: str) -> str:
    return (
        f"apiVersion: networking.k8s.io/v1\nkind: NetworkPolicy\n"
        f"metadata:\n  name: default-deny\n  namespace: {ns}\n"
        f"spec:\n  podSelector: {{}}\n  policyTypes:\n  - Ingress\n  - Egress\n"
        f"  ingress:\n  - from:\n    - podSelector: {{}}\n"
        f"  egress:\n  - to:\n    - podSelector: {{}}\n"
        f"  - to:\n    - namespaceSelector:\n        matchLabels:\n          name: kube-system\n"
        f"    ports:\n    - protocol: UDP\n      port: 53\n"
    )


def gen_limitrange_yaml(ns: str, cpu_default: str, mem_default: str) -> str:
    return (
        f"apiVersion: v1\nkind: LimitRange\nmetadata:\n"
        f"  name: tenant-limits\n  namespace: {ns}\n"
        f"spec:\n  limits:\n  - default:\n"
        f"      cpu: \"{cpu_default}\"\n      memory: \"{mem_default}\"\n"
        f"    defaultRequest:\n"
        f"      cpu: \"100m\"\n      memory: \"128Mi\"\n"
        f"    type: Container\n"
    )


# ── Apply helpers ─────────────────────────────────────────────────────

def apply_yaml(yaml_str: str) -> float:
    """Apply a YAML manifest via stdin and return wall-clock latency (seconds)."""
    t0 = time.perf_counter()
    subprocess.run(
        ["kubectl", "apply", "-f", "-"],
        input=yaml_str, capture_output=True, text=True, check=True, timeout=30,
    )
    return time.perf_counter() - t0


def measure_api_latency() -> float:
    """Control Plane latency: time to list all namespaces."""
    t0 = time.perf_counter()
    run_cmd(["kubectl", "get", "namespaces", "-o", "json"])
    return time.perf_counter() - t0


def measure_resource_list_latency(ns: str) -> float:
    """Per-tenant: time to list all resources in a namespace."""
    t0 = time.perf_counter()
    run_cmd(["kubectl", "get", "all,quota,netpol,limitrange", "-n", ns, "-o", "json"], check=False)
    return time.perf_counter() - t0


def validate_tenant(ns: str) -> bool:
    """Check that quota, netpol, limitrange exist."""
    for kind in ("resourcequota/tenant-quota", "networkpolicy/default-deny", "limitrange/tenant-limits"):
        r = run_cmd(["kubectl", "get", kind, "-n", ns, "-o", "name"], check=False)
        if r.returncode != 0:
            return False
    return True


def cleanup_all() -> None:
    """Delete all experiment namespaces."""
    r = run_cmd(["kubectl", "get", "ns", "-l", "experiment=exp2", "-o", "jsonpath={.items[*].metadata.name}"], check=False)
    if r.returncode == 0 and r.stdout.strip():
        for ns in r.stdout.strip().split():
            run_cmd(["kubectl", "delete", "ns", ns, "--ignore-not-found", "--timeout=30s"], check=False)


# ── Static provisioning ──────────────────────────────────────────────

def provision_static(n_tenants: int) -> Dict[str, Any]:
    """
    Static: N tenants, each with equal share of cluster resources.
    Resources are fixed and never change.
    """
    cpu_per = max(100, int(CLUSTER_CPU_TOTAL * 1000 / n_tenants))   # millicores
    mem_per = max(128, int(CLUSTER_MEM_TOTAL_GI * 1024 / n_tenants))  # Mi

    cpu_req = f"{cpu_per}m"
    mem_req = f"{mem_per}Mi"
    cpu_lim = f"{cpu_per * 2}m"
    mem_lim = f"{mem_per * 2}Mi"
    cpu_default = f"{max(50, cpu_per // 4)}m"
    mem_default = f"{max(64, mem_per // 4)}Mi"

    provision_latencies = []
    per_tenant_latencies = []
    cp_latencies = []
    validated = 0

    t_start = time.perf_counter()

    for i in range(n_tenants):
        ns = ns_name("static", i)

        # provision this tenant
        lat = 0.0
        lat += apply_yaml(gen_namespace_yaml(ns))
        lat += apply_yaml(gen_quota_yaml(ns, cpu_req, mem_req, cpu_lim, mem_lim))
        lat += apply_yaml(gen_netpol_yaml(ns))
        lat += apply_yaml(gen_limitrange_yaml(ns, cpu_default, mem_default))
        provision_latencies.append(lat)

        # control plane latency snapshot
        cp_lat = measure_api_latency()
        cp_latencies.append(cp_lat)

        # per-tenant response
        pt_lat = measure_resource_list_latency(ns)
        per_tenant_latencies.append(pt_lat)

        if validate_tenant(ns):
            validated += 1

    total_time = time.perf_counter() - t_start

    return {
        "mode": "static",
        "n_tenants": n_tenants,
        "provision_latencies": provision_latencies,
        "cp_latencies": cp_latencies,
        "per_tenant_latencies": per_tenant_latencies,
        "validated": validated,
        "total_time": total_time,
        "avg_provision": float(np.mean(provision_latencies)),
        "avg_cp_latency": float(np.mean(cp_latencies)),
        "avg_tenant_latency": float(np.mean(per_tenant_latencies)),
    }


# ── Dynamic (Operator) provisioning ──────────────────────────────────

def provision_dynamic(n_tenants: int) -> Dict[str, Any]:
    """
    Dynamic: Operator creates all namespaces, then runs a re-balancing cycle.
    
    Phase 1 - Initial provisioning (parallel-friendly, generous initial quota)
    Phase 2 - Usage monitoring + re-balancing:
              Simulate that ~40% of tenants are 'active' (heavy usage),
              remainder 'idle'. Operator shrinks idle quotas and expands
              active quotas, effectively allowing more tenants.
    """
    # generous initial quota (overcommit factor 1.5x)
    overcommit = 1.5
    cpu_per = max(100, int(CLUSTER_CPU_TOTAL * 1000 * overcommit / max(n_tenants, 1)))
    mem_per = max(128, int(CLUSTER_MEM_TOTAL_GI * 1024 * overcommit / max(n_tenants, 1)))

    provision_latencies = []
    cp_latencies = []
    per_tenant_latencies = []
    validated = 0

    t_start = time.perf_counter()

    # Phase 1: rapid provisioning
    for i in range(n_tenants):
        ns = ns_name("dynamic", i)

        cpu_req = f"{cpu_per}m"
        mem_req = f"{mem_per}Mi"
        cpu_lim = f"{cpu_per * 2}m"
        mem_lim = f"{mem_per * 2}Mi"
        cpu_default = f"{max(50, cpu_per // 4)}m"
        mem_default = f"{max(64, mem_per // 4)}Mi"

        lat = 0.0
        lat += apply_yaml(gen_namespace_yaml(ns))
        lat += apply_yaml(gen_quota_yaml(ns, cpu_req, mem_req, cpu_lim, mem_lim))
        lat += apply_yaml(gen_netpol_yaml(ns))
        lat += apply_yaml(gen_limitrange_yaml(ns, cpu_default, mem_default))
        provision_latencies.append(lat)

    # Phase 2: Dynamic re-balancing
    # Simulate: 40% active, 60% idle
    n_active = max(1, int(n_tenants * 0.4))
    n_idle = n_tenants - n_active

    # idle tenants: shrink quota to 30% of original
    # active tenants: expand quota with reclaimed resources
    idle_cpu = max(50, int(cpu_per * 0.3))
    idle_mem = max(64, int(mem_per * 0.3))
    reclaimed_cpu = (cpu_per - idle_cpu) * n_idle
    reclaimed_mem = (mem_per - idle_mem) * n_idle
    active_bonus_cpu = reclaimed_cpu // max(n_active, 1)
    active_bonus_mem = reclaimed_mem // max(n_active, 1)
    active_cpu = cpu_per + active_bonus_cpu
    active_mem = mem_per + active_bonus_mem

    rebalance_latencies = []
    for i in range(n_tenants):
        ns = ns_name("dynamic", i)
        if i < n_active:
            lat = apply_yaml(gen_quota_yaml(ns, f"{active_cpu}m", f"{active_mem}Mi",
                                            f"{active_cpu * 2}m", f"{active_mem * 2}Mi"))
        else:
            lat = apply_yaml(gen_quota_yaml(ns, f"{idle_cpu}m", f"{idle_mem}Mi",
                                            f"{idle_cpu * 2}m", f"{idle_mem * 2}Mi"))
        rebalance_latencies.append(lat)

    # Measure final state
    for i in range(n_tenants):
        ns = ns_name("dynamic", i)
        cp_latencies.append(measure_api_latency())
        per_tenant_latencies.append(measure_resource_list_latency(ns))
        if validate_tenant(ns):
            validated += 1

    total_time = time.perf_counter() - t_start

    return {
        "mode": "dynamic",
        "n_tenants": n_tenants,
        "provision_latencies": provision_latencies,
        "rebalance_latencies": rebalance_latencies,
        "cp_latencies": cp_latencies,
        "per_tenant_latencies": per_tenant_latencies,
        "validated": validated,
        "total_time": total_time,
        "avg_provision": float(np.mean(provision_latencies)),
        "avg_rebalance": float(np.mean(rebalance_latencies)),
        "avg_cp_latency": float(np.mean(cp_latencies)),
        "avg_tenant_latency": float(np.mean(per_tenant_latencies)),
        "rebalance_info": {
            "n_active": n_active,
            "n_idle": n_idle,
            "active_cpu_m": active_cpu,
            "active_mem_mi": active_mem,
            "idle_cpu_m": idle_cpu,
            "idle_mem_mi": idle_mem,
        },
    }


# ── Main ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Experiment 2: Tenant Density & Dynamic Re-balancing")
    parser.add_argument("--max-tenants", type=int, default=20)
    parser.add_argument("--step", type=int, default=1, help="Tenant count increment step")
    parser.add_argument("--runs", type=int, default=2, help="Runs per data point")
    parser.add_argument("--skip-apply", action="store_true")
    args = parser.parse_args()

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RESULTS_DIR / "exp2_density_results.json"

    if not args.skip_apply:
        log("=" * 70)
        log("EXPERIMENT 2: Tenant Density & Dynamic Re-balancing")
        log("=" * 70)
        log(f"  Max tenants : {args.max_tenants}")
        log(f"  Step        : {args.step}")
        log(f"  Runs        : {args.runs}")
        log(f"  Cluster cap : {CLUSTER_CPU_TOTAL} CPU, {CLUSTER_MEM_TOTAL_GI}Gi MEM")

        tenant_counts = list(range(1, args.max_tenants + 1, args.step))
        all_results: List[Dict[str, Any]] = []
        total_iterations = len(tenant_counts) * 2 * args.runs
        current = 0

        for n in tenant_counts:
            log(f"\n{'─'*60}")
            log(f"  TENANTS = {n}")
            log(f"{'─'*60}")

            for run_idx in range(args.runs):
                for mode in ("static", "dynamic"):
                    current += 1
                    log(f"\n  [{current}/{total_iterations}] {mode.upper()}, N={n}, Run {run_idx+1}/{args.runs}")

                    cleanup_all()
                    time.sleep(0.5)

                    if mode == "static":
                        rec = provision_static(n)
                    else:
                        rec = provision_dynamic(n)

                    rec["run_idx"] = run_idx
                    all_results.append(rec)

                    log(f"    Provisioned {rec['validated']}/{n} tenants in {rec['total_time']:.2f}s")
                    log(f"    Avg CP latency    : {rec['avg_cp_latency']*1000:.1f} ms")
                    log(f"    Avg tenant latency: {rec['avg_tenant_latency']*1000:.1f} ms")

                    cleanup_all()
                    time.sleep(0.5)

        with open(out_path, "w") as f:
            json.dump(all_results, f, indent=2)
        log(f"\n✓ Raw results saved to {out_path}")

    else:
        log(f"Loading results from {out_path} ...")
        with open(out_path) as f:
            all_results = json.load(f)
        log(f"✓ Loaded {len(all_results)} records")

    # Summary
    log("\n" + "=" * 70)
    log("SUMMARY")
    log("=" * 70)

    for mode in ("static", "dynamic"):
        recs = [r for r in all_results if r["mode"] == mode]
        if not recs:
            continue
        tenants = sorted(set(r["n_tenants"] for r in recs))
        log(f"\n  [{mode.upper()}]")
        for n in tenants:
            sub = [r for r in recs if r["n_tenants"] == n]
            avg_cp = np.mean([r["avg_cp_latency"] for r in sub]) * 1000
            avg_t = np.mean([r["avg_tenant_latency"] for r in sub]) * 1000
            avg_total = np.mean([r["total_time"] for r in sub])
            log(f"    N={n:>2}  CP={avg_cp:>6.1f}ms  Tenant={avg_t:>6.1f}ms  Total={avg_total:>6.2f}s")

    log("\n✓ Experiment 2 complete. Run plot_density.py to generate figures.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
