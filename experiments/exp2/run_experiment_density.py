#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
실험 2: Tenant Density & Autonomous Re-balancing
─────────────────────────────────────────────────
**이번 버전부터는 Python 이 40/60 비율을 흉내내지 않는다.** 실제 Go Operator 가
30 초 마다 (configurable) 클러스터 전체를 관찰하고 스스로 quota 재조정 여부를
판단한다. 본 스크립트는 그 결정 결과를 외부에서 관찰·계측한다.

Modes
─────
  static  : namespace + quota + netpol + limitrange 를 직접 kubectl apply
            (모두 동일 quota — re-balancing 없음, baseline)
  agentic : N 개의 ChatSpace CR 을 apply.  각 CR 에는 ground-truth label
            `portal.kcu.ac.kr/usage = "active" | "idle"` 어노테이션을 붙인다.
            Operator 는 이 힌트(또는 시간 기반 휴리스틱)를 보고 boost/
            reclaim 여부를 자율적으로 결정한다.

추가 측정값 (agentic 전용)
─────────────────────────
  - rebalance_decision_s :
      CR `metadata.creationTimestamp` →
      `status.agenticActions` 에 "boosted"/"reclaimed"/"no rebalancing"
      행이 처음 등장한 시각 (= 첫 Operator 결정까지 걸린 시간)
  - actions_log          : status.agenticActions 전체 (시간 진행 분석용)
  - idle_classification  : (expected, predicted) 쌍 — 정확도/혼동행렬 계산용
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple

os.environ["PYTHONUNBUFFERED"] = "1"

import numpy as np

SCRIPT_DIR = Path(__file__).resolve().parent
RESULTS_DIR = SCRIPT_DIR / "results"

NS_PREFIX = "exp2-density"
CS_PREFIX = "cs-exp2"
CLUSTER_CPU_TOTAL = 8        # CPU cores
CLUSTER_MEM_TOTAL_GI = 16    # Memory GiB
DEFAULT_IDLE_RATIO = 0.6     # 60% idle / 40% active (matches the user's prior assumption,
                             # but the **Operator** decides — not the script)


def log(msg: str) -> None:
    print(msg, flush=True)


def run_cmd(cmd: List[str], *, check: bool = True, timeout: int = 60,
            stdin: Optional[str] = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, input=stdin, capture_output=True, text=True,
                          check=check, timeout=timeout)


def parse_k8s_time(ts: str) -> Optional[float]:
    if not ts:
        return None
    try:
        s = ts.replace("Z", "+00:00")
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


# ── Manifest generators (Static mode) ────────────────────────────────

def static_yaml(ns: str, cpu_m: int, mem_mi: int) -> List[Tuple[str, str]]:
    """Returns (kind, yaml) tuples that fully describe a static tenant."""
    return [
        ("namespace", (
            f"apiVersion: v1\nkind: Namespace\n"
            f"metadata:\n  name: {ns}\n  labels:\n    experiment: exp2\n"
            f"    method: static\n"
        )),
        ("resourcequota", (
            f"apiVersion: v1\nkind: ResourceQuota\n"
            f"metadata:\n  name: tenant-quota\n  namespace: {ns}\n"
            f"  labels:\n    experiment: exp2\n    method: static\n"
            f"spec:\n  hard:\n"
            f"    requests.cpu: \"{cpu_m}m\"\n"
            f"    requests.memory: \"{mem_mi}Mi\"\n"
            f"    limits.cpu: \"{cpu_m * 2}m\"\n"
            f"    limits.memory: \"{mem_mi * 2}Mi\"\n"
            f"    count/pods: \"20\"\n"
        )),
        ("networkpolicy", (
            f"apiVersion: networking.k8s.io/v1\nkind: NetworkPolicy\n"
            f"metadata:\n  name: default-deny\n  namespace: {ns}\n"
            f"  labels:\n    experiment: exp2\n    method: static\n"
            f"spec:\n  podSelector: {{}}\n  policyTypes: [Ingress, Egress]\n"
            f"  ingress:\n  - from:\n    - podSelector: {{}}\n"
            f"  egress:\n  - to:\n    - podSelector: {{}}\n"
            f"  - to:\n    - namespaceSelector:\n        matchLabels:\n"
            f"          name: kube-system\n    ports:\n    - protocol: UDP\n      port: 53\n"
        )),
        ("limitrange", (
            f"apiVersion: v1\nkind: LimitRange\n"
            f"metadata:\n  name: tenant-limits\n  namespace: {ns}\n"
            f"  labels:\n    experiment: exp2\n    method: static\n"
            f"spec:\n  limits:\n  - type: Container\n"
            f"    default:\n      cpu: \"{max(50, cpu_m // 4)}m\"\n"
            f"      memory: \"{max(64, mem_mi // 4)}Mi\"\n"
            f"    defaultRequest:\n      cpu: \"100m\"\n      memory: \"128Mi\"\n"
        )),
    ]


def chatspace_yaml(idx: int, expected: Literal["active", "idle"]) -> str:
    """Render a ChatSpace CR with the ground-truth `usage` annotation."""
    return (
        f"apiVersion: portal.kcu.ac.kr/v1\nkind: ChatSpace\n"
        f"metadata:\n  name: {CS_PREFIX}-{idx:03d}\n"
        f"  annotations:\n    portal.kcu.ac.kr/usage: {expected}\n"
        f"  labels:\n"
        f"    experiment: exp2\n    method: agentic\n"
        f"    expected-usage: {expected}\n"
        f"spec:\n  tenantId: exp2-{idx:03d}\n  tier: standard\n"
        f"  agentic:\n    enabled: true\n    hardIsolation: true\n"
        f"    reclaimIdleAfterSeconds: 60\n    activeBoostFactor: \"1.5\"\n"
    )


# ── Apply / measure helpers ──────────────────────────────────────────

def apply_yaml(yaml_str: str) -> None:
    run_cmd(["kubectl", "apply", "-f", "-"], stdin=yaml_str, timeout=30)


def measure_api_latency() -> float:
    t0 = time.perf_counter()
    run_cmd(["kubectl", "get", "namespaces", "-o", "json"], check=False)
    return time.perf_counter() - t0


def measure_resource_list_latency(ns: str) -> float:
    t0 = time.perf_counter()
    run_cmd(["kubectl", "get", "all,quota,netpol,limitrange", "-n", ns, "-o", "json"],
            check=False)
    return time.perf_counter() - t0


def is_static_ready(ns: str) -> bool:
    for kind in ("resourcequota/tenant-quota", "networkpolicy/default-deny",
                 "limitrange/tenant-limits"):
        if run_cmd(["kubectl", "get", kind, "-n", ns, "-o", "name"],
                   check=False).returncode != 0:
            return False
    return True


def get_chatspace(name: str) -> Optional[Dict[str, Any]]:
    r = run_cmd(["kubectl", "get", "chatspace", name, "-o", "json"],
                check=False, timeout=20)
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return None


def is_chatspace_ready(name: str) -> bool:
    obj = get_chatspace(name)
    if obj is None:
        return False
    return (obj.get("status") or {}).get("phase") == "Ready"


def list_all_chatspaces_for_exp() -> List[Dict[str, Any]]:
    r = run_cmd(["kubectl", "get", "chatspace", "-l", "experiment=exp2", "-o", "json"],
                check=False, timeout=30)
    if r.returncode != 0:
        return []
    try:
        return json.loads(r.stdout).get("items", [])
    except json.JSONDecodeError:
        return []


def cleanup_all() -> None:
    """Remove namespaces and ChatSpaces from prior runs."""
    # ChatSpaces (their finalizer cascades to namespaces)
    r = run_cmd(["kubectl", "get", "chatspace", "-l", "experiment=exp2",
                 "-o", "jsonpath={.items[*].metadata.name}"], check=False)
    if r.returncode == 0 and r.stdout.strip():
        names = r.stdout.strip().split()
        run_cmd(["kubectl", "delete", "chatspace", *names,
                 "--ignore-not-found", "--wait=false"],
                check=False, timeout=120)
    # Static-mode namespaces
    r = run_cmd(["kubectl", "get", "ns", "-l", "experiment=exp2",
                 "-o", "jsonpath={.items[*].metadata.name}"], check=False)
    if r.returncode == 0 and r.stdout.strip():
        nss = r.stdout.strip().split()
        run_cmd(["kubectl", "delete", "ns", *nss,
                 "--ignore-not-found", "--wait=false",
                 "--grace-period=0", "--force"], check=False, timeout=120)


# ── Static mode ──────────────────────────────────────────────────────

def submit_static(n_tenants: int) -> Tuple[float, List[str]]:
    """Apply N static tenants in parallel; return (apply_latency_s, ns_list)."""
    cpu_m = max(100, int(CLUSTER_CPU_TOTAL * 1000 / max(n_tenants, 1)))
    mem_mi = max(128, int(CLUSTER_MEM_TOTAL_GI * 1024 / max(n_tenants, 1)))
    ns_list = [f"{NS_PREFIX}-static-{i:03d}" for i in range(n_tenants)]

    def _apply_one(ns: str) -> None:
        for _, y in static_yaml(ns, cpu_m, mem_mi):
            apply_yaml(y)

    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=min(16, n_tenants)) as ex:
        list(ex.map(_apply_one, ns_list))
    return time.perf_counter() - t0, ns_list


# ── Agentic mode ─────────────────────────────────────────────────────

def submit_agentic(n_tenants: int, idle_ratio: float
                   ) -> Tuple[float, List[Dict[str, str]]]:
    """
    Apply N ChatSpaces in parallel.
    Returns (apply_latency_s, [{"name", "ns", "expected"}]).
    """
    n_idle = int(round(n_tenants * idle_ratio))
    items: List[Dict[str, str]] = []
    for i in range(n_tenants):
        expected = "idle" if i < n_idle else "active"
        items.append({
            "name": f"{CS_PREFIX}-{i:03d}",
            "ns":   f"tenant-exp2-{i:03d}",   # matches Operator convention
            "expected": expected,
            "idx": str(i),
        })

    def _apply_one(item: Dict[str, str]) -> None:
        apply_yaml(chatspace_yaml(int(item["idx"]), item["expected"]))

    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=min(16, n_tenants)) as ex:
        list(ex.map(_apply_one, items))
    return time.perf_counter() - t0, items


# ── Convergence wait ─────────────────────────────────────────────────

def wait_until(predicate, timeout: float, interval: float = 1.0,
               name: str = "condition") -> Tuple[bool, float]:
    deadline = time.perf_counter() + timeout
    t0 = time.perf_counter()
    while time.perf_counter() < deadline:
        if predicate():
            return True, time.perf_counter() - t0
        time.sleep(interval)
    return False, time.perf_counter() - t0


# ── Operator-internal observations ───────────────────────────────────

ACTION_KEYWORDS = {
    "reclaimed": "reclaimed",         # idle decision
    "boosted":   "boosted",           # active w/ idle peers
    "no_rebal":  "no rebalancing",    # all peers active
    "agentic_off": "agentic disabled",
    "fallback": "falling back",
}


def categorize_action(action: str) -> str:
    a = action.lower()
    for tag, kw in ACTION_KEYWORDS.items():
        if kw in a:
            return tag
    return "other"


def observe_agentic(items: List[Dict[str, str]],
                    rebalance_wait_s: float) -> Dict[str, Any]:
    """
    Wait at least `rebalance_wait_s` for the Operator to converge,
    then snapshot every ChatSpace and extract:
      - rebalance_decision_s: CR creationTimestamp → time of last action
                              that has a clear classification
      - latest action category (reclaimed/boosted/no_rebal/...)
      - confusion matrix (expected vs predicted as idle)
      - full action log
    """
    log(f"    Waiting {rebalance_wait_s:.0f}s for Operator's autonomous "
        f"re-balancing cycle ...")
    time.sleep(rebalance_wait_s)

    snapshots: Dict[str, Dict[str, Any]] = {}
    decision_times: List[float] = []
    confusion = {"TP": 0, "TN": 0, "FP": 0, "FN": 0, "unknown": 0}
    action_counts: Dict[str, int] = {k: 0 for k in ACTION_KEYWORDS}
    action_counts["other"] = 0

    for item in items:
        obj = get_chatspace(item["name"])
        if obj is None:
            continue
        actions = (obj.get("status") or {}).get("agenticActions") or []
        created = parse_k8s_time(obj.get("metadata", {}).get("creationTimestamp", ""))
        last_updated = parse_k8s_time(
            (obj.get("status") or {}).get("lastUpdated", ""))

        latest_cat = "unknown"
        if actions:
            latest_cat = categorize_action(actions[-1])
            action_counts[latest_cat] = action_counts.get(latest_cat, 0) + 1
        else:
            action_counts["other"] = action_counts.get("other", 0) + 1

        # Time-to-decision: first action that *classifies* the tenant.
        # We cannot get per-action timestamps from the ring buffer,
        # so we approximate with status.lastUpdated (refreshed on every
        # reconcile that produces an action).  This bounds the decision
        # time from above.
        decision_dt: Optional[float] = None
        if created and last_updated and last_updated > created and \
                latest_cat in ("reclaimed", "boosted", "no_rebal"):
            decision_dt = max(0.0, last_updated - created)
            decision_times.append(decision_dt)

        # Confusion matrix (idle = positive class)
        expected_idle = (item["expected"] == "idle")
        predicted_idle = (latest_cat == "reclaimed")
        if latest_cat == "unknown":
            confusion["unknown"] += 1
        elif expected_idle and predicted_idle:
            confusion["TP"] += 1
        elif (not expected_idle) and (not predicted_idle):
            confusion["TN"] += 1
        elif (not expected_idle) and predicted_idle:
            confusion["FP"] += 1
        elif expected_idle and (not predicted_idle):
            confusion["FN"] += 1

        snapshots[item["name"]] = {
            "expected": item["expected"],
            "latest_action_category": latest_cat,
            "actions": actions,
            "applied_quota": (obj.get("status") or {}).get("appliedQuota"),
            "created_epoch": created,
            "last_updated_epoch": last_updated,
            "decision_dt_s": decision_dt,
            "phase": (obj.get("status") or {}).get("phase"),
        }

    n = max(1, sum(confusion.values()) - confusion["unknown"])
    accuracy = (confusion["TP"] + confusion["TN"]) / n
    precision = (confusion["TP"] /
                 (confusion["TP"] + confusion["FP"])) if (confusion["TP"] + confusion["FP"]) else None
    recall = (confusion["TP"] /
              (confusion["TP"] + confusion["FN"])) if (confusion["TP"] + confusion["FN"]) else None

    return {
        "n_observed": len(snapshots),
        "decision_times_s": decision_times,
        "decision_mean_s": float(np.mean(decision_times)) if decision_times else None,
        "decision_median_s": float(np.median(decision_times)) if decision_times else None,
        "decision_p95_s": float(np.percentile(decision_times, 95)) if decision_times else None,
        "confusion": confusion,
        "accuracy": float(accuracy),
        "precision": float(precision) if precision is not None else None,
        "recall": float(recall) if recall is not None else None,
        "action_counts": action_counts,
        "snapshots": snapshots,
    }


# ── One full run ─────────────────────────────────────────────────────

def run_static(n_tenants: int) -> Dict[str, Any]:
    log(f"  → STATIC submit (parallel) ...")
    cleanup_all(); time.sleep(0.5)
    apply_lat, ns_list = submit_static(n_tenants)
    log(f"    apply done in {apply_lat:.2f}s, polling for ready ...")

    ok, ready_lat = wait_until(
        lambda: all(is_static_ready(ns) for ns in ns_list),
        timeout=180, interval=1.0)
    log(f"    {'all ready' if ok else 'TIMEOUT'} in {ready_lat:.2f}s")

    cp = [measure_api_latency() for _ in range(5)]
    pt = [measure_resource_list_latency(ns) for ns in ns_list[:min(20, len(ns_list))]]
    return {
        "mode": "static",
        "n_tenants": n_tenants,
        "apply_latency_s": apply_lat,
        "ready_latency_s": ready_lat if ok else float("nan"),
        "cp_latencies": cp,
        "per_tenant_latencies": pt,
        "avg_cp_latency": float(np.mean(cp)),
        "avg_tenant_latency": float(np.mean(pt)) if pt else 0.0,
        "validated": sum(1 for ns in ns_list if is_static_ready(ns)),
    }


def run_agentic(n_tenants: int, idle_ratio: float,
                rebalance_wait_s: float) -> Dict[str, Any]:
    log(f"  → AGENTIC submit (parallel) — idle_ratio={idle_ratio:.2f} ...")
    cleanup_all(); time.sleep(0.5)
    apply_lat, items = submit_agentic(n_tenants, idle_ratio)
    log(f"    apply done in {apply_lat:.2f}s, polling for Phase=Ready ...")

    ok, ready_lat = wait_until(
        lambda: all(is_chatspace_ready(it["name"]) for it in items),
        timeout=240, interval=1.0)
    log(f"    {'all Ready' if ok else 'TIMEOUT'} in {ready_lat:.2f}s")

    obs = observe_agentic(items, rebalance_wait_s=rebalance_wait_s)

    nss = [it["ns"] for it in items]
    cp = [measure_api_latency() for _ in range(5)]
    pt = [measure_resource_list_latency(ns) for ns in nss[:min(20, len(nss))]]

    log(f"    accuracy={obs['accuracy']:.3f} "
        f"(TP={obs['confusion']['TP']}, TN={obs['confusion']['TN']}, "
        f"FP={obs['confusion']['FP']}, FN={obs['confusion']['FN']})")
    if obs["decision_mean_s"]:
        log(f"    rebalance decision: mean={obs['decision_mean_s']:.2f}s, "
            f"median={obs['decision_median_s']:.2f}s, "
            f"p95={obs['decision_p95_s']:.2f}s")

    return {
        "mode": "agentic",
        "n_tenants": n_tenants,
        "idle_ratio": idle_ratio,
        "apply_latency_s": apply_lat,
        "ready_latency_s": ready_lat if ok else float("nan"),
        "rebalance_wait_s": rebalance_wait_s,
        "cp_latencies": cp,
        "per_tenant_latencies": pt,
        "avg_cp_latency": float(np.mean(cp)),
        "avg_tenant_latency": float(np.mean(pt)) if pt else 0.0,
        "validated": sum(1 for it in items if is_chatspace_ready(it["name"])),
        "agentic": obs,
    }


# ── Main ─────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="Experiment 2: Tenant Density (real Operator)")
    parser.add_argument("--max-tenants", type=int, default=50)
    parser.add_argument("--step", type=int, default=10)
    parser.add_argument("--runs", type=int, default=2)
    parser.add_argument("--idle-ratio", type=float, default=DEFAULT_IDLE_RATIO,
                        help="Ground-truth fraction marked usage=idle (default 0.6).")
    parser.add_argument("--rebalance-wait", type=float, default=35.0,
                        help="Seconds to wait after Ready, allowing the Operator's "
                             "30s rebalance cycle to fire at least once.")
    parser.add_argument("--modes", default="static,agentic")
    parser.add_argument("--skip-apply", action="store_true")
    parser.add_argument("--out", default="results/exp2_density_results.json")
    args = parser.parse_args()

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = SCRIPT_DIR / args.out
    modes = [m.strip() for m in args.modes.split(",") if m.strip()]

    if args.skip_apply:
        log(f"Loading existing results from {out_path} ...")
        with open(out_path) as f:
            all_results = json.load(f)
        log(f"  ({len(all_results)} records)")
    else:
        log("=" * 70)
        log("EXPERIMENT 2: Tenant Density & Autonomous Re-balancing")
        log("=" * 70)
        log(f"  Modes              : {modes}")
        log(f"  Tenant counts      : 1..{args.max_tenants} step={args.step}")
        log(f"  Runs / data point  : {args.runs}")
        log(f"  Ground-truth idle  : {args.idle_ratio:.0%}")
        log(f"  Rebalance wait     : {args.rebalance_wait}s")

        tenant_counts = list(range(args.step, args.max_tenants + 1, args.step))
        if 1 not in tenant_counts:
            tenant_counts = [1] + tenant_counts
        all_results: List[Dict[str, Any]] = []
        total = len(tenant_counts) * len(modes) * args.runs
        idx = 0

        for n in tenant_counts:
            log(f"\n{'─' * 60}\n  TENANTS = {n}\n{'─' * 60}")
            for run_idx in range(args.runs):
                for mode in modes:
                    idx += 1
                    log(f"\n  [{idx}/{total}] mode={mode}, N={n}, "
                        f"run={run_idx + 1}/{args.runs}")
                    if mode == "static":
                        rec = run_static(n)
                    elif mode == "agentic":
                        rec = run_agentic(n, args.idle_ratio, args.rebalance_wait)
                    else:
                        log(f"    ⚠ unknown mode '{mode}' — skipping")
                        continue
                    rec["run_idx"] = run_idx
                    all_results.append(rec)
                    cleanup_all(); time.sleep(0.3)

        with open(out_path, "w") as f:
            json.dump(all_results, f, indent=2, default=str)
        log(f"\n✓ Raw results saved to {out_path}")

    # ── Summary ──────────────────────────────────────────────────────
    log("\n" + "=" * 70)
    log("SUMMARY")
    log("=" * 70)
    for mode in modes:
        recs = [r for r in all_results if r["mode"] == mode]
        if not recs:
            continue
        log(f"\n  [{mode.upper()}]")
        ns = sorted({r["n_tenants"] for r in recs})
        for n in ns:
            sub = [r for r in recs if r["n_tenants"] == n]
            cp = np.mean([r["avg_cp_latency"] for r in sub]) * 1000
            pt = np.mean([r["avg_tenant_latency"] for r in sub]) * 1000
            line = f"    N={n:>3}  CP={cp:>6.1f}ms  Tenant={pt:>6.1f}ms"
            if mode == "agentic":
                accs = [r["agentic"]["accuracy"] for r in sub]
                dts = [r["agentic"]["decision_mean_s"] for r in sub
                       if r["agentic"]["decision_mean_s"] is not None]
                line += f"  Accuracy={np.mean(accs):.3f}"
                if dts:
                    line += f"  RebalDt={np.mean(dts):.2f}s"
            log(line)

    log("\n✓ Run plot_density.py to generate figures.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
