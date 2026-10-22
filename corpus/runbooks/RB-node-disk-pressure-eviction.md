<!-- SYNTHETIC runbook. id: RB-node-disk-pressure-eviction category: node_disk_pressure_eviction -->
# Runbook: Node Disk Pressure Eviction (`node_disk_pressure_eviction`)

## Symptoms
- Multiple pods on a single GPU node transition to `Evicted` status.
- `kubectl describe node` shows `DiskPressure: True`.
- Application workloads experience sudden terminations and local data loss.

## Checks
- `kubectl get nodes` — Identify the node with `NotReady` or abnormal status.
- `kubectl describe node <NODE_NAME>` — Look for `DiskPressure` in the conditions list and check allocated resource percentages.
- `kubectl get pods -A --field-selector spec.nodeName=<NODE_NAME>` — Verify which pods were evicted due to local storage exhaustion.
- `ssh <NODE_IP> df -h` — Check root partition and `/var/lib/docker` or `/var/lib/containerd` disk usage.

## Likely Causes
- Accumulated unused container images consuming the node's root/containerd disk.
- Unrotated container log files or rogue workloads writing excessively to `emptyDir` volumes.
- Missing or insufficient pod `ephemeral-storage` requests and limits.

## Remediation
1. Cordon and drain the affected node if necessary: `kubectl cordon <NODE>`
2. Log into the node and prune unused container images/volumes: `crictl rmi --prune` or `docker system prune -f`.
3. Clean up stale logs in `/var/log/pods/`.
4. Uncordon the node once disk usage drops below the kubelet threshold: `kubectl uncordon <NODE>`.
5. Enforce strict `ephemeral-storage` resource limits on workloads running on this cluster.

## When to Escalate
Escalate to the Infrastructure/Storage team if disk usage immediately spikes post-cleanup, indicating hardware failure, unmanaged log runaway, or if the node requires physical storage expansion.
