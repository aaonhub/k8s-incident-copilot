<!-- SYNTHETIC runbook. id: RB-resource-quota-exceeded category: resource_quota_exceeded -->
# Runbook: resource_quota_exceeded

## Symptoms
Deployments fail to scale; new ReplicaSets create zero pods. `kubectl get events` shows `FailedCreate` with the message `exceeded quota`.

## Checks
* `kubectl get events --sort-by='.metadata.creationTimestamp'` — Look for `FailedCreate` warnings mentioning specific quota limits.
* `kubectl describe quota -n <namespace>` — Look for hard limits that match current usage (e.g., `requests.nvidia.com/gpu`, `requests.cpu`, `requests.memory`).
* `kubectl top pods -n <namespace>` — Compare actual usage against allocated resource requests.
* `kubectl get deployment <name> -o yaml` — Check the container-level `resources.requests` definitions for inflated values.

## Likely causes
* Namespace `ResourceQuota` limits have been reached due to surging tenant workloads or oversized GPU/CPU/memory requests.
* Default limit ranges are automatically injecting higher-than-expected resource requests into incoming pods.

## Remediation
1. Coordinate with the tenant to reduce `resources.requests` on idle or over-provisioned pods.
2. If justified, submit a quota increase request to platform engineering to raise the namespace limits.
3. Delete orphaned or zombie pods/jobs consuming quota in the target namespace.

## When to escalate
Escalate to the Platform/Capacity Engineering team if the tenant requires a permanent quota increase beyond standard tier limits, or if quota controller sync failures are suspected.
