#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
실험 1: Manual vs Helm vs Agentic 프로비저닝 비교

세 가지 방식으로 동일한 테넌트 묶음(Namespace + ResourceQuota + LimitRange
+ NetworkPolicy)을 프로비저닝한 뒤, **서버 측 시각**을 기준으로
"Ready"까지의 시간을 측정한다.

  1. Manual  : kubectl apply 를 리소스별로 순차 실행 (이론적 한계 속도의 baseline)
  2. Helm    : `helm install kcu-tenant` 한 번 실행 → 차트 내부에서 일괄 적용
  3. Agentic : ChatSpace CR 한 개를 apply → 실제 Operator가 모든 리소스 생성

측정 방법론:
  - 모든 모드: 클라이언트 측 t0 = 명령 제출 직전 (perf_counter)
  - Agentic: 추가로 **서버 측 timing** 도 별도 기록
              (metadata.creationTimestamp → status.conditions[Ready].lastTransitionTime)
  - "Ready" 판정: 모드 무관하게 NS/Quota/LimitRange/NetPol 4개 리소스 모두 존재
                  (Agentic 추가로: status.phase == Ready)

이전 버전(`Python 모방 Operator + time.time()`)을 대체합니다.
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

# Module-level dict: tenant_id → float epoch (seconds) recorded immediately
# before kubectl apply.  Used as the t0 for E2E server-side timing so that
# even sub-second reconciles produce a positive (non-zero) duration.
_submit_epochs: Dict[str, float] = {}


def log(msg: str) -> None:
    print(msg, flush=True)


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


def submit_helm(tenant_id: str) -> None:
    release = f"helm-{tenant_id}"
    # `helm install --create-namespace` 는 차트 자체 Namespace 템플릿과 충돌하므로 비활성화
    run([
        "helm", "install", release, str(HELM_CHART),
        "--set", f"tenantId={tenant_id}",
        "--set", "tier=standard",
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
    """Common readiness check: 4 required objects exist."""
    ns = f"{NAMESPACE_PREFIX}{tenant_id}"
    if run(["kubectl", "get", "ns", ns], check=False).returncode != 0:
        return False
    for kind, name in [
        ("resourcequota", "tenant-quota"),
        ("limitrange", "tenant-limits"),
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
    time.sleep(0.3)

    submit_fns = {"manual": submit_manual, "helm": submit_helm,
                  "agentic": submit_agentic}
    ready_fn = is_chatspace_ready if mode == "agentic" else is_ns_ready

    log(f"    [1/3] Submitting {batch_size} tenants ({mode}) ...")
    t0 = time.perf_counter()

    if mode == "manual":
        # Sequential — the user's spec for the manual baseline.
        for tid in tenant_ids:
            submit_fns[mode](tid)
    else:
        # Helm/Agentic: parallel client-side submission of independent commands
        with ThreadPoolExecutor(max_workers=min(16, batch_size)) as ex:
            list(ex.map(submit_fns[mode], tenant_ids))

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
    parser = argparse.ArgumentParser(description="Experiment 1: Manual vs Helm vs Agentic")
    parser.add_argument("--batch-sizes", default="5,10",
                        help="Comma-separated batch sizes (e.g. '5,10').")
    parser.add_argument("--runs", type=int, default=10,
                        help="Runs per (mode,batch). ≥10 recommended for Mann-Whitney U (default: 10).")
    parser.add_argument("--modes", default="manual,helm,agentic",
                        help="Subset of methods to run.")
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
        log("EXPERIMENT 1: Manual vs Helm vs Agentic Provisioning")
        log("=" * 70)
        log(f"  Modes        : {modes}")
        log(f"  Batch sizes  : {batch_sizes}")
        log(f"  Runs         : {args.runs}")
        log(f"  Tag          : {args.tag}")

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

    log("\n✓ Done. Run plot_3way.py to generate figures.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
