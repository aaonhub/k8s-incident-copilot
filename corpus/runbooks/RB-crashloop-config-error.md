<!-- SYNTHETIC runbook. id: RB-crashloop-config-error category: crashloop_config_error -->
# Runbook: crashloop_config_error

## Symptoms
* Pods stuck in `CrashLoopBackOff` immediately following a configuration or environment variable update.
* Elevated application error rates or zero readiness probe successes for the affected deployment.

## Checks
* `kubectl get pods -n <namespace>` — Look for pods with high restart counts and a `CrashLoopBackOff` status.
* `kubectl logs <pod-name> -n <namespace> --previous --tail=50` — Inspect prior container output to identify the exact validation error, missing key, or exit code 1.
* `kubectl describe pod <pod-name> -n <namespace>` — Verify recently injected environment variables, ConfigMaps, or Secret references.
* `kubectl rollout history deployment/<deployment-name> -n <namespace>` — Check recent revision history to pinpoint the problematic update.

## Likely causes
* An invalid parameter, malformed JSON/YAML, or missing mandatory environment variable introduced in the latest update fails application startup validation.

## Remediation
1. Immediately revert the faulty configuration:
   `kubectl rollout undo deployment/<deployment-name> -n <namespace>`
2. Alternatively, patch the ConfigMap/Secret back to the last known working state.
3. Confirm pods stabilize and pass readiness checks:
   `kubectl rollout status deployment/<deployment-name> -n <namespace>`

## When to escalate
Escalate to the Application Engineering team if rolling back does not resolve the crash loop within 10 minutes, or if the startup failure persists despite valid configuration parameters.
