<!-- SYNTHETIC runbook. id: RB-nccl-timeout category: nccl_timeout -->
# Runbook: `nccl_timeout`

## Symptoms
- Multi-node distributed training job hangs during collective operations (e.g., `AllReduce`).
- Job fails with `NCCL WARN` or `watchdog timeout` errors in application logs.
- GPU utilization drops to 0% across participating nodes prior to failure.

## Checks
- `kubectl get pods -A | grep CrashLoopBackOff` — Look for restarting training pods.
- `kubectl logs <training-pod> --tail=100` — Verify exact NCCL timeout and rank failure messages.
- `kubectl describe node <node>` — Check for Kernel OOMs, hardware events, or NotReady status.
- `kubectl get events --sort-by='.metadata.creationTimestamp'` — Identify recent node disruptions or network flaps.

## Likely causes
- Flapping InfiniBand (IB) or RoCE network link causing packet drops during heavy PCIe/RDMA transfers.
- A single degraded GPU or malfunctioning NVSwitch/NIC on one worker node stalling collective communication.

## Remediation
1. Isolate the faulty node by running `nccl-tests` across the cluster to pinpoint hardware degradation.
2. Cordon and drain the offending node: `kubectl cordon <bad-node>` then `kubectl drain <bad-node> --ignore-daemonsets`.
3. Notify the user to restart the training job from the last valid model checkpoint on the healthy node pool.
4. File an infrastructure ticket for physical NIC/IB cable replacement on the isolated node.

## When to Escalate
Escalate to the Hardware/Networking team if `nccl-tests` fail across multiple random nodes, or if the same node fails repeatedly after a full power cycle and GPU firmware reset.
