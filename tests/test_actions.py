"""Approval plumbing: idempotency, audit hash chain, refusal on stale state. kubectl is faked."""
import json

from copilot import actions
from copilot.guard import Action


def fake_kubectl(calls):
    def _k(argv, kubeconfig):
        calls.append(argv)
        return True, "deployment.apps/webapp rolled back"
    return _k


def test_execute_is_idempotent(load, monkeypatch):
    calls = []
    monkeypatch.setattr(actions, "_kubectl", fake_kubectl(calls))
    f = load("bad-image-tag-v1")
    a = Action(type="rollout_undo", target="deployment/webapp")
    key = actions.idempotency_key(f, a)
    assert key == actions.idempotency_key(f, a)  # stable
    r1 = actions.execute(f, "image_pull_error", a, key, "sam", "inv1")
    r2 = actions.execute(f, "image_pull_error", a, key, "alex", "inv2")
    assert r1["status"] == "applied" and r2["status"] == "duplicate"
    real = [c for c in calls if "--dry-run=server" not in c]
    assert real == [["rollout", "undo", "deployment/webapp", "-n", "shop"]]  # applied exactly once


def test_refuses_when_evidence_is_gone(load, monkeypatch):
    calls = []
    monkeypatch.setattr(actions, "_kubectl", fake_kubectl(calls))
    healthy = load("healthy-v1")
    a = Action(type="rollout_undo", target="deployment/webapp")
    r = actions.execute(healthy, "image_pull_error", a, "k1", "sam", "inv1")
    assert r["status"] == "refused" and calls == []


def test_audit_chain_detects_tampering():
    for i in range(3):
        actions.audit("proposed", n=i)
    assert actions.verify_audit()[0]
    lines = actions.AUDIT_LOG.read_text().splitlines()
    rec = json.loads(lines[1])
    rec["n"] = 99
    lines[1] = json.dumps(rec)
    actions.AUDIT_LOG.write_text("\n".join(lines) + "\n")
    ok, n, msg = actions.verify_audit()
    assert not ok and n == 2


def test_audit_chain_detects_deletion():
    for i in range(3):
        actions.audit("proposed", n=i)
    lines = actions.AUDIT_LOG.read_text().splitlines()
    actions.AUDIT_LOG.write_text("\n".join([lines[0], lines[2]]) + "\n")
    assert not actions.verify_audit()[0]


def test_kubectl_argv():
    a = Action(type="set_resources", target="deployment/webapp", container="webapp", memory_limit="256Mi")
    assert actions.kubectl_argv("shop", a) == ["set", "resources", "deployment/webapp", "-n", "shop",
                                               "--limits=memory=256Mi", "-c", "webapp"]
    s = Action(type="patch_service_selector", target="service/webapp", selector_label="app.kubernetes.io/name=webapp")
    assert json.loads(actions.kubectl_argv("shop", s)[-1]) == {"spec": {"selector": {"app.kubernetes.io/name": "webapp"}}}
