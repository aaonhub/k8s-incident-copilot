<!-- SYNTHETIC runbook. id: RB-dependency-timeout category: dependency_timeout -->
# Runbook: Dependency Timeout (504 Gateway Timeout)

## Symptoms
* HTTP 504 errors returned to API clients.
* Service logs show `upstream request timeout` or connection pool exhaustion.
* Kubernetes pods for the affected service report `Running` and `Ready` with healthy probes.

## Checks
* `kubectl get pods -l app=<service> -o wide` — Verify pod status, restart counts, and node distribution.
* `kubectl logs -l app=<service> --tail=100 --prefix` — Look for recurring timeout patterns or slow downstream calls.
* `kubectl top pods -l app=<service>` — Check if CPU/memory saturation is causing internal processing delays.
* `kubectl get endpoints <downstream-service>` — Confirm endpoints are populated and routing traffic correctly.

## Likely causes
* A recent deployment on a downstream dependency introduced latency or blocking behavior.
* Downstream GPU resource starvation or node degradation causing request queuing.
* Insufficient upstream timeout configuration or missing circuit breakers under load.

## Remediation
1. **Rollback:** Revert the recent deployment of the slow downstream dependency (`kubectl rollout undo deployment/<downstream>`).
2. **Mitigation:** If rollback isn't immediate, temporarily increase upstream timeout thresholds or shed non-critical traffic.
3. **Resilience:** Tune circuit breaker thresholds to fail fast rather than hanging threads.

## When to Escalate
* Escalate to the downstream service owner if the rollback fails or root cause is unclear within 15 minutes.
* Escalate to Infrastructure/SRE if GPU node degradation or networking fabric issues are suspected.
