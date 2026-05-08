#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
실험 1 재설계: 자동화 완성도 비교 (Helm vs Agentic Operator)
═══════════════════════════════════════════════════════════════════════════════

핵심 질문: "동일한 결과를 얻으려면 각 방법이 얼마나 많은 인간의 개입이 필요한가?"

측정 지표:
  ① PCR  - Policy Coverage Rate        (자동 적용 격리 정책 비율)
  ② MTTR - Mean Time To Recovery       (드리프트 복구 시간; 핵심 실험)
  ③ HIS  - Human Intervention Score    (운영자 개입 횟수 모델)
  ④ 누적 수렴 곡선                      (보조; 기존 실험 참고)

완전한 테넌트 격리 스택 (7가지 정책):
  1. Namespace
  2. ResourceQuota
  3. LimitRange
  4. NetworkPolicy
  5. PriorityClass (참조/어노테이션)
  6. RBAC (RoleBinding)
  7. status/agenticActions 기록

방법별 자동화 범위:
  Helm (기본, NS+RQ 2종):  PCR = 2/7 = 28%  — 수동 values.yaml + helm install
  Helm (현재 chart, 5종):  PCR = 5/7 = 71%  — RBAC·status 누락, 드리프트 감지 불가
  Agentic Operator:         PCR = 7/7 = 100% — CR 1줄 + 지속적 재조정
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple

os.environ["PYTHONUNBUFFERED"] = "1"

import numpy as np

SCRIPT_DIR = Path(__file__).resolve().parent
HELM_CHART = SCRIPT_DIR / "helm"
RESULTS_DIR = SCRIPT_DIR / "results"

NAMESPACE_PREFIX = "tenant-"
POLL_INTERVAL   = 0.5    # MTTR recovery polling interval (seconds)
STD_POLL        = 1.0    # standard readiness polling interval
MAX_WAIT        = 120.0  # max seconds for readiness / Agentic MTTR
HELM_MTTR_WAIT  = 90.0   # seconds to confirm Helm has NO auto-recovery
STABILIZE_WAIT  = 60.0   # seconds after deploy before injecting drift

ISOLATION_POLICIES = [
    "namespace", "resourcequota", "limitrange",
    "networkpolicy", "priorityclass", "rbac", "status",
]
N_POLICIES = len(ISOLATION_POLICIES)  # 7

PRIORITY_CLASSES = [
    {"name": "kcu-tenant-priority", "value": 10000,
     "preemptionPolicy": "PreemptLowerPriority",
     "description": "High-priority tenants protected from noisy neighbors"},
    {"name": "kcu-tenant-standard", "value": 1000,
     "preemptionPolicy": "Never",
     "description": "Default-priority tenants"},
    {"name": "kcu-tenant-internal", "value": 100,
     "preemptionPolicy": "Never",
     "description": "Best-effort/internal-only tenants, first to be reclaimed"},
]

_submit_epochs: Dict[str, float] = {}


def log(msg: str) -> None:
    print(msg, flush=True)


# ── kubectl helpers ───────────────────────────────────────────────────

def run(cmd: List[str], *, check: bool = False, timeout: int = 60,
        stdin: str = "") -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, input=stdin or None,
        capture_output=True, text=True, check=check, timeout=timeout,
    )


def parse_k8s_time(ts: str) -> Optional[float]:
    if not ts:
        return None
    try:
        s = ts.replace("Z", "+00:00")
        return datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp() \
            if "+" not in s else datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


# ── Cluster setup ─────────────────────────────────────────────────────

def setup_cluster_priority_classes() -> None:
    log("  [setup] Ensuring shared PriorityClasses ...")
    for pc in PRIORITY_CLASSES:
        yaml = (
            f"apiVersion: scheduling.k8s.io/v1\n"
            f"kind: PriorityClass\n"
            f"metadata:\n  name: {pc['name']}\n  labels:\n"
            f"    app.kubernetes.io/managed-by: kcu-experiment\n"
            f"    experiment: exp1\n"
            f"value: {pc['value']}\n"
            f"globalDefault: false\n"
            f"preemptionPolicy: {pc['preemptionPolicy']}\n"
            f"description: {pc['description']!r}\n"
        )
        r = run(["kubectl", "apply", "--server-side",
                 "--field-manager=kcu-exp1", "-f", "-"],
                stdin=yaml, check=False, timeout=30)
        if r.returncode != 0:
            run(["kubectl", "apply", "-f", "-"], stdin=yaml, check=True, timeout=30)
        log(f"    ✓ PriorityClass {pc['name']}")


# ── Policy coverage check functions ──────────────────────────────────

def _k8s_get(kind: str, name: str, ns: str = "") -> bool:
    cmd = ["kubectl", "get", kind, name]
    if ns:
        cmd += ["-n", ns]
    return run(cmd, check=False).returncode == 0


def check_namespace(ns: str) -> bool:
    return _k8s_get("ns", ns)


def check_priorityclass_ref(ns: str) -> bool:
    """True if the namespace carries a PriorityClass annotation."""
    r = run(["kubectl", "get", "ns", ns, "-o",
             "jsonpath={.metadata.annotations.portal\\.kcu\\.ac\\.kr/priority-class}"],
            check=False)
    return r.returncode == 0 and bool(r.stdout.strip())


def check_rbac(ns: str) -> bool:
    """True if any RoleBinding exists in the namespace."""
    r = run(["kubectl", "get", "rolebinding", "-n", ns, "-o", "name"], check=False)
    return r.returncode == 0 and bool(r.stdout.strip())


def check_status_recorded(tid: str) -> bool:
    """True if ChatSpace has a non-empty operator-managed status."""
    r = run(["kubectl", "get", "chatspace", f"cs-{tid}", "-o", "json"], check=False)
    if r.returncode != 0:
        return False
    try:
        obj = json.loads(r.stdout)
        status = obj.get("status") or {}
        return bool(status.get("phase")) or bool(status.get("conditions"))
    except json.JSONDecodeError:
        return False


def measure_pcr(mode: str, tenant_id: str) -> Dict[str, bool]:
    ns = f"{NAMESPACE_PREFIX}{tenant_id}"
    return {
        "namespace":    check_namespace(ns),
        "resourcequota": _k8s_get("resourcequota", "tenant-quota", ns),
        "limitrange":   _k8s_get("limitrange", "tenant-limits", ns),
        "networkpolicy": _k8s_get("networkpolicy.networking.k8s.io", "tenant-isolation", ns),
        "priorityclass": check_priorityclass_ref(ns),
        "rbac":          check_rbac(ns),
        "status":        check_status_recorded(tenant_id) if mode == "agentic" else False,
    }


# ── Submit functions ──────────────────────────────────────────────────

def submit_helm_basic(tenant_id: str) -> None:
    """Minimal chart simulation: only Namespace + ResourceQuota (PCR 2/7 = 28%)."""
    ns = f"{NAMESPACE_PREFIX}{tenant_id}"
    ns_yaml = (
        f"apiVersion: v1\nkind: Namespace\n"
        f"metadata:\n  name: {ns}\n  labels:\n"
        f"    exp1: \"true\"\n    method: helm-basic\n"
        f"    portal.kcu.ac.kr/tenant: {tenant_id}\n"
    )
    rq_yaml = (
        f"apiVersion: v1\nkind: ResourceQuota\n"
        f"metadata:\n  name: tenant-quota\n  namespace: {ns}\n"
        f"  labels:\n    exp1: \"true\"\n    method: helm-basic\n"
        f"spec:\n  hard:\n"
        f"    requests.cpu: \"500m\"\n    requests.memory: \"512Mi\"\n"
        f"    limits.cpu: \"1\"\n    limits.memory: \"1Gi\"\n"
        f"    count/pods: \"10\"\n"
    )
    run(["kubectl", "apply", "-f", "-"], stdin=ns_yaml, check=True)
    run(["kubectl", "apply", "-f", "-"], stdin=rq_yaml, check=True)


def submit_helm(tenant_id: str, tier: str = "standard") -> None:
    """Full Helm chart: NS + RQ + LR + NetPol + PriorityClass ref (PCR 5/7 = 71%)."""
    release = f"helm-{tenant_id}"
    run([
        "helm", "install", release, str(HELM_CHART),
        "--set", f"tenantId={tenant_id}",
        "--set", f"tier={tier}",
        "--set", "priorityClass.create=false",
        "--wait=false",
    ], check=True, timeout=60)


def submit_agentic(tenant_id: str) -> None:
    """CR 1줄 제출 → Operator가 7종 정책 자동 생성 + 지속 재조정."""
    t_submit = time.time()
    _submit_epochs[tenant_id] = t_submit
    cs_yaml = (
        f"apiVersion: portal.kcu.ac.kr/v1\nkind: ChatSpace\n"
        f"metadata:\n  name: cs-{tenant_id}\n"
        f"  annotations:\n"
        f"    portal.kcu.ac.kr/client-submit-epoch: \"{t_submit:.6f}\"\n"
        f"  labels:\n    exp1: \"true\"\n    method: agentic\n"
        f"spec:\n  tenantId: {tenant_id}\n  tier: standard\n"
        f"  agentic:\n    enabled: true\n    hardIsolation: true\n"
    )
    run(["kubectl", "apply", "-f", "-"], stdin=cs_yaml, check=True)


# ── Readiness checks ──────────────────────────────────────────────────

def _ns_basic_ready(tenant_id: str) -> bool:
    """helm-basic: just NS + RQ."""
    ns = f"{NAMESPACE_PREFIX}{tenant_id}"
    return check_namespace(ns) and _k8s_get("resourcequota", "tenant-quota", ns)


def _ns_full_ready(tenant_id: str) -> bool:
    """helm: NS + RQ + LR + NetPol."""
    ns = f"{NAMESPACE_PREFIX}{tenant_id}"
    if not check_namespace(ns):
        return False
    for kind, name in [
        ("resourcequota", "tenant-quota"),
        ("limitrange",    "tenant-limits"),
        ("networkpolicy.networking.k8s.io", "tenant-isolation"),
    ]:
        if not _k8s_get(kind, name, ns):
            return False
    return True


def _chatspace_ready(tenant_id: str) -> bool:
    """agentic: status.phase == Ready + 4 core resources exist."""
    r = run(["kubectl", "get", "chatspace", f"cs-{tenant_id}", "-o", "json"],
            check=False)
    if r.returncode != 0:
        return False
    try:
        obj = json.loads(r.stdout)
    except json.JSONDecodeError:
        return False
    if (obj.get("status") or {}).get("phase") != "Ready":
        return False
    return _ns_full_ready(tenant_id)


def get_ready_fn(mode: str):
    return {
        "helm-basic": _ns_basic_ready,
        "helm":        _ns_full_ready,
        "agentic":     _chatspace_ready,
    }[mode]


# ── Cleanup ───────────────────────────────────────────────────────────

def cleanup(tenant_ids: List[str], mode: str = "") -> None:
    cs_names = [f"cs-{tid}" for tid in tenant_ids]
    if cs_names:
        run(["kubectl", "delete", "chatspace", *cs_names,
             "--ignore-not-found", "--wait=false"], check=False, timeout=60)
    if mode in ("helm", "") and shutil.which("helm"):
        for tid in tenant_ids:
            release = f"helm-{tid}"
            if run(["helm", "status", release], check=False, timeout=10).returncode == 0:
                run(["helm", "uninstall", release, "--wait=false"],
                    check=False, timeout=30)
    nss = [f"{NAMESPACE_PREFIX}{tid}" for tid in tenant_ids]
    if nss:
        run(["kubectl", "delete", "ns", *nss,
             "--ignore-not-found", "--wait=false",
             "--grace-period=0", "--force"], check=False, timeout=60)


def wait_for_cleanup(tenant_ids: List[str], timeout: float = 60) -> None:
    deadline = time.perf_counter() + timeout
    cs_names = [f"cs-{tid}" for tid in tenant_ids]
    ns_names = [f"{NAMESPACE_PREFIX}{tid}" for tid in tenant_ids]
    while time.perf_counter() < deadline:
        remaining = (
            [n for n in cs_names if run(["kubectl", "get", "chatspace", n],
                                        check=False).returncode == 0] +
            [n for n in ns_names if run(["kubectl", "get", "ns", n],
                                        check=False).returncode == 0]
        )
        if not remaining:
            return
        time.sleep(1.0)
    log("    ⚠ cleanup timeout — proceeding anyway")


def _wait_all_ready(tenant_ids: List[str], ready_fn, label: str = "") -> int:
    """Poll until all tenants are ready. Returns count of ready tenants."""
    already_ready: set = set()
    deadline = time.perf_counter() + MAX_WAIT
    while time.perf_counter() < deadline and len(already_ready) < len(tenant_ids):
        for tid in tenant_ids:
            if tid not in already_ready and ready_fn(tid):
                already_ready.add(tid)
        if len(already_ready) < len(tenant_ids):
            time.sleep(STD_POLL)
    n = len(already_ready)
    if label:
        log(f"    Stable: {n}/{len(tenant_ids)} ready ({label})")
    return n


# ── EXP 1-A: Policy Coverage Rate ────────────────────────────────────

@dataclass
class PCRResult:
    mode: str
    tenant_id: str
    policy_details: Dict[str, bool]
    pcr: float
    policies_applied: int
    policies_total: int = N_POLICIES


def run_exp1a(modes: List[str], n_tenants: int, tag: str) -> List[PCRResult]:
    log("\n" + "=" * 70)
    log("EXP 1-A: Policy Coverage Rate (PCR)")
    log("  측정: 자동으로 적용된 격리 정책 수 / 전체 필요 정책 수 (7가지)")
    log("=" * 70)

    results: List[PCRResult] = []

    for mode in modes:
        tenant_ids = [f"{tag}-1a-{mode.replace('-','')[:6]}-{i:02d}"
                      for i in range(n_tenants)]
        cleanup(tenant_ids, mode)
        wait_for_cleanup(tenant_ids)

        log(f"\n  [{mode.upper()}] {n_tenants}개 테넌트 배포 중 ...")
        submit_fn = {"helm-basic": submit_helm_basic,
                     "helm":       submit_helm,
                     "agentic":    submit_agentic}[mode]

        errors: List[str] = []
        with ThreadPoolExecutor(max_workers=min(8, n_tenants)) as ex:
            futs = {ex.submit(submit_fn, tid): tid for tid in tenant_ids}
            for fut, tid in futs.items():
                try:
                    fut.result()
                except Exception as e:
                    errors.append(f"{tid}: {e}")
        if errors:
            for e in errors:
                log(f"    ⚠ {e}")

        _wait_all_ready(tenant_ids, get_ready_fn(mode), label=mode)
        time.sleep(3.0)  # brief settle before measuring

        for tid in tenant_ids:
            details = measure_pcr(mode, tid)
            applied = sum(1 for v in details.values() if v)
            pcr = applied / N_POLICIES
            results.append(PCRResult(
                mode=mode, tenant_id=tid,
                policy_details=details, pcr=pcr, policies_applied=applied,
            ))

        mode_pcrs = [r.pcr for r in results if r.mode == mode]
        log(f"    PCR {mode}: mean={np.mean(mode_pcrs):.0%}  "
            f"({int(np.mean([r.policies_applied for r in results if r.mode == mode]))}/{N_POLICIES})")
        log(f"    policy detail: {results[-1].policy_details}")
        cleanup(tenant_ids, mode)

    return results


# ── EXP 1-B: Drift Recovery Time (MTTR) — 핵심 실험 ─────────────────

@dataclass
class MTTRResult:
    mode: str
    run_idx: int
    n_tenants: int
    n_drifted: int
    mttr_s: Optional[float]        # None = 자동 복구 불가 (∞)
    detected: bool
    per_tenant_mttr: List[Optional[float]] = field(default_factory=list)


def _inject_drift(drift_tenant_ids: List[str]) -> float:
    """Delete NetworkPolicies. Returns perf_counter at injection start."""
    t0 = time.perf_counter()
    for tid in drift_tenant_ids:
        ns = f"{NAMESPACE_PREFIX}{tid}"
        run(["kubectl", "delete", "networkpolicy", "tenant-isolation",
             "-n", ns, "--ignore-not-found"], check=False, timeout=15)
    elapsed = time.perf_counter() - t0
    log(f"    ✗ Drift injected in {elapsed:.1f}s: "
        f"NetworkPolicy 삭제 × {len(drift_tenant_ids)} namespaces")
    return time.perf_counter()


def _poll_recovery(drift_tenant_ids: List[str], t_inject: float,
                   max_wait: float) -> Tuple[Optional[float], List[Optional[float]]]:
    """
    Poll until all NetworkPolicies are restored.
    Returns (overall_mttr_s, per_tenant_mttr_list).
    None elements mean no recovery within max_wait.
    """
    per: Dict[str, Optional[float]] = {tid: None for tid in drift_tenant_ids}
    deadline = time.perf_counter() + max_wait

    while time.perf_counter() < deadline:
        elapsed = time.perf_counter() - t_inject
        for tid in drift_tenant_ids:
            if per[tid] is None:
                ns = f"{NAMESPACE_PREFIX}{tid}"
                if _k8s_get("networkpolicy.networking.k8s.io", "tenant-isolation", ns):
                    per[tid] = elapsed
                    log(f"      ✓ {ns}: 복구 t={elapsed:.1f}s")
        if all(v is not None for v in per.values()):
            overall = max(v for v in per.values() if v is not None)
            return overall, list(per.values())
        time.sleep(POLL_INTERVAL)

    not_recovered = sum(1 for v in per.values() if v is None)
    log(f"    ⚠ 복구 timeout {max_wait:.0f}s — {not_recovered}개 미복구")
    return None, list(per.values())


def run_exp1b(modes: List[str], n_tenants: int, n_runs: int, tag: str) -> List[MTTRResult]:
    log("\n" + "=" * 70)
    log("EXP 1-B: Drift Recovery Time (MTTR) — 핵심 실험")
    log("  Helm MTTR = ∞ (수동 개입 필요)  |  Agentic MTTR = ~30s (자동)")
    log("=" * 70)

    results: List[MTTRResult] = []
    test_modes = [m for m in modes if m != "helm-basic"]
    n_drifted = max(1, n_tenants // 2)

    for run_idx in range(n_runs):
        log(f"\n  ── Run {run_idx + 1}/{n_runs} ──")
        for mode in test_modes:
            tenant_ids = [f"{tag}-1b-{mode[:3]}-r{run_idx:02d}-{i:02d}"
                          for i in range(n_tenants)]
            drift_ids  = tenant_ids[:n_drifted]

            log(f"\n  [{mode.upper()}] tenants={n_tenants} drift={n_drifted}")
            cleanup(tenant_ids, mode)
            wait_for_cleanup(tenant_ids)

            submit_fn = submit_helm if mode == "helm" else submit_agentic
            with ThreadPoolExecutor(max_workers=min(16, n_tenants)) as ex:
                list(ex.map(submit_fn, tenant_ids))

            n_ready = _wait_all_ready(tenant_ids, get_ready_fn(mode), label=mode)
            if n_ready < n_tenants:
                log(f"    ⚠ {n_tenants - n_ready}개 미준비 — 계속 진행")

            log(f"    [{mode}] {STABILIZE_WAIT:.0f}s 안정화 대기 중 ...")
            time.sleep(STABILIZE_WAIT)

            t_inject = _inject_drift(drift_ids)

            if mode == "helm":
                log(f"    [Helm] {HELM_MTTR_WAIT:.0f}s 동안 자동 복구 없음을 확인 ...")
                overall, per_t = _poll_recovery(drift_ids, t_inject, HELM_MTTR_WAIT)
                rec = MTTRResult(
                    mode=mode, run_idx=run_idx, n_tenants=n_tenants, n_drifted=n_drifted,
                    mttr_s=None,    # Helm never auto-recovers
                    detected=False,
                    per_tenant_mttr=per_t,
                )
                if overall is not None:
                    log(f"    ⚠ Helm이 예외적으로 {overall:.1f}s만에 복구됨 (외부 개입?)")
                    rec.mttr_s = overall
                    rec.detected = True
                else:
                    log(f"    ✓ Helm CONFIRMED: 자동 복구 없음 (MTTR = ∞)")
            else:  # agentic
                log(f"    [Agentic] Operator 재조정 모니터링 (최대 {MAX_WAIT:.0f}s) ...")
                overall, per_t = _poll_recovery(drift_ids, t_inject, MAX_WAIT)
                rec = MTTRResult(
                    mode=mode, run_idx=run_idx, n_tenants=n_tenants, n_drifted=n_drifted,
                    mttr_s=overall, detected=overall is not None,
                    per_tenant_mttr=per_t,
                )
                if overall is not None:
                    log(f"    ✓ Agentic MTTR = {overall:.1f}s")
                else:
                    log(f"    ✗ Agentic 복구 timeout (Operator가 실행 중인지 확인)")

            results.append(rec)
            cleanup(tenant_ids, mode)

    return results


# ── EXP 1-C: Human Intervention Score (model-based) ──────────────────

@dataclass
class HISResult:
    mode: str
    n_tenants: int
    n_provisioning: int
    n_drift_events: int
    n_drift_fixes: int    # Helm=n_drift_events, Agentic=0
    n_scale_ups: int
    his_total: int


def compute_his_model(
    tenant_counts: List[int],
    drift_events: int = 3,
    scale_ups: int = 2,
) -> List[HISResult]:
    """
    시나리오: N개 테넌트를 1시간 운영
    (초기 프로비저닝 N회 + 정책 변조 drift_events회 + 확장 scale_ups회)

    Helm:    프로비저닝 N + 드리프트 수동 복구 drift_events + 확장 scale_ups
    Agentic: 프로비저닝 N + 드리프트 자동 복구 0            + 확장 scale_ups
    """
    results: List[HISResult] = []
    for n in tenant_counts:
        for mode, drift_fixes in [("helm", drift_events), ("agentic", 0)]:
            results.append(HISResult(
                mode=mode,
                n_tenants=n,
                n_provisioning=n,
                n_drift_events=drift_events,
                n_drift_fixes=drift_fixes,
                n_scale_ups=scale_ups,
                his_total=n + drift_fixes + scale_ups,
            ))
    return results


# ── EXP 1-D: Cumulative Convergence Curve (supplement) ───────────────

@dataclass
class ConvergenceResult:
    mode: str
    batch_size: int
    run_idx: int
    tenant_ids: List[str]
    times_s: List[float]     = field(default_factory=list)
    cumulative_pct: List[float] = field(default_factory=list)
    ready_latency_s: float   = 0.0
    apply_latency_s: float   = 0.0


def run_exp1d(modes: List[str], batch_sizes: List[int],
              n_runs: int, tag: str) -> List[ConvergenceResult]:
    log("\n" + "=" * 70)
    log("EXP 1-D: 누적 수렴 곡선 (보조)")
    log("=" * 70)

    results: List[ConvergenceResult] = []
    test_modes = [m for m in modes if m != "helm-basic"]

    for batch in batch_sizes:
        for mode in test_modes:
            for run_idx in range(n_runs):
                tenant_ids = [f"{tag}-1d-{mode[:3]}-b{batch}-r{run_idx:02d}-{i:03d}"
                              for i in range(batch)]
                rec = ConvergenceResult(mode=mode, batch_size=batch, run_idx=run_idx,
                                        tenant_ids=tenant_ids)

                log(f"\n  [{mode.upper()} | B={batch} | run {run_idx+1}/{n_runs}]")
                cleanup(tenant_ids, mode)
                wait_for_cleanup(tenant_ids)

                submit_fn = submit_helm if mode == "helm" else submit_agentic
                ready_fn  = get_ready_fn(mode)

                t0 = time.perf_counter()
                with ThreadPoolExecutor(max_workers=min(16, batch)) as ex:
                    list(ex.map(submit_fn, tenant_ids))
                rec.apply_latency_s = time.perf_counter() - t0

                rec.times_s.append(0.0)
                rec.cumulative_pct.append(0.0)
                already_ready: set = set()
                deadline = time.perf_counter() + MAX_WAIT
                last_ready = 0

                while time.perf_counter() < deadline:
                    elapsed = time.perf_counter() - t0
                    for tid in tenant_ids:
                        if tid not in already_ready and ready_fn(tid):
                            already_ready.add(tid)
                    ready = len(already_ready)
                    rec.times_s.append(elapsed)
                    rec.cumulative_pct.append(100.0 * ready / batch)
                    if ready != last_ready:
                        log(f"      t={elapsed:5.1f}s  {ready}/{batch} ready")
                        last_ready = ready
                    if ready >= batch:
                        rec.ready_latency_s = elapsed
                        break
                    time.sleep(STD_POLL)
                else:
                    rec.ready_latency_s = float("nan")

                results.append(rec)
                cleanup(tenant_ids, mode)

    return results


# ── Summary printers ──────────────────────────────────────────────────

def print_summary(all_results: Dict[str, Any]) -> None:
    log("\n" + "=" * 70)
    log("결과 요약")
    log("=" * 70)

    if "exp1a_pcr" in all_results:
        log("\n  [1-A] Policy Coverage Rate:")
        by_mode: Dict[str, List[float]] = {}
        for r in all_results["exp1a_pcr"]:
            by_mode.setdefault(r["mode"], []).append(r["pcr"])
        for mode, pcrs in sorted(by_mode.items()):
            n_pol = round(np.mean(pcrs) * N_POLICIES)
            log(f"    {mode:<12s}: {np.mean(pcrs):.0%}  ({n_pol}/{N_POLICIES})")

    if "exp1b_mttr" in all_results:
        log("\n  [1-B] MTTR (드리프트 복구 시간):")
        helm_none = sum(1 for r in all_results["exp1b_mttr"]
                        if r["mode"] == "helm" and r["mttr_s"] is None)
        helm_total = sum(1 for r in all_results["exp1b_mttr"] if r["mode"] == "helm")
        agnt_vals = [r["mttr_s"] for r in all_results["exp1b_mttr"]
                     if r["mode"] == "agentic" and r["mttr_s"] is not None]
        log(f"    Helm:    {helm_none}/{helm_total} runs 자동 복구 없음 → MTTR = ∞")
        if agnt_vals:
            log(f"    Agentic: mean={np.mean(agnt_vals):.1f}s  "
                f"median={np.median(agnt_vals):.1f}s  "
                f"max={np.max(agnt_vals):.1f}s  n={len(agnt_vals)}")
        else:
            log(f"    Agentic: 복구 데이터 없음 (Operator 실행 확인 필요)")

    if "exp1c_his" in all_results:
        log("\n  [1-C] Human Intervention Score (N=10 시나리오):")
        for r in all_results["exp1c_his"]:
            if r["n_tenants"] == 10:
                log(f"    {r['mode']:<10s}: HIS={r['his_total']}  "
                    f"(프로비저닝{r['n_provisioning']} + 드리프트복구{r['n_drift_fixes']} "
                    f"+ 확장{r['n_scale_ups']})")


# ── Main ──────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Experiment 1: Automation Completeness (PCR | MTTR | HIS | Convergence)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--modes", default="helm,agentic",
                        help="helm,agentic (helm-basic는 1-A에 자동 추가)")
    parser.add_argument("--n-tenants", type=int, default=10,
                        help="1-A·1-B에 사용할 테넌트 수 (default: 10)")
    parser.add_argument("--mttr-runs", type=int, default=5,
                        help="드리프트 주입 반복 횟수 (default: 5)")
    parser.add_argument("--batch-sizes", default="5,10",
                        help="1-D 수렴 곡선 배치 크기 (default: '5,10')")
    parser.add_argument("--conv-runs", type=int, default=5,
                        help="1-D 배치당 반복 횟수 (default: 5)")
    parser.add_argument("--tag", default="exp1v2",
                        help="테넌트 ID 접두사 (충돌 방지용)")
    parser.add_argument("--skip-1a", action="store_true", help="PCR 실험 건너뜀")
    parser.add_argument("--skip-1b", action="store_true", help="MTTR 실험 건너뜀")
    parser.add_argument("--skip-1d", action="store_true", help="수렴 곡선 건너뜀")
    parser.add_argument("--skip-apply", action="store_true",
                        help="기존 JSON에서 요약만 출력")
    parser.add_argument("--out", default="results/exp1_results.json")
    args = parser.parse_args()

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = SCRIPT_DIR / args.out

    if args.skip_apply and out_path.exists():
        log(f"기존 결과 로드: {out_path}")
        with open(out_path) as f:
            all_results = json.load(f)
        print_summary(all_results)
        log("\n  → python3 plot_results.py 로 Figure 5 생성")
        return 0

    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    if "helm" in modes and not shutil.which("helm"):
        log("⚠ helm 미설치 — helm 모드 제외")
        modes = [m for m in modes if m != "helm"]

    log("=" * 70)
    log("EXPERIMENT 1 (재설계): 자동화 완성도 비교")
    log("  PCR | MTTR | HIS | Convergence")
    log("=" * 70)
    log(f"  modes={modes}  n_tenants={args.n_tenants}  "
        f"mttr_runs={args.mttr_runs}  tag={args.tag}")

    setup_cluster_priority_classes()

    all_results: Dict[str, Any] = {}

    if not args.skip_1a:
        pcr_modes = ["helm-basic"] + modes
        all_results["exp1a_pcr"] = [
            asdict(r) for r in run_exp1a(pcr_modes, n_tenants=5, tag=args.tag)
        ]

    if not args.skip_1b:
        all_results["exp1b_mttr"] = [
            asdict(r) for r in run_exp1b(
                modes, args.n_tenants, args.mttr_runs, args.tag
            )
        ]

    # 1-C: model-based, no cluster interaction
    all_results["exp1c_his"] = [
        asdict(r) for r in compute_his_model(
            tenant_counts=[1, 5, 10, 20, 50, 100],
            drift_events=3,
            scale_ups=2,
        )
    ]

    if not args.skip_1d:
        batch_sizes = [int(x) for x in args.batch_sizes.split(",") if x.strip()]
        all_results["exp1d_convergence"] = [
            asdict(r) for r in run_exp1d(modes, batch_sizes, args.conv_runs, args.tag)
        ]

    with open(out_path, "w") as f:
        json.dump(all_results, f, indent=2)
    log(f"\n✓ 결과 저장: {out_path}")

    print_summary(all_results)
    log("\n  → python3 plot_results.py 로 Figure 5 생성")
    return 0


if __name__ == "__main__":
    sys.exit(main())
