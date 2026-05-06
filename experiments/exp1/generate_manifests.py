#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
실험 1: 복잡도(1~10)별 테넌트 매니페스트 생성.
- Namespace 1개 + NetworkPolicy k개 + ResourceQuota k개 (총 1+2k 리소스)
"""
import os
import sys
from typing import List

EXPERIMENT_NS_PREFIX = "exp1"
MANIFESTS_DIR = os.path.join(os.path.dirname(__file__), "manifests")


def ns_name(complexity: int, run_id: str = "") -> str:
    suffix = f"-run{run_id}" if run_id else ""
    return f"{EXPERIMENT_NS_PREFIX}-c{complexity}{suffix}"


def namespace_yaml(complexity: int, run_id: str = "") -> str:
    name = ns_name(complexity, run_id)
    return f"""apiVersion: v1
kind: Namespace
metadata:
  name: {name}
  labels:
    tenant: exp1
    complexity: "{complexity}"
"""


def network_policy_yaml(complexity: int, index: int, run_id: str = "") -> str:
    name = ns_name(complexity, run_id)
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


def resource_quota_yaml(complexity: int, index: int, run_id: str = "") -> str:
    name = ns_name(complexity, run_id)
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


def generate_for_complexity(complexity: int, run_id: str = "", out_dir: str = "") -> List[str]:
    """복잡도 k에 대해 매니페스트 파일 경로 리스트 반환 (적용 순서 유지)."""
    base = out_dir or os.path.join(MANIFESTS_DIR, f"c{complexity}")
    if run_id:
        base = os.path.join(base, f"run_{run_id}")
    os.makedirs(base, exist_ok=True)
    paths = []

    # 1. Namespace (반드시 먼저)
    ns_path = os.path.join(base, "00-namespace.yaml")
    with open(ns_path, "w") as f:
        f.write(namespace_yaml(complexity, run_id))
    paths.append(ns_path)

    # 2. NetworkPolicies (namespace 생성 후)
    for i in range(1, complexity + 1):
        p = os.path.join(base, f"10-networkpolicy-{i}.yaml")
        with open(p, "w") as f:
            f.write(network_policy_yaml(complexity, i, run_id))
        paths.append(p)

    # 3. ResourceQuotas
    for i in range(1, complexity + 1):
        p = os.path.join(base, f"20-resourcequota-{i}.yaml")
        with open(p, "w") as f:
            f.write(resource_quota_yaml(complexity, i, run_id))
        paths.append(p)

    return paths


def total_resources(complexity: int) -> int:
    return 1 + 2 * complexity  # namespace + k netpols + k rqs


def main():
    os.makedirs(MANIFESTS_DIR, exist_ok=True)
    for c in range(1, 11):
        generate_for_complexity(c)
        print(f"Complexity {c}: {total_resources(c)} resources -> {MANIFESTS_DIR}/c{c}/")
    print("Done. Use apply_manual_baseline.sh and run_experiment.py next.")


if __name__ == "__main__":
    main()
