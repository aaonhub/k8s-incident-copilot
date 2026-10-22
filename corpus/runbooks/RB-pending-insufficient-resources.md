<!-- SYNTHETIC runbook. id: RB-pending-insufficient-resources category: pending_insufficient_resources -->
# Runbook: `pending_insufficient_resources`

## Symptoms
* Pods remain stuck in `Pending` state indefinitely.
* Events show `FailedScheduling: Insufficient cpu / memory / nvidia.com/gpu`.

## Checks
```bash
kubectl describe pod <pod-name> -n <namespace> # Look for failed scheduling events and exact resource requests.
kubectl get nodes -o custom-columns=NAME:.metadata.name,TAINTS:.spec.taints # Verify node taints and availability.
kubectl top nodes # Check cluster-wide CPU and memory utilization.
kubectl describe nodes | grep -A 5 "Allocated resources" # Inspect total vs. requested GPU/CPU/Memory per node.
kubectl get pods --all-namespaces --field-selector=status.phase=Pending # Assess cluster-wide pending pod pressure.
```

## Likely Causes
* Resource requests exceed available node capacity across the entire cluster.
* Unmatched node selectors, affinity rules, or missing tolerations preventing placement.
* GPU fragmentation where total free GPUs exist, but no single node has enough contiguous capacity for the pod.

## Remediation
1. **Right-size:** Adjust the workload's CPU, memory, or GPU requests downward to match node availability.
2. **Scale Capacity:** Provision additional GPU/CPU worker nodes via the autoscaler if demand is legitimate.
3. **Correct Constraints:** Fix invalid node selectors, anti-affinity rules, or missing taints/tolerations.

## When to Escalate
* If auto-scaling fails to provision new nodes within 15 minutes due to cloud quota limits or underlying infrastructure faults.
* If critical production workloads remain blocked and no spare capacity can be safely reclaimed.
