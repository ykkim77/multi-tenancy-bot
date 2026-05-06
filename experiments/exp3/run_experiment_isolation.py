#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
실험 3 (재설계): Hard Isolation under Noisy Neighbors
─────────────────────────────────────────────────────
이전 버전은 "Operator 모드"라고 부른 것이 사실은 사람이 PriorityClass /
ResourceQuota / NetworkPolicy 를 손으로 적용한 것이었다.  이번 버전은
이 한계를 정면으로 풀어, **세 가지 운영 방식**을 같은 시나리오에서 비교한다.

비교 대상 (3 modes)
───────────────────
  B1  baseline : 모든 테넌트 동등 quota, NetworkPolicy 없음, 보호 없음
  B2  manual   : 사람이 PriorityClass + 차등 quota + NetPol + LimitRange
                 를 직접 kubectl apply (현업 모범 practice — 정적)
  B3  agentic  : ChatSpace CR 만 제출. 실제 Operator 가
                 - 첫 reconcile 에서 모든 정책 자동 생성
                 - 30 초마다 클러스터 전체 자율 관찰
                 - quota 재조정 + agenticActions 기록

시나리오 (3 scenarios)
──────────────────────
  A  basic         : 1 victim + 1 aggressor                       (기준점)
  B  multi-attack  : 1 victim + 3 aggressors + 2 idle background  (다수 공격자)
  C  multi-tenant  : 5 victims + 5 aggressors + 5 idle background (실제 multi-tenant SLA)
                     → 5 victims 중 3 명은 priority, 2 명은 standard tier 로
                       구성하여 **tier-별 보호 차등** 검증

수집 지표
─────────
  기존 (모드 무관):
    - victim 별 latency time-series (probe = ConfigMap CRUD round-trip)
    - latency CDF / P50 / P95 / P99 / jitter (std)
  agentic 전용 추가 지표:
    - agenticActions timeline:
        * 노이즈 주입 시각 → 관련 action(보강/회수)이 등장한 시각 까지
          걸린 "Operator 자동 대응 시간"
        * 실험 기간 중 발생한 rebalance 횟수와 종류
    - ChatSpace.status.lastUpdated 진행 → reconcile 빈도 측정
    - tier 별 보호 차등(P95 priority vs standard victim)
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, Optional, Tuple

os.environ["PYTHONUNBUFFERED"] = "1"

import numpy as np

SCRIPT_DIR = Path(__file__).resolve().parent
RESULTS_DIR = SCRIPT_DIR / "results"

# ── Constants ────────────────────────────────────────────────────────
NS_PREFIX = "exp3-iso"
CS_PREFIX = "cs-exp3"
SAMPLE_INTERVAL = 0.10            # latency probe spacing
DEFAULT_DURATION = 60             # stress measurement window
PRE_NOISE_SAMPLES = 15
POLL_AGENTIC_EVERY = 3.0          # seconds between agenticActions snapshots
WARMUP_AFTER_STRESS = 5           # seconds to let pods schedule

SCENARIOS: Dict[str, Dict[str, Any]] = {
    "A": {"name": "basic",        "victims": 1, "aggressors": 1, "idle_bg": 0,
          "victim_tiers": ["priority"]},
    "B": {"name": "multi-attack", "victims": 1, "aggressors": 3, "idle_bg": 2,
          "victim_tiers": ["priority"]},
    "C": {"name": "multi-tenant", "victims": 5, "aggressors": 5, "idle_bg": 5,
          "victim_tiers": ["priority", "priority", "priority",
                           "standard", "standard"]},
}
MODES: Tuple[str, ...] = ("baseline", "manual", "agentic")


def log(msg: str) -> None:
    print(msg, flush=True)


def run_cmd(cmd: List[str], *, check: bool = False, timeout: int = 60,
            stdin: Optional[str] = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, input=stdin, capture_output=True, text=True,
                          check=check, timeout=timeout)


def parse_k8s_time(ts: str) -> Optional[float]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


# ── YAML generators ──────────────────────────────────────────────────

def yaml_namespace(name: str, scenario: str, mode: str, role: str) -> str:
    return (
        f"apiVersion: v1\nkind: Namespace\n"
        f"metadata:\n  name: {name}\n  labels:\n"
        f"    experiment: exp3\n    scenario: {scenario}\n"
        f"    mode: {mode}\n    role: {role}\n"
    )


def yaml_priority_class(name: str, value: int, preempt: bool = False) -> str:
    return (
        f"apiVersion: scheduling.k8s.io/v1\nkind: PriorityClass\n"
        f"metadata:\n  name: {name}\n  labels:\n    experiment: exp3\n"
        f"value: {value}\nglobalDefault: false\n"
        f"preemptionPolicy: {'PreemptLowerPriority' if preempt else 'Never'}\n"
        f"description: \"exp3 priority class\"\n"
    )


def yaml_quota(ns: str, cpu_req: str, mem_req: str, cpu_lim: str, mem_lim: str,
               pods: str = "20") -> str:
    return (
        f"apiVersion: v1\nkind: ResourceQuota\n"
        f"metadata:\n  name: tenant-quota\n  namespace: {ns}\n"
        f"  labels:\n    experiment: exp3\n"
        f"spec:\n  hard:\n"
        f"    requests.cpu: \"{cpu_req}\"\n    requests.memory: \"{mem_req}\"\n"
        f"    limits.cpu: \"{cpu_lim}\"\n    limits.memory: \"{mem_lim}\"\n"
        f"    count/pods: \"{pods}\"\n"
    )


def yaml_limitrange(ns: str, cpu_def: str, mem_def: str,
                    cpu_max: str, mem_max: str) -> str:
    return (
        f"apiVersion: v1\nkind: LimitRange\n"
        f"metadata:\n  name: tenant-limits\n  namespace: {ns}\n"
        f"  labels:\n    experiment: exp3\n"
        f"spec:\n  limits:\n  - type: Container\n"
        f"    default:\n      cpu: \"{cpu_def}\"\n      memory: \"{mem_def}\"\n"
        f"    max:\n      cpu: \"{cpu_max}\"\n      memory: \"{mem_max}\"\n"
        f"    defaultRequest:\n      cpu: \"50m\"\n      memory: \"64Mi\"\n"
    )


def yaml_netpol_isolate(ns: str) -> str:
    return (
        f"apiVersion: networking.k8s.io/v1\nkind: NetworkPolicy\n"
        f"metadata:\n  name: tenant-isolation\n  namespace: {ns}\n"
        f"  labels:\n    experiment: exp3\n"
        f"spec:\n  podSelector: {{}}\n  policyTypes: [Ingress, Egress]\n"
        f"  ingress:\n  - from:\n    - podSelector: {{}}\n"
        f"  egress:\n  - to:\n    - podSelector: {{}}\n"
        f"  - to:\n    - namespaceSelector:\n        matchLabels:\n"
        f"          name: kube-system\n    ports:\n    - protocol: UDP\n      port: 53\n"
    )


def yaml_stress_pod(ns: str, name: str, priority_class: str = "",
                    cpu_req: str = "100m", mem_req: str = "64Mi",
                    cpu_lim: str = "500m", mem_lim: str = "256Mi") -> str:
    pc_line = f"  priorityClassName: {priority_class}\n" if priority_class else ""
    return (
        f"apiVersion: v1\nkind: Pod\n"
        f"metadata:\n  name: {name}\n  namespace: {ns}\n"
        f"  labels:\n    role: noise\n    experiment: exp3\n"
        f"spec:\n{pc_line}  terminationGracePeriodSeconds: 0\n"
        f"  containers:\n"
        f"  - name: cpu-burn\n    image: busybox\n"
        f"    command: [\"sh\",\"-c\",\"while true; do :; done\"]\n"
        f"    resources:\n"
        f"      requests: {{cpu: \"{cpu_req}\", memory: \"{mem_req}\"}}\n"
        f"      limits:   {{cpu: \"{cpu_lim}\", memory: \"{mem_lim}\"}}\n"
        f"  - name: io-burn\n    image: busybox\n"
        f"    command:\n"
        f"    - sh\n    - -c\n"
        f"    - |\n"
        f"      while true; do dd if=/dev/urandom of=/tmp/junk bs=4k count=2048 2>/dev/null; rm -f /tmp/junk; done\n"
        f"    resources:\n"
        f"      requests: {{cpu: \"50m\", memory: \"64Mi\"}}\n"
        f"      limits:   {{cpu: \"200m\", memory: \"128Mi\"}}\n"
        f"  restartPolicy: Always\n"
    )


def yaml_chatspace(name: str, tenant_id: str, tier: str, scenario: str,
                   role: str, usage: str = "active",
                   hard_isolation: bool = True) -> str:
    return (
        f"apiVersion: portal.kcu.ac.kr/v1\nkind: ChatSpace\n"
        f"metadata:\n  name: {name}\n"
        f"  annotations:\n    portal.kcu.ac.kr/usage: {usage}\n"
        f"  labels:\n"
        f"    experiment: exp3\n    scenario: {scenario}\n"
        f"    mode: agentic\n    role: {role}\n    tier: {tier}\n"
        f"spec:\n  tenantId: {tenant_id}\n  tier: {tier}\n"
        f"  agentic:\n    enabled: true\n"
        f"    hardIsolation: {str(hard_isolation).lower()}\n"
        f"    reclaimIdleAfterSeconds: 60\n    activeBoostFactor: \"1.5\"\n"
    )


# ── kubectl helpers ──────────────────────────────────────────────────

def apply_yaml(yaml_str: str) -> None:
    r = run_cmd(["kubectl", "apply", "-f", "-"], stdin=yaml_str, timeout=30)
    if r.returncode != 0:
        log(f"    apply error: {r.stderr.strip()[:160]}")


def apply_yaml_parallel(yamls: List[str], workers: int = 8) -> None:
    if not yamls:
        return
    with ThreadPoolExecutor(max_workers=min(workers, len(yamls))) as ex:
        list(ex.map(apply_yaml, yamls))


def measure_victim_latency(ns: str) -> float:
    """Round-trip time for ConfigMap create → get → delete in `ns`."""
    cm_name = f"probe-{int(time.time() * 1000) % 100000}"
    cm_yaml = (
        f"apiVersion: v1\nkind: ConfigMap\n"
        f"metadata:\n  name: {cm_name}\n  namespace: {ns}\n"
        f"data:\n  k: v\n"
    )
    t0 = time.perf_counter()
    run_cmd(["kubectl", "apply", "-f", "-"], stdin=cm_yaml, timeout=15)
    run_cmd(["kubectl", "get", "configmap", cm_name, "-n", ns, "-o", "json"], timeout=15)
    run_cmd(["kubectl", "delete", "configmap", cm_name, "-n", ns,
             "--ignore-not-found"], timeout=15)
    return time.perf_counter() - t0


def get_chatspace(name: str) -> Optional[Dict[str, Any]]:
    r = run_cmd(["kubectl", "get", "chatspace", name, "-o", "json"], timeout=15)
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return None


def is_chatspace_ready(name: str) -> bool:
    obj = get_chatspace(name)
    if not obj:
        return False
    return (obj.get("status") or {}).get("phase") == "Ready"


def is_ns_ready(ns: str) -> bool:
    for kind in ("resourcequota/tenant-quota", "limitrange/tenant-limits"):
        if run_cmd(["kubectl", "get", kind, "-n", ns, "-o", "name"]).returncode != 0:
            return False
    return True


def cleanup_exp3() -> None:
    log("  Cleaning up exp3 resources ...")
    # ChatSpaces
    r = run_cmd(["kubectl", "get", "chatspace", "-l", "experiment=exp3",
                 "-o", "jsonpath={.items[*].metadata.name}"])
    if r.returncode == 0 and r.stdout.strip():
        names = r.stdout.strip().split()
        run_cmd(["kubectl", "delete", "chatspace", *names,
                 "--ignore-not-found", "--wait=false"], timeout=120)
    # Namespaces (manual / baseline)
    r = run_cmd(["kubectl", "get", "ns", "-l", "experiment=exp3",
                 "-o", "jsonpath={.items[*].metadata.name}"])
    if r.returncode == 0 and r.stdout.strip():
        nss = r.stdout.strip().split()
        run_cmd(["kubectl", "delete", "ns", *nss, "--ignore-not-found",
                 "--wait=false", "--grace-period=0", "--force"], timeout=180)
    # Manual-mode PriorityClasses
    for pc in ("exp3-noise-low", "exp3-victim-high"):
        run_cmd(["kubectl", "delete", "priorityclass", pc, "--ignore-not-found"])
    time.sleep(2)


# ── Tenant model ─────────────────────────────────────────────────────

@dataclass
class Tenant:
    role: str          # 'victim' | 'aggressor' | 'background'
    idx: int
    tier: str          # 'priority' | 'standard'
    namespace: str
    cs_name: Optional[str] = None  # only set for agentic mode
    expected_idle: bool = False    # ground-truth flag for agentic


def build_tenants(scenario: str, mode: str) -> List[Tenant]:
    sc = SCENARIOS[scenario]
    out: List[Tenant] = []
    for i in range(sc["victims"]):
        tier = sc["victim_tiers"][i] if i < len(sc["victim_tiers"]) else "priority"
        if mode == "agentic":
            ns = f"tenant-exp3-{scenario.lower()}-victim-{i}"
            cs = f"{CS_PREFIX}-{scenario.lower()}-victim-{i}"
        else:
            ns = f"{NS_PREFIX}-{scenario.lower()}-{mode}-victim-{i}"
            cs = None
        out.append(Tenant("victim", i, tier, ns, cs, expected_idle=False))
    for i in range(sc["aggressors"]):
        if mode == "agentic":
            ns = f"tenant-exp3-{scenario.lower()}-aggr-{i}"
            cs = f"{CS_PREFIX}-{scenario.lower()}-aggr-{i}"
        else:
            ns = f"{NS_PREFIX}-{scenario.lower()}-{mode}-aggr-{i}"
            cs = None
        out.append(Tenant("aggressor", i, "standard", ns, cs, expected_idle=False))
    for i in range(sc["idle_bg"]):
        if mode == "agentic":
            ns = f"tenant-exp3-{scenario.lower()}-bg-{i}"
            cs = f"{CS_PREFIX}-{scenario.lower()}-bg-{i}"
        else:
            ns = f"{NS_PREFIX}-{scenario.lower()}-{mode}-bg-{i}"
            cs = None
        out.append(Tenant("background", i, "internal", ns, cs, expected_idle=True))
    return out


# ── Provisioning per mode ────────────────────────────────────────────

def provision_baseline(scenario: str, tenants: List[Tenant]) -> None:
    """B1 — equal fixed quota for everyone, no NetPol, no PriorityClass."""
    yamls: List[str] = []
    for t in tenants:
        yamls.append(yaml_namespace(t.namespace, scenario, "baseline", t.role))
    apply_yaml_parallel(yamls)

    yamls = []
    for t in tenants:
        yamls.append(yaml_quota(t.namespace, "300m", "256Mi", "600m", "512Mi"))
        yamls.append(yaml_limitrange(t.namespace, "100m", "128Mi", "300m", "256Mi"))
    apply_yaml_parallel(yamls)


def provision_manual(scenario: str, tenants: List[Tenant]) -> None:
    """B2 — manually applied PriorityClass + tiered quota + NetPol + LimitRange."""
    apply_yaml(yaml_priority_class("exp3-noise-low", 100))
    apply_yaml(yaml_priority_class("exp3-victim-high", 10000, preempt=True))

    yamls = [yaml_namespace(t.namespace, scenario, "manual", t.role) for t in tenants]
    apply_yaml_parallel(yamls)

    yamls = []
    for t in tenants:
        if t.role == "victim":
            if t.tier == "priority":
                yamls.append(yaml_quota(t.namespace, "2", "2Gi", "4", "4Gi", pods="30"))
                yamls.append(yaml_limitrange(t.namespace, "500m", "512Mi", "2", "2Gi"))
            else:  # standard tier victim
                yamls.append(yaml_quota(t.namespace, "500m", "512Mi", "1", "1Gi", pods="10"))
                yamls.append(yaml_limitrange(t.namespace, "200m", "128Mi", "1", "1Gi"))
        elif t.role == "aggressor":
            yamls.append(yaml_quota(t.namespace, "200m", "128Mi", "500m", "256Mi", pods="3"))
            yamls.append(yaml_limitrange(t.namespace, "100m", "64Mi", "300m", "128Mi"))
        else:  # background
            yamls.append(yaml_quota(t.namespace, "100m", "128Mi", "300m", "256Mi", pods="5"))
            yamls.append(yaml_limitrange(t.namespace, "50m", "64Mi", "200m", "128Mi"))
        yamls.append(yaml_netpol_isolate(t.namespace))
    apply_yaml_parallel(yamls)


def provision_agentic(scenario: str, tenants: List[Tenant]) -> None:
    """B3 — submit ChatSpace CRs only; Operator does the rest."""
    yamls: List[str] = []
    for t in tenants:
        usage = "idle" if t.expected_idle else "active"
        yamls.append(yaml_chatspace(
            name=t.cs_name, tenant_id=f"exp3-{scenario.lower()}-{t.role}-{t.idx}",
            tier=t.tier, scenario=scenario, role=t.role, usage=usage,
            hard_isolation=True))
    apply_yaml_parallel(yamls, workers=4)

    # Wait until all CRs reach Phase=Ready
    deadline = time.time() + 180
    pending = [t.cs_name for t in tenants]
    while pending and time.time() < deadline:
        still = [n for n in pending if not is_chatspace_ready(n)]
        if len(still) != len(pending):
            log(f"    Operator: {len(tenants) - len(still)}/{len(tenants)} ChatSpaces Ready")
        pending = still
        if not pending:
            break
        time.sleep(2)
    if pending:
        log(f"    ⚠ {len(pending)} ChatSpaces did not reach Ready in 180s")


# ── agenticActions polling (background thread) ───────────────────────

@dataclass
class AgenticSnapshot:
    t_rel: float
    cs_name: str
    role: str
    tier: str
    phase: Optional[str]
    last_updated_epoch: Optional[float]
    actions_len: int
    latest_action: Optional[str]


def _classify_action(action: str) -> str:
    a = action.lower()
    if "reclaimed" in a:
        return "reclaimed"
    if "boosted" in a:
        return "boosted"
    if "no rebalancing" in a:
        return "no_rebal"
    if "agentic disabled" in a:
        return "agentic_off"
    if "falling back" in a:
        return "fallback"
    return "other"


class AgenticPoller(threading.Thread):
    """Polls every ChatSpace's status during the stress window and records
    the timeline of `agenticActions` plus reconcile timestamps."""

    def __init__(self, tenants: List[Tenant], t0: float, stop_evt: threading.Event):
        super().__init__(daemon=True)
        self.tenants = [t for t in tenants if t.cs_name]
        self.t0 = t0
        self.stop_evt = stop_evt
        self.snapshots: List[AgenticSnapshot] = []
        # Final per-CR action lists
        self.final: Dict[str, Dict[str, Any]] = {}

    def run(self) -> None:
        while not self.stop_evt.is_set():
            for t in self.tenants:
                obj = get_chatspace(t.cs_name)
                if not obj:
                    continue
                status = obj.get("status") or {}
                actions = status.get("agenticActions") or []
                self.snapshots.append(AgenticSnapshot(
                    t_rel=time.perf_counter() - self.t0,
                    cs_name=t.cs_name, role=t.role, tier=t.tier,
                    phase=status.get("phase"),
                    last_updated_epoch=parse_k8s_time(status.get("lastUpdated", "")),
                    actions_len=len(actions),
                    latest_action=actions[-1] if actions else None,
                ))
                self.final[t.cs_name] = {
                    "role": t.role, "tier": t.tier,
                    "actions": list(actions),
                    "applied_quota": status.get("appliedQuota"),
                    "phase": status.get("phase"),
                    "created_epoch": parse_k8s_time(
                        obj.get("metadata", {}).get("creationTimestamp", "")),
                    "last_updated_epoch": parse_k8s_time(status.get("lastUpdated", "")),
                }
            if self.stop_evt.wait(POLL_AGENTIC_EVERY):
                break


# ── Probing helpers ──────────────────────────────────────────────────

def measure_pre_noise(victims: List[Tenant], n_samples: int = PRE_NOISE_SAMPLES
                      ) -> Dict[str, List[float]]:
    out: Dict[str, List[float]] = {v.namespace: [] for v in victims}
    for _ in range(n_samples):
        for v in victims:
            out[v.namespace].append(measure_victim_latency(v.namespace))
            time.sleep(SAMPLE_INTERVAL)
    return out


def deploy_stress(scenario: str, mode: str, aggressors: List[Tenant]) -> None:
    """Deploy 1 stress pod per aggressor namespace."""
    pc = "" if mode != "manual" else "exp3-noise-low"
    yamls: List[str] = []
    for t in aggressors:
        yamls.append(yaml_stress_pod(
            ns=t.namespace, name=f"stress-{t.idx}", priority_class=pc,
        ))
    apply_yaml_parallel(yamls)


def stress_window(victims: List[Tenant], duration: float
                  ) -> Dict[str, List[Tuple[float, float]]]:
    """Round-robin probe of victim namespaces for `duration` seconds.
    Returns per-victim list of (t_rel, latency_s)."""
    out: Dict[str, List[Tuple[float, float]]] = {v.namespace: [] for v in victims}
    t0 = time.perf_counter()
    last_log = 0.0
    while time.perf_counter() - t0 < duration:
        t_rel = time.perf_counter() - t0
        for v in victims:
            lat = measure_victim_latency(v.namespace)
            out[v.namespace].append((t_rel, lat))
            time.sleep(SAMPLE_INTERVAL)
        if t_rel - last_log >= 10.0:
            last = {ns: vs[-1][1] * 1000 for ns, vs in out.items() if vs}
            log(f"    t={t_rel:5.1f}s  latencies(ms)=" +
                ", ".join(f"{ns.split('-')[-1]}:{v:.0f}" for ns, v in last.items()))
            last_log = t_rel
    return out


# ── One full (scenario, mode) run ────────────────────────────────────

def run_one(scenario: str, mode: str, duration: float, run_idx: int
            ) -> Dict[str, Any]:
    log(f"\n{'─' * 64}")
    log(f"  Scenario {scenario} ({SCENARIOS[scenario]['name']}) | "
        f"mode={mode.upper()} | run={run_idx + 1}")
    log(f"{'─' * 64}")
    cleanup_exp3()
    time.sleep(1)

    tenants = build_tenants(scenario, mode)
    victims = [t for t in tenants if t.role == "victim"]
    aggressors = [t for t in tenants if t.role == "aggressor"]

    # ── 1. Provision ───────────────────────────────────────────────
    log(f"  [1/5] Provisioning {len(tenants)} tenants ({mode}) ...")
    t_prov0 = time.perf_counter()
    if mode == "baseline":
        provision_baseline(scenario, tenants)
    elif mode == "manual":
        provision_manual(scenario, tenants)
    elif mode == "agentic":
        provision_agentic(scenario, tenants)
    prov_dt = time.perf_counter() - t_prov0
    log(f"        provisioning took {prov_dt:.1f}s")

    # Validate non-agentic ns are ready
    if mode != "agentic":
        for t in tenants:
            for _ in range(20):
                if is_ns_ready(t.namespace):
                    break
                time.sleep(0.5)

    # ── 2. Pre-noise baseline measurement ──────────────────────────
    log(f"  [2/5] Pre-noise baseline ({PRE_NOISE_SAMPLES} samples × {len(victims)} victims) ...")
    pre_noise = measure_pre_noise(victims)

    # ── 3. Deploy stress pods ──────────────────────────────────────
    log(f"  [3/5] Deploying {len(aggressors)} stress pods ...")
    stress_t0_epoch = time.time()
    deploy_stress(scenario, mode, aggressors)
    log(f"        warm-up {WARMUP_AFTER_STRESS}s ...")
    time.sleep(WARMUP_AFTER_STRESS)

    # ── 4. Concurrently measure victim latency + poll agenticActions
    log(f"  [4/5] Measuring victim latency ({duration}s) "
        f"{'+ polling agenticActions' if mode == 'agentic' else ''} ...")
    poller: Optional[AgenticPoller] = None
    stop_evt = threading.Event()
    if mode == "agentic":
        poller = AgenticPoller(tenants, t0=time.perf_counter(), stop_evt=stop_evt)
        poller.start()

    series = stress_window(victims, duration)

    if poller is not None:
        stop_evt.set()
        poller.join(timeout=10)

    # ── 5. Statistics ──────────────────────────────────────────────
    per_victim_stats: Dict[str, Dict[str, Any]] = {}
    for v in victims:
        s = series.get(v.namespace, [])
        lat = np.array([x[1] for x in s]) * 1000
        if len(lat) == 0:
            continue
        per_victim_stats[v.namespace] = {
            "role": v.role, "tier": v.tier, "idx": v.idx,
            "n_samples": len(lat),
            "mean_ms": float(np.mean(lat)),
            "median_ms": float(np.median(lat)),
            "p95_ms": float(np.percentile(lat, 95)),
            "p99_ms": float(np.percentile(lat, 99)),
            "std_ms": float(np.std(lat)),
            "min_ms": float(np.min(lat)),
            "max_ms": float(np.max(lat)),
        }

    # Aggregate across all victims
    flat_lat = np.concatenate([np.array([x[1] for x in s]) for s in series.values()
                               if s]) * 1000 if any(series.values()) else np.array([0.0])
    overall = {
        "n_samples": int(len(flat_lat)),
        "mean_ms": float(np.mean(flat_lat)),
        "median_ms": float(np.median(flat_lat)),
        "p95_ms": float(np.percentile(flat_lat, 95)),
        "p99_ms": float(np.percentile(flat_lat, 99)),
        "std_ms": float(np.std(flat_lat)),
    }

    log(f"  Results — overall P95={overall['p95_ms']:.1f}ms, "
        f"P99={overall['p99_ms']:.1f}ms, jitter={overall['std_ms']:.1f}ms")

    # Tier-broken stats (priority vs standard victims)
    tier_stats: Dict[str, Dict[str, float]] = {}
    for tier in ("priority", "standard"):
        lats: List[float] = []
        for v in victims:
            if v.tier != tier:
                continue
            lats.extend([x[1] * 1000 for x in series.get(v.namespace, [])])
        if lats:
            arr = np.array(lats)
            tier_stats[tier] = {
                "n": int(len(arr)),
                "mean_ms": float(np.mean(arr)),
                "p95_ms": float(np.percentile(arr, 95)),
                "p99_ms": float(np.percentile(arr, 99)),
                "std_ms": float(np.std(arr)),
            }

    # ── Agentic-only extras ────────────────────────────────────────
    agentic_data: Optional[Dict[str, Any]] = None
    if poller is not None:
        agentic_data = build_agentic_summary(poller, stress_t0_epoch)
        log(f"  Operator audit: total actions="
            f"{agentic_data['total_actions']}, "
            f"new during stress={agentic_data['new_actions_during_stress']}, "
            f"first-response="
            f"{agentic_data['first_response_s']:.1f}s"
            if agentic_data['first_response_s'] is not None
            else "  Operator audit: no rebalance during stress")

    # ── Cleanup ────────────────────────────────────────────────────
    cleanup_exp3()

    return {
        "scenario": scenario,
        "scenario_name": SCENARIOS[scenario]["name"],
        "mode": mode,
        "run_idx": run_idx,
        "duration_s": duration,
        "n_victims": len(victims),
        "n_aggressors": len(aggressors),
        "n_idle_bg": SCENARIOS[scenario]["idle_bg"],
        "stress_t0_epoch": stress_t0_epoch,
        "provisioning_s": prov_dt,
        "pre_noise_per_victim_ms": {ns: [x * 1000 for x in vs]
                                    for ns, vs in pre_noise.items()},
        "series_per_victim": {ns: [(t, lat * 1000) for (t, lat) in s]
                              for ns, s in series.items()},
        "stats_per_victim": per_victim_stats,
        "stats_overall": overall,
        "stats_per_tier": tier_stats,
        "agentic": agentic_data,
    }


def build_agentic_summary(poller: AgenticPoller, stress_t0_epoch: float
                          ) -> Dict[str, Any]:
    """Aggregate the poller's snapshots into a compact summary."""
    timeline: List[Dict[str, Any]] = []
    seen_actions_count: Dict[str, int] = {}
    new_action_events: List[Dict[str, Any]] = []
    first_response_s: Optional[float] = None

    for snap in poller.snapshots:
        timeline.append({
            "t_rel_s": snap.t_rel,
            "cs": snap.cs_name, "role": snap.role, "tier": snap.tier,
            "phase": snap.phase,
            "actions_len": snap.actions_len,
            "last_updated_epoch": snap.last_updated_epoch,
            "latest_action": snap.latest_action,
        })
        prev = seen_actions_count.get(snap.cs_name, 0)
        if snap.actions_len > prev and snap.latest_action:
            cat = _classify_action(snap.latest_action)
            new_action_events.append({
                "t_rel_s": snap.t_rel, "cs": snap.cs_name,
                "role": snap.role, "tier": snap.tier,
                "category": cat, "action": snap.latest_action,
            })
            if first_response_s is None and cat in ("reclaimed", "boosted"):
                first_response_s = snap.t_rel
        seen_actions_count[snap.cs_name] = snap.actions_len

    counts: Dict[str, int] = {}
    for ev in new_action_events:
        counts[ev["category"]] = counts.get(ev["category"], 0) + 1

    # Per-CR final action list
    final_per_cr = poller.final
    total_actions = sum(len(v["actions"]) for v in final_per_cr.values())

    return {
        "timeline": timeline,
        "new_action_events": new_action_events,
        "action_counts": counts,
        "total_actions": total_actions,
        "new_actions_during_stress": len(new_action_events),
        "first_response_s": first_response_s,
        "per_cr": final_per_cr,
    }


# ── Main ─────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="Experiment 3: Hard Isolation under Noisy Neighbors")
    parser.add_argument("--scenarios", default="A,B,C")
    parser.add_argument("--modes", default="baseline,manual,agentic")
    parser.add_argument("--duration", type=float, default=DEFAULT_DURATION)
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--skip-apply", action="store_true")
    parser.add_argument("--out", default="results/exp3_isolation_results.json")
    args = parser.parse_args()

    scenarios = [s.strip().upper() for s in args.scenarios.split(",") if s.strip()]
    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = SCRIPT_DIR / args.out

    if args.skip_apply:
        log(f"Loading existing results from {out_path} ...")
        with open(out_path) as f:
            all_results = json.load(f)
        log(f"  ({len(all_results)} records)")
    else:
        log("=" * 70)
        log("EXPERIMENT 3 (redesigned): Hard Isolation under Noisy Neighbors")
        log("=" * 70)
        log(f"  Scenarios   : {scenarios}")
        log(f"  Modes       : {modes}")
        log(f"  Duration    : {args.duration}s")
        log(f"  Runs        : {args.runs}")
        log("=" * 70)

        all_results: List[Dict[str, Any]] = []
        total = len(scenarios) * len(modes) * args.runs
        idx = 0
        for sc in scenarios:
            for mode in modes:
                for run_idx in range(args.runs):
                    idx += 1
                    log(f"\n[{idx}/{total}]")
                    rec = run_one(sc, mode, args.duration, run_idx)
                    all_results.append(rec)

        with open(out_path, "w") as f:
            json.dump(all_results, f, indent=2)
        log(f"\n✓ Raw results saved to {out_path}")

    # ── Summary table ───────────────────────────────────────────────
    log("\n" + "=" * 70)
    log("SUMMARY  (overall, averaged across runs)")
    log("=" * 70)
    log(f"  {'Scen':<6}{'Mode':<10}{'P50':>8}{'P95':>8}{'P99':>8}{'σ':>8}{'  Operator':<14}")
    by_key: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for r in all_results:
        by_key.setdefault((r["scenario"], r["mode"]), []).append(r)
    for (sc, mode) in sorted(by_key.keys()):
        recs = by_key[(sc, mode)]
        p50 = np.mean([r["stats_overall"]["median_ms"] for r in recs])
        p95 = np.mean([r["stats_overall"]["p95_ms"] for r in recs])
        p99 = np.mean([r["stats_overall"]["p99_ms"] for r in recs])
        std = np.mean([r["stats_overall"]["std_ms"] for r in recs])
        op_info = ""
        if mode == "agentic":
            firsts = [r["agentic"]["first_response_s"] for r in recs
                      if r.get("agentic") and r["agentic"]["first_response_s"] is not None]
            new_actions = np.mean([r["agentic"]["new_actions_during_stress"]
                                   for r in recs if r.get("agentic")])
            op_info = (f"resp={np.mean(firsts):.1f}s," if firsts else "resp=n/a, "
                      ) + f"#={new_actions:.0f}"
        log(f"  {sc:<6}{mode:<10}{p50:>8.1f}{p95:>8.1f}{p99:>8.1f}{std:>8.1f}  {op_info}")

    log("\n✓ Run plot_isolation.py and plot_agentic_analysis.py.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
