<!-- SYNTHETIC runbook. id: RB-oom-killed category: oom_killed -->
### Runbook: Kubernetes Container OOM-Killed (Exit Code 137)

#### Symptoms
* Pod containers repeatedly restart with exit code `137`.
* Grafana/Prometheus memory utilization graphs flatline at the container limit right before the crash.
* Particularly common on GPU nodes following recent batch-size or model-cache updates.

#### Checks
* `kubectl get pods -A | grep CrashLoopBackOff` — Identify affected workloads and restart counts.
* `kubectl describe pod <pod-name> -n <namespace>` — Look for `OOMKilled: true` and `Exit Code: 137` in the last state.
* `kubectl logs <pod-name> -n <namespace> --previous` — Verify if an application-level out-of-memory error preceded the termination.
* `kubectl top pod <pod-name> -n <namespace>` — Check real-time memory consumption against allocated limits.
* `dmesg -T | grep -i oom` *(via node shell)* — Confirm Linux kernel OOM killer actions and target process IDs.

#### Likely Causes
* Container memory limit is set too low for current GPU workload requirements.
* Recent deployment introduced a larger batch size, higher precision weights, or aggressive caching.
* Memory leak in the application or CUDA driver interaction.

#### Remediation
1. **Immediate:** Edit the deployment manifest to increase the container memory limit.
2. **Rollback:** If this follows a recent release, revert the configuration change (e.g., lower batch size or cache capacity).
3. Apply changes: `kubectl apply -f <fixed-manifest.yaml>` and verify pod stability.

#### When to Escalate
* Escalate to the **AI/ML Platform Team** if memory limits exceed node capacity or trigger multi-tenant resource contention.
* Escalate to **Infrastructure Engineering** if OOM events persist despite correct limits, indicating potential GPU driver or kernel memory leaks.
