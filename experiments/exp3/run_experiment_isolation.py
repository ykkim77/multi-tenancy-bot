#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
실험 3: Hard Isolation — 커널 수준 자원 간섭 차단

Noise 테넌트(1-3): CPU + Memory/Disk I/O 집약적 부하
Victim 테넌트(4): 보호 대상 — P95 Latency 시계열 측정

비교:
  - Baseline : 기본 네임스페이스 (동등 ResourceQuota, PriorityClass 없음)
  - Operator : PriorityClass(victim 우선), 차등 ResourceQuota, LimitRange,
               NetworkPolicy 격리를 통한 Hard Isolation
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

os.environ["PYTHONUNBUFFERED"] = "1"

import numpy as np

SCRIPT_DIR = Path(__file__).resolve().parent
RESULTS_DIR = SCRIPT_DIR / "results"
NS_PREFIX = "exp3-iso"

NOISE_TENANTS = 3
VICTIM_IDX = 3          # 0-indexed → tenant 4
STRESS_DURATION = 40    # seconds of measurement under stress
SAMPLE_INTERVAL = 0.05  # seconds between latency probes


def log(msg: str) -> None:
    print(msg, flush=True)


def run_cmd(cmd: List[str], check: bool = True, timeout: int = 60, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=check, timeout=timeout, **kw)


def ns_name(mode: str, idx: int) -> str:
    return f"{NS_PREFIX}-{mode}-{idx}"


# ── YAML generators ──────────────────────────────────────────────────

def yaml_namespace(name: str) -> str:
    return (
        f"apiVersion: v1\nkind: Namespace\nmetadata:\n"
        f"  name: {name}\n  labels:\n    experiment: exp3\n"
    )


def yaml_priority_class(name: str, value: int, preempt: bool = False) -> str:
    preempt_str = "PreemptLowerPriority" if preempt else "Never"
    return (
        f"apiVersion: scheduling.k8s.io/v1\nkind: PriorityClass\n"
        f"metadata:\n  name: {name}\n"
        f"value: {value}\nglobalDefault: false\n"
        f"preemptionPolicy: {preempt_str}\n"
        f"description: \"Experiment 3 priority class\"\n"
    )


def yaml_quota(ns: str, cpu_req: str, mem_req: str, cpu_lim: str, mem_lim: str, pods: str = "30") -> str:
    return (
        f"apiVersion: v1\nkind: ResourceQuota\nmetadata:\n"
        f"  name: tenant-quota\n  namespace: {ns}\n"
        f"spec:\n  hard:\n"
        f"    requests.cpu: \"{cpu_req}\"\n    requests.memory: \"{mem_req}\"\n"
        f"    limits.cpu: \"{cpu_lim}\"\n    limits.memory: \"{mem_lim}\"\n"
        f"    count/pods: \"{pods}\"\n"
    )


def yaml_limitrange(ns: str, cpu_def: str, mem_def: str, cpu_max: str, mem_max: str) -> str:
    return (
        f"apiVersion: v1\nkind: LimitRange\nmetadata:\n"
        f"  name: tenant-limits\n  namespace: {ns}\n"
        f"spec:\n  limits:\n  - default:\n"
        f"      cpu: \"{cpu_def}\"\n      memory: \"{mem_def}\"\n"
        f"    max:\n      cpu: \"{cpu_max}\"\n      memory: \"{mem_max}\"\n"
        f"    defaultRequest:\n      cpu: \"50m\"\n      memory: \"64Mi\"\n"
        f"    type: Container\n"
    )


def yaml_netpol_deny_cross(ns: str) -> str:
    return (
        f"apiVersion: networking.k8s.io/v1\nkind: NetworkPolicy\n"
        f"metadata:\n  name: deny-cross-tenant\n  namespace: {ns}\n"
        f"spec:\n  podSelector: {{}}\n  policyTypes:\n  - Ingress\n  - Egress\n"
        f"  ingress:\n  - from:\n    - podSelector: {{}}\n"
        f"  egress:\n  - to:\n    - podSelector: {{}}\n"
        f"  - to:\n    - namespaceSelector:\n        matchLabels:\n"
        f"          name: kube-system\n    ports:\n    - protocol: UDP\n      port: 53\n"
    )


def yaml_stress_pod(ns: str, name: str, priority_class: str = "",
                    cpu_req: str = "200m", mem_req: str = "128Mi",
                    cpu_lim: str = "500m", mem_lim: str = "256Mi") -> str:
    """CPU stress + I/O stress in a single pod with two containers."""
    pc_line = f"  priorityClassName: {priority_class}\n" if priority_class else ""
    return f"""apiVersion: v1
kind: Pod
metadata:
  name: {name}
  namespace: {ns}
  labels:
    role: noise
spec:
{pc_line}  terminationGracePeriodSeconds: 0
  containers:
  - name: cpu-burn
    image: busybox
    command: ["sh", "-c", "while true; do :; done"]
    resources:
      requests:
        cpu: "{cpu_req}"
        memory: "64Mi"
      limits:
        cpu: "{cpu_lim}"
        memory: "128Mi"
  - name: io-burn
    image: busybox
    command:
    - sh
    - -c
    - |
      while true; do
        dd if=/dev/urandom of=/tmp/junk bs=4k count=2048 2>/dev/null
        rm -f /tmp/junk
      done
    resources:
      requests:
        cpu: "50m"
        memory: "{mem_req}"
      limits:
        cpu: "200m"
        memory: "{mem_lim}"
  restartPolicy: Always
"""


# ── Apply / measure helpers ──────────────────────────────────────────

def apply_yaml(yaml_str: str) -> float:
    t0 = time.perf_counter()
    r = subprocess.run(
        ["kubectl", "apply", "-f", "-"],
        input=yaml_str, capture_output=True, text=True, check=False, timeout=30,
    )
    lat = time.perf_counter() - t0
    if r.returncode != 0:
        log(f"    ⚠ apply error: {r.stderr.strip()[:120]}")
    return lat


def measure_victim_latency(ns: str) -> float:
    """
    Probe the victim namespace: create a ConfigMap, read it back, delete it.
    Returns round-trip wall-clock time (seconds).
    """
    cm_name = f"probe-{int(time.time()*1000) % 100000}"
    cm_yaml = (
        f"apiVersion: v1\nkind: ConfigMap\nmetadata:\n"
        f"  name: {cm_name}\n  namespace: {ns}\ndata:\n  key: value\n"
    )

    t0 = time.perf_counter()
    # create
    subprocess.run(["kubectl", "apply", "-f", "-"],
                   input=cm_yaml, capture_output=True, text=True, check=False, timeout=15)
    # read
    subprocess.run(["kubectl", "get", "configmap", cm_name, "-n", ns, "-o", "json"],
                   capture_output=True, text=True, check=False, timeout=15)
    # delete
    subprocess.run(["kubectl", "delete", "configmap", cm_name, "-n", ns, "--ignore-not-found"],
                   capture_output=True, text=True, check=False, timeout=15)
    return time.perf_counter() - t0


def wait_pods_running(ns: str, timeout: int = 60) -> bool:
    """Wait until all pods in ns are Running or timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = run_cmd(["kubectl", "get", "pods", "-n", ns, "-o",
                     "jsonpath={.items[*].status.phase}"], check=False)
        phases = r.stdout.strip().split() if r.stdout.strip() else []
        if phases and all(p == "Running" for p in phases):
            return True
        time.sleep(2)
    return False


def cleanup_exp3() -> None:
    log("  Cleaning up experiment 3 resources...")
    # Delete namespaces
    r = run_cmd(["kubectl", "get", "ns", "-l", "experiment=exp3",
                 "-o", "jsonpath={.items[*].metadata.name}"], check=False)
    if r.returncode == 0 and r.stdout.strip():
        for ns in r.stdout.strip().split():
            run_cmd(["kubectl", "delete", "ns", ns, "--ignore-not-found",
                     "--timeout=30s", "--grace-period=0", "--force"], check=False)
    # Delete PriorityClasses
    for pc in ("exp3-noise-low", "exp3-victim-high"):
        run_cmd(["kubectl", "delete", "priorityclass", pc, "--ignore-not-found"], check=False)


# ── Scenario runners ─────────────────────────────────────────────────

def run_scenario(mode: str) -> Dict[str, Any]:
    """
    mode='baseline' : 동등 ResourceQuota, PriorityClass 없음
    mode='operator'  : 차등 Quota, PriorityClass, LimitRange, NetworkPolicy
    """
    log(f"\n{'='*60}")
    log(f"  SCENARIO: {mode.upper()}")
    log(f"{'='*60}")

    victim_ns = ns_name(mode, VICTIM_IDX)
    noise_nss = [ns_name(mode, i) for i in range(NOISE_TENANTS)]

    # ── 1. Create namespaces ─────────────────────────────────────
    log("  [1/5] Creating namespaces...")
    for i in range(NOISE_TENANTS + 1):
        apply_yaml(yaml_namespace(ns_name(mode, i)))

    # ── 2. Apply policies ────────────────────────────────────────
    log("  [2/5] Applying resource policies...")

    if mode == "baseline":
        # Equal quotas for everyone
        for i in range(NOISE_TENANTS + 1):
            ns = ns_name(mode, i)
            apply_yaml(yaml_quota(ns, "2", "2Gi", "4", "4Gi"))
            apply_yaml(yaml_limitrange(ns, "500m", "512Mi", "2", "2Gi"))
    else:
        # PriorityClasses
        apply_yaml(yaml_priority_class("exp3-noise-low", 100))
        apply_yaml(yaml_priority_class("exp3-victim-high", 10000, preempt=True))

        # Noise tenants: strict limits
        for i in range(NOISE_TENANTS):
            ns = ns_name(mode, i)
            apply_yaml(yaml_quota(ns, "500m", "512Mi", "1", "1Gi", pods="5"))
            apply_yaml(yaml_limitrange(ns, "200m", "128Mi", "500m", "256Mi"))
            apply_yaml(yaml_netpol_deny_cross(ns))

        # Victim tenant: generous guaranteed resources
        apply_yaml(yaml_quota(victim_ns, "4", "4Gi", "6", "8Gi", pods="20"))
        apply_yaml(yaml_limitrange(victim_ns, "500m", "512Mi", "2", "2Gi"))
        apply_yaml(yaml_netpol_deny_cross(victim_ns))

    # ── 3. Baseline latency (before noise) ───────────────────────
    log("  [3/5] Measuring baseline latency (no noise)...")
    pre_noise_latencies: List[float] = []
    for _ in range(20):
        lat = measure_victim_latency(victim_ns)
        pre_noise_latencies.append(lat)
        time.sleep(SAMPLE_INTERVAL)
    log(f"    Pre-noise: mean={np.mean(pre_noise_latencies)*1000:.1f}ms, "
        f"p95={np.percentile(pre_noise_latencies, 95)*1000:.1f}ms")

    # ── 4. Deploy noise pods ─────────────────────────────────────
    log("  [4/5] Deploying noise pods in tenants 1-3...")
    pc = "" if mode == "baseline" else "exp3-noise-low"

    if mode == "baseline":
        stress_cpu_req, stress_mem_req = "200m", "128Mi"
        stress_cpu_lim, stress_mem_lim = "500m", "256Mi"
    else:
        stress_cpu_req, stress_mem_req = "100m", "64Mi"
        stress_cpu_lim, stress_mem_lim = "250m", "128Mi"

    for i in range(NOISE_TENANTS):
        ns = ns_name(mode, i)
        for j in range(2):  # 2 stress pods per noise tenant
            apply_yaml(yaml_stress_pod(
                ns, f"stress-{j}", priority_class=pc,
                cpu_req=stress_cpu_req, mem_req=stress_mem_req,
                cpu_lim=stress_cpu_lim, mem_lim=stress_mem_lim,
            ))

    # Wait for stress pods to start
    log("    Waiting for noise pods to start...")
    for i in range(NOISE_TENANTS):
        ns = ns_name(mode, i)
        ok = wait_pods_running(ns, timeout=90)
        log(f"    {ns}: {'Running' if ok else 'TIMEOUT'}")

    # Warm-up period
    log("    Warm-up (5s)...")
    time.sleep(5)

    # ── 5. Measure victim latency under stress ───────────────────
    log(f"  [5/5] Measuring victim latency under stress ({STRESS_DURATION}s)...")
    stress_latencies: List[float] = []
    timestamps: List[float] = []
    t_start = time.perf_counter()

    sample_count = 0
    while (time.perf_counter() - t_start) < STRESS_DURATION:
        t_rel = time.perf_counter() - t_start
        lat = measure_victim_latency(victim_ns)
        timestamps.append(t_rel)
        stress_latencies.append(lat)
        sample_count += 1
        if sample_count % 20 == 0:
            log(f"    t={t_rel:.1f}s  latency={lat*1000:.1f}ms  "
                f"(samples={sample_count})")
        time.sleep(SAMPLE_INTERVAL)

    # Statistics
    lat_arr = np.array(stress_latencies) * 1000  # ms
    stats = {
        "mean_ms": float(np.mean(lat_arr)),
        "median_ms": float(np.median(lat_arr)),
        "p95_ms": float(np.percentile(lat_arr, 95)),
        "p99_ms": float(np.percentile(lat_arr, 99)),
        "std_ms": float(np.std(lat_arr)),      # Jitter
        "min_ms": float(np.min(lat_arr)),
        "max_ms": float(np.max(lat_arr)),
    }

    log(f"\n  Results ({mode.upper()}):")
    log(f"    Samples       : {len(stress_latencies)}")
    log(f"    Mean latency  : {stats['mean_ms']:.1f} ms")
    log(f"    P95 latency   : {stats['p95_ms']:.1f} ms")
    log(f"    P99 latency   : {stats['p99_ms']:.1f} ms")
    log(f"    Jitter (σ)    : {stats['std_ms']:.1f} ms")

    pre_arr = np.array(pre_noise_latencies) * 1000
    return {
        "mode": mode,
        "timestamps": timestamps,
        "latencies_ms": lat_arr.tolist(),
        "pre_noise_latencies_ms": pre_arr.tolist(),
        "stats": stats,
        "pre_noise_stats": {
            "mean_ms": float(np.mean(pre_arr)),
            "p95_ms": float(np.percentile(pre_arr, 95)),
            "std_ms": float(np.std(pre_arr)),
        },
        "n_samples": len(stress_latencies),
    }


# ── Main ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Experiment 3: Hard Isolation")
    parser.add_argument("--duration", type=int, default=40, help="Stress measurement duration (s)")
    parser.add_argument("--skip-apply", action="store_true")
    args = parser.parse_args()

    global STRESS_DURATION
    STRESS_DURATION = args.duration

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RESULTS_DIR / "exp3_isolation_results.json"

    if not args.skip_apply:
        log("=" * 70)
        log("EXPERIMENT 3: Hard Isolation — Kernel-Level Interference Blocking")
        log("=" * 70)
        log(f"  Noise tenants     : {NOISE_TENANTS} (CPU + I/O stress)")
        log(f"  Victim tenant     : Tenant {VICTIM_IDX + 1}")
        log(f"  Stress duration   : {STRESS_DURATION}s")
        log(f"  Sample interval   : {SAMPLE_INTERVAL}s")

        all_results: List[Dict[str, Any]] = []

        # ── Baseline ─────────────────────────────────────────────
        cleanup_exp3()
        time.sleep(2)
        baseline = run_scenario("baseline")
        all_results.append(baseline)
        cleanup_exp3()
        time.sleep(3)

        # ── Operator ─────────────────────────────────────────────
        operator = run_scenario("operator")
        all_results.append(operator)
        cleanup_exp3()

        with open(out_path, "w") as f:
            json.dump(all_results, f, indent=2)
        log(f"\n✓ Results saved to {out_path}")

    else:
        log(f"Loading results from {out_path} ...")
        with open(out_path) as f:
            all_results = json.load(f)

    # ── Summary ──────────────────────────────────────────────────
    log("\n" + "=" * 70)
    log("COMPARISON SUMMARY")
    log("=" * 70)

    for rec in all_results:
        mode = rec["mode"].upper()
        s = rec["stats"]
        log(f"\n  [{mode}]")
        log(f"    Mean   : {s['mean_ms']:.1f} ms")
        log(f"    P95    : {s['p95_ms']:.1f} ms")
        log(f"    P99    : {s['p99_ms']:.1f} ms")
        log(f"    Jitter : {s['std_ms']:.1f} ms")

    if len(all_results) == 2:
        b, o = all_results[0]["stats"], all_results[1]["stats"]
        p95_improvement = ((b["p95_ms"] - o["p95_ms"]) / b["p95_ms"]) * 100
        jitter_improvement = ((b["std_ms"] - o["std_ms"]) / b["std_ms"]) * 100
        log(f"\n  ★ Operator vs Baseline:")
        log(f"    P95 improvement   : {p95_improvement:+.1f}%")
        log(f"    Jitter reduction  : {jitter_improvement:+.1f}%")

    log("\n✓ Experiment 3 complete. Run plot_isolation.py to generate figures.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
