"""Read-only collection of cluster state into a point-in-time snapshot.

The agent never talks to the API server while it reasons. It works from one
consistent snapshot taken up front, which buys three things:
  * every investigation is replayable byte-for-byte (fixtures, CI, audits);
  * the model can't race a changing cluster mid-investigation;
  * the credentials used here are a read-only ServiceAccount (see rbac.yaml),
    so even a confused model has nothing to mutate with.
"""
from __future__ import annotations

import gzip
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

LOG_TAIL = 120


def _strip(obj: dict) -> dict:
    md = obj.get("metadata") or {}
    md.pop("managedFields", None)
    ann = md.get("annotations") or {}
    ann.pop("kubectl.kubernetes.io/last-applied-configuration", None)
    return obj


def collect_snapshot(namespace: str, kubeconfig: str | None = None) -> dict:
    """Pull everything an investigation may need, once. Read-only API calls only."""
    from kubernetes import client, config
    from kubernetes.client.rest import ApiException

    if kubeconfig is None and os.environ.get("KUBERNETES_SERVICE_HOST"):
        config.load_incluster_config()  # running as a pod: use its (read-only) ServiceAccount
    else:
        config.load_kube_config(config_file=kubeconfig)
    api = client.ApiClient()
    core, apps = client.CoreV1Api(api), client.AppsV1Api(api)
    disc, custom = client.DiscoveryV1Api(api), client.CustomObjectsApi(api)
    ser = api.sanitize_for_serialization
    t0 = time.time()

    def items(resp) -> list[dict]:
        return [_strip(ser(i)) for i in resp.items]

    snap: dict = {
        "namespace": namespace,
        "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "pods": items(core.list_namespaced_pod(namespace)),
        "events": items(core.list_namespaced_event(namespace)),
        "deployments": items(apps.list_namespaced_deployment(namespace)),
        "replicasets": items(apps.list_namespaced_replica_set(namespace)),
        "services": items(core.list_namespaced_service(namespace)),
        "endpointslices": items(disc.list_namespaced_endpoint_slice(namespace)),
        "logs": {},
        "pod_metrics": [],
        "app_metrics": {},
        "errors": [],
    }
    for pod in snap["pods"]:
        name = pod["metadata"]["name"]
        statuses = {s["name"]: s for s in (pod.get("status") or {}).get("containerStatuses") or []}
        for c in pod["spec"]["containers"]:
            cs = statuses.get(c["name"], {})
            for previous in (False, True):
                if previous and not cs.get("restartCount"):
                    continue
                try:
                    txt = core.read_namespaced_pod_log(
                        name, namespace, container=c["name"], tail_lines=LOG_TAIL, previous=previous
                    )
                except ApiException as e:
                    snap["errors"].append(f"logs {name}/{c['name']} previous={previous}: HTTP {e.status}")
                    continue
                key = f"{name}/{c['name']}" + ("/previous" if previous else "")
                snap["logs"][key] = txt
        # The app's own /metrics (latency, upstream errors, cgroup throttling), via the API server proxy.
        if pod.get("status", {}).get("phase") == "Running" and any(
            p.get("containerPort") == 8080 for c in pod["spec"]["containers"] for p in c.get("ports") or []
        ):
            try:
                snap["app_metrics"][name] = core.connect_get_namespaced_pod_proxy_with_path(
                    f"{name}:8080", namespace, "metrics", _request_timeout=3
                )
            except Exception as e:  # noqa: BLE001 - a dead pod is evidence, not a crash
                snap["errors"].append(f"app metrics {name}: {type(e).__name__}")
    try:
        resp = custom.list_namespaced_custom_object("metrics.k8s.io", "v1beta1", namespace, "pods")
        snap["pod_metrics"] = resp.get("items", [])
    except ApiException as e:
        snap["errors"].append(f"metrics.k8s.io: HTTP {e.status}")
    snap["collect_seconds"] = round(time.time() - t0, 3)
    return snap


def save_snapshot(snap: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt") as f:
        json.dump(snap, f, indent=0, sort_keys=True)


def load_snapshot(path: Path) -> dict:
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rt") as f:
        return json.load(f)
