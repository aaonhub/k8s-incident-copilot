<!-- SYNTHETIC runbook. id: RB-tls-cert-expired category: tls_cert_expired -->
# Runbook: `tls_cert_expired`

## Symptoms
* Client applications suddenly fail with `x509: certificate has expired or is not yet valid`.
* Ingress controllers or API gateways log TLS handshake failures.
* Prometheus alerts fire for impending or active TLS certificate expiration.

## Checks
* `kubectl get certificates -A` - Look for `Ready=False` and examine the age/expiration columns.
* `kubectl describe certificate <cert-name> -n <namespace>` - Check events for ACME challenge failures or renewal errors.
* `kubectl get certificaterequests -A` - Look for pending requests that are failing validation.
* `kubectl get clusterissuer,issuer -A` - Verify the issuer status is `Ready=True` and API connectivity is healthy.
* `kubectl logs -n cert-manager -l app.kubernetes.io/name=cert-manager` - Scan for controller errors, rate limits, or DNS/HTTP01 solver failures.

## Likely causes
* Automated renewal failed due to broken DNS/HTTP01 validation challenges.
* The backing `Issuer` or `ClusterIssuer` misconfigured, or API credentials expired.
* Manual rotation missed a static secret mounted directly into workloads.
* `cert-manager` controller crash-looping or experiencing RBAC permission issues.

## Remediation
1. Fix the underlying issuer or validation block (e.g., DNS record, network policy blocking ACME).
2. Force immediate renewal by deleting the existing CertificateRequest: `kubectl delete certificaterequest <name> -n <namespace>`.
3. If using static secrets, generate a valid TLS certificate and update the Kubernetes secret manually: `kubectl create secret tls <name> --cert=... --key=... --dry-run=client -o yaml | kubectl apply -f -`.
4. Restart affected ingress controllers or pods to flush cached expired certs: `kubectl rollout restart deployment/<ingress-controller> -n <namespace>`.

## When to Escalate
* Escalate to **Security/PKI Team** if root/intermediate CA certificates are compromised or expired.
* Escalate to **Network Engineering** if external validation traffic is blocked by firewall rules preventing ACME challenges.
