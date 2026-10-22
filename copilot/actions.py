"""Human-approved, idempotent, audited remediation.

Lifecycle of a proposal:
  proposed  -> written to the audit log with its idempotency key
  approved  -> a named human typed the key back (CLI) or POSTed it (API)
  rechecked -> fresh snapshot; the evidence gate must STILL hold, else refuse (state moved on)
  dry-run   -> server-side dry run with the remediator identity must pass
  applied   -> exactly once per key (SQLite UNIQUE); a repeat returns the first result
  verified  -> poll until the signal clears and the target is healthy, else hand off.
               There is no automatic retry: a failed fix is a human's problem.

The audit log is append-only JSONL with a SHA-256 hash chain (each record carries
the previous record's hash), so editing or deleting a line is detectable:
`copilot audit verify`.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from .facts import Facts
from .guard import EVIDENCE_GATES, Action, evidence_ok

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / "runs"
AUDIT_LOG = RUNS / "audit.jsonl"
ACTIONS_DB = RUNS / "actions.sqlite"
REMEDIATOR = ROOT / ".kube" / "copilot-remediator.yaml"
READER = ROOT / ".kube" / "copilot-reader.yaml"


# ------------------------------------------------------------------ audit log
def _last_hash() -> str:
    if not AUDIT_LOG.exists():
        return "0" * 64
    last = ""
    with AUDIT_LOG.open() as f:
        for line in f:
            if line.strip():
                last = line
    return json.loads(last)["hash"] if last else "0" * 64


def audit(event: str, **fields) -> dict:
    RUNS.mkdir(exist_ok=True)
    rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), "event": event, **fields,
           "prev_hash": _last_hash()}
    rec["hash"] = hashlib.sha256(json.dumps(rec, sort_keys=True, default=str).encode()).hexdigest()
    with AUDIT_LOG.open("a") as f:
        f.write(json.dumps(rec, default=str) + "\n")
    return rec


def verify_audit(path: Path | None = None) -> tuple[bool, int, str]:
    path = path or AUDIT_LOG
    prev, n = "0" * 64, 0
    if not path.exists():
        return True, 0, "empty"
    for n, line in enumerate(path.read_text().splitlines(), 1):
        rec = json.loads(line)
        h = rec.pop("hash")
        if rec["prev_hash"] != prev:
            return False, n, f"record {n}: prev_hash does not match record {n - 1}"
        if hashlib.sha256(json.dumps(rec, sort_keys=True, default=str).encode()).hexdigest() != h:
            return False, n, f"record {n}: content hash mismatch (edited)"
        prev = h
    return True, n, "chain intact"


# -------------------------------------------------------------- idempotency
def idempotency_key(facts: Facts, action: Action) -> str:
    """Same action against the same observed state -> same key. The target's current
    revision is part of the state, so after a rollout the same proposal gets a new key."""
    kind, _, name = action.target.partition("/")
    rev = ""
    for d in facts.snap["deployments"]:
        if kind == "deployment" and d["metadata"]["name"] == name:
            rev = (d["metadata"].get("annotations") or {}).get("deployment.kubernetes.io/revision", "")
    for s in facts.snap["services"]:
        if kind == "service" and s["metadata"]["name"] == name:
            rev = json.dumps(s["spec"].get("selector"), sort_keys=True)
    blob = json.dumps({"ns": facts.ns, "action": action.model_dump(exclude={"rationale"}), "state": rev}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def _db() -> sqlite3.Connection:
    RUNS.mkdir(exist_ok=True)
    db = sqlite3.connect(ACTIONS_DB)
    db.execute("""CREATE TABLE IF NOT EXISTS actions (
        idempotency_key TEXT PRIMARY KEY, investigation_id TEXT, action TEXT, approver TEXT,
        status TEXT, output TEXT, created_at TEXT, finished_at TEXT)""")
    return db


# ------------------------------------------------------------------ kubectl
def kubectl_argv(ns: str, a: Action) -> list[str]:
    kind, _, name = a.target.partition("/")
    if a.type == "rollout_undo":
        return ["rollout", "undo", f"deployment/{name}", "-n", ns]
    if a.type == "set_resources":
        lim = ",".join(x for x in (f"memory={a.memory_limit}" if a.memory_limit else "",
                                   f"cpu={a.cpu_limit}" if a.cpu_limit else "") if x)
        argv = ["set", "resources", f"deployment/{name}", "-n", ns, f"--limits={lim}"]
        return argv + (["-c", a.container] if a.container else [])
    if a.type == "patch_service_selector":
        key, _, val = a.selector_label.partition("=")
        return ["patch", "service", name, "-n", ns, "-p", json.dumps({"spec": {"selector": {key: val}}})]
    raise ValueError(f"not an executable action: {a.type}")


def _kubectl(argv: list[str], kubeconfig: Path) -> tuple[bool, str]:
    r = subprocess.run(["kubectl", "--kubeconfig", str(kubeconfig), *argv], capture_output=True, text=True, timeout=60)
    return r.returncode == 0, (r.stdout + r.stderr).strip()


def dry_run(ns: str, a: Action, kubeconfig: Path = REMEDIATOR) -> tuple[bool, str]:
    if a.type == "rollout_undo":  # `rollout undo` supports --dry-run=server too
        return _kubectl(kubectl_argv(ns, a) + ["--dry-run=server"], kubeconfig)
    return _kubectl(kubectl_argv(ns, a) + ["--dry-run=server", "-o", "name"], kubeconfig)


# -------------------------------------------------------------------- apply
def execute(facts_now: Facts, category: str, a: Action, key: str, approver: str, investigation_id: str,
            kubeconfig: Path = REMEDIATOR) -> dict:
    """Apply an approved action exactly once. `facts_now` must be a FRESH snapshot."""
    db = _db()
    row = db.execute("SELECT status, output, approver, finished_at FROM actions WHERE idempotency_key=?", (key,)).fetchone()
    if row:
        audit("duplicate_suppressed", key=key, investigation_id=investigation_id, approver=approver, first_status=row[0])
        return {"status": "duplicate", "first_status": row[0], "output": row[1], "first_approver": row[2],
                "finished_at": row[3]}
    if category in EVIDENCE_GATES and not evidence_ok(facts_now, category, a.target):
        audit("refused_stale", key=key, investigation_id=investigation_id,
              reason=f"{category} evidence no longer present on {a.target}")
        return {"status": "refused", "output": "state changed since the proposal: evidence is gone; re-investigate"}
    ok, out = dry_run(facts_now.ns, a, kubeconfig)
    audit("dry_run", key=key, ok=ok, output=out[:500])
    if not ok:
        return {"status": "refused", "output": f"server dry-run failed: {out}"}
    try:  # claim the key first; the UNIQUE constraint is the idempotency guarantee under concurrency
        db.execute("INSERT INTO actions VALUES (?,?,?,?,?,?,?,?)", (key, investigation_id, a.model_dump_json(), approver,
                   "applying", "", datetime.now(timezone.utc).isoformat(), None))
        db.commit()
    except sqlite3.IntegrityError:
        return {"status": "duplicate", "output": "another approval claimed this key first"}
    ok, out = _kubectl(kubectl_argv(facts_now.ns, a), kubeconfig)
    status = "applied" if ok else "failed"
    db.execute("UPDATE actions SET status=?, output=?, finished_at=? WHERE idempotency_key=?",
               (status, out[:2000], datetime.now(timezone.utc).isoformat(), key))
    db.commit()
    audit(status, key=key, investigation_id=investigation_id, approver=approver, action=a.model_dump(),
          kubectl=" ".join(kubectl_argv(facts_now.ns, a)), output=out[:500])
    return {"status": status, "output": out}


def verify_fix(ns: str, category: str, target: str, kubeconfig: Path = READER, timeout: int = 150) -> dict:
    """Poll until the gate signal is gone and nothing else is actively wrong."""
    from .cluster import collect_snapshot
    from .guard import ACTIVE_SIGNALS

    end, last = time.time() + timeout, []
    while time.time() < end:
        f = Facts(collect_snapshot(ns, str(kubeconfig)))
        last = sorted({f"{s.kind}@{s.subject}" for s in f.signals if s.kind in ACTIVE_SIGNALS})
        if not evidence_ok(f, category, target) and not last:
            return {"verified": True, "remaining_signals": []}
        time.sleep(10)
    return {"verified": False, "remaining_signals": last}
