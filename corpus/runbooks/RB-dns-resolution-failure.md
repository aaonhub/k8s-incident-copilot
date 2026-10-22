<!-- SYNTHETIC runbook. id: RB-dns-resolution-failure category: dns_resolution_failure -->
# Runbook: DNS Resolution Failure (`dns_resolution_failure`)

## Symptoms
* Intermittent `no such host` or `i/o timeout` errors resolving cluster internal services.
* Application pods failing health checks due to unreachable dependencies.

## Checks
* `kubectl get pods -n kube-system -l k8s-app=kube-dns` — Look for CrashLoopBackOff, high restarts, or OOMKilled states.
* `kubectl top pods -n kube-system -l k8s-app=kube-dns` — Check if CPU or memory utilization is pegged at limits.
* `kubectl logs -n kube-system -l k8s-app=kube-dns --tail=100` — Scan for error logs, timeout messages, or plugin failures.
* `kubectl get endpoints kube-dns -n kube-system` — Verify active endpoints exist and match running CoreDNS pods.

## Likely Causes
* CoreDNS pods are resource-starved (OOM/CPU throttling) under high query load.
* Excessive DNS query storms triggered by default `ndots: 5` configurations in pod resolv.conf.
* Node-level packet drops or network congestion affecting UDP/TCP traffic to the cluster DNS IP.

## Remediation
1. Scale up CoreDNS replicas: `kubectl scale deployment coredns -n kube-system --replicas=<N>`.
2. Increase resource limits for the CoreDNS deployment if pods are OOMKilled or throttled.
3. Deploy or verify NodeLocal DNSCache DaemonSet to cache queries locally and bypass iptables bottlenecks.
4. Mitigate lookup storms by setting `dnsConfig.options` with a lower `ndots` (e.g., `2`) in problematic workload manifests.

## When to Escalate
Escalate to the Core Infrastructure/Networking team if DNS failures persist after scaling CoreDNS and enabling NodeLocal DNSCache, or if underlying CNI/node networking degradation is suspected.
