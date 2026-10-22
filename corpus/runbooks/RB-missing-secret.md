<!-- SYNTHETIC runbook. id: RB-missing-secret category: missing_secret -->
# Runbook: `missing_secret` (GPU Cluster)

### Symptoms
New pods stuck in `CreateContainerConfigError` or `CreateContainerError` after a deploy/restart. Existing pods continue serving traffic successfully.

### Checks
*   `kubectl get pods -A | grep CreateContainer`
    *   *Look for:* Pods failing to initialize in target namespaces.
*   `kubectl describe pod <pod-name> -n <namespace>`
    *   *Look for:* Events citing `secret "<secret-name>" not found`.
*   `kubectl get secret <secret-name> -n <namespace>`
    *   *Look for:* Non-existence of the resource or missing keys referenced in the pod spec.
*   `kubectl get events --sort-by='.metadata.creationTimestamp' -n <namespace>`
    *   *Look for:* Recent deletion or modification timestamps matching the failure timeline.

### Likely Causes
A required Kubernetes Secret (or specific data key) referenced by the pod configuration was accidentally deleted, renamed, or corrupted—commonly caused by automated credential rotation jobs or manual operator error.

### Remediation
1. Identify the missing secret name from the pod description.
2. Coordinate with the security/ops team to restore the Secret via external Secret Manager or re-apply the baseline manifest.
3. Verify the secret exists: `kubectl get secret <secret-name> -n <namespace>`.
4. Force pod reconciliation if necessary: `kubectl delete pod <pod-name> -n <namespace>`.
5. Confirm new pods transition to `Running` and GPU workloads initialize: `kubectl get pods -w`.

### When to Escalate
Escalate to the **Security/IAM Team** if the automated secret rotation pipeline is broken or if access permissions prevent manual recreation. Escalate to the **Platform Lead** if restoring the secret does not resolve container creation within 10 minutes.
