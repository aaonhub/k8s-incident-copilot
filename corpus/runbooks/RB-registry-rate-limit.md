<!-- SYNTHETIC runbook. id: RB-registry-rate-limit category: registry_rate_limit -->
# Runbook: registry_rate_limit

## Symptoms
- Pods stuck in `ImagePullBackOff` or `ErrImagePull`.
- Event logs show HTTP `429 Too Many Requests` or `toomanyrequests: Rate exceeded`.
- Failures occur intermittently across multiple GPU nodes, typically during large-scale job launches.

## Checks
- `kubectl get pods -A | grep -E "ImagePull|ErrImage"` - Identify affected workloads and namespaces.
- `kubectl describe pod <pod-name> -n <namespace>` - Confirm the exact 429 error message from the container runtime.
- `kubectl get nodes -o wide` - Verify if failures concentrate on specific nodes sharing a public IP.
- `crictl pods` / `crictl ps` (via SSH/node debug) - Check local container runtime logs for registry authentication status.

## Likely Causes
- Workloads using anonymous image pulls hitting public container registry (e.g., Docker Hub) rate limits.
- Missing or expired `imagePullSecrets` in pod/deployment specifications.
- Cluster pull-through cache/mirror misconfigured or offline.

## Remediation
1. Attach valid credentials via `imagePullSecrets` to the affected deployments.
2. Configure or verify the node-level container runtime image mirror/pull-through cache to bypass public limits.
3. If using Docker Hub, switch base image pulls to an authenticated internal artifact registry.

## When to Escalate
Escalate to the Platform/Infrastructure team if the pull-through cache infrastructure is down, or if authenticated pulls continue to receive 429 errors due to exhausted organization-tier rate limits.
