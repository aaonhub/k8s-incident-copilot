<!-- SYNTHETIC runbook. id: RB-readiness-probe-failure category: readiness_probe_failure -->
# Runbook: Readiness Probe Failure (`readiness_probe_failure`)

## Symptoms
- Pods remain in `Running` state indefinitely without transitioning to `Ready`.
- Deployments/rollouts stall and endpoints disappear from the Service.
- Application logs show healthy behavior, but traffic is not routed to the pods.

## Checks
```bash
# Check pod status and restart counts (look for failing readiness/liveness probes)
kubectl describe pod <pod-name> -n <namespace>

# View recent container logs to verify if the app is listening on the expected port
kubectl logs <pod-name> -n <namespace> --tail=50

# Inspect the Service endpoints to confirm pods are missing from rotation
kubectl get endpoints <service-name> -n <namespace>

# Verify readiness probe configuration (port, path, scheme) in the deployment spec
kubectl get deployment <deployment-name> -n <namespace> -o yaml
```

## Likely Causes
- The readiness probe is configured with an incorrect HTTP path, incorrect port, or wrong protocol.
- The probe endpoint requires authentication or heavy resource initialization that times out, especially under GPU workload startup constraints.

## Remediation
1. **Immediate mitigation:** Roll back the deployment to the last known working revision:
   ```bash
   kubectl rollout undo deployment/<deployment-name> -n <namespace>
   ```
2. **Permanent fix:** Update the Deployment YAML to correct the `readinessProbe` path/port or increase `initialDelaySeconds`/`failureThreshold`, then apply:
   ```bash
   kubectl apply -f corrected-deployment.yaml
   ```

## When to Escalate
Escalate to the Application Engineering or AI/ML Model Team if the probe configuration is correct and the application itself is failing internal startup checks due to GPU initialization or dependency failures.
