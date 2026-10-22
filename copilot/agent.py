"""The investigation loop.

    snapshot (deterministic) -> Facts + detector signals (deterministic)
      -> auto-retrieval from the signals (deterministic query, hybrid search)
      -> model turns: read-only tool calls, then submit_diagnosis (function calling)
      -> guard.py (code) decides what, if anything, may go to a human for approval

Stop conditions inside the loop: MAX_STEPS model turns, a repeated identical
tool call, two malformed submissions, or the spend cap. Every one of them ends
in a handoff, not a guess.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from .facts import Facts
from .guard import ACTIONS, CATEGORIES, GuardResult, apply_guard
from .llm import Gemini
from .retrieval import KnowledgeBase
from .trace import Tracer

MAX_STEPS = 6
KB_TOP_K = 5

SYSTEM = """You are an incident investigator for a Kubernetes namespace, used by support engineers.
You work from a point-in-time snapshot. Facts come in citable artifacts written as [artifact-id] text.

Rules:
- Ground every claim: each claim cites one artifact_id and a `quote` copied EXACTLY (verbatim substring) from that
  artifact. Claims that can't be grounded will be discarded by code.
- Detector signals are deterministic hints. They can be incomplete, and events can be stale (they outlive the pods
  that produced them; check "(object no longer exists)" and ages). Current pod/deployment state beats old events.
- Use the read-only tools to check before concluding (e.g. logs --previous for a crashing container, the rollout
  diff for what changed). Do not repeat a tool call.
- Past tickets and runbooks are similar cases, not evidence about THIS cluster. Cite them in similar_cases.
- If nothing is wrong now, status=no_incident and action none. If evidence is thin or contradictory, status=
  insufficient_evidence and action handoff. Never guess to fill the form.
- target is the object at fault ("deployment/<name>" or "service/<name>"), which may differ from where the
  symptom shows up.
- You can only PROPOSE one action; a human approves it and code validates it. Allowed types:
  rollout_undo (deployment back to its previous revision), set_resources (raise a container's cpu/memory limit;
  give container plus cpu_limit and/or memory_limit like "256Mi" or "500m"), patch_service_selector
  (selector_label "key=value" that matches the intended pods), handoff (a human must act, e.g. restoring secret
  values), none. Prefer undoing the change that caused the incident over compensating for it.
- Finish by calling submit_diagnosis exactly once.

Category definitions (pick by ROOT CAUSE, not by symptom; target = the object whose change or state is the cause):
- image_pull_error: image/tag can't be pulled.
- crashloop_config_error: the container exits on startup because of its own bad config.
- readiness_probe_failure: containers run but the readiness probe fails, so pods never become Ready.
- liveness_probe_restart_loop: the liveness probe fails, so the kubelet keeps killing working containers.
- oom_killed: a container exceeds its memory limit and is killed (exit 137).
- missing_secret: a referenced Secret/key doesn't exist, so containers can't be created.
- service_selector_mismatch: a Service selector matches no (or the wrong) pods.
- cpu_throttling: the CPU limit is too low; latency rises from CFS throttling.
- dependency_timeout: a service is slow or failing because a DOWNSTREAM service it calls became slow or
  unavailable (e.g. after the dependency's own deploy). The target is the dependency, not the caller.
- config_regression: a service's OWN config is wrong but doesn't crash it (bad URL/host/port/flag), so its
  requests fail. The target is that service.
- Other categories exist for problems outside this list; use unknown if none fits."""


def tool_decls(use_retrieval: bool) -> list[dict]:
    s, n, b = {"type": "string"}, {"type": "number"}, {"type": "boolean"}
    decls = [
        {"name": "get_logs", "description": "Container log tail (up to 120 lines). previous=true gives the log of the "
         "last terminated instance (what `kubectl logs --previous` shows).",
         "parameters": {"type": "object", "properties": {"pod": s, "container": s, "previous": b,
                                                         "lines": {"type": "integer"}}, "required": ["pod"]}},
        {"name": "describe", "description": "Like kubectl describe: the object's artifact plus all its events "
         "(Normal and Warning). object is pod/<name>, deployment/<name> or service/<name>.",
         "parameters": {"type": "object", "properties": {"object": s}, "required": ["object"]}},
        {"name": "get_rollout_diff", "description": "Revision history of a deployment and the pod-template diff "
         "between the current and previous revision (image, env, resources, probes).",
         "parameters": {"type": "object", "properties": {"deployment": s}, "required": ["deployment"]}},
        {"name": "get_metrics", "description": "CPU/memory usage vs limits, CFS throttling, latency and error "
         "counters for one pod.", "parameters": {"type": "object", "properties": {"pod": s}, "required": ["pod"]}},
    ]
    if use_retrieval:
        decls.append({"name": "search_knowledge", "description": "Search past support tickets and runbooks "
                      "(hybrid BM25 + embeddings). Returns top 5 with ids.",
                      "parameters": {"type": "object", "properties": {"query": s}, "required": ["query"]}})
    decls.append({
        "name": "submit_diagnosis",
        "description": "Submit the final structured diagnosis. Call exactly once, last.",
        "parameters": {"type": "object", "properties": {
            "status": {"type": "string", "enum": ["incident", "no_incident", "insufficient_evidence"]},
            "category": {"type": "string", "enum": CATEGORIES},
            "target": s,
            "summary": {"type": "string", "description": "2-3 sentences for the on-call engineer"},
            "claims": {"type": "array", "items": {"type": "object", "properties": {
                "statement": s, "artifact_id": s, "quote": s}, "required": ["statement", "artifact_id", "quote"]}},
            "similar_cases": {"type": "array", "items": s},
            "confidence": n,
            "action": {"type": "object", "properties": {
                "type": {"type": "string", "enum": ACTIONS}, "target": s, "container": s, "memory_limit": s,
                "cpu_limit": s, "selector_label": s, "rationale": s}, "required": ["type", "rationale"]},
            "open_questions": {"type": "array", "items": s},
        }, "required": ["status", "category", "target", "summary", "claims", "confidence", "action"]},
    })
    return decls


def auto_query(facts: Facts) -> str:
    """Deterministic retrieval query: signal kinds + details + the loudest log lines."""
    parts = [f"{s.kind.replace('_', ' ')} {s.detail}" for s in facts.signals if s.kind != "recent_rollout"]
    for a in facts.artifacts.values():
        if a.kind == "log":
            parts += [ln.split(" ", 3)[-1] for ln in a.text.splitlines()
                      if any(w in ln for w in ("CRITICAL", "ERROR", "timed out"))][-2:]
    return " ".join(dict.fromkeys(parts))[:600] or "all pods healthy no errors"


@dataclass
class Investigation:
    guard: GuardResult
    trace: Tracer
    steps: int
    tool_calls: list[dict] = field(default_factory=list)
    retrieved: list[str] = field(default_factory=list)
    stop_reason: str = "submitted"
    wall_ms: int = 0

    def report(self) -> dict:
        g = self.guard
        d = g.diagnosis
        return {
            "status": g.status, "category": g.category, "target": g.target,
            "summary": d.summary if d else "", "confidence": d.confidence if d else 0.0,
            "claims": [c.model_dump() for c in d.claims] if d else [],
            "unsupported_claims": g.unsupported_claims, "similar_cases": d.similar_cases if d else [],
            "model_action": g.raw_action, "final_action": g.final_action.model_dump(),
            "guard_reasons": g.reasons, "stop_reason": self.stop_reason, "steps": self.steps,
            "tool_calls": self.tool_calls, "retrieved": self.retrieved,
            "open_questions": d.open_questions if d else [], **self.trace.totals(), "wall_ms": self.wall_ms,
        }


class Toolbox:
    """Read-only tools over the snapshot. Every result is a list of citable artifacts."""

    def __init__(self, facts: Facts, kb: KnowledgeBase | None):
        self.f, self.kb = facts, kb
        self.seen: dict[str, str] = {}

    def show(self, artifacts) -> str:
        out = []
        for a in artifacts:
            self.seen[a.id] = a.text
            out.append(a.render())
        return "\n".join(out) or "(nothing found)"

    def _pod(self, name: str) -> str | None:
        name = name.removeprefix("pod/")
        if f"pod/{name}" in self.f.artifacts:
            return name
        matches = [a.id[4:] for a in self.f.artifacts.values() if a.kind == "pod" and a.id[4:].startswith(name)]
        return matches[0] if len(matches) == 1 else None

    def call(self, name: str, args: dict) -> str:
        A = self.f.artifacts
        if name == "get_logs":
            pod = self._pod(args.get("pod", ""))
            if not pod:
                return f"error: unknown pod {args.get('pod')!r}"
            ctr = args.get("container") or pod.rsplit("-", 2)[0]
            key = f"log/{pod}/{ctr}" + ("/previous" if args.get("previous") else "")
            if key not in A:
                return f"error: no log {key} (container never restarted, or wrong container name)"
            lines = A[key].text.splitlines()[-int(args.get("lines") or 120):]
            self.seen[key] = A[key].text
            return f"[{key}]\n" + "\n".join(lines)
        if name == "describe":
            obj = args.get("object", "")
            kind, _, oname = obj.partition("/")
            if kind == "pod":
                pod = self._pod(oname)
                arts = [A[f"pod/{pod}"]] if pod else []
                arts += [a for a in A.values() if a.kind == "event" and getattr(a, "meta", {}).get("name") == pod]
            elif kind == "deployment":
                arts = [A[k] for k in (f"deploy/{oname}", f"rollout/{oname}") if k in A]
                arts += [a for a in A.values() if a.kind in ("pod", "event") and a.subject == f"deployment/{oname}"]
            elif kind == "service":
                arts = [A[k] for k in (f"svc/{oname}",) if k in A]
            else:
                return "error: object must be pod/<name>, deployment/<name> or service/<name>"
            return self.show(arts) if arts else f"error: {obj} not found"
        if name == "get_rollout_diff":
            dep = args.get("deployment", "").removeprefix("deployment/")
            return self.show([A[f"rollout/{dep}"]]) if f"rollout/{dep}" in A else f"error: no deployment {dep!r}"
        if name == "get_metrics":
            pod = self._pod(args.get("pod", ""))
            return self.show([A[f"metrics/{pod}"]]) if pod and f"metrics/{pod}" in A else "error: no metrics for that pod"
        if name == "search_knowledge" and self.kb:
            return self.show_hits(self.kb.search(args.get("query", ""), "hybrid", KB_TOP_K))
        return f"error: unknown tool {name}"

    def show_hits(self, hits) -> str:
        out = []
        for h in hits:
            self.seen[h.id] = h.doc.text
            body = h.doc.text
            if h.id.startswith("ticket/"):
                keep = [ln for ln in body.splitlines() if ln.startswith(("Root cause:", "Resolution:"))]
                body = h.doc.title + " | " + " | ".join(keep)
            out.append(f"[{h.id}] (score {h.score:.3f}) {body[:600]}")
        return "\n".join(out)


def investigate(facts: Facts, llm: Gemini, kb: KnowledgeBase | None = None, use_retrieval: bool = True,
                tracer: Tracer | None = None, max_steps: int = MAX_STEPS) -> Investigation:
    tr = tracer or Tracer()
    t_start = time.time()
    tb = Toolbox(facts, kb if use_retrieval else None)
    inv = Investigation(guard=None, trace=tr, steps=0)  # type: ignore[arg-type]
    with tr.span("investigate", namespace=facts.ns, model=llm.model, retrieval=use_retrieval) as root:
        root["signals"] = [f"{s.kind}@{s.subject}" for s in facts.signals]
        for a in facts.artifacts.values():  # everything in the fact sheet counts as "shown"
            if a.kind != "log":
                tb.seen[a.id] = a.text
            else:  # the fact sheet only shows log tails
                tb.seen[a.id] = "\n".join(a.text.splitlines()[-6:])
        user = "# Fact sheet\n" + facts.fact_sheet()
        if use_retrieval and kb:
            q = auto_query(facts)
            with tr.span("retrieval.auto", query=q, mode="hybrid") as sp:
                hits = kb.search(q, "hybrid", KB_TOP_K)
                sp["hits"] = [{"id": h.id, "score": round(h.score, 4), "category": h.doc.category} for h in hits]
            inv.retrieved = [h.id for h in hits]
            user += "\n\n# Similar past cases (auto-retrieved; not evidence about this cluster)\n" + tb.show_hits(hits)
        user += "\n\nInvestigate, then call submit_diagnosis."
        contents = [{"role": "user", "parts": [{"text": user}]}]
        decls = tool_decls(use_retrieval and kb is not None)
        payload, parse_error, bad_submits = None, None, 0
        seen_calls: set[str] = set()
        while True:
            inv.steps += 1
            final_turn = inv.steps >= max_steps
            body = {
                "systemInstruction": {"parts": [{"text": SYSTEM}]},
                "contents": contents,
                "tools": [{"functionDeclarations": decls}],
                "toolConfig": {"functionCallingConfig": {
                    "mode": "ANY", **({"allowedFunctionNames": ["submit_diagnosis"]} if final_turn else {})}},
                "generationConfig": {"temperature": 0, "thinkingConfig": {"thinkingLevel": "low"}},
            }
            with tr.span("llm.generate", step=inv.steps) as sp:
                res = llm.generate(body)
                sp.update(input_tokens=res.input_tokens, output_tokens=res.output_tokens,
                          cost_usd=round(res.cost_usd, 6), latency_ms=res.latency_ms, replayed=res.replayed)
            contents.append(res.content)
            calls = [p["functionCall"] for p in res.parts if "functionCall" in p]
            if not calls:
                parse_error, inv.stop_reason = "model returned no function call", "no_function_call"
                break
            responses, submitted = [], False
            for c in calls:
                name, args = c["name"], c.get("args") or {}
                if name == "submit_diagnosis":
                    from pydantic import ValidationError

                    from .guard import Diagnosis
                    try:
                        Diagnosis.model_validate(args)
                        payload, submitted = args, True
                        resp = {"result": "received"}
                    except ValidationError as e:
                        bad_submits += 1
                        parse_error = f"{e.errors()[0]['loc']}: {e.errors()[0]['msg']}"
                        resp = {"error": f"invalid diagnosis ({parse_error}); fix and resubmit"}
                else:
                    sig = json.dumps([name, args], sort_keys=True)
                    with tr.span(f"tool.{name}", args=args) as sp:
                        if sig in seen_calls:
                            out = "error: identical call already answered above; use that result"
                            sp["repeated"] = True
                        else:
                            seen_calls.add(sig)
                            out = tb.call(name, args)
                        sp["result_chars"] = len(out)
                    inv.tool_calls.append({"tool": name, "args": args})
                    resp = {"result": out}
                fr = {"name": name, "response": resp}
                if c.get("id"):
                    fr["id"] = c["id"]
                responses.append({"functionResponse": fr})
            if submitted:
                parse_error, inv.stop_reason = None, "submitted"
                break
            if bad_submits >= 2:
                inv.stop_reason = "invalid_output_twice"
                break
            if final_turn:
                inv.stop_reason = "max_steps"
                break
            contents.append({"role": "user", "parts": responses})
        with tr.span("guard") as sp:
            inv.guard = apply_guard(facts, payload, tb.seen, parse_error)
            sp.update(status=inv.guard.status, category=inv.guard.category, model_action=inv.guard.raw_action,
                      final_action=inv.guard.final_action.type, reasons=inv.guard.reasons,
                      supported_claims=inv.guard.supported_claims, unsupported_claims=len(inv.guard.unsupported_claims))
        root.update(stop_reason=inv.stop_reason, steps=inv.steps, **tr.totals())
    inv.wall_ms = int((time.time() - t_start) * 1000)
    return inv
