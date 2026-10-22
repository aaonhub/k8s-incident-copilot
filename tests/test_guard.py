"""The safety contract. Each test is one way a model can be wrong; the guard must catch it."""
from copilot.guard import apply_guard


def base(**over):
    d = {"status": "incident", "category": "image_pull_error", "target": "deployment/webapp",
         "summary": "bad tag", "confidence": 0.9, "claims": [], "similar_cases": [],
         "action": {"type": "rollout_undo", "target": "deployment/webapp", "rationale": "revert"}}
    d.update(over)
    return d


def seen_for(f):
    return {k: a.text for k, a in f.artifacts.items()}


def claim_from(f, art_id, needle):
    assert needle in f.artifacts[art_id].text
    return {"statement": "s", "artifact_id": art_id, "quote": needle}


def pod_id(f, deployment="webapp", current=True):
    return next(a.id for a in f.artifacts.values() if a.kind == "pod" and a.subject == f"deployment/{deployment}"
                and ("current revision" in a.text) == current)


def test_grounded_diagnosis_is_offered(load):
    f = load("bad-image-tag-v1")
    c = claim_from(f, "rollout/webapp", "1.0.404")
    g = apply_guard(f, base(claims=[c]), seen_for(f))
    assert g.final_action.type == "rollout_undo" and not g.reasons


def test_fabricated_quote_is_dropped_and_blocks_action(load):
    f = load("bad-image-tag-v1")
    bad = {"statement": "s", "artifact_id": "rollout/webapp", "quote": "image 9.9.9 was deleted from the registry"}
    g = apply_guard(f, base(claims=[bad]), seen_for(f))
    assert len(g.unsupported_claims) == 1
    assert g.final_action.type == "handoff" and g.status == "insufficient_evidence"


def test_citing_unseen_artifact_is_unsupported(load):
    f = load("bad-image-tag-v1")
    c = claim_from(f, "rollout/webapp", "1.0.404")
    g = apply_guard(f, base(claims=[c]), {})  # model was shown nothing
    assert g.unsupported_claims and g.final_action.type == "handoff"


def test_evidence_gate_blocks_wrong_category(load):
    f = load("bad-image-tag-v1")  # no OOMKilled anywhere
    c = claim_from(f, "rollout/webapp", "1.0.404")
    d = base(category="oom_killed", claims=[c],
             action={"type": "set_resources", "target": "deployment/webapp", "container": "webapp",
                     "memory_limit": "512Mi", "rationale": "x"})
    g = apply_guard(f, d, seen_for(f))
    assert g.final_action.type == "handoff" and "conflicting evidence" in g.reasons[0]


def test_action_must_target_diagnosed_object(load):
    f = load("bad-image-tag-v1")
    c = claim_from(f, "rollout/webapp", "1.0.404")
    d = base(claims=[c], action={"type": "rollout_undo", "target": "deployment/inventory", "rationale": "x"})
    assert apply_guard(f, d, seen_for(f)).final_action.type == "handoff"


def test_resources_can_only_go_up_and_are_capped(load):
    f = load("oom-killed-v3")
    pid = pod_id(f)
    c = claim_from(f, pid, "OOMKilled")
    for mem in ("32Mi", "4Gi"):
        d = base(category="oom_killed", claims=[c], action={
            "type": "set_resources", "target": "deployment/webapp", "container": "webapp", "memory_limit": mem,
            "rationale": "x"})
        g = apply_guard(f, d, seen_for(f))
        assert g.final_action.type == "handoff", mem
    d = base(category="oom_killed", claims=[c], action={
        "type": "set_resources", "target": "deployment/webapp", "container": "webapp", "memory_limit": "256Mi",
        "rationale": "x"})
    assert apply_guard(f, d, seen_for(f)).final_action.type == "set_resources"


def test_missing_secret_never_gets_an_automatic_action(load):
    f = load("missing-secret-v1")
    pid = pod_id(f)
    c = claim_from(f, pid, "CreateContainerConfigError")
    d = base(category="missing_secret", claims=[c],
             action={"type": "rollout_undo", "target": "deployment/webapp", "rationale": "x"})
    g = apply_guard(f, d, seen_for(f))
    assert g.final_action.type == "handoff" and "not permitted" in g.reasons[0]


def test_selector_patch_must_match_live_pods(load):
    f = load("bad-service-selector-v1")
    c = claim_from(f, "svc/webapp", "matching selector=0")
    for label, ok in (("app.kubernetes.io/name=webapp", True), ("app.kubernetes.io/name=web", False),
                      ("app=webapp", False)):
        d = base(category="service_selector_mismatch", target="service/webapp", claims=[c], action={
            "type": "patch_service_selector", "target": "service/webapp", "selector_label": label, "rationale": "x"})
        g = apply_guard(f, d, seen_for(f))
        assert (g.final_action.type == "patch_service_selector") is ok, label


def test_no_incident_contradicted_by_detectors(load):
    f = load("oom-killed-v1")
    d = base(status="no_incident", category="no_incident", target="", action={"type": "none", "rationale": "x"})
    g = apply_guard(f, d, seen_for(f))
    assert g.status == "insufficient_evidence" and g.final_action.type == "handoff"


def test_low_confidence_hands_off(load):
    f = load("bad-image-tag-v1")
    c = claim_from(f, "rollout/webapp", "1.0.404")
    assert apply_guard(f, base(claims=[c], confidence=0.3), seen_for(f)).final_action.type == "handoff"


def test_malformed_output_hands_off(load):
    f = load("bad-image-tag-v1")
    g = apply_guard(f, {"status": "incident"}, {})
    assert g.final_action.type == "handoff" and g.status == "insufficient_evidence"
