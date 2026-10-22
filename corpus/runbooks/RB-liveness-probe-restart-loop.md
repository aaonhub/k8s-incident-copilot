<!-- SYNTHETIC runbook. id: RB-liveness-probe-restart-loop category: liveness_probe_restart_loop -->
# Runbook: liveness_probe_restart_loop

## Symptoms
Pods cycle through `CrashLoopBackOff` or restart every few minutes during startup or heavy load. `kubectl describe pod` events show repeated `Liveness probe failed` warnings.

## Checks
* `kubectl get pods -n <namespace>` -> Look for high `RESTARTS` count and `Running` vs `CrashLoopBackOff` states.
* `kubectl describe pod <pod-name> -n <namespace>` -> Look for "Liveness probe failed: HTTP probe failed..." in the Events section.
* `kubectl logs <pod-name> -n <namespace> --previous` -> Check if the application crashed or was prematurely terminated mid-initialization.
* `kubectl top pod <pod-name> -n <namespace>` -> Check if GPU/CPU/Memory resource starvation is slowing down the application response.

## Likely causes
The liveness probe timeout or period is too aggressive for the application's slow startup time or high GPU/compute load, causing the kubelet to falsely assume the container is dead and kill it.

## Remediation
1. Edit the Deployment/Pod spec to add a `startupProbe` with a higher `failureThreshold` and `periodSeconds` to cover slow initialization.
2. Increase `initialDelaySeconds`, `periodSeconds`, or `failureThreshold` on the existing liveness probe.
3. Apply changes: `kubectl apply -f <manifest>.yaml`.
4. Monitor stabilization: `kubectl rollout status deployment/<name> -n <namespace>`.

## When to escalate
Escalate to the Application Engineering team if pods continue failing after adjusting probe thresholds, or if logs indicate underlying out-of-memory (OOM) errors or unrecoverable GPU driver faults.
