<!-- SYNTHETIC runbook. id: RB-cpu-throttling category: cpu_throttling -->
# Runbook: CPU Throttling (CFS)

## Symptoms
* API latency spikes and liveness/readiness probes time out intermittently.
* Application logs show zero errors; `kubectl top` shows flat, seemingly normal CPU usage.

## Checks
```bash
# Check pod restart counts and current state
kubectl get pods -A | grep -v Running

# Inspect container limits and requests
kubectl get pod <pod> -n <namespace> -o jsonpath='{range .spec.containers[*]}{.name}{"\tLimits: "}{.resources.limits}{"\n"}{end}'

# Check node-level or cgroup throttling metrics (requires metrics-server/Prometheus)
kubectl top pod <pod> -n <namespace> --containers

# Verify CFS throttling via node shell (look for high nr_throttled vs nr_periods)
cat /sys/fs/cgroup/kubepods.slice/.../cpu.stat
```
*Look for:* Containers hitting `limits.cpu` ceilings while `nr_throttled` rapidly increases.

## Likely Causes
* Recently deployed CPU limits are too restrictive for peak GPU workload orchestration/preprocessing threads.
* Default namespace LimitRanges enforcing aggressive CPU caps.

## Remediation
1. Edit the deployment/pod spec to increase or completely remove `resources.limits.cpu` (GPU nodes rely on memory/GPU limits, not strict CPU throttling).
2. If recent, `kubectl rollout undo deployment/<name>` to revert the offending change.
3. Apply changes: `kubectl apply -f updated-manifest.yaml`.

## When to Escalate
* Throttling persists after removing CPU limits (indicates node-level CPU starvation or thermal capping).
* Node kernel/cgroup corruption is suspected. 
* Escalate to the **GPU Cluster Infrastructure Team** if node-level CPU contention impacts multiple tenants.
