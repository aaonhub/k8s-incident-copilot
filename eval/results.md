### Retrieval (synthetic corpus: 154 tickets + 22 runbooks, relevance = same root-cause category)

| Query set | n | BM25 R@5 / MRR | Dense R@5 / MRR | Hybrid (RRF) R@5 / MRR |
|---|---|---|---|---|
| first reports (dev) | 22 | 0.56 / 0.82 | 0.42 / 0.62 | 0.55 / 0.87 |
| first reports (test) | 44 | 0.64 / 0.90 | 0.51 / 0.76 | 0.62 / 0.88 |
| plain language (test) | 44 | 0.28 / 0.50 | 0.27 / 0.44 | 0.32 / 0.63 |

Agent's own auto-query on the incident snapshots (built from detector signals): Hit@5 = 93% (n=27).

### Diagnosis on captured incidents: held-out (18 snapshots, model `gemini-3.5-flash`)

| System | Diagnosis accuracy | Action matches expert | Handoff / abstain | Unsafe actions offered | Model's own unsafe proposals | Unsupported claims (pre-guard) | Latency p50 / p95 | Cost / investigation |
|---|---|---|---|---|---|---|---|---|
| Deterministic rules | 83% | 83% | 17% | 0 of 12 | n/a | n/a | < 1 ms | $0.0000 |
| LLM, no retrieval | 100% | 100% | 11% | 0 of 14 | 0 | 0% of 44 | 7.4 / 14.6 s | $0.0303 |
| LLM + retrieval (full agent) | 100% | 100% | 11% | 0 of 14 | 0 | 0% of 48 | 8.8 / 25.9 s | $0.0377 |

### Diagnosis on captured incidents: dev, used while building (13 snapshots, model `gemini-3.5-flash`)

| System | Diagnosis accuracy | Action matches expert | Handoff / abstain | Unsafe actions offered | Model's own unsafe proposals | Unsupported claims (pre-guard) | Latency p50 / p95 | Cost / investigation |
|---|---|---|---|---|---|---|---|---|
| Deterministic rules | 77% | 77% | 15% | 0 of 7 | n/a | n/a | < 1 ms | $0.0000 |
| LLM + retrieval (full agent) | 100% | 100% | 15% | 0 of 9 | 0 | 0% of 31 | 9.7 / 12.6 s | $0.0360 |
