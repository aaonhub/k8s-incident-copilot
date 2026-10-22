"""Deterministic baseline: detector signals -> diagnosis, no model.

This is what a careful SRE would script in an afternoon: a priority-ordered
decision list. It is the yardstick the model has to beat to earn its cost.
"""
from __future__ import annotations

from .facts import Facts, parse_quantity
from .guard import Action, GuardResult

PRIORITY = ["container_config_error", "image_pull_failure", "oom_killed", "crash_loop", "service_no_endpoints",
            "cpu_throttled", "upstream_timeouts", "not_ready", "readiness_probe_failing"]


def _limit(facts: Facts, dep: str, key: str) -> float | None:
    for d in facts.snap["deployments"]:
        if d["metadata"]["name"] == dep:
            c = d["spec"]["template"]["spec"]["containers"][0]
            return parse_quantity(((c.get("resources") or {}).get("limits") or {}).get(key))
    return None


def diagnose(facts: Facts) -> GuardResult:
    sig = {s.kind: s for s in facts.signals}
    recent = {s.subject for s in facts.signals if s.kind == "recent_rollout"}

    def result(cat: str, target: str, action: Action, why: str) -> GuardResult:
        return GuardResult(None, action.type, action, "incident", cat, target, reasons=[f"rule: {why}"])

    for kind in PRIORITY:
        s = sig.get(kind)
        if not s:
            continue
        t, name = s.subject, s.subject.partition("/")[2]
        if kind == "container_config_error":
            cat = "missing_secret" if "secret" in s.detail.lower() else "crashloop_config_error"
            return result(cat, t, Action(type="handoff"), f"{kind}: {s.detail[:80]}")
        if kind == "image_pull_failure":
            return result("image_pull_error", t, Action(type="rollout_undo", target=t), kind)
        if kind == "oom_killed":
            if t in recent:
                return result("oom_killed", t, Action(type="rollout_undo", target=t), "OOMKilled after a recent rollout")
            mem = _limit(facts, name, "memory") or 128 * 2**20
            return result("oom_killed", t, Action(type="set_resources", target=t, container=name,
                                                  memory_limit=f"{int(mem * 2 / 2**20)}Mi"), "OOMKilled, no recent rollout")
        if kind == "crash_loop":
            return result("crashloop_config_error", t, Action(type="rollout_undo", target=t), kind)
        if kind == "service_no_endpoints":
            return result("service_selector_mismatch", t, Action(
                type="patch_service_selector", target=t, selector_label=f"app.kubernetes.io/name={name}"),
                "service selects 0 pods; assume pods are labelled with the service name")
        if kind == "cpu_throttled":
            if t in recent:
                return result("cpu_throttling", t, Action(type="rollout_undo", target=t), "throttled after a recent rollout")
            cpu = _limit(facts, name, "cpu") or 0.1
            return result("cpu_throttling", t, Action(type="set_resources", target=t, container=name,
                                                      cpu_limit=f"{int(cpu * 4000)}m"), kind)
        if kind == "upstream_timeouts":
            # The caller is the symptom; blame a dependency that rolled out recently.
            others = sorted(r for r in recent if r != t)
            if len(others) == 1:
                return result("dependency_timeout", others[0], Action(type="rollout_undo", target=others[0]),
                              f"{t} times out calling upstream; {others[0]} rolled out recently")
            return result("dependency_timeout", t, Action(type="handoff"), "upstream timeouts, no single suspect")
        if kind in ("not_ready", "readiness_probe_failing"):
            return result("readiness_probe_failure", t, Action(type="rollout_undo", target=t), kind)
    return GuardResult(None, "none", Action(type="none"), "no_incident", "no_incident", "", reasons=["rule: no active signal"])
