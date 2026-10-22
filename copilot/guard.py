"""The safety contract, enforced in code. The model proposes; this file decides.

A model diagnosis becomes actionable only if it survives, in order:
  1. Structure   - parses into the Diagnosis schema (one repair attempt, else abstain).
  2. Grounding   - every claim cites an artifact the model was actually shown, and its
                   `quote` appears verbatim in that artifact. Unsupported claims are
                   dropped and counted; no grounded claims left -> abstain.
  3. Evidence    - the diagnosed category's required signal must exist in the raw facts
                   for that target (e.g. "oom_killed" needs an OOMKilled container status).
                   A diagnosis the facts don't support is "conflicting evidence" -> handoff.
  4. Confidence  - below MIN_CONFIDENCE -> handoff.
  5. Action      - on the allowlist, permitted for this category, aimed at the diagnosed
                   object, inside hard bounds (increase-only resources, caps, selector must
                   match live pods of exactly one Deployment).
Anything that fails 3-5 is downgraded to `handoff`, never silently "fixed".
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from .facts import Facts, parse_quantity

INCIDENT_CATEGORIES = [
    "image_pull_error", "crashloop_config_error", "readiness_probe_failure", "oom_killed", "missing_secret",
    "service_selector_mismatch", "cpu_throttling", "dependency_timeout", "config_regression",
]
OTHER_CATEGORIES = [
    "registry_rate_limit", "liveness_probe_restart_loop", "node_disk_pressure_eviction", "network_policy_block",
    "dns_resolution_failure", "tls_cert_expired", "pvc_mount_failure", "gpu_xid_error", "nccl_timeout",
    "node_not_ready", "resource_quota_exceeded", "pdb_blocking_drain", "pending_insufficient_resources",
]
CATEGORIES = INCIDENT_CATEGORIES + OTHER_CATEGORIES + ["no_incident", "unknown"]
ACTIONS = ["rollout_undo", "set_resources", "patch_service_selector", "handoff", "none"]
MUTATING = {"rollout_undo", "set_resources", "patch_service_selector"}
MIN_CONFIDENCE = 0.6
MEMORY_CAP = 1 * 2**30  # 1Gi
CPU_CAP = 2.0  # cores

# Signals that mean "something is actively wrong right now".
ACTIVE_SIGNALS = {"image_pull_failure", "container_config_error", "oom_killed", "crash_loop", "not_ready",
                  "service_no_endpoints", "cpu_throttled", "upstream_timeouts", "upstream_failures", "high_error_ratio"}

# category -> signal kinds, any of which (on the diagnosed target) supports the diagnosis.
EVIDENCE_GATES: dict[str, set[str]] = {
    "image_pull_error": {"image_pull_failure"},
    "crashloop_config_error": {"crash_loop"},
    "readiness_probe_failure": {"not_ready", "readiness_probe_failing"},
    "oom_killed": {"oom_killed"},
    "missing_secret": {"container_config_error"},
    "service_selector_mismatch": {"service_no_endpoints"},
    "cpu_throttling": {"cpu_throttled"},
    "dependency_timeout": {"upstream_timeouts"},  # observed on the caller, not the target; checked namespace-wide
    "config_regression": {"upstream_failures", "high_error_ratio"},
}

# Which mutating actions may be offered for which diagnosis. Everything else is a handoff.
ACTION_POLICY: dict[str, set[str]] = {
    "image_pull_error": {"rollout_undo"},
    "crashloop_config_error": {"rollout_undo"},
    "readiness_probe_failure": {"rollout_undo"},
    "oom_killed": {"rollout_undo", "set_resources"},
    "missing_secret": set(),  # needs the real secret value: a human job
    "service_selector_mismatch": {"patch_service_selector"},
    "cpu_throttling": {"set_resources", "rollout_undo"},
    "dependency_timeout": {"rollout_undo"},
    "config_regression": {"rollout_undo"},
}


class Claim(BaseModel):
    statement: str
    artifact_id: str
    quote: str = Field(description="exact substring copied from the cited artifact")


class Action(BaseModel):
    type: Literal["rollout_undo", "set_resources", "patch_service_selector", "handoff", "none"]
    target: str = ""
    container: str = ""
    memory_limit: str = ""
    cpu_limit: str = ""
    selector_label: str = ""  # key=value
    rationale: str = ""


class Diagnosis(BaseModel):
    status: Literal["incident", "no_incident", "insufficient_evidence"]
    category: str
    target: str = ""
    summary: str
    claims: list[Claim] = []
    similar_cases: list[str] = []
    confidence: float = Field(ge=0, le=1)
    action: Action
    open_questions: list[str] = []


@dataclass
class GuardResult:
    diagnosis: Diagnosis | None
    raw_action: str  # what the model asked for (type:target)
    final_action: Action
    status: str
    category: str
    target: str
    supported_claims: int = 0
    unsupported_claims: list[dict] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)  # every downgrade, in words

    @property
    def actionable(self) -> bool:
        return self.final_action.type in MUTATING


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def check_claims(claims: list[Claim], seen: dict[str, str]) -> tuple[list[Claim], list[dict]]:
    ok, bad = [], []
    for c in claims:
        text = seen.get(c.artifact_id)
        if text is None:
            bad.append({"claim": c.statement, "artifact_id": c.artifact_id, "why": "artifact not shown to model"})
        elif len(c.quote.strip()) < 4 or _norm(c.quote) not in _norm(text):
            bad.append({"claim": c.statement, "artifact_id": c.artifact_id, "why": "quote not found in artifact"})
        else:
            ok.append(c)
    return ok, bad


def evidence_ok(facts: Facts, category: str, target: str) -> bool:
    need = EVIDENCE_GATES.get(category)
    if not need:
        return False
    if category == "dependency_timeout":
        return any(s.kind in need for s in facts.signals)
    return any(s.kind in need and s.subject == target for s in facts.signals)


def _containers(facts: Facts, deployment: str) -> dict[str, dict]:
    for d in facts.snap["deployments"]:
        if d["metadata"]["name"] == deployment:
            return {c["name"]: c for c in d["spec"]["template"]["spec"]["containers"]}
    return {}


def validate_action(facts: Facts, category: str, target: str, a: Action) -> str | None:
    """None if the action may be offered for approval, else the reason it may not."""
    if a.type not in MUTATING:
        return None
    if a.type not in ACTION_POLICY.get(category, set()):
        return f"action {a.type} is not permitted for category {category}"
    if a.target != target:
        return f"action target {a.target!r} differs from diagnosed target {target!r}"
    kind, _, name = a.target.partition("/")
    if a.type in ("rollout_undo", "set_resources"):
        if kind != "deployment" or not _containers(facts, name):
            return f"{a.target} is not a deployment in namespace {facts.ns}"
    if a.type == "rollout_undo":
        ro = facts.artifacts.get(f"rollout/{name}")
        if not ro or getattr(ro, "meta", {}).get("revisions", 0) < 2:
            return "no previous revision to roll back to"
    if a.type == "set_resources":
        cs = _containers(facts, name)
        c = cs.get(a.container) or (next(iter(cs.values())) if len(cs) == 1 and not a.container else None)
        if c is None:
            return f"container {a.container!r} not found in {a.target}"
        lim = (c.get("resources") or {}).get("limits") or {}
        if not (a.memory_limit or a.cpu_limit):
            return "set_resources needs memory_limit and/or cpu_limit"
        try:
            if a.memory_limit:
                new, cur = parse_quantity(a.memory_limit), parse_quantity(lim.get("memory"))
                if new is None or new > MEMORY_CAP or (cur is not None and new <= cur):
                    return f"memory_limit {a.memory_limit} must increase the current {lim.get('memory')} and be <= 1Gi"
            if a.cpu_limit:
                new, cur = parse_quantity(a.cpu_limit), parse_quantity(lim.get("cpu"))
                if new is None or new > CPU_CAP or (cur is not None and new <= cur):
                    return f"cpu_limit {a.cpu_limit} must increase the current {lim.get('cpu')} and be <= 2 cores"
        except ValueError:
            return "unparseable resource quantity"
    if a.type == "patch_service_selector":
        if kind != "service" or not any(s["metadata"]["name"] == name for s in facts.snap["services"]):
            return f"{a.target} is not a service in namespace {facts.ns}"
        key, eq, val = a.selector_label.partition("=")
        svc = next(s for s in facts.snap["services"] if s["metadata"]["name"] == name)
        if not eq or key not in (svc["spec"].get("selector") or {}):
            return "selector_label must be key=value for a key already in the service selector"
        pods = [p for p in facts.snap["pods"] if (p["metadata"].get("labels") or {}).get(key) == val]
        deps = {facts.pod_deployment(p) for p in pods}
        if not pods or len(deps) != 1:
            return f"selector {a.selector_label} must match live pods of exactly one deployment (matches {len(pods)})"
    return None


def apply_guard(facts: Facts, payload: dict | None, seen: dict[str, str], parse_error: str | None = None) -> GuardResult:
    handoff = Action(type="handoff")
    if payload is None or parse_error:
        return GuardResult(None, "none", handoff, "insufficient_evidence", "unknown", "", reasons=[
            f"model output was not a valid diagnosis: {parse_error or 'missing'}"])
    try:
        d = Diagnosis.model_validate(payload)
    except ValidationError as e:
        return GuardResult(None, "none", handoff, "insufficient_evidence", "unknown", "", reasons=[
            f"schema validation failed: {e.errors()[0]['msg']}"])
    raw_action = f"{d.action.type}:{d.action.target}" if d.action.type in MUTATING else d.action.type
    ok, bad = check_claims(d.claims, seen)
    g = GuardResult(d, raw_action, d.action, d.status, d.category, d.target, len(ok), bad)
    if bad:
        g.reasons.append(f"dropped {len(bad)} unsupported claim(s)")
    if d.category not in CATEGORIES:
        g.reasons.append(f"unknown category {d.category!r}")
        g.category, g.status, g.final_action = "unknown", "insufficient_evidence", handoff
        return g
    active = [s for s in facts.signals if s.kind in ACTIVE_SIGNALS]
    if d.status == "no_incident":
        if active:
            g.reasons.append("conflicting evidence: model says no incident but detectors report "
                             + ", ".join(sorted({s.kind for s in active})))
            g.status, g.final_action = "insufficient_evidence", handoff
        else:
            g.final_action = Action(type="none")
        return g
    if d.status == "insufficient_evidence":
        g.final_action = handoff if d.action.type != "none" else Action(type="handoff")
        return g
    # status == incident
    if not ok:
        g.reasons.append("no grounded claims: every claim failed citation checks")
        g.status, g.final_action = "insufficient_evidence", handoff
        return g
    if d.category in EVIDENCE_GATES and not evidence_ok(facts, d.category, d.target):
        g.reasons.append(f"conflicting evidence: raw facts lack the signal {sorted(EVIDENCE_GATES[d.category])} "
                         f"required for {d.category} on {d.target or '?'}")
        g.final_action = handoff
        return g
    if d.confidence < MIN_CONFIDENCE and d.action.type in MUTATING:
        g.reasons.append(f"confidence {d.confidence:.2f} < {MIN_CONFIDENCE}: handing off")
        g.final_action = handoff
        return g
    why = validate_action(facts, d.category, d.target, d.action)
    if why:
        g.reasons.append(f"action blocked: {why}")
        g.final_action = handoff
    elif d.action.type == "none":
        g.final_action = handoff  # an incident with no action is a human's call
    return g
