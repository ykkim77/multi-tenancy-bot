# KCU Portal — Agentic Operator

The **Agentic Operator** is a Kubernetes controller that turns a single
`ChatSpace` Custom Resource into a fully-isolated tenant — and then
keeps watching the cluster to autonomously redistribute resources between
busy and idle tenants.

It is the "Operator" benchmark target referenced by the experiments under
`experiments/exp1`, `exp2`, `exp3`. Previously those experiments compared
a manual `kubectl apply` baseline against a Python-simulated "operator
behavior"; with this controller, the **agentic** path actually runs in
the cluster as a Go reconciler.

## What's "agentic" about it?

The autonomy lives in two places:

1. **`controllers/agentic.go`** — every Reconcile invocation lists all
   peer ChatSpaces and decides, on its own, whether to:
   - shrink an idle tenant's quota to 30 % (reclamation), or
   - boost an active tenant's quota by `activeBoostFactor` × headroom.
2. **Periodic requeue** — the controller re-evaluates each ChatSpace at
   `--rebalance-interval` (default 30s), so decisions converge over time
   without any external trigger.

Each decision is stamped into `status.agenticActions` so the auditor can
trace what the Operator did and why:

```yaml
status:
  phase: Ready
  appliedQuota:
    cpuLimits: 6000m       # boosted from base 4000m
    memoryLimits: ...
  agenticActions:
  - "agentic: boosted active tenant quota ×1.42 (reclaimed from 6 idle peer(s))"
  - "agentic: reclaimed 70% of quota (idle tenant; cluster has 4 active / 6 idle peers)"
```

## What gets provisioned per tenant

| Resource              | Purpose                                                  |
|-----------------------|----------------------------------------------------------|
| `Namespace`           | Tenant isolation boundary, labelled with tier            |
| `ResourceQuota`       | Hard CPU/Memory/Pod cap, dynamically adjusted            |
| `LimitRange`          | Per-Container default + max so unlabeled Pods are sane   |
| `NetworkPolicy`       | Cross-tenant deny when `agentic.hardIsolation = true`    |
| `PriorityClass` (×3)  | Cluster-shared: `priority`, `standard`, `internal`       |

## Files

```
operator/
├── api/v1/chatspace_types.go         # CRD types: Spec, Status, AgenticPolicy
├── api/v1/zz_generated.deepcopy.go   # Hand-maintained deepcopy methods
├── controllers/
│   ├── chatspace_controller.go       # Reconcile loop + Watches
│   ├── provisioner.go                # NS/Quota/LimitRange/NetPol/PriorityClass
│   ├── agentic.go                    # Re-balancing decision logic
│   └── defaults.go                   # Tier-based default quotas
├── config/
│   ├── crd/bases/portal.kcu.ac.kr_chatspaces.yaml
│   ├── rbac/role.yaml                # ClusterRole `manager-role`
│   ├── manager/manager.yaml          # Namespace + SA + Binding + Deployment
│   └── default/kustomization.yaml    # `kustomize build .. | kubectl apply -f -`
├── main.go                           # Manager entrypoint (with --rebalance-interval)
└── Dockerfile / Makefile
```

## Quickstart

```bash
# 1. Build the manager binary (sanity check)
cd operator
go build ./...

# 2. Build a container image
make docker-build IMG=kcu/portal-operator:local

# 3. Load image into your kind/minikube cluster, then deploy:
make deploy

# 4. Create a tenant via CRD:
cat <<EOF | kubectl apply -f -
apiVersion: portal.kcu.ac.kr/v1
kind: ChatSpace
metadata:
  name: cs-cs-dept
spec:
  tenantId: cs-dept
  tier: priority
  agentic:
    enabled: true
    hardIsolation: true
EOF

# 5. Watch the Operator do its job:
kubectl get chatspaces -w
kubectl get ns | grep tenant-cs-dept
kubectl get resourcequota,limitrange,networkpolicy -n tenant-cs-dept
kubectl get chatspace cs-cs-dept -o jsonpath='{.status.agenticActions}'
```

## Hooking the experiments

The Python helper in `experiments/common/agentic_operator.py` builds
`ChatSpace` YAML and waits for `status.phase=Ready`. A typical experiment
loop now looks like:

```python
from common.agentic_operator import (
    specs_for_density_experiment,
    provision_via_operator,
    wait_for_ready,
    cleanup_all_chatspaces,
)

specs = specs_for_density_experiment(50)
apply_latency = provision_via_operator(specs)            # one kubectl apply
ready_latency = wait_for_ready(specs, timeout=120)       # observed by Operator
cleanup_all_chatspaces()                                 # cascade-deletes namespaces
```

The "Manual Baseline" path in the same experiments still calls
`kubectl apply` per resource (Namespace, ResourceQuota, …) directly —
giving the head-to-head comparison its meaning.
