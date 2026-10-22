<!-- SYNTHETIC runbook. id: RB-pvc-mount-failure category: pvc_mount_failure -->
# Runbook: PVC Mount Failure (`pvc_mount_failure`)

## Symptoms
* Pods stuck in `ContainerCreating`.
* Events show `FailedMount`, `FailedAttachVolume`, or `Multi-Attach error for volume`.

## Checks
* `kubectl get pods -A | grep ContainerCreating` — Identify affected pods and namespaces.
* `kubectl describe pod <pod-name> -n <namespace>` — Look for specific CSI attach/mount error messages.
* `kubectl get pvc <pvc-name> -n <namespace>` — Check if the PVC status is `Bound`.
* `kubectl get volumeattachment` — Verify if the underlying volume is stuck attached to a dead/draining node.
* `kubectl get pods -n kube-system -l app=gpu-csi-controller` — Check if the GPU CSI controller pods are running and healthy.

## Likely Causes
* ReadWriteOnce (RWO) volume is zombie-attached to a node that failed or is slow to terminate.
* CSI controller plugin is crashed, deadlocked, or failing to communicate with storage backend.

## Remediation
1. **Orphaned Attachment:** Force-delete the stuck `VolumeAttachment` object (`kubectl delete volumeattachment <id>`) to release the lock, allowing the new node to attach.
2. **CSI Driver Failure:** Restart the CSI controller: `kubectl rollout restart deployment gpu-csi-controller -n kube-system`.
3. **Node Isolation:** If a node is unresponsive and holding locks, cordoning and force-deleting the old pod with `kubectl delete pod --grace-period=0 --force` may be required.

## When to Escalate
* Volume remains locked after force-deleting the `VolumeAttachment`.
* Storage backend control plane API is returning 5xx errors.
* Escalate to the **Storage/Infra Team** if unresolved after 15 minutes.
