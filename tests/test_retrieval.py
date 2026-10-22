from copilot.retrieval import KnowledgeBase, fts_query, hit_at_k, recall_at_k, reciprocal_rank


def test_metrics():
    ranked = ["a", "b", "c", "d", "e", "f"]
    assert reciprocal_rank(ranked, {"c"}) == 1 / 3
    assert recall_at_k(ranked, {"a", "b", "z"}, 5) == 2 / 3
    assert recall_at_k(ranked, {"a", "b", "c", "d", "e", "f", "g"}, 5) == 1.0  # capped at k
    assert hit_at_k(ranked, {"f"}, 5) == 0.0


def test_fts_query_drops_stopwords_and_quotes():
    q = fts_query('The pod "web-1" is in ImagePullBackOff')
    assert '"imagepullbackoff"' in q and '"the"' not in q and " OR " in q


def test_bm25_finds_exact_error_token():
    kb = KnowledgeBase()
    hits = kb.bm25("x509: certificate has expired or is not yet valid", 5)
    assert hits and hits[0].doc.category == "tls_cert_expired"
