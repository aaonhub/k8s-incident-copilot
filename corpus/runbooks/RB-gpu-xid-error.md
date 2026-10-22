<!-- SYNTHETIC runbook. id: RB-gpu-xid-error category: gpu_xid_error -->
### Runbook: GPU Xid Error (`gpu_xid_error`)

#### Symptoms
* GPU workloads fail immediately or remain stuck in `Pending`.
* `kubectl describe node <node>` reports fewer allocatable GPUs than expected.
* Node `dmesg` logs contain NVIDIA Xid error codes (e.g., `NVRM: Xid (PCI:...): 79, GPU has fallen off the bus`).

#### Checks
* `kubectl get nodes -l nvidia.com/gpu.present` — Check if nodes with missing GPUs are still marked as ready.
* `kubectl describe node <node> | grep -A 5 "Allocatable:"` — Verify the current allocatable GPU capacity of the suspect node.
* `kubectl get pods -A --field-selector=status.phase=Pending` — Identify workloads blocked by the faulty node.
* `ssh <node> "dmesg -T | grep -i xid"` — Inspect kernel logs for specific NVIDIA Xid error codes and timestamps.
* `ssh <node> "nvidia-smi"` — Check if the NVIDIA driver can still communicate with all installed physical GPUs.

#### Likely Causes
* Hardware failure (e.g., GPU overheated, PCIe bus error, power fault triggering Xid 79/31).
* Corrupted NVIDIA driver state preventing proper device initialization.
* Kubernetes NVIDIA device plugin marking the faulted GPU unhealthy and taint-isolating it.

#### Remediation
1. Cordon the affected node: `kubectl cordon <node>`
2. Drain existing workloads gracefully: `kubectl drain <node> --ignore-daemonsets --delete-emptydir-data`
3. SSH into the node and attempt a GPU driver/device reset (if supported): `nvidia-smi --gpu-reset` (or reboot the node).
4. If `nvidia-smi` fails to detect the GPU post-reboot, open a hardware RMA ticket for physical replacement.

#### When to Escalate
Escalate to Hardware Operations if `nvidia-smi` cannot detect the GPU after a full node reboot, or to the Platform Engineering team if Xid errors recur across multiple nodes within the same rack.
