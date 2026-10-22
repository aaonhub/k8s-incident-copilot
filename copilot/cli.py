"""copilot CLI.

    copilot investigate                    # live cluster (read-only identity), prompts for approval
    copilot investigate --fixture eval/fixtures/oom-killed-v2.json.gz   # replay a captured incident
    copilot approve <id> --key <key> --approver you
    copilot audit verify | tail
    copilot kb "pods restart with exit code 137"
    copilot serve                          # HTTP API with the same approval flow
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import textwrap


def _print_report(r: dict) -> None:
    w = textwrap.TextWrapper(width=100, initial_indent="  ", subsequent_indent="  ")
    print(f"\n== Investigation {r['id']}  ({r['steps']} model turns, {r['tool_call_count']} tool calls, "
          f"{r['input_tokens']}+{r['output_tokens']} tokens, ${r['cost_usd']:.4f}, model {r['llm_latency_ms']}ms)")
    print(f"status:     {r['status']}   category: {r['category']}   target: {r['target'] or '-'}   "
          f"confidence: {r['confidence']:.2f}")
    print("summary:\n" + w.fill(r["summary"] or "-"))
    print("evidence (code-verified quotes):")
    bad = {(u["artifact_id"], u["claim"]) for u in r["unsupported_claims"]}
    for c in r["claims"]:
        mark = "x DROPPED" if (c["artifact_id"], c["statement"]) in bad else "ok"
        print(f"  [{mark}] {c['statement']}\n         <{c['artifact_id']}> \"{c['quote'][:140]}\"")
    if r["similar_cases"]:
        print("similar past cases: " + ", ".join(r["similar_cases"]))
    for why in r["guard_reasons"]:
        print(f"guard: {why}")
    a = r["final_action"]
    print(f"model proposed: {r['model_action']}   ->   offered: {a['type']} {a.get('target') or ''}")
    if a.get("rationale"):
        print(w.fill("why: " + a["rationale"]))


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="copilot")
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("investigate")
    i.add_argument("--namespace", default="shop")
    i.add_argument("--fixture")
    i.add_argument("--mode", choices=["live", "record", "replay"],
                   help="LLM mode (default: env COPILOT_LLM_MODE, else replay for fixtures / live for a cluster)")
    i.add_argument("--no-retrieval", action="store_true")
    i.add_argument("--no-prompt", action="store_true", help="never ask for approval")
    i.add_argument("--json", action="store_true")
    a = sub.add_parser("approve")
    a.add_argument("id")
    a.add_argument("--key", required=True)
    a.add_argument("--approver", required=True)
    a.add_argument("--reject", action="store_true")
    au = sub.add_parser("audit")
    au.add_argument("what", choices=["verify", "tail"])
    k = sub.add_parser("kb")
    k.add_argument("query")
    k.add_argument("--mode", default="hybrid", choices=["bm25", "dense", "hybrid"])
    ru = sub.add_parser("rules", help="deterministic baseline only (no model, no key)")
    ru.add_argument("--namespace", default="shop")
    ru.add_argument("--fixture")
    ru.add_argument("--expect", help="exit 1 unless the rules diagnose this category")
    s = sub.add_parser("serve")
    s.add_argument("--port", type=int, default=8088)
    args = ap.parse_args(argv)

    if args.cmd == "investigate":
        from .service import decide, run_investigation

        mode = args.mode or os.environ.get("COPILOT_LLM_MODE") or ("replay" if args.fixture else "live")
        r = run_investigation(args.namespace, args.fixture, mode, not args.no_retrieval)
        if args.json:
            print(json.dumps(r, indent=1, default=str))
        else:
            _print_report(r)
        if r.get("idempotency_key") and not args.fixture and not args.no_prompt and sys.stdin.isatty():
            key = r["idempotency_key"]
            print(f"\nApproval required. Idempotency key: {key}")
            ans = input(f"Type 'approve {key[:8]}' to apply, anything else to reject: ").strip()
            who = os.environ.get("USER", "unknown")
            res = decide(r["id"], who, "approve" if ans == f"approve {key[:8]}" else "reject", key)
            print(json.dumps(res, indent=1))
        elif r.get("idempotency_key"):
            print(f"\npending approval: copilot approve {r['id']} --key {r['idempotency_key']} --approver <you>")
    elif args.cmd == "approve":
        from .service import decide

        print(json.dumps(decide(args.id, args.approver, "reject" if args.reject else "approve", args.key), indent=1))
    elif args.cmd == "audit":
        from .actions import AUDIT_LOG, verify_audit

        if args.what == "verify":
            ok, n, msg = verify_audit()
            print(f"{'OK' if ok else 'TAMPERED'}: {n} records, {msg}")
            sys.exit(0 if ok else 1)
        for line in AUDIT_LOG.read_text().splitlines()[-20:] if AUDIT_LOG.exists() else []:
            rec = json.loads(line)
            print(rec["ts"], rec["event"], {k: v for k, v in rec.items() if k not in ("ts", "event", "hash", "prev_hash")})
    elif args.cmd == "kb":
        from .retrieval import KnowledgeBase

        for h in KnowledgeBase().search(args.query, args.mode, 5):
            print(f"{h.rank}. {h.id:28} {h.doc.category:28} {h.score:.4f}  {h.doc.title[:70]}")
    elif args.cmd == "rules":
        from pathlib import Path

        from . import rules
        from .actions import READER
        from .cluster import collect_snapshot, load_snapshot
        from .facts import Facts

        snap = load_snapshot(Path(args.fixture)) if args.fixture else collect_snapshot(
            args.namespace, str(READER) if READER.exists() else None)
        f = Facts(snap)
        g = rules.diagnose(f)
        for sig in f.signals:
            print(sig.render())
        print(f"rules: {g.status} {g.category} {g.target} -> {g.final_action.type} {g.final_action.target}")
        if args.expect and g.category != args.expect:
            sys.exit(f"expected {args.expect}, got {g.category}")
    elif args.cmd == "serve":
        import uvicorn

        from .api import app

        uvicorn.run(app, host="0.0.0.0", port=args.port)


if __name__ == "__main__":
    main()
