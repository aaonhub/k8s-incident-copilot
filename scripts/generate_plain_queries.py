"""Second retrieval query set: plain-language reports with NO error codes, status names or
Kubernetes jargon (how a customer, not an SRE, describes a problem). Same leakage control as
generate_corpus.py: the prompt sees only the category's symptom brief, never ticket text.

    COPILOT_LLM_MODE=live python scripts/generate_plain_queries.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from copilot.llm import Gemini  # noqa: E402
from scripts.generate_corpus import TAXONOMY  # noqa: E402

PROMPT = """A customer of a cloud provider is reporting a problem. They are not a Kubernetes expert.
Write 2 different short reports (20-50 words each) based ONLY on this observed behaviour, translated into
plain language:

"{symptoms}"

Strict rules: no error codes, no exit codes, no Kubernetes status or object names (no CrashLoopBackOff,
ImagePullBackOff, OOMKilled, pod, node, probe, Service, Secret, PVC, 137, 429, x509, NCCL, Xid...), no
guess at the cause. Describe what they see and when. Invent names. Return a JSON array of 2 strings."""


def main() -> None:
    llm = Gemini("gemini-3.5-flash-lite", mode="live", tag="plain-queries")
    out = []
    only = sys.argv[sys.argv.index("--only") + 1] if "--only" in sys.argv else None
    path = ROOT / "eval" / "retrieval_queries_plain.jsonl"
    existing = [json.loads(x) for x in path.read_text().splitlines()] if only else []
    for cat, (symptoms, _) in TAXONOMY.items():
        if only and cat != only:
            continue
        r = llm.generate({"contents": [{"role": "user", "parts": [{"text": PROMPT.format(symptoms=symptoms)}]}],
                          "generationConfig": {"temperature": 1.0, "responseMimeType": "application/json",
                                               "responseSchema": {"type": "array", "items": {"type": "string"}}}})
        for q in json.loads(r.text)[:2]:
            out.append({"query": q, "category": cat, "split": "test-plain"})
    for i, q in enumerate(out):
        q["id"] = f"P-{len(existing) + i + 1:03d}"
    with path.open("w") as f:
        for q in existing + out:
            f.write(json.dumps(q) + "\n")
    print(len(out), "plain queries", file=sys.stderr)


if __name__ == "__main__":
    main()
