<!-- SYNTHETIC runbook. id: RB-pdb-blocking-drain category: pdb_blocking_drain -->
# Runbook: `pdb_blocking_drain`

## Symptoms
Node drain or cluster upgrade hangs indefinitely. Kubernetes logs show eviction API calls repeatedly rejected for workload pods due to PodDisruptionBudget (PDB) constraints.

## Checks
* `kubectl get nodes` — Identify the node stuck in draining/SchedulingDisabled state.
* `kubectl get pods -A --field-selector spec.nodeName=<node>` — List remaining pods on the target GPU node.
* `kubectl get pdb -A` — Look for PDBs with `ALLOWED DISRUPTIONS: 0`.
* `kubectl describe pdb <pdb-name> -n <namespace>` — Verify if `minAvailable` matches or exceeds replica count.

## Likely causes
A PDB is misconfigured with `minAvailable` equal to the total replica count (or `maxUnavailable: 0`), preventing any pod eviction required for the node drain.

## Remediation
1. Temporarily scale up the affected deployment to satisfy the PDB during the drain:
   `kubectl scale deployment <name> --replicas=<current+1> -n <namespace>`
2. Alternatively, patch the PDB to allow one disruption:
   `kubectl patch pdb <name> -n <namespace> --type='json' -p='[{"op": "replace", "path": "/spec/minAvailable", "value": <new-value>}]'`
3. Retry the node drain, then restore the original PDB/replica settings once the node is successfully cordoned or replaced.

## When to escalate
Escalate to the Workload Owner (Service Tier 2) if scaling or modifying the PDB causes application degradation, or if the PDB blocks a critical security patch and the owner is unresponsive after 30 minutes.
