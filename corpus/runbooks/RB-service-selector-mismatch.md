<!-- SYNTHETIC runbook. id: RB-service-selector-mismatch category: service_selector_mismatch -->
# Runbook: service_selector_mismatch

## Symptoms
Clients receive `connection refused` or `503 Service Unavailable` when calling a Kubernetes Service, despite target GPU pods showing `Running` and `Ready`.

## Checks
* `kubectl get endpoints <service-name>` — Check if the endpoints list is empty (`<none>`).
* `kubectl get svc <service-name> -o yaml` — Inspect the `spec.selector` block for typos or outdated keys.
* `kubectl get pods --show-labels` — Verify the actual labels assigned to the target GPU pods.

## Likely causes
A Service selector key or value does not match the target pods' labels due to a recent typo, manual update, or label refactoring (e.g., `app.kubernetes.io/name` vs `app`).

## Remediation
1. Edit the Service manifest or live resource:
   `kubectl edit svc <service-name>`
2. Update `spec.selector` to match the exact key-value pairs from the running pods.
3. Confirm endpoints populate successfully:
   `kubectl get endpoints <service-name>`
4. Test connectivity from a debug pod to verify the 503/connection refused errors are resolved.

## When to escalate
Escalate to the owning application or platform engineering team if updating the selector does not resolve the routing issue, or if pod admission webhooks are actively reverting the corrected labels.
