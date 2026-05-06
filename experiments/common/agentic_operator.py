#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
실험에서 Agentic Operator(ChatSpace CR)를 통해 테넌트를 프로비저닝하기 위한
공용 헬퍼.

이 모듈은 두 가지 모드를 모두 지원합니다:

  1. Operator-driven (`mode="operator"`):
     ChatSpace CR을 단 하나 `kubectl apply` 하면, 실제 Go 컨트롤러가
     Namespace + ResourceQuota + LimitRange + NetworkPolicy + (필요시) PriorityClass
     를 자동 생성하고, Re-balancing 결정을 Status에 기록합니다.

  2. Manual (`mode="manual"`):
     기존 실험과 동일하게 각 리소스를 개별적으로 `kubectl apply` 합니다.

따라서 실험 1~3 모두 본 모듈의 `provision_via_operator()` / `wait_for_ready()`
헬퍼를 호출하기만 하면, 동일한 인터페이스로 두 방식을 비교할 수 있습니다.
"""
from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from typing import Iterable, List, Optional


@dataclass
class TenantSpec:
    """ChatSpace CR로 변환할 테넌트 명세."""
    tenant_id: str
    tier: str = "standard"           # priority | standard | internal
    cpu_requests: str = ""
    memory_requests: str = ""
    cpu_limits: str = ""
    memory_limits: str = ""
    max_pods: int = 0
    agentic_enabled: bool = True
    hard_isolation: bool = True
    reclaim_idle_after_seconds: int = 120
    active_boost_factor: str = "1.5"
    usage_hint: str = ""             # "active" | "idle" | ""


# ── YAML rendering ───────────────────────────────────────────────────

def render_chatspace_yaml(spec: TenantSpec, name: Optional[str] = None) -> str:
    """ChatSpace 1개에 대한 YAML 렌더링."""
    cs_name = name or f"cs-{spec.tenant_id}"

    # spec.resourceQuota: 빈 값은 생략 (spec 하위는 2-space 들여쓰기)
    rq_lines: List[str] = []
    if spec.cpu_requests:
        rq_lines.append(f"    cpuRequests: \"{spec.cpu_requests}\"")
    if spec.memory_requests:
        rq_lines.append(f"    memoryRequests: \"{spec.memory_requests}\"")
    if spec.cpu_limits:
        rq_lines.append(f"    cpuLimits: \"{spec.cpu_limits}\"")
    if spec.memory_limits:
        rq_lines.append(f"    memoryLimits: \"{spec.memory_limits}\"")
    if spec.max_pods > 0:
        rq_lines.append(f"    maxPods: {spec.max_pods}")
    rq_block = ("  resourceQuota:\n" + "\n".join(rq_lines) + "\n") if rq_lines else ""

    annotations = ""
    if spec.usage_hint:
        annotations = (
            "  annotations:\n"
            f"    portal.kcu.ac.kr/usage: {spec.usage_hint}\n"
        )

    return (
        f"apiVersion: portal.kcu.ac.kr/v1\n"
        f"kind: ChatSpace\n"
        f"metadata:\n"
        f"  name: {cs_name}\n"
        f"  labels:\n"
        f"    experiment: kcu-portal\n"
        f"{annotations}"
        f"spec:\n"
        f"  tenantId: {spec.tenant_id}\n"
        f"  tier: {spec.tier}\n"
        f"{rq_block}"
        f"  agentic:\n"
        f"    enabled: {str(spec.agentic_enabled).lower()}\n"
        f"    hardIsolation: {str(spec.hard_isolation).lower()}\n"
        f"    reclaimIdleAfterSeconds: {spec.reclaim_idle_after_seconds}\n"
        f"    activeBoostFactor: \"{spec.active_boost_factor}\"\n"
    )


def render_many(specs: Iterable[TenantSpec]) -> str:
    """여러 ChatSpace를 단일 multi-document YAML로 렌더링."""
    return "\n---\n".join(render_chatspace_yaml(s) for s in specs)


# ── kubectl helpers ──────────────────────────────────────────────────

def kubectl_apply_stdin(yaml_str: str, timeout: int = 30) -> float:
    """YAML을 stdin으로 apply하고 wall-clock 시간을 초 단위로 반환."""
    t0 = time.perf_counter()
    r = subprocess.run(
        ["kubectl", "apply", "-f", "-"],
        input=yaml_str, capture_output=True, text=True,
        check=False, timeout=timeout,
    )
    if r.returncode != 0:
        raise RuntimeError(f"kubectl apply failed: {r.stderr.strip()[:200]}")
    return time.perf_counter() - t0


def provision_via_operator(specs: List[TenantSpec]) -> float:
    """
    Operator 방식: ChatSpace CR들을 한 번에 apply.
    Operator가 깨어나서 실제 리소스를 생성하기 전까지의 시간은 포함되지 않음
    → 그래서 `wait_for_ready()`를 함께 사용해야 한다.
    """
    return kubectl_apply_stdin(render_many(specs))


def get_chatspace_status(name: str) -> dict:
    """ChatSpace의 status 서브리소스를 dict로 반환."""
    r = subprocess.run(
        ["kubectl", "get", "chatspace", name, "-o", "json"],
        capture_output=True, text=True, check=False, timeout=10,
    )
    if r.returncode != 0:
        return {}
    try:
        obj = json.loads(r.stdout)
    except json.JSONDecodeError:
        return {}
    return obj.get("status", {}) or {}


def is_ready(name: str) -> bool:
    return get_chatspace_status(name).get("phase") == "Ready"


def wait_for_ready(specs: List[TenantSpec], timeout: float = 120.0,
                   poll: float = 0.5, on_progress=None) -> float:
    """
    모든 ChatSpace가 Phase=Ready가 될 때까지 대기.
    Operator가 namespace/quota/netpol을 모두 만들고 Status를 갱신하면 Ready.
    on_progress(ready_count, total) 콜백을 받을 수 있음.
    반환: Ready까지 걸린 시간(초) 또는 timeout 도달 시 -1.
    """
    names = [f"cs-{s.tenant_id}" for s in specs]
    total = len(names)
    deadline = time.time() + timeout
    t0 = time.perf_counter()
    while time.time() < deadline:
        ready = sum(1 for n in names if is_ready(n))
        if on_progress:
            on_progress(ready, total)
        if ready >= total:
            return time.perf_counter() - t0
        time.sleep(poll)
    return -1.0


def cleanup_all_chatspaces() -> None:
    """experiment=kcu-portal 라벨이 붙은 모든 ChatSpace 삭제 (Operator가 NS도 정리)."""
    subprocess.run(
        ["kubectl", "delete", "chatspaces.portal.kcu.ac.kr",
         "-l", "experiment=kcu-portal", "--ignore-not-found",
         "--wait=false"],
        capture_output=True, text=True, check=False, timeout=60,
    )


# ── Convenience: spec generators for common scenarios ────────────────

def specs_for_density_experiment(n_tenants: int) -> List[TenantSpec]:
    """실험 2 — N 테넌트 균등 배포."""
    return [
        TenantSpec(
            tenant_id=f"density-{i:03d}",
            tier="standard",
            agentic_enabled=True,
            hard_isolation=False,
            usage_hint="active" if i < int(n_tenants * 0.4) else "idle",
        )
        for i in range(n_tenants)
    ]


def specs_for_isolation_experiment(noise_count: int = 99) -> List[TenantSpec]:
    """실험 3 — N개의 noise + 1개의 priority victim."""
    out = [
        TenantSpec(
            tenant_id=f"noise-{i:03d}",
            tier="internal",
            agentic_enabled=True,
            hard_isolation=True,
            usage_hint="active",
        )
        for i in range(noise_count)
    ]
    out.append(TenantSpec(
        tenant_id="victim-001",
        tier="priority",
        agentic_enabled=True,
        hard_isolation=True,
        usage_hint="active",
    ))
    return out
