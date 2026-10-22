"""Hybrid retrieval over the (synthetic) support-ticket + runbook corpus.

  bm25   - SQLite FTS5 (porter stemming), terms OR-ed together.
  dense  - BAAI/bge-small-en-v1.5 via fastembed (ONNX, CPU), cosine similarity.
  hybrid - Reciprocal Rank Fusion of the two lists: score = sum 1/(k + rank).

Why both: error strings ("ImagePullBackOff", "exit code 137", "x509") are exact
tokens that BM25 nails and embeddings blur; vague first reports ("site is slow
after the deploy") are paraphrases that embeddings catch and BM25 misses.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
CORPUS = ROOT / "corpus"
EMBED_MODEL = "BAAI/bge-small-en-v1.5"
RRF_K = 60
CANDIDATES = 50

STOP = set("""a an and are as at be been but by can could did do does for from had has have he her his how i if in
into is it its just me my no not of on or our out over so some than that the their them then there these they this
to up us was we were what when where which while who why will with would you your after again all also any because
before being both down each few more most only other same should such too very""".split())


@dataclass
class Doc:
    id: str
    category: str
    title: str
    text: str


@dataclass
class Hit:
    id: str
    score: float
    rank: int
    doc: Doc


def load_corpus() -> list[Doc]:
    docs = []
    for line in (CORPUS / "tickets.jsonl").read_text().splitlines():
        t = json.loads(line)
        body = (f"{t['title']}\nService: {t['service']}\nReported: {t['reported_symptoms']}\n"
                f"Investigation: {t['investigation']}\nRoot cause: {t['root_cause']}\nResolution: {t['resolution']}")
        docs.append(Doc(f"ticket/{t['id']}", t["category"], t["title"], body))
    for p in sorted((CORPUS / "runbooks").glob("*.md")):
        txt = p.read_text()
        m = re.search(r"category: (\S+)", txt)
        body = re.sub(r"<!--.*?-->\n?", "", txt, flags=re.S).strip()
        title = next((ln.lstrip("# ").strip() for ln in body.splitlines() if ln.strip()), p.stem)
        docs.append(Doc(f"runbook/{p.stem}", m.group(1) if m else "", title, body))
    return docs


def fts_query(text: str) -> str:
    toks = [t for t in re.findall(r"[a-z0-9][a-z0-9_.\-]*[a-z0-9]|[a-z0-9]", text.lower()) if t not in STOP and len(t) > 1]
    seen: list[str] = []
    for t in toks:
        t = t.replace('"', "")
        if t not in seen:
            seen.append(t)
    return " OR ".join(f'"{t}"' for t in seen[:64])


class _Embedder:
    _model = None

    @classmethod
    def get(cls):
        if cls._model is None:
            from fastembed import TextEmbedding

            # Small batches + 2 threads: the default batch of 256 x 512 tokens needs ~3 GB of RAM.
            cls._model = TextEmbedding(EMBED_MODEL, threads=2, cache_dir=str(Path.home() / ".cache" / "fastembed"))
        return cls._model


class KnowledgeBase:
    def __init__(self, docs: list[Doc] | None = None):
        self.docs = docs if docs is not None else load_corpus()
        self.by_id = {d.id: d for d in self.docs}
        self.db = sqlite3.connect(":memory:")
        self.db.execute("CREATE VIRTUAL TABLE kb USING fts5(id UNINDEXED, title, body, tokenize='porter unicode61')")
        self.db.executemany("INSERT INTO kb VALUES (?,?,?)", [(d.id, d.title, d.text) for d in self.docs])
        self._emb: np.ndarray | None = None

    # ------------------------------------------------------------ embeddings
    @property
    def emb(self) -> np.ndarray:
        if self._emb is None:
            cache = ROOT / "runs" / "kb_embeddings.npz"
            ids = [d.id for d in self.docs]
            sig = hashlib.sha256("\x00".join(d.text for d in self.docs).encode()).hexdigest()
            if cache.exists():
                z = np.load(cache, allow_pickle=False)
                if str(z["sig"]) == sig and list(z["ids"]) == ids:
                    self._emb = z["emb"]
                    return self._emb
            vecs = np.array(list(_Embedder.get().passage_embed([d.text for d in self.docs], batch_size=8)), dtype=np.float32)
            vecs /= np.linalg.norm(vecs, axis=1, keepdims=True)
            cache.parent.mkdir(parents=True, exist_ok=True)
            np.savez(cache, emb=vecs, ids=np.array(ids), sig=np.array(sig))
            self._emb = vecs
        return self._emb

    # ---------------------------------------------------------------- search
    def bm25(self, query: str, k: int = CANDIDATES) -> list[Hit]:
        q = fts_query(query)
        if not q:
            return []
        rows = self.db.execute("SELECT id, bm25(kb) FROM kb WHERE kb MATCH ? ORDER BY bm25(kb) LIMIT ?", (q, k)).fetchall()
        return [Hit(i, -s, r + 1, self.by_id[i]) for r, (i, s) in enumerate(rows)]

    def dense(self, query: str, k: int = CANDIDATES) -> list[Hit]:
        qv = np.array(next(iter(_Embedder.get().query_embed([query]))), dtype=np.float32)
        qv /= np.linalg.norm(qv)
        sims = self.emb @ qv
        order = np.argsort(-sims)[:k]
        return [Hit(self.docs[i].id, float(sims[i]), r + 1, self.docs[i]) for r, i in enumerate(order)]

    def hybrid(self, query: str, k: int = CANDIDATES, rrf_k: int = RRF_K) -> list[Hit]:
        scores: dict[str, float] = {}
        for hits in (self.bm25(query, k), self.dense(query, k)):
            for h in hits:
                scores[h.id] = scores.get(h.id, 0.0) + 1.0 / (rrf_k + h.rank)
        order = sorted(scores, key=lambda i: -scores[i])[:k]
        return [Hit(i, scores[i], r + 1, self.by_id[i]) for r, i in enumerate(order)]

    def search(self, query: str, mode: str = "hybrid", top_k: int = 5) -> list[Hit]:
        return {"bm25": self.bm25, "dense": self.dense, "hybrid": self.hybrid}[mode](query)[:top_k]


# ------------------------------------------------------------------ metrics
def recall_at_k(ranked: list[str], relevant: set[str], k: int = 5) -> float:
    """|relevant in top k| / min(k, |relevant|) - 1.0 means the top k is as full of relevant docs as it can be."""
    if not relevant:
        return 0.0
    return len([d for d in ranked[:k] if d in relevant]) / min(k, len(relevant))


def hit_at_k(ranked: list[str], relevant: set[str], k: int = 5) -> float:
    return float(any(d in relevant for d in ranked[:k]))


def reciprocal_rank(ranked: list[str], relevant: set[str]) -> float:
    for i, d in enumerate(ranked, 1):
        if d in relevant:
            return 1.0 / i
    return 0.0
