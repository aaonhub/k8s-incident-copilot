"""Gemini client with a spend cap and record/replay.

Modes (env COPILOT_LLM_MODE or constructor arg):
  replay  - default. Answers come from eval/recordings/. No key, no network, no cost.
            A request that was never recorded is an error, not a silent live call.
  record  - replay if this exact request was recorded before, else live call + save.
  live    - live call, nothing saved.

The recording key is a hash of the exact request body, so a replay only matches
if the prompt, tools and prior turns are byte-identical to the recorded run.

Spend: every live call appends its token counts and dollar cost to
runs/spend.jsonl. Before each live call the running total is checked against
COPILOT_SPEND_CAP_USD (default 3.00); over the cap, the call is refused.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
RECORDINGS = ROOT / "eval" / "recordings"
SPEND_LOG = ROOT / "runs" / "spend.jsonl"
API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# USD per 1M tokens (input, output incl. thinking). Google AI Studio list prices, Oct 2026.
PRICES = {
    "gemini-3.5-flash": (1.50, 9.00),
    "gemini-3.5-flash-lite": (0.30, 2.50),
}
DEFAULT_MODEL = os.environ.get("COPILOT_MODEL", "gemini-3.5-flash")


USED_RECORDINGS: set[str] = set()  # file names touched this process (for pruning stale recordings)


class BudgetExceeded(RuntimeError):
    pass


class MissingRecording(RuntimeError):
    pass


@dataclass
class LLMResult:
    raw: dict
    input_tokens: int
    output_tokens: int  # candidates + thinking
    cost_usd: float
    latency_ms: int
    replayed: bool

    @property
    def parts(self) -> list[dict]:
        cands = self.raw.get("candidates") or [{}]
        return (cands[0].get("content") or {}).get("parts") or []

    @property
    def content(self) -> dict:
        cands = self.raw.get("candidates") or [{}]
        return cands[0].get("content") or {"role": "model", "parts": []}

    @property
    def text(self) -> str:
        return "".join(p.get("text", "") for p in self.parts if not p.get("thought"))


def spent_usd() -> float:
    if not SPEND_LOG.exists():
        return 0.0
    return sum(json.loads(line)["cost_usd"] for line in SPEND_LOG.read_text().splitlines() if line.strip())


def cost_of(model: str, usage: dict) -> tuple[int, int, float]:
    pin, pout = PRICES.get(model, PRICES["gemini-3.5-flash"])
    tin = usage.get("promptTokenCount", 0)
    tout = usage.get("candidatesTokenCount", 0) + usage.get("thoughtsTokenCount", 0)
    return tin, tout, (tin * pin + tout * pout) / 1e6


class Gemini:
    def __init__(self, model: str = DEFAULT_MODEL, mode: str | None = None, cap_usd: float | None = None,
                 tag: str = ""):
        self.model = model
        self.mode = mode or os.environ.get("COPILOT_LLM_MODE", "replay")
        self.cap = cap_usd if cap_usd is not None else float(os.environ.get("COPILOT_SPEND_CAP_USD", "3.00"))
        self.tag = tag

    def _key(self, body: dict) -> str:
        blob = json.dumps({"model": self.model, "body": body}, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(blob.encode()).hexdigest()

    def generate(self, body: dict) -> LLMResult:
        key = self._key(body)
        path = RECORDINGS / f"{key[:20]}.json"
        if self.mode == "replay" or (self.mode == "record" and path.exists()):
            if not path.exists():
                raise MissingRecording(f"no recording for request {key[:20]} (run with COPILOT_LLM_MODE=record)")
            rec = json.loads(path.read_text())
            USED_RECORDINGS.add(path.name)
            tin, tout, cost = cost_of(self.model, rec["response"].get("usageMetadata", {}))
            return LLMResult(rec["response"], tin, tout, cost, rec["latency_ms"], True)

        total = spent_usd()
        if total >= self.cap:
            raise BudgetExceeded(f"spend cap reached: ${total:.4f} >= ${self.cap:.2f}")
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY is not set (or use COPILOT_LLM_MODE=replay)")
        t0 = time.time()
        for attempt in range(4):
            r = httpx.post(API.format(model=self.model), json=body, timeout=120,
                           headers={"x-goog-api-key": api_key})
            if r.status_code in (429, 500, 503) and attempt < 3:
                time.sleep(2 * (attempt + 1))
                continue
            r.raise_for_status()
            break
        latency = int((time.time() - t0) * 1000)
        resp = r.json()
        tin, tout, cost = cost_of(self.model, resp.get("usageMetadata", {}))
        SPEND_LOG.parent.mkdir(parents=True, exist_ok=True)
        with SPEND_LOG.open("a") as f:
            f.write(json.dumps({"ts": time.time(), "model": self.model, "tag": self.tag, "input_tokens": tin,
                                "output_tokens": tout, "cost_usd": round(cost, 6)}) + "\n")
        if self.mode == "record":
            RECORDINGS.mkdir(parents=True, exist_ok=True)
            USED_RECORDINGS.add(path.name)
            path.write_text(json.dumps({"key": key, "model": self.model, "latency_ms": latency, "response": resp},
                                       indent=1, ensure_ascii=False))
        return LLMResult(resp, tin, tout, cost, latency, False)
