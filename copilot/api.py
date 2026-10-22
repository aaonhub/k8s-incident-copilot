"""HTTP face of the copilot (for chat bots / internal tools). Same contract as the CLI:
investigations are read-only; a mutation needs a named approver quoting the idempotency key.

    POST /investigations                {"namespace": "shop"}
    GET  /investigations/{id}
    POST /investigations/{id}/decision  {"approver": "sam", "decision": "approve", "key": "<idempotency key>"}
    GET  /audit                         chain verification + last 20 records
"""
from __future__ import annotations

import json
import os

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .actions import AUDIT_LOG, verify_audit
from .service import decide, load_investigation, run_investigation

app = FastAPI(title="k8s-incident-copilot")


class InvestigateReq(BaseModel):
    namespace: str = "shop"
    use_retrieval: bool = True


class DecisionReq(BaseModel):
    approver: str
    decision: str  # approve | reject
    key: str


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.post("/investigations")
def create(req: InvestigateReq):
    return run_investigation(req.namespace, None, os.environ.get("COPILOT_LLM_MODE", "live"), req.use_retrieval,
                             os.environ.get("COPILOT_KUBECONFIG"))


@app.get("/investigations/{inv_id}")
def get(inv_id: str):
    try:
        return load_investigation(inv_id)
    except FileNotFoundError:
        raise HTTPException(404, "no such investigation") from None


@app.post("/investigations/{inv_id}/decision")
def decision(inv_id: str, req: DecisionReq):
    if req.decision not in ("approve", "reject"):
        raise HTTPException(400, "decision must be approve or reject")
    try:
        return decide(inv_id, req.approver, req.decision, req.key)
    except FileNotFoundError:
        raise HTTPException(404, "no such investigation") from None


@app.get("/audit")
def audit_view():
    ok, n, msg = verify_audit()
    tail = [json.loads(x) for x in AUDIT_LOG.read_text().splitlines()[-20:]] if AUDIT_LOG.exists() else []
    return {"chain_ok": ok, "records": n, "detail": msg, "tail": tail}
