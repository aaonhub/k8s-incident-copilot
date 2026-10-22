<!-- SYNTHETIC runbook. id: RB-node-not-ready category: node_not_ready -->
### Runbook: Node Not Ready (GPU Cluster)

#### Symptoms
* Node status shows `NotReady` in cluster output.
* Pods residing on the node transition to `Unknown` or get stuck in `Terminating`.
* Slow rescheduling of workloads due to default pod eviction timeouts.

#### Checks
* `kubectl get nodes -o wide` — Look for nodes with `NotReady` status and high pod counts.
* `kubectl describe node <node-name>` — Check conditions (`KubeletReady`, `MemoryPressure`) and recent events.
* `kubectl get pods -A --field-selector spec.nodeName=<node-name>` — Identify affected GPU workloads.
* `ssh <node-ip> systemctl status kubelet` — Verify if the kubelet service is active or crashed.
* `ssh <node-ip> dmesg -T | grep -iE 'oom|gpu|nvrm'` — Check for Out-Of-Memory kills or hardware/driver faults.

#### Likely Causes
* Kubelet crash-looping due to memory exhaustion or corrupted state.
* Network partition isolating the node from the control plane.
* GPU driver crash or kernel panic freezing node communication.

#### Remediation
1. Attempt a safe drain: `kubectl drain <node-name> --ignore-daemonsets --delete-emptydir-data --force`.
2. If unreachable, force-delete stuck pods: `kubectl delete pod <pod-name> --grace-period=0 --force`.
3. Restart kubelet: `sudo systemctl restart kubelet`.
4. If unresponsive, issue a hard reboot via the cloud provider hypervisor/IPMI.
5. Re-image or replace the node if hardware/driver corruption persists.

#### When to Escalate
* Hardware or persistent GPU/NVMe failures requiring physical RMA.
* Widespread `NotReady` nodes indicating control plane or network infrastructure degradation.
