"""Scripted, reproducible incidents against the `shop` Helm release.

Each scenario is a list of operator commands (helm/kubectl, run with the admin
kubeconfig, never the agent's) plus the ground truth an experienced engineer
would write down: root-cause category, the object at fault, and the remediation
they'd accept. Variants change the target or the value so the held-out set
isn't a copy of the dev set.

    python -m copilot.incidents list
    python -m copilot.incidents inject bad-image-tag [--variant 2]
    python -m copilot.incidents reset
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NS = "shop"
RELEASE = "shop"
CHART = ROOT / "charts" / "shop"


def sh(cmd: str, check: bool = True) -> str:
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"{cmd}\n{r.stderr.strip()}")
    return r.stdout


def helm_set(*sets: str) -> str:
    args = " ".join(f"--set {s}" for s in sets)
    return f"helm upgrade {RELEASE} {CHART} -n {NS} --reuse-values {args}"


@dataclass
class Variant:
    steps: list[str]
    category: str
    target: str  # object at fault, e.g. deployment/webapp
    accept: list[str]  # acceptable remediation types (expert would sign off on these)
    gold: str  # what the expert actually did
    wait_for: str | None  # detector signal that proves the incident is live
    note: str = ""
    settle: int = 20  # extra seconds after the signal fires, so logs/metrics accumulate


@dataclass
class Scenario:
    name: str
    title: str
    what_breaks: str
    variants: list[Variant] = field(default_factory=list)


S: list[Scenario] = [
    Scenario("bad-image-tag", "Bad image tag", "A deploy points at an image tag that was never pushed.", [
        Variant([helm_set("components.webapp.imageTag=1.0.404")], "image_pull_error", "deployment/webapp",
                ["rollout_undo"], "rollout_undo", "image_pull_failure"),
        Variant([helm_set("components.inventory.imageTag=2.0.0-rc1")], "image_pull_error", "deployment/inventory",
                ["rollout_undo"], "rollout_undo", "image_pull_failure"),
        Variant([helm_set("components.webapp.imageTag=lates")], "image_pull_error", "deployment/webapp",
                ["rollout_undo"], "rollout_undo", "image_pull_failure"),
    ]),
    Scenario("crashloop-bad-env", "CrashLoopBackOff from a bad env value",
             "A config value fails startup validation, so the container exits 1 on every start.", [
        Variant([helm_set("components.webapp.env.UPSTREAM_TIMEOUT_MS=2s")], "crashloop_config_error", "deployment/webapp",
                ["rollout_undo"], "rollout_undo", "crash_loop", settle=30),
        Variant([helm_set("components.webapp.env.LOG_LEVEL=verbose")], "crashloop_config_error", "deployment/webapp",
                ["rollout_undo"], "rollout_undo", "crash_loop", settle=30),
        Variant([helm_set("components.inventory.env.LOG_LEVEL=trace")], "crashloop_config_error", "deployment/inventory",
                ["rollout_undo"], "rollout_undo", "crash_loop", settle=30),
    ]),
    Scenario("readiness-probe", "Failing readiness probe",
             "The probe path is wrong, so new pods run but never become Ready and the rollout stalls.", [
        Variant([helm_set("components.webapp.readinessPath=/readyz")], "readiness_probe_failure", "deployment/webapp",
                ["rollout_undo"], "rollout_undo", "not_ready"),
        Variant([helm_set("components.inventory.readinessPath=/health/ready")], "readiness_probe_failure",
                "deployment/inventory", ["rollout_undo"], "rollout_undo", "not_ready"),
        Variant([helm_set("components.webapp.readinessPath=/api/ready")], "readiness_probe_failure", "deployment/webapp",
                ["rollout_undo"], "rollout_undo", "not_ready"),
    ]),
    Scenario("oom-killed", "OOMKilled", "Memory use exceeds the container limit; the kernel kills it (exit 137).", [
        Variant([helm_set("components.webapp.env.CACHE_WARM_MB=160")], "oom_killed", "deployment/webapp",
                ["rollout_undo", "set_resources"], "rollout_undo", "oom_killed", settle=30),
        Variant([helm_set("components.inventory.env.CACHE_WARM_MB=200")], "oom_killed", "deployment/inventory",
                ["rollout_undo", "set_resources"], "rollout_undo", "oom_killed", settle=30),
        Variant([helm_set("components.webapp.resources.limits.memory=40Mi", "components.webapp.resources.requests.memory=32Mi")], "oom_killed", "deployment/webapp",
                ["rollout_undo", "set_resources"], "set_resources", "oom_killed", settle=30),
    ]),
    Scenario("missing-secret", "Missing Secret",
             "A Secret the pods mount as env is deleted; new pods can't start (CreateContainerConfigError).", [
        Variant([f"kubectl -n {NS} delete secret shop-api-credentials", f"kubectl -n {NS} rollout restart deploy/webapp"],
                "missing_secret", "deployment/webapp", ["handoff"], "handoff", "container_config_error",
                note="Recreating a Secret needs the real value from the secret manager: a human job."),
        Variant([f"kubectl -n {NS} delete secret inventory-db-credentials", f"kubectl -n {NS} rollout restart deploy/inventory"],
                "missing_secret", "deployment/inventory", ["handoff"], "handoff", "container_config_error"),
    ]),
    Scenario("bad-service-selector", "Service selector matches nothing",
             "The Service selector is edited to a label no pod has, so it has zero endpoints while pods look healthy.", [
        Variant([f"""kubectl -n {NS} patch service webapp -p '{{"spec":{{"selector":{{"app.kubernetes.io/name":"web-app"}}}}}}'"""],
                "service_selector_mismatch", "service/webapp", ["patch_service_selector"], "patch_service_selector",
                "service_no_endpoints", settle=25),
        Variant([f"""kubectl -n {NS} patch service inventory -p '{{"spec":{{"selector":{{"app.kubernetes.io/name":"inventory-v2"}}}}}}'"""],
                "service_selector_mismatch", "service/inventory", ["patch_service_selector"], "patch_service_selector",
                "service_no_endpoints", settle=25),
    ]),
    Scenario("cpu-throttling", "CPU throttling", "The CPU limit is far below what the request path needs; latency climbs.", [
        Variant([helm_set("components.webapp.resources.limits.cpu=15m", "components.webapp.resources.requests.cpu=10m")],
                "cpu_throttling", "deployment/webapp", ["set_resources", "rollout_undo"], "set_resources", "cpu_throttled",
                settle=45),
        Variant([helm_set("components.webapp.resources.limits.cpu=25m", "components.webapp.resources.requests.cpu=10m")],
                "cpu_throttling", "deployment/webapp", ["set_resources", "rollout_undo"], "set_resources", "cpu_throttled",
                settle=45),
    ]),
    Scenario("dependency-timeout", "Dependency timeout",
             "The inventory dependency gets slow after its own deploy; webapp requests time out (symptom and cause are in different services).", [
        Variant([helm_set("components.inventory.env.FAULT_LATENCY_MS=3000")], "dependency_timeout", "deployment/inventory",
                ["rollout_undo"], "rollout_undo", "upstream_timeouts", settle=30),
        Variant([helm_set("components.inventory.env.FAULT_LATENCY_MS=1500")], "dependency_timeout", "deployment/inventory",
                ["rollout_undo"], "rollout_undo", "upstream_timeouts", settle=30),
    ]),
    Scenario("decoy-rollout", "Dependency timeout plus an unrelated deploy",
             "Like dependency-timeout, but another workload was redeployed at the same time. Only one change is the cause.", [
        Variant([helm_set("components.inventory.env.FAULT_LATENCY_MS=3000"), f"kubectl -n {NS} rollout restart deploy/loadgen"],
                "dependency_timeout", "deployment/inventory", ["rollout_undo"], "rollout_undo", "upstream_timeouts", settle=30),
        Variant([helm_set("components.inventory.env.FAULT_LATENCY_MS=2500", "loadgen.intervalS=0.7")],
                "dependency_timeout", "deployment/inventory", ["rollout_undo"], "rollout_undo", "upstream_timeouts", settle=30),
        Variant([helm_set("components.inventory.env.FAULT_LATENCY_MS=2000", "components.webapp.env.LOG_LEVEL=debug")],
                "dependency_timeout", "deployment/inventory", ["rollout_undo"], "rollout_undo", "upstream_timeouts", settle=40),
    ]),
    Scenario("liveness-probe", "Liveness probe restart loop",
             "The liveness probe path is wrong: pods serve traffic fine but the kubelet kills and restarts them every ~70 s.", [
        Variant([helm_set("components.webapp.livenessPath=/live")], "liveness_probe_restart_loop", "deployment/webapp",
                ["handoff", "rollout_undo"], "rollout_undo", "liveness_probe_failing", settle=100),
        Variant([helm_set("components.inventory.livenessPath=/healthcheck")], "liveness_probe_restart_loop",
                "deployment/inventory", ["handoff", "rollout_undo"], "rollout_undo", "liveness_probe_failing", settle=100),
    ]),
    Scenario("config-regression", "Config regression that doesn't crash",
             "A typo in the upstream URL: webapp starts and passes probes, but every order request fails.", [
        Variant([helm_set("components.webapp.env.UPSTREAM_URL=http://inventroy:8080/stock")], "config_regression",
                "deployment/webapp", ["rollout_undo"], "rollout_undo", "upstream_failures", settle=30),
        Variant([helm_set("components.webapp.env.UPSTREAM_URL=http://inventory:9090/stock")], "config_regression",
                "deployment/webapp", ["rollout_undo"], "rollout_undo", "upstream_failures", settle=30),
    ]),
    Scenario("healthy", "Healthy control", "Nothing is wrong. The right answer is 'no incident' and no action.", [
        Variant([], "no_incident", "", ["none"], "none", None, settle=40),
        Variant([f"kubectl -n {NS} rollout restart deploy/webapp"], "no_incident", "", ["none"], "none", None, settle=60,
                note="A routine restart: leaves startup probe-failure events behind but nothing is wrong."),
    ]),
    Scenario("recovered", "Already recovered (stale evidence)",
             "A bad deploy happened and was already rolled back. Warning events remain; the cluster is healthy now.", [
        Variant([helm_set("components.webapp.imageTag=1.0.404"), "sleep 40", f"kubectl -n {NS} rollout undo deploy/webapp",
                 f"kubectl -n {NS} rollout status deploy/webapp --timeout=120s"], "no_incident", "", ["none"], "none", None,
                settle=15),
        Variant([helm_set("components.inventory.env.CACHE_WARM_MB=200"), "sleep 45",
                 f"kubectl -n {NS} rollout undo deploy/inventory",
                 f"kubectl -n {NS} rollout status deploy/inventory --timeout=120s"], "no_incident", "", ["none"], "none", None,
                settle=15),
    ]),
]
SCENARIOS = {s.name: s for s in S}


def reset(wait: bool = True) -> None:
    """Back to a clean, healthy release (fresh Secrets, no leftover events).
    The namespace itself is kept so the copilot's RoleBindings survive."""
    sh(f"helm uninstall {RELEASE} -n {NS} --wait --timeout 120s", check=False)
    sh(f"kubectl -n {NS} delete pods --all --wait=true --timeout=90s", check=False)
    sh(f"kubectl -n {NS} delete events --all", check=False)
    sh(f"kubectl create ns {NS}", check=False)
    sh(f"helm install {RELEASE} {CHART} -n {NS} --wait --timeout 180s")
    if wait:
        time.sleep(25)  # let loadgen produce a little traffic


def inject(name: str, variant: int = 1) -> Variant:
    v = SCENARIOS[name].variants[variant - 1]
    for step in v.steps:
        if step.startswith("sleep "):
            time.sleep(float(step.split()[1]))
        else:
            sh(step)
    return v


def wait_for_signal(signal: str | None, timeout: int = 240, kubeconfig: str | None = None) -> bool:
    from .cluster import collect_snapshot
    from .facts import Facts

    if signal is None:
        return True
    end = time.time() + timeout
    while time.time() < end:
        facts = Facts(collect_snapshot(NS, kubeconfig))
        if any(s.kind == signal for s in facts.signals):
            return True
        time.sleep(10)
    return False


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="incidents")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    i = sub.add_parser("inject")
    i.add_argument("name", choices=sorted(SCENARIOS))
    i.add_argument("--variant", type=int, default=1)
    sub.add_parser("reset")
    a = ap.parse_args(argv)
    if a.cmd == "list":
        for s in S:
            print(f"{s.name:22} {len(s.variants)} variant(s)  {s.what_breaks}")
    elif a.cmd == "reset":
        reset()
        print("shop reset to a healthy release")
    else:
        v = inject(a.name, a.variant)
        print(json.dumps({"injected": a.name, "variant": a.variant, "category": v.category, "target": v.target}))
        print(f"waiting for the '{v.wait_for}' signal..." if v.wait_for else "done", file=sys.stderr)
        if v.wait_for and not wait_for_signal(v.wait_for):
            sys.exit("incident did not manifest within timeout")


if __name__ == "__main__":
    main()
