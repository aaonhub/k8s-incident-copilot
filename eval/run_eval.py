"""Evaluation harness. Default: replay mode (recorded model responses, no key, no cost).

    python eval/run_eval.py                    # replay; writes eval/results.{json,md} and updates README
    COPILOT_LLM_MODE=record python eval/run_eval.py   # live model calls, recorded for replay

Part 1  Retrieval   held-out queries x {BM25, dense, hybrid}: Recall@5, MRR, Hit@5.
Part 2  Diagnosis   captured incident snapshots x {rules, LLM without retrieval, LLM + retrieval}:
                    accuracy, action match, unsafe actions, unsupported claims, latency, tokens, cost.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from copilot import rules  # noqa: E402
from copilot.agent import investigate  # noqa: E402
from copilot.cluster import load_snapshot  # noqa: E402
from copilot.facts import Facts  # noqa: E402
from copilot.guard import MUTATING, Action, GuardResult, validate_action  # noqa: E402
from copilot.llm import Gemini  # noqa: E402
from copilot.retrieval import KnowledgeBase, hit_at_k, recall_at_k, reciprocal_rank  # noqa: E402
from copilot.trace import Tracer  # noqa: E402

EVAL = ROOT / "eval"
MODES = ["bm25", "dense", "hybrid"]


# ------------------------------------------------------------------ retrieval
def retrieval_eval(kb: KnowledgeBase) -> dict:
    sets = {
        "first reports (dev)": [q for q in _jsonl(EVAL / "retrieval_queries.jsonl") if q["split"] == "dev"],
        "first reports (test)": [q for q in _jsonl(EVAL / "retrieval_queries.jsonl") if q["split"] == "test"],
        "plain language (test)": _jsonl(EVAL / "retrieval_queries_plain.jsonl"),
    }
    out = {}
    for name, qs in sets.items():
        row = {"n": len(qs)}
        for mode in MODES:
            r = m = h = 0.0
            for q in qs:
                rel = {d.id for d in kb.docs if d.category == q["category"]}
                ranked = [x.id for x in kb.search(q["query"], mode, top_k=50)]
                r += recall_at_k(ranked, rel, 5)
                m += reciprocal_rank(ranked, rel)
                h += hit_at_k(ranked, rel, 5)
            row[mode] = {"recall@5": r / len(qs), "mrr": m / len(qs), "hit@5": h / len(qs)}
        out[name] = row
    return out


def _jsonl(p: Path) -> list[dict]:
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]


# ------------------------------------------------------------------ diagnosis
def score(label: dict, g: GuardResult, facts: Facts) -> dict:
    gold_cat, gold_tgt = label["category"], label["target"]
    if gold_cat == "no_incident":
        correct = g.status == "no_incident"
    else:
        correct = g.status == "incident" and g.category == gold_cat and g.target == gold_tgt
    a = g.final_action
    if a.type in MUTATING:
        action_ok = a.type in label["accept"] and a.target == gold_tgt
        # unsafe = an action reaching the approval prompt that is off-list, aimed at the wrong object,
        # or offered when the expert says no automated action is appropriate
        unsafe = (validate_action(facts, g.category, g.target, a) is not None or a.target != gold_tgt
                  or not (set(label["accept"]) & MUTATING))
    else:
        action_ok = a.type in label["accept"]
        unsafe = False
    return {"correct": correct, "action_ok": action_ok, "unsafe": unsafe,
            "abstained": g.status == "insufficient_evidence", "final_action": f"{a.type}:{a.target}" if a.type in MUTATING else a.type}


def model_unsafe(label: dict, raw_action: str) -> bool:
    """The model's own proposal, before the guard. Same definition as `unsafe`."""
    t, _, tgt = raw_action.partition(":")
    if t not in MUTATING:
        return False
    return tgt != label["target"] or t not in label["accept"]


def run_rules(f: Facts) -> GuardResult:
    g = rules.diagnose(f)
    if g.final_action.type in MUTATING:
        why = validate_action(f, g.category, g.target, g.final_action)
        if why:
            g.reasons.append(f"action blocked: {why}")
            g.final_action = Action(type="handoff")
    return g


def diagnosis_eval(kb: KnowledgeBase, mode: str) -> list[dict]:
    fixtures = sorted((EVAL / "fixtures").glob("*.json.gz"))
    rows = []
    for fx in fixtures:
        snap = load_snapshot(fx)
        label = snap["label"]
        facts = Facts(snap)
        t0 = time.perf_counter()
        g = run_rules(facts)
        rows.append({"fixture": fx.name, "system": "rules", **score(label, g, facts), "category": g.category,
                     "target": g.target, "latency_ms": (time.perf_counter() - t0) * 1000, "cost_usd": 0.0,
                     "input_tokens": 0, "output_tokens": 0, "claims": 0, "unsupported": 0, "model_unsafe": False,
                     "steps": 0, "reasons": g.reasons, "label": label})
        # The no-retrieval ablation runs on the held-out split only (keeps the eval under the $3 budget).
        systems = (("llm_no_retrieval", False), ("llm_retrieval_guarded", True)) if label["split"] == "test" \
            else (("llm_retrieval_guarded", True),)
        for system, use_kb in systems:
            tr = Tracer(ROOT / "runs" / "eval_traces" / f"{fx.name.removesuffix('.json.gz')}--{system}.jsonl")
            if tr.path.exists():
                tr.path.unlink()
            inv = investigate(facts, Gemini(mode=mode, tag=f"eval:{fx.name}:{system}"), kb if use_kb else None,
                              use_kb, tr)
            g, t = inv.guard, tr.totals()
            n_claims = len(g.diagnosis.claims) if g.diagnosis else 0
            rows.append({"fixture": fx.name, "system": system, **score(label, g, facts), "category": g.category,
                         "target": g.target, "latency_ms": t["llm_latency_ms"] + snap.get("collect_seconds", 0) * 1000,
                         "cost_usd": t["cost_usd"], "input_tokens": t["input_tokens"], "output_tokens": t["output_tokens"],
                         "claims": n_claims, "unsupported": len(g.unsupported_claims),
                         "model_unsafe": model_unsafe(label, g.raw_action), "model_action": g.raw_action,
                         "model_status": g.diagnosis.status if g.diagnosis else "invalid",
                         "model_category": g.diagnosis.category if g.diagnosis else "invalid",
                         "steps": inv.steps, "stop": inv.stop_reason, "reasons": g.reasons, "label": label,
                         "retrieved": inv.retrieved, "tool_calls": [c["tool"] for c in inv.tool_calls]})
            print(f"  {fx.name:34} {system:22} correct={rows[-1]['correct']!s:5} action={rows[-1]['final_action']:32} "
                  f"${t['cost_usd']:.4f}", file=sys.stderr)
        r = rows[-1 - len(systems)]
        print(f"  {fx.name:34} {'rules':22} correct={r['correct']!s:5} action={r['final_action']}", file=sys.stderr)
    return rows


def summarize(rows: list[dict], split: str) -> dict:
    summary = {}
    for system in ("rules", "llm_no_retrieval", "llm_retrieval_guarded"):
        rs = [r for r in rows if r["system"] == system and (split == "all" or r["label"]["split"] == split)]
        if not rs:
            continue
        lat = sorted(r["latency_ms"] for r in rs)
        offered = [r for r in rs if r["final_action"].split(":")[0] in MUTATING]
        claims = sum(r["claims"] for r in rs)
        summary[system] = {
            "n": len(rs),
            "accuracy": sum(r["correct"] for r in rs) / len(rs),
            "action_match": sum(r["action_ok"] for r in rs) / len(rs),
            "handoff_or_abstain": sum(r["abstained"] or r["final_action"] == "handoff" for r in rs) / len(rs),
            "actions_offered": len(offered),
            "unsafe_actions_offered": sum(r["unsafe"] for r in rs),
            "model_unsafe_proposals": sum(r["model_unsafe"] for r in rs),
            "claims": claims,
            "unsupported_claim_rate": (sum(r["unsupported"] for r in rs) / claims) if claims else None,
            "latency_p50_ms": statistics.median(lat),
            "latency_p95_ms": lat[min(len(lat) - 1, int(0.95 * len(lat)))],
            "mean_cost_usd": sum(r["cost_usd"] for r in rs) / len(rs),
            "mean_tokens": sum(r["input_tokens"] + r["output_tokens"] for r in rs) / len(rs),
        }
    return summary


def agent_retrieval(rows: list[dict], kb: KnowledgeBase) -> dict:
    """Did the agent's own auto-query (built from detector signals) surface a same-category case?"""
    rs = [r for r in rows if r["system"] == "llm_retrieval_guarded" and r["label"]["category"] != "no_incident"]
    hits = [hit_at_k(r["retrieved"], {d.id for d in kb.docs if d.category == r["label"]["category"]}, 5) for r in rs]
    return {"n": len(rs), "hit@5": sum(hits) / len(hits) if hits else None}


# -------------------------------------------------------------------- report
def pct(x) -> str:
    return "n/a" if x is None else f"{100 * x:.0f}%"


def render(ret: dict, diags: dict, agent_ret: dict, model: str, counts: dict) -> str:
    L = ["### Retrieval (synthetic corpus: 154 tickets + 22 runbooks, relevance = same root-cause category)", "",
         "| Query set | n | BM25 R@5 / MRR | Dense R@5 / MRR | Hybrid (RRF) R@5 / MRR |", "|---|---|---|---|---|"]
    for name, row in ret.items():
        cells = [f"{row[m]['recall@5']:.2f} / {row[m]['mrr']:.2f}" for m in MODES]
        L.append(f"| {name} | {row['n']} | " + " | ".join(cells) + " |")
    L += ["", f"Agent's own auto-query on the incident snapshots (built from detector signals): Hit@5 = "
          f"{pct(agent_ret['hit@5'])} (n={agent_ret['n']}).", ""]
    names = {"rules": "Deterministic rules", "llm_no_retrieval": "LLM, no retrieval",
             "llm_retrieval_guarded": "LLM + retrieval (full agent)"}
    for split, title in (("test", "held-out"), ("dev", "dev, used while building")):
        diag = diags[split]
        L += [f"### Diagnosis on captured incidents: {title} ({counts[split]} snapshots, model `{model}`)", "",
              "| System | Diagnosis accuracy | Action matches expert | Handoff / abstain | Unsafe actions offered "
              "| Model's own unsafe proposals | Unsupported claims (pre-guard) | Latency p50 / p95 | Cost / investigation |",
              "|---|---|---|---|---|---|---|---|---|"]
        for s, m in diag.items():
            unsup = "n/a" if m["unsupported_claim_rate"] is None else f"{pct(m['unsupported_claim_rate'])} of {m['claims']}"
            lat = ("< 1 ms" if s == "rules" and m["latency_p95_ms"] < 1 else
                   f"{m['latency_p50_ms']:.1f} / {m['latency_p95_ms']:.1f} ms" if s == "rules"
                   else f"{m['latency_p50_ms'] / 1000:.1f} / {m['latency_p95_ms'] / 1000:.1f} s")
            L.append(f"| {names[s]} | {pct(m['accuracy'])} | {pct(m['action_match'])} | {pct(m['handoff_or_abstain'])} "
                     f"| {m['unsafe_actions_offered']} of {m['actions_offered']} | "
                     f"{'n/a' if s == 'rules' else m['model_unsafe_proposals']} | {unsup} | {lat} | "
                     f"${m['mean_cost_usd']:.4f} |")
        L.append("")
    return "\n".join(L).rstrip()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default=None, help="LLM mode: replay (default) | record | live")
    ap.add_argument("--no-readme", action="store_true")
    ap.add_argument("--prune", action="store_true", help="delete recordings this run did not use")
    a = ap.parse_args()
    import os

    mode = a.mode or os.environ.get("COPILOT_LLM_MODE", "replay")
    kb = KnowledgeBase()
    ret = retrieval_eval(kb)
    rows = diagnosis_eval(kb, mode)
    agent_ret = agent_retrieval(rows, kb)
    diags = {sp: summarize(rows, sp) for sp in ("test", "dev", "all")}
    counts = {sp: len({r["fixture"] for r in rows if sp == "all" or r["label"]["split"] == sp}) for sp in diags}
    md = render(ret, diags, agent_ret, Gemini().model, counts)
    (EVAL / "results.json").write_text(json.dumps(
        {"retrieval": ret, "agent_retrieval": agent_ret, "diagnosis": diags, "rows": rows}, indent=1, default=str))
    (EVAL / "results.md").write_text(md + "\n")
    print(md)
    if a.prune:
        from copilot.llm import RECORDINGS, USED_RECORDINGS

        stale = [p for p in RECORDINGS.glob("*.json") if p.name not in USED_RECORDINGS]
        for p in stale:
            p.unlink()
        print(f"pruned {len(stale)} unused recordings, kept {len(USED_RECORDINGS)}", file=sys.stderr)
    readme = ROOT / "README.md"
    if not a.no_readme and readme.exists():
        txt = readme.read_text()
        s, e = "<!-- RESULTS:START -->", "<!-- RESULTS:END -->"
        if s in txt and e in txt:
            readme.write_text(txt[: txt.index(s) + len(s)] + "\n" + md + "\n" + txt[txt.index(e):])


if __name__ == "__main__":
    main()
