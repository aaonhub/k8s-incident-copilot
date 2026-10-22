"""Minimal span tracer: one JSONL line per finished span, OpenTelemetry-shaped.

Fields follow OTel naming (trace_id, span_id, parent_span_id, name, start/end in
unix nanos, attributes) so the file can be converted to OTLP later; see README
"What I'd harden" for why this isn't the OTel SDK yet.
"""
from __future__ import annotations

import json
import secrets
import time
from contextlib import contextmanager
from pathlib import Path


class Tracer:
    def __init__(self, path: Path | None = None, trace_id: str | None = None):
        self.trace_id = trace_id or secrets.token_hex(16)
        self.path = path
        self.spans: list[dict] = []
        self._stack: list[str] = []

    @contextmanager
    def span(self, name: str, **attrs):
        span_id = secrets.token_hex(8)
        rec = {"trace_id": self.trace_id, "span_id": span_id,
               "parent_span_id": self._stack[-1] if self._stack else None,
               "name": name, "start_unix_nano": time.time_ns(), "attributes": dict(attrs), "status": "OK"}
        self._stack.append(span_id)
        try:
            yield rec["attributes"]
        except Exception as e:
            rec["status"] = "ERROR"
            rec["attributes"]["error"] = f"{type(e).__name__}: {e}"
            raise
        finally:
            self._stack.pop()
            rec["end_unix_nano"] = time.time_ns()
            rec["duration_ms"] = round((rec["end_unix_nano"] - rec["start_unix_nano"]) / 1e6, 1)
            self.spans.append(rec)
            if self.path:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a") as f:
                    f.write(json.dumps(rec, default=str) + "\n")

    def totals(self) -> dict:
        llm = [s for s in self.spans if s["name"] == "llm.generate"]
        return {
            "llm_calls": len(llm),
            "input_tokens": sum(s["attributes"].get("input_tokens", 0) for s in llm),
            "output_tokens": sum(s["attributes"].get("output_tokens", 0) for s in llm),
            "cost_usd": round(sum(s["attributes"].get("cost_usd", 0) for s in llm), 6),
            "llm_latency_ms": sum(s["attributes"].get("latency_ms", 0) for s in llm),
            "tool_call_count": sum(1 for s in self.spans if s["name"].startswith("tool.")),
        }
