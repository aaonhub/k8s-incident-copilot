<!-- SYNTHETIC runbook. id: RB-config-regression category: config_regression -->
# Runbook: Config Regression (GPU Service)

## Symptoms
- HTTP 500/5xx errors returned to callers immediately following a configuration update.
- Application pods pass Kubernetes liveness and readiness probes, but business-logic requests fail.
- Error logs indicate connection timeouts, DNS resolution failures, or invalid feature flag states.

## Checks
* `kubectl get pods -l app=gpu-service` — Verify pods are running and restart counts are not incrementing.
* `kubectl logs deployment/gpu-service --tail=100` — Look for initialization errors, bad URLs, or rejected connections.
* `kubectl describe deployment/gpu-service` — Inspect mounted ConfigMaps, environment variables, and recent pod spec updates.
* `kubectl get configmap gpu-service-config -o yaml` — Review the exact key-value pairs applied in the latest deployment.
* `kubectl get events --sort-by='.metadata.creationTimestamp'` — Check for warnings related to config mounting or container startup.

## Likely Causes
- Typo in an upstream dependency hostname, URL, or port number within the configuration.
- Incorrectly toggled feature flag causing downstream integration failures.
- Malformed JSON/YAML syntax parsed as a valid fallback or empty value by the application.

## Remediation
1. Immediately roll back the faulty configuration change via GitOps or `kubectl rollout undo deployment/gpu-service`.
2. Verify pod recovery and error rate reduction via monitoring dashboards.
3. Patch the configuration locally, add strict startup schema validation, and open a ticket to prevent recurrence.

## When to Escalate
- Rollback fails to restore service functionality within 10 minutes.
- The root cause requires a data migration or external infrastructure adjustment.
