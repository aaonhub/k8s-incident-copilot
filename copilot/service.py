"""Glue shared by the CLI and the HTTP API: run an investigation, record it, approve it."""
from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone
from pathlib import Path

from .actions import READER, REMEDIATOR, RUNS, audit, execute, idempotency_key, verify_fix
from .agent import investigate
from .cluster import collect_snapshot, load_snapshot
from .facts import Facts
from .guard import Action
from .llm import Gemini
from .retrieval import KnowledgeBase
from .trace import Tracer

INV_DIR = RUNS / "investigations"
TRACE_DIR = RUNS / "traces"
_KB: KnowledgeBase | None = None


def kb() -> KnowledgeBase:
    global _KB
    if _KB is None:
        _KB = KnowledgeBase()
    return _KB


def run_investigation(namespace: str = "shop", fixture: str | None = None, mode: str | None = None,
                      use_retrieval: bool = True, kubeconfig: str | None = None) -> dict:
    inv_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + secrets.token_hex(3)
    tr = Tracer(TRACE_DIR / f"{inv_id}.jsonl")
    with tr.span("snapshot", source=fixture or "live") as sp:
        if fixture:
            snap = load_snapshot(Path(fixture))
        else:
            kc = kubeconfig or (str(READER) if READER.exists() else None)  # None in-cluster: the pod's ServiceAccount
            snap = collect_snapshot(namespace, kc)
        sp.update(pods=len(snap["pods"]), events=len(snap["events"]), collect_errors=len(snap.get("errors", [])))
    facts = Facts(snap)
    inv = investigate(facts, Gemini(mode=mode, tag=inv_id), kb() if use_retrieval else None, use_retrieval, tr)
    rep = {"id": inv_id, "namespace": facts.ns, "fixture": fixture, "trace_id": tr.trace_id, **inv.report()}
    act = inv.guard.final_action
    if inv.guard.actionable:
        rep["idempotency_key"] = idempotency_key(facts, act)
        rep["approval"] = "pending"
        audit("proposed", investigation_id=inv_id, key=rep["idempotency_key"], category=inv.guard.category,
              action=act.model_dump(), confidence=rep["confidence"], claims=len(rep["claims"]))
    else:
        audit("no_action", investigation_id=inv_id, status=inv.guard.status, category=inv.guard.category,
              final_action=act.type, reasons=inv.guard.reasons)
    INV_DIR.mkdir(parents=True, exist_ok=True)
    (INV_DIR / f"{inv_id}.json").write_text(json.dumps(rep, indent=1, default=str))
    return rep


def load_investigation(inv_id: str) -> dict:
    return json.loads((INV_DIR / f"{inv_id}.json").read_text())


def decide(inv_id: str, approver: str, decision: str, key: str, verify: bool = True) -> dict:
    """A human's decision on a pending proposal. `key` must match: you approve exactly what you saw."""
    rep = load_investigation(inv_id)
    if "idempotency_key" not in rep:
        return {"status": "nothing_to_approve"}
    if key != rep["idempotency_key"]:
        audit("approval_rejected_key_mismatch", investigation_id=inv_id, approver=approver)
        return {"status": "refused", "output": "idempotency key does not match the proposal"}
    if rep.get("fixture"):
        return {"status": "refused", "output": "replayed investigation (fixture): nothing to apply"}
    if decision == "approve" and not REMEDIATOR.exists():
        # The in-cluster service runs as the read-only identity and holds no mutating credentials.
        audit("approval_needs_operator", investigation_id=inv_id, key=key, approver=approver)
        return {"status": "refused", "output": "this deployment is read-only; apply from an operator machine with "
                f"`copilot approve {inv_id} --key {key} --approver <you>`"}
    if decision != "approve":
        audit("rejected", investigation_id=inv_id, key=key, approver=approver)
        rep["approval"] = "rejected"
        (INV_DIR / f"{inv_id}.json").write_text(json.dumps(rep, indent=1, default=str))
        return {"status": "rejected"}
    audit("approved", investigation_id=inv_id, key=key, approver=approver)
    tr = Tracer(TRACE_DIR / f"{inv_id}.jsonl", trace_id=rep["trace_id"])
    act = Action.model_validate(rep["final_action"])
    with tr.span("approval.apply", approver=approver, key=key, action=act.type, target=act.target) as sp:
        fresh = Facts(collect_snapshot(rep["namespace"], str(READER)))  # re-check: state may have moved on
        result = execute(fresh, rep["category"], act, key, approver, inv_id)
        sp["result"] = result["status"]
    if result["status"] == "applied" and verify:
        with tr.span("approval.verify") as sp:
            v = verify_fix(rep["namespace"], rep["category"], rep["target"])
            sp.update(**v)
        result["verification"] = v
        audit("verified" if v["verified"] else "not_verified_handoff", investigation_id=inv_id, key=key, **v)
    rep["approval"] = result["status"]
    rep["approval_result"] = result
    (INV_DIR / f"{inv_id}.json").write_text(json.dumps(rep, indent=1, default=str))
    return result
