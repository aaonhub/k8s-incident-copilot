"""Detectors on real captured snapshots: every incident must raise the signal its scenario waits for."""
import pytest
from conftest import FIXTURES

from copilot import incidents
from copilot.facts import parse_prom, parse_quantity

ALL = sorted(p.name.removesuffix(".json.gz") for p in FIXTURES.glob("*.json.gz"))


@pytest.mark.parametrize("name", ALL)
def test_expected_signal_present(load, name):
    f = load(name)
    lab = f.snap["label"]
    v = incidents.SCENARIOS[lab["scenario"]].variants[lab["variant"] - 1]
    kinds = {s.kind for s in f.signals}
    if v.wait_for:
        assert v.wait_for in kinds, f"{name}: {sorted(kinds)}"
    else:  # healthy / recovered: nothing actively wrong
        from copilot.guard import ACTIVE_SIGNALS

        assert not (kinds & ACTIVE_SIGNALS), f"{name}: {sorted(kinds)}"


@pytest.mark.parametrize("name", ALL)
def test_every_signal_cites_existing_artifacts(load, name):
    f = load(name)
    for s in f.signals:
        assert s.artifact_ids and all(i in f.artifacts for i in s.artifact_ids)


def test_rollout_diff_shows_the_change(load):
    f = load("bad-image-tag-v1")
    assert "1.0.404" in f.artifacts["rollout/webapp"].text


def test_stale_events_are_marked(load):
    f = load("recovered-v1")
    assert any("(object no longer exists)" in a.text for a in f.artifacts.values() if a.kind == "event")


def test_quantities_and_prom():
    assert parse_quantity("128Mi") == 128 * 2**20
    assert parse_quantity("250m") == 0.25
    assert parse_quantity("2") == 2.0
    p = parse_prom('shop_requests_total{role="webapp"} 5\ncontainer_cpu_nr_throttled 3\n')
    assert p["shop_requests_total"] == 5 and p["container_cpu_nr_throttled"] == 3
