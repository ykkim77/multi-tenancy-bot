#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
실험 1: Helm vs Agentic 프로비저닝 속도 비교
═══════════════════════════════════════════════════════════════════════════════

비교 대상
─────────
  Helm    : helm install 한 번 실행 → 차트 내부에서 4종 리소스 일괄 생성
            (Namespace + ResourceQuota + LimitRange + NetworkPolicy)
  Agentic : ChatSpace CR 하나만 제출 → Operator가 동일한 4종 리소스를
            MaxConcurrentReconciles=20 으로 병렬 생성 + status 업데이트

설계 근거: 공정 비교(Fair-Comparison) 원칙
─────────────────────────────────────────
  과거 실험은 Agentic이 Helm보다 많은 리소스를 생성(+PriorityClass +RBAC
  +agenticActions)해서 구조적으로 느릴 수밖에 없었다.
  두 경로가 **동일한 4종 테넌트 리소스**를 프로비저닝하도록 통일한다:

    Helm    : Chart 내 NS + RQ + LR + NetPol  (기존 chart 에 이미 포함됨)
    Agentic : Operator 가 NS + RQ + LR + NetPol 생성
              (+ status/agenticActions 는 Operator overhead 로 측정에 반영됨)

  PriorityClass 는 클러스터-스코프 공유 리소스이므로 실험 setup 단계에서
  한 번만 생성하고, 두 경로 모두 pre-existing 상태에서 참조만 한다.

측정 방법론
───────────
  t0      : 명령 제출 직전 time.time() (µs 정밀도)
  Helm    : t0 → NS/RQ/LR/NetPol 4종 모두 존재 (kubectl get 폴링)
  Agentic : t0 → status.phase == Ready (+ 내부적으로 4종 확인)
            서버 측 E2E: client-submit-epoch 어노테이션 → conditions[Ready].lastTransitionTime
            (Kubernetes RFC3339 1초 해상도 문제를 우회하기 위해 µs epoch 을 annotation 에 삽입)

이전 버전(`Python 모방 Operator + time.time()`, Manual 포함)을 대체합니다.
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
TENANT_LABEL = "exp1=true"
NAMESPACE_PREFIX = "tenant-"
POLL_INTERVAL = 0.1
MAX_WAIT = 180.0

# The same 3 PriorityClasses as ensureClusterPriorityClasses() in the Operator.
# They are cluster-scoped and shared; created once during experiment setup.
PRIORITY_CLASSES = [
    {"name": "kcu-tenant-priority",  "value": 10000,
     "preemptionPolicy": "PreemptLowerPriority",
     "description": "High-priority tenants protected from noisy neighbors"},
    {"name": "kcu-tenant-standard",  "value": 1000,
     "preemptionPolicy": "Never",
     "description": "Default-priority tenants"},
    {"name": "kcu-tenant-internal",  "value": 100,
     "preemptionPolicy": "Never",
     "description": "Best-effort/internal-only tenants, first to be reclaimed"},
]

# Module-level dict: tenant_id → float epoch (seconds) recorded immediately
# before kubectl apply.  Used as the t0 for E2E server-side timing so that
# even sub-second reconciles produce a positive (non-zero) duration.
_submit_epochs: Dict[str, float] = {}


def log(msg: str) -> None:
    print(msg, flush=True)


# ── Cluster setup (run once before any experiment) ────────────────────

def setup_cluster_priority_classes() -> None:
    """
    Idempotently create the three shared PriorityClasses that both Helm and
    the Agentic Operator reference.  Mirrors the Operator's
    ensureClusterPriorityClasses() so that neither path has to create them
    during the timed provisioning window.

    This must be called before any experiment run; it is harmless to call
    multiple times (uses kubectl apply --server-side for idempotency).
    """
    log("  [setup] Ensuring cluster PriorityClasses ...")
    for pc in PRIORITY_CLASSES:
        yaml = (
            f"apiVersion: scheduling.k8s.io/v1\n"
            f"kind: PriorityClass\n"
            f"metadata:\n"
            f"  name: {pc['name']}\n"
            f"  labels:\n"
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
            # Fallback: non-server-side apply (older kubectl)
            run(["kubectl", "apply", "-f", "-"], stdin=yaml,
                check=True, timeout=30)
        log(f"    ✓ PriorityClass {pc['name']} ({pc['value']})")


# ── kubectl helpers ──────────────────────────────────────────────────

def run(cmd: List[str], *, check: bool = False, timeout: int = 60,
        stdin: str = "") -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, input=stdin if stdin else None,
        capture_output=True, text=True, check=check, timeout=timeout,
    )


def parse_k8s_time(ts: str) -> Optional[float]:
    """Parse RFC3339/ISO timestamp from Kubernetes (e.g. '2026-05-07T01:23:45Z') to epoch seconds."""
    if not ts:
        return None
    try:
        # Python 3.11+ accepts 'Z'; older needs replace.
        s = ts.replace("Z", "+00:00")
        return datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp() \
            if "+" not in s else datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


# ── Manifest builders for the Manual baseline ────────────────────────

def manual_yamls(tenant_id: str) -> List[Tuple[str, str]]:
    """Returns a list of (name, yaml) tuples in apply order."""
    ns = f"{NAMESPACE_PREFIX}{tenant_id}"
    return [
        ("namespace", (
            f"apiVersion: v1\nkind: Namespace\n"
            f"metadata:\n  name: {ns}\n  labels:\n"
            f"    name: {ns}\n    exp1: \"true\"\n    method: manual\n"
            f"    portal.kcu.ac.kr/tenant: {tenant_id}\n"
        )),
        ("resourcequota", (
            f"apiVersion: v1\nkind: ResourceQuota\n"
            f"metadata:\n  name: tenant-quota\n  namespace: {ns}\n"
            f"  labels:\n    exp1: \"true\"\n    method: manual\n"
            f"spec:\n  hard:\n"
            f"    requests.cpu: \"500m\"\n    requests.memory: \"512Mi\"\n"
            f"    limits.cpu: \"1\"\n    limits.memory: \"1Gi\"\n"
            f"    count/pods: \"10\"\n"
        )),
        ("limitrange", (
            f"apiVersion: v1\nkind: LimitRange\n"
            f"metadata:\n  name: tenant-limits\n  namespace: {ns}\n"
            f"  labels:\n    exp1: \"true\"\n    method: manual\n"
            f"spec:\n  limits:\n  - type: Container\n"
            f"    default:\n      cpu: \"200m\"\n      memory: \"128Mi\"\n"
            f"    defaultRequest:\n      cpu: \"50m\"\n      memory: \"64Mi\"\n"
        )),
        ("networkpolicy", (
            f"apiVersion: networking.k8s.io/v1\nkind: NetworkPolicy\n"
            f"metadata:\n  name: tenant-isolation\n  namespace: {ns}\n"
            f"  labels:\n    exp1: \"true\"\n    method: manual\n"
            f"spec:\n  podSelector: {{}}\n  policyTypes: [Ingress, Egress]\n"
            f"  ingress:\n  - from:\n    - podSelector: {{}}\n"
            f"  egress:\n  - to:\n    - podSelector: {{}}\n"
            f"  - to:\n    - namespaceSelector:\n        matchLabels:\n"
            f"          name: kube-system\n    ports:\n"
            f"    - protocol: UDP\n      port: 53\n"
        )),
    ]


# ── Per-mode "submit" and "ready" detection ──────────────────────────

def submit_manual(tenant_id: str) -> None:
    for _name, y in manual_yamls(tenant_id):
        run(["kubectl", "apply", "-f", "-"], stdin=y, check=True)


def submit_helm(tenant_id: str, tier: str = "standard") -> None:
    """
    Install the kcu-tenant Helm chart for one tenant.

    The chart provisions the SAME 4 resources as the Agentic Operator:
      Namespace + ResourceQuota + LimitRange + NetworkPolicy

    PriorityClass (`kcu-tenant-{tier}`) is pre-created by setup_cluster_priority_classes()
    and referenced via a Namespace annotation; the chart does NOT create it
    (priorityClass.create: false) to avoid parallel-install name conflicts.
    """
    release = f"helm-{tenant_id}"
    run([
        "helm", "install", release, str(HELM_CHART),
        "--set", f"tenantId={tenant_id}",
        "--set", f"tier={tier}",
        "--set", "priorityClass.create=false",
        "--wait=false",
    ], check=True, timeout=60)


def submit_agentic(tenant_id: str) -> None:
    # Record the client-side submit epoch with microsecond precision BEFORE
    # sending to the API server.  This becomes the t0 for E2E server-side
    # timing and avoids the "0-second duration" problem caused by
    # Kubernetes RFC3339 timestamps having only 1-second granularity.
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


def is_ns_ready(tenant_id: str) -> bool:
    """
    Shared readiness check used by both Helm and Agentic modes.

    Verifies that all 4 tenant-scoped resources provisioned by BOTH paths
    exist and are queryable:
      1. Namespace        tenant-{id}
      2. ResourceQuota    tenant-quota
      3. LimitRange       tenant-limits
      4. NetworkPolicy    tenant-isolation

    Resource names are identical in:
      - helm/templates/{resourcequota,limitrange,networkpolicy}.yaml
      - operator/controllers/provisioner.go reconcileResourceQuota/LimitRange/NetworkPolicy
    """
    ns = f"{NAMESPACE_PREFIX}{tenant_id}"
    if run(["kubectl", "get", "ns", ns], check=False).returncode != 0:
        return False
    for kind, name in [
        ("resourcequota",                "tenant-quota"),
        ("limitrange",                   "tenant-limits"),
        ("networkpolicy.networking.k8s.io", "tenant-isolation"),
    ]:
        r = run(["kubectl", "get", kind, name, "-n", ns], check=False)
        if r.returncode != 0:
            return False
    return True


def is_chatspace_ready(tenant_id: str) -> bool:
    """Agentic-specific: status.phase == Ready (and 4 resources exist)."""
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
    return is_ns_ready(tenant_id)


def server_side_timing_for_chatspace(tenant_id: str) -> Optional[Dict[str, float]]:
    """
    Return timing data for a ChatSpace CR.

    Two durations are computed:

    1. e2e_duration (primary, always > 0):
         client_submit_epoch  →  conditions[Ready].lastTransitionTime
         Uses _submit_epochs[] (set just before kubectl apply) as t0.
         Includes network RTT + API admission + Reconcile time.
         Never 0 even if Reconcile completes within the same calendar second.

    2. server_duration (informational, may be 0 for fast Reconciles):
         metadata.creationTimestamp  →  conditions[Ready].lastTransitionTime
         Pure server-side view — bounded by RFC3339 1-second granularity.
         Use as a lower-bound / sanity check only.

    Root cause of "0-second" results in earlier runs:
      Kubernetes RFC3339 timestamps have 1-second resolution.  If the
      Reconcile loop completes within the same clock-second as the CR was
      admitted (very common for KinD clusters with co-located API server
      and controller), creationTimestamp == lastTransitionTime → diff = 0.
      Using the client-side submit epoch fixes this permanently.
    """
    r = run(["kubectl", "get", "chatspace", f"cs-{tenant_id}", "-o", "json"],
            check=False)
    if r.returncode != 0:
        return None
    try:
        obj = json.loads(r.stdout)
    except json.JSONDecodeError:
        return None

    meta = obj.get("metadata", {})
    created = parse_k8s_time(meta.get("creationTimestamp", ""))

    # Ready condition timestamp (t1 — server clock, 1-second granularity)
    ready_at: Optional[float] = None
    for c in (obj.get("status") or {}).get("conditions", []) or []:
        if c.get("type") == "Ready" and c.get("status") == "True":
            ready_at = parse_k8s_time(c.get("lastTransitionTime", ""))
            break
    if ready_at is None:
        return None

    # t0: prefer in-memory dict (most precise); fall back to annotation on CR.
    client_submit = _submit_epochs.get(tenant_id)
    if client_submit is None:
        ann = meta.get("annotations") or {}
        raw = ann.get("portal.kcu.ac.kr/client-submit-epoch", "")
        if raw:
            try:
                client_submit = float(raw)
            except ValueError:
                pass

    result: Dict[str, float] = {"ready_epoch": ready_at}
    if created is not None:
        result["created_epoch"] = created
        result["server_duration"] = max(0.0, ready_at - created)

    if client_submit is not None:
        result["client_submit_epoch"] = client_submit
        # Primary metric: never 0 because client clock has µs precision.
        result["e2e_duration"] = max(0.001, ready_at - client_submit)

    # Canonical "duration" field: E2E when available, server otherwise.
    result["duration"] = result.get("e2e_duration") or result.get("server_duration", 0.0)
    return result


# ── Cleanup ──────────────────────────────────────────────────────────

def cleanup(tenant_ids: List[str]) -> None:
    """Remove all artefacts created by this run, regardless of method."""
    # Delete ChatSpaces (Agentic finalizer cascades to namespaces)
    cs_names = [f"cs-{tid}" for tid in tenant_ids]
    if cs_names:
        run(["kubectl", "delete", "chatspace", *cs_names,
             "--ignore-not-found", "--wait=false"], check=False, timeout=60)
    # Helm releases
    if shutil.which("helm"):
        for tid in tenant_ids:
            release = f"helm-{tid}"
            r = run(["helm", "status", release], check=False, timeout=10)
            if r.returncode == 0:
                run(["helm", "uninstall", release, "--wait=false"],
                    check=False, timeout=30)
    # Namespaces (manual + leftover)
    nss = [f"{NAMESPACE_PREFIX}{tid}" for tid in tenant_ids]
    if nss:
        run(["kubectl", "delete", "ns", *nss,
             "--ignore-not-found", "--wait=false",
             "--grace-period=0", "--force"], check=False, timeout=60)


def _wait_for_cleanup(tenant_ids: List[str], timeout: float = 30) -> None:
    """
    Block until all ChatSpaces and tenant Namespaces from a previous run are
    fully deleted (or until `timeout` seconds elapse).

    This prevents the "object already exists" / "Terminating" races that occur
    when a new run starts before the previous run's finalizer teardown is done.
    ChatSpace finalizers cascade to namespace deletion, which can take 2-10 s.
    """
    deadline = time.perf_counter() + timeout
    cs_names = [f"cs-{tid}" for tid in tenant_ids]
    ns_names = [f"{NAMESPACE_PREFIX}{tid}" for tid in tenant_ids]

    while time.perf_counter() < deadline:
        remaining = []
        for n in cs_names:
            r = run(["kubectl", "get", "chatspace", n], check=False)
            if r.returncode == 0:
                remaining.append(f"cs/{n}")
        for n in ns_names:
            r = run(["kubectl", "get", "ns", n], check=False)
            if r.returncode == 0:
                remaining.append(f"ns/{n}")
        if not remaining:
            return
        time.sleep(0.5)

    log(f"    ⚠ cleanup timeout — some artefacts still exist (proceeding anyway)")


# ── Run a single (mode, batch) configuration ─────────────────────────

@dataclass
class RunRecord:
    mode: str                         # manual | helm | agentic
    batch_size: int
    run_idx: int
    tenant_ids: List[str]
    times_s: List[float] = field(default_factory=list)        # client-side
    cumulative_pct: List[float] = field(default_factory=list) # 0..100
    apply_latency_s: float = 0.0      # time the submit command(s) blocked
    ready_latency_s: float = 0.0      # client-side: t0 → all-Ready
    server_side: Optional[Dict[str, Any]] = None  # Agentic only


def execute_run(mode: Literal["manual", "helm", "agentic"],
                batch_size: int, run_idx: int, tag: str) -> RunRecord:
    tenant_ids = [f"{tag}-{run_idx}-{i:03d}" for i in range(batch_size)]
    rec = RunRecord(mode=mode, batch_size=batch_size, run_idx=run_idx,
                    tenant_ids=tenant_ids)

    log(f"\n  ── {mode.upper()} | batch={batch_size} | run={run_idx + 1} ──")

    cleanup(tenant_ids)
    # Wait for previous-run artefacts to be fully removed before creating
    # new ones with the same names (finalizer teardown can take a few seconds).
    _wait_for_cleanup(tenant_ids, timeout=30)

    submit_fns = {"manual": submit_manual, "helm": submit_helm,
                  "agentic": submit_agentic}
    ready_fn = is_chatspace_ready if mode == "agentic" else is_ns_ready

    log(f"    [1/3] Submitting {batch_size} tenants ({mode}) ...")
    t0 = time.perf_counter()

    if mode == "manual":
        for tid in tenant_ids:
            submit_fns[mode](tid)
    else:
        # Parallel submission — surface per-CR errors immediately
        errors_seen: List[str] = []
        with ThreadPoolExecutor(max_workers=min(16, batch_size)) as ex:
            futures = {ex.submit(submit_fns[mode], tid): tid
                       for tid in tenant_ids}
            for fut in futures:
                tid = futures[fut]
                try:
                    fut.result()
                except Exception as exc:
                    errors_seen.append(f"{tid}: {exc}")
        if errors_seen:
            log(f"    ⚠ Submission errors ({len(errors_seen)}/{batch_size}):")
            for e in errors_seen:
                log(f"      {e}")
            if len(errors_seen) == batch_size:
                log("    ✗ ALL submissions failed — aborting this run")
                rec.ready_latency_s = float("nan")
                return rec

    rec.apply_latency_s = time.perf_counter() - t0
    log(f"    [2/3] Submission done in {rec.apply_latency_s:.2f}s; polling for Ready ...")

    # ── Cumulative success curve ──
    # Optimisation: once a tenant is confirmed ready, skip rechecking it.
    # Without this, B=10 × 10 runs generates hundreds of redundant API calls
    # per second against an already-loaded API server, distorting latency.
    rec.times_s.append(0.0)
    rec.cumulative_pct.append(0.0)

    already_ready: set = set()
    deadline = time.perf_counter() + MAX_WAIT
    last_ready = 0
    while time.perf_counter() < deadline:
        elapsed = time.perf_counter() - t0
        # Only recheck tenants that are NOT yet confirmed ready.
        for tid in tenant_ids:
            if tid not in already_ready and ready_fn(tid):
                already_ready.add(tid)
        ready = len(already_ready)
        rec.times_s.append(elapsed)
        rec.cumulative_pct.append(100.0 * ready / batch_size)
        if ready != last_ready:
            log(f"      t={elapsed:5.2f}s  {ready}/{batch_size} ready ({100*ready/batch_size:.1f}%)")
            last_ready = ready
        if ready >= batch_size:
            rec.ready_latency_s = elapsed
            break
        time.sleep(POLL_INTERVAL)
    else:
        log(f"    ⚠ Timeout after {MAX_WAIT}s (only {len(already_ready)}/{batch_size} ready)")
        rec.ready_latency_s = float("nan")

    # ── Agentic: collect server-side timing ──
    if mode == "agentic" and rec.ready_latency_s == rec.ready_latency_s:  # not NaN
        log(f"    [3/3] Collecting server-side timestamps from ChatSpace status ...")
        ss = []
        for tid in tenant_ids:
            t = server_side_timing_for_chatspace(tid)
            if t:
                ss.append(t)
        if ss:
            # Primary metric: e2e_duration (client submit → server Ready timestamp).
            # Falls back to server_duration if e2e not available.
            durations = [x["duration"] for x in ss]
            e2e_available = sum(1 for x in ss if "e2e_duration" in x)
            rec.server_side = {
                "n": len(ss),
                "n_e2e": e2e_available,
                "mean_s": float(np.mean(durations)),
                "median_s": float(np.median(durations)),
                "p95_s": float(np.percentile(durations, 95)),
                "max_s": float(np.max(durations)),
                "min_s": float(np.min(durations)),
                "per_tenant": ss,
            }
            log(f"      Server-side timings (E2E n={e2e_available}/{len(ss)}) — "
                f"mean={rec.server_side['mean_s']:.3f}s  "
                f"median={rec.server_side['median_s']:.3f}s  "
                f"p95={rec.server_side['p95_s']:.3f}s  "
                f"max={rec.server_side['max_s']:.3f}s")
    else:
        log(f"    [3/3] (server-side timing skipped — {mode})")

    log(f"    → client ready_latency={rec.ready_latency_s:.2f}s, "
        f"apply_latency={rec.apply_latency_s:.2f}s")
    cleanup(tenant_ids)
    return rec


# ── Main ──────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Experiment 1: Helm vs Agentic Provisioning Speed Comparison.\n"
            "Both modes provision identical resources: Namespace + ResourceQuota "
            "+ LimitRange + NetworkPolicy.  PriorityClasses are pre-created "
            "once by the setup step and shared by both paths."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--batch-sizes", default="5,10",
                        help="Comma-separated batch sizes (e.g. '5,10').")
    parser.add_argument("--runs", type=int, default=10,
                        help="Runs per (mode,batch). ≥10 recommended for Mann-Whitney U (default: 10).")
    parser.add_argument("--modes", default="helm,agentic",
                        help=(
                            "Subset of methods to run. "
                            "Choices: helm,agentic[,manual]. "
                            "Default: helm,agentic (fair comparison — both use "
                            "identical 4-resource provisioning)."
                        ))
    parser.add_argument("--tag", default="exp1",
                        help="Prefix for the synthetic tenant IDs (avoid collisions).")
    parser.add_argument("--skip-apply", action="store_true",
                        help="Skip experiment, just re-plot from existing JSON.")
    parser.add_argument("--out", default="results/exp1_results.json")
    args = parser.parse_args()

    batch_sizes = [int(x) for x in args.batch_sizes.split(",") if x.strip()]
    modes = [m.strip() for m in args.modes.split(",") if m.strip()]

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = SCRIPT_DIR / args.out

    if args.skip_apply:
        log(f"Loading existing results from {out_path} ...")
        with open(out_path) as f:
            data = json.load(f)
        log(f"  ({len(data)} records)")
    else:
        # Sanity: tools available?
        if "helm" in modes and not shutil.which("helm"):
            log("⚠ helm not found in PATH — dropping helm from modes.")
            modes = [m for m in modes if m != "helm"]

        log("=" * 70)
        log("EXPERIMENT 1: Helm vs Agentic Provisioning Speed Comparison")
        log("  Fair-comparison: both paths provision NS+RQ+LR+NetPol (4 resources)")
        log("  PriorityClasses: pre-created once (shared cluster resource)")
        log("=" * 70)
        log(f"  Modes        : {modes}")
        log(f"  Batch sizes  : {batch_sizes}")
        log(f"  Runs         : {args.runs}")
        log(f"  Tag          : {args.tag}")

        # ── Pre-setup: cluster PriorityClasses (idempotent) ──
        setup_cluster_priority_classes()

        records: List[RunRecord] = []
        total = len(modes) * len(batch_sizes) * args.runs
        idx = 0
        for batch in batch_sizes:
            for mode in modes:
                for r in range(args.runs):
                    idx += 1
                    log(f"\n[{idx}/{total}]")
                    rec = execute_run(mode, batch, r, args.tag)
                    records.append(rec)

        with open(out_path, "w") as f:
            json.dump([asdict(r) for r in records], f, indent=2)
        log(f"\n✓ Raw results saved to {out_path}")

        data = [asdict(r) for r in records]

    # ── Summary table ───────────────────────────────────────────────
    log("\n" + "=" * 70)
    log("SUMMARY (client-side, ready_latency_s)")
    log("=" * 70)
    by_key: Dict[Tuple[str, int], List[float]] = {}
    server_by_key: Dict[Tuple[str, int], List[float]] = {}
    for rec in data:
        if rec["ready_latency_s"] != rec["ready_latency_s"]:  # NaN
            continue
        by_key.setdefault((rec["mode"], rec["batch_size"]), []).append(rec["ready_latency_s"])
        if rec.get("server_side"):
            # Collect per-tenant "duration" values (e2e preferred, server fallback)
            per_t = rec["server_side"].get("per_tenant", [])
            for t in per_t:
                server_by_key.setdefault(
                    (rec["mode"], rec["batch_size"]), []
                ).append(t["duration"])

    log(f"  {'Mode':<10s}{'Batch':>8s}{'Mean(s)':>12s}{'Std(s)':>10s}{'N':>5s}")
    for (mode, batch) in sorted(by_key.keys()):
        vals = by_key[(mode, batch)]
        log(f"  {mode:<10s}{batch:>8d}{np.mean(vals):>12.3f}{np.std(vals):>10.3f}{len(vals):>5d}")

    if server_by_key:
        log("\n  Agentic — E2E server-side (client submit → conditions[Ready], µs precision):")
        for (mode, batch), vals in sorted(server_by_key.items()):
            log(f"    {mode:<10s}{batch:>8d}"
                f"  mean={np.mean(vals):>7.3f}s"
                f"  median={np.median(vals):>7.3f}s"
                f"  p95={np.percentile(vals,95):>7.3f}s"
                f"  n={len(vals)}")

    log("\n✓ Done. Run plot_results.py (or plot_3way.py) to generate figures.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
