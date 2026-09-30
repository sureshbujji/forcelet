"""Semantic (vector) search over records. — Forcelet platform module.

The default provider is a dependency-free TF-IDF + cosine-similarity engine:
each searchable record becomes a document, the query is scored against the
corpus, and the top matches come back ranked with a 0–1 relevance score and
a text snippet.  Because it scores on word overlap rather than substrings,
it finds "windshield wiper motor" for the query "wiper motor broken" where
the keyword search (substring match) misses.  True synonym matching
("windshield" vs "windscreen") needs a dense embedding backend.

The provider interface is pluggable: register a denser embedding backend
(e.g. sentence-transformers) via :func:`register_provider` and select it
with the ``forcelet.semantic_provider`` setting.  Anything unregistered
falls back to TF-IDF.

By Suresh Itha — part of the Forcelet platform.
"""
from __future__ import annotations

import math
import re
import time
from collections import Counter

_WORD = re.compile(r"[a-z0-9]+")

# tiny English stop-word list — keeps the index small and scores meaningful
_STOP = frozenset("""
a an the and or of to in on for with is are was were be been being at as by
from it its this that these those we you he she they them his her their our
your my me him us do does did not no yes if then than so such can will just
about into over after before between during each other more most some any all
""".split())

_PROVIDERS: dict = {}
_INDEX_CACHE: dict = {}  # key -> {"built_at": ts, "index": ...}
CACHE_TTL = 120


def tokenize(text: str) -> list:
    return [w for w in _WORD.findall((text or "").lower()) if w not in _STOP]


# ------------------------------------------------------------ TF-IDF engine
def build_index(docs: list) -> dict:
    """docs: list of (key, text). Returns an opaque index dict."""
    tokenized = [(key, tokenize(text)) for key, text in docs]
    df: Counter = Counter()
    for _key, toks in tokenized:
        for t in set(toks):
            df[t] += 1
    n = max(len(tokenized), 1)
    idf = {t: math.log((n + 1) / (c + 1)) + 1.0 for t, c in df.items()}
    vectors = []
    for key, toks in tokenized:
        tf = Counter(toks)
        vec = {t: (1 + math.log(c)) * idf[t] for t, c in tf.items()}
        norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
        vectors.append((key, {t: v / norm for t, v in vec.items()}))
    return {"idf": idf, "vectors": vectors, "size": len(vectors)}


def _query_vector(index: dict, query: str) -> dict:
    tf = Counter(tokenize(query))
    idf = index["idf"]
    vec = {t: (1 + math.log(c)) * idf[t] for t, c in tf.items() if t in idf}
    norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
    return {t: v / norm for t, v in vec.items()}


def search_index(index: dict, query: str, top_n: int = 25) -> list:
    """Returns [(key, score)] sorted by descending cosine similarity."""
    qvec = _query_vector(index, query)
    if not qvec:
        return []
    scored = []
    for key, vec in index["vectors"]:
        s = sum(qvec.get(t, 0.0) * v for t, v in vec.items())
        if s > 0:
            scored.append((key, s))
    scored.sort(key=lambda kv: kv[1], reverse=True)
    return scored[:top_n]


# ------------------------------------------------------------ provider plug-in
def register_provider(name: str, search_fn) -> None:
    """Register a provider: search_fn(corpus, query, top_n) -> [(key, score)].

    corpus is a list of (key, text) tuples; key is an opaque caller string.
    """
    _PROVIDERS[name] = search_fn


def _tfidf_provider(corpus: list, query: str, top_n: int) -> list:
    return search_index(build_index(corpus), query, top_n)


def active_provider_name(store=None) -> str:
    name = None
    if store is not None:
        try:
            rows = store._execute(
                "SELECT value FROM mf_settings WHERE key='semantic_provider'").fetchall()
            name = rows[0]["value"] if rows else None
        except Exception:
            name = None
    return name if name in _PROVIDERS else "tfidf"


# ------------------------------------------------------------ record search
def _record_text(obj: dict, rec: dict, text_fields: list) -> str:
    parts = []
    for f in text_fields:
        v = rec.get(f)
        if v:
            parts.append(str(v))
    # the record's display name gets extra weight by repetition
    name = rec.get("Name") or rec.get("Subject") or ""
    if name:
        parts.append(f"{name} {name}")
    return " ".join(parts)


def _snippet(text: str, query: str, width: int = 140) -> str:
    low = text.lower()
    terms = [t for t in tokenize(query) if t in low]
    if not terms:
        return text[:width] + ("…" if len(text) > width else "")
    i = low.find(terms[0])
    start = max(i - width // 3, 0)
    frag = text[start:start + width]
    return ("…" if start else "") + frag + ("…" if start + width < len(text) else "")


def semantic_search(store, registry, security, user, query: str,
                     limit: int = 25) -> dict:
    """Ranked semantic search across every readable object.

    Returns {"provider": name, "results": [{object, label, id, name, score,
    snippet}]} with score in 0–1.
    """
    from .expressions import record_context
    from .api._shared import _visible_records

    query = (query or "").strip()
    if not query:
        return {"provider": active_provider_name(store), "results": []}
    provider_name = active_provider_name(store)
    provider = _PROVIDERS.get(provider_name, _tfidf_provider)

    corpus, meta = [], {}
    for obj in registry.list_objects():
        obj_name = obj["name"]
        if not security.can(user, "read", obj_name):
            continue
        readable = set(security.readable_fields(user, obj))
        text_fields = [f["name"] for f in obj.get("fields", [])
                       if f["name"] in readable
                       and f["type"] in ("Text", "TextArea", "Email", "Phone", "URL")]
        if not text_fields:
            continue
        records, _obj = _visible_records(user, obj_name)
        for rec in records:
            ctx_rec = record_context(rec)
            text = _record_text(obj, ctx_rec, text_fields)
            if not text.strip():
                continue
            key = f"{obj_name}:{rec.get('id')}"
            corpus.append((key, text))
            meta[key] = {"object": obj_name,
                         "label": obj.get("label", obj_name),
                         "id": rec.get("id"),
                         "name": rec.get("Name") or rec.get("Subject") or rec.get("id"),
                         "text": text}
    if not corpus:
        return {"provider": provider_name, "results": []}

    cache_key = f"{provider_name}:{len(corpus)}"
    entry = _INDEX_CACHE.get(cache_key)
    if provider_name == "tfidf" and (
            not entry or time.time() - entry["built_at"] > CACHE_TTL):
        entry = {"built_at": time.time(), "index": build_index(corpus)}
        _INDEX_CACHE[cache_key] = entry

    if provider_name == "tfidf":
        ranked = search_index(entry["index"], query, limit)
    else:
        ranked = provider(corpus, query, limit) or []

    results = []
    for key, score in ranked:
        m = meta.get(key)
        if not m:
            continue
        results.append({"object": m["object"], "label": m["label"],
                        "id": m["id"], "name": m["name"],
                        "score": round(min(max(score, 0.0), 1.0), 3),
                        "snippet": _snippet(m["text"], query)})
    return {"provider": provider_name, "results": results}
