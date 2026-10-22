<!-- SYNTHETIC runbook. id: RB-image-pull-error category: image_pull_error -->
# Runbook: `image_pull_error` (GPU Cloud)

### Symptoms
* Newly deployed GPU workloads stuck in `ErrImagePull` or `ImagePullBackOff`.
* Pod startup latency alerts firing for recent deployments.

### Checks
* `kubectl get pods -n <namespace> -o wide` — Identify failing pods and affected GPU nodes.
* `kubectl describe pod <pod-name> -n <namespace>` — Look for `NotFound`, `Manifest unknown`, or `AccessDenied` in events.
* `kubectl get events --sort-by='.metadata.creationTimestamp' -n <namespace>` — Scan for repeated registry authentication or pull failure loops.

### Likely causes
* Typo in the image tag or repository path within the workload manifest.
* CI/CD pipeline failed to push the newly built GPU container image to the private registry.
* Expired or misconfigured image pull secrets in the target namespace.

### Remediation
1. **Roll back immediately:** Revert the deployment to the last known working revision:
   `kubectl rollout undo deployment/<deployment-name> -n <namespace>`
2. **Verify recovery:** Confirm pods transition to `Running` and GPU allocation succeeds:
   `kubectl get pods -n <namespace> -w`
3. **Fix root cause:** Notify the engineering team to correct the image tag or investigate the CI/CD build failure.

### When to escalate
Escalate to the Core Infrastructure / Registry team if the failure affects multiple namespaces, persists after rollbacks, or stems from a registry-wide outage rather than a bad deployment tag.
