"""The full agent loop, replayed from recorded model responses: no key, no network, deterministic."""
import pytest

from copilot.agent import investigate
from copilot.llm import Gemini, MissingRecording
from copilot.retrieval import KnowledgeBase


@pytest.mark.parametrize("name", ["bad-image-tag-v2", "missing-secret-v2", "recovered-v1"])
def test_replayed_investigation(load, name):
    f = load(name)
    inv = investigate(f, Gemini(mode="replay"), KnowledgeBase(), True)
    lab = f.snap["label"]
    r = inv.report()
    assert r["cost_usd"] > 0 and r["input_tokens"] > 0  # recorded usage is replayed too
    assert inv.trace.spans[-1]["name"] == "investigate"
    if lab["category"] == "missing_secret":
        assert r["final_action"]["type"] == "handoff"


def test_unrecorded_request_is_an_error_not_a_live_call(load):
    f = load("healthy-v1")
    f.snap["captured_at"] = "2030-01-01T00:00:00+00:00"  # changes the prompt -> no recording
    with pytest.raises(MissingRecording):
        investigate(type(f)(f.snap), Gemini(mode="replay"), None, False)
