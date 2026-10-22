"""Stage every incident variant on the live k3d cluster and save a snapshot fixture.

For each scenario/variant: reset -> inject -> wait for the detector signal that
proves the incident is live -> settle -> snapshot with the READ-ONLY identity
-> save eval/fixtures/<scenario>-v<n>.json.gz with the expert labels.

    python eval/capture.py                 # everything (~40 min)
    python eval/capture.py oom-killed:2    # one variant
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from copilot import incidents  # noqa: E402
from copilot.cluster import collect_snapshot, save_snapshot  # noqa: E402

FIXTURES = ROOT / "eval" / "fixtures"
READER = str(ROOT / ".kube" / "copilot-reader.yaml")
# variant 1 of every scenario is the dev split (used while writing prompts/rules); the rest is held out.


def capture(name: str, idx: int) -> dict:
    v = incidents.SCENARIOS[name].variants[idx - 1]
    incidents.reset()
    t0 = time.time()
    incidents.inject(name, idx)
    ok = incidents.wait_for_signal(v.wait_for, timeout=300, kubeconfig=READER)
    time.sleep(v.settle)
    snap = collect_snapshot(incidents.NS, READER)
    snap["label"] = {
        "scenario": name, "variant": idx, "split": "dev" if idx == 1 else "test",
        "category": v.category, "target": v.target, "accept": v.accept, "gold": v.gold,
        "manifested": ok, "inject_to_snapshot_s": round(time.time() - t0, 1), "note": v.note,
    }
    save_snapshot(snap, FIXTURES / f"{name}-v{idx}.json.gz")
    return snap["label"]


def main() -> None:
    todo = []
    if len(sys.argv) > 1:
        for arg in sys.argv[1:]:
            n, _, i = arg.partition(":")
            todo += [(n, int(i))] if i else [(n, k + 1) for k in range(len(incidents.SCENARIOS[n].variants))]
    else:
        todo = [(s.name, k + 1) for s in incidents.S for k in range(len(s.variants))]
    for n, i in todo:
        try:
            lab = capture(n, i)
            print(json.dumps(lab), flush=True)
        except Exception as e:  # noqa: BLE001 - keep capturing the rest
            print(json.dumps({"scenario": n, "variant": i, "error": str(e)[:300]}), flush=True)
    incidents.reset(wait=False)


if __name__ == "__main__":
    main()
