<!-- SYNTHETIC runbook. id: RB-network-policy-block category: network_policy_block -->
# Runbook: NetworkPolicy Block (GPU Cloud)

## Symptoms
- Inter-service gRPC/HTTP timeouts between GPU workloads.
- DNS resolution succeeds (`nslookup`/`dig` work), but TCP/UDP connection attempts hang or fail with connection refused.
- Spike in error rates following a recent security policy or GitOps deployment.

## Checks
- `kubectl get networkpolicy -n <namespace>` — Check for recently added or modified network policies.
- `kubectl describe networkpolicy <policy-name> -n <namespace>` — Look for overly restrictive ingress/egress selectors.
- `kubectl get pods -n <namespace> -o wide` — Verify source and destination pod labels match policy selectors.
- `kubectl run tmp-netcheck --rm -it --image=nicolaka/netshoot -- namespace=<ns> -- nc -zv <dest-ip> <port>` — Test raw TCP connectivity bypassing application logic.
- `kubectl get events -n <namespace> --sort-by='.metadata.creationTimestamp'` — Check for CNI-related enforcement errors.

## Likely Causes
- Default-deny `NetworkPolicy` applied to the namespace without an explicit allow rule for inter-service communication.
- Pod label mismatch in `podSelector` or `namespaceSelector` after a refactor.
- Missing egress rule preventing pods from reaching backend databases or distributed training coordinators.

## Remediation
1. Identify the blocking policy via checks.
2. Draft an explicit `NetworkPolicy` ingress/egress rule allowing traffic between the specific source and destination app labels on the required port.
3. Apply the fix: `kubectl apply -f fixed-networkpolicy.yaml`.
4. Re-run connectivity checks (`nc -zv`) to confirm traffic flows.

## When to Escalate
Escalate to the Security or Cluster Networking team if the traffic drop is caused by a mandatory organization-wide compliance policy that cannot be safely overridden, or if CNI plugins (e.g., Calico/Cilium) are unresponsive.
