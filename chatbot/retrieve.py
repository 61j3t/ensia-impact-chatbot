"""Retrieve top-k chunks from the ChromaDB index for a given query.

Hybrid + rerank + diversify pipeline:
  1. Dense (BGE-M3) and BM25 lexical rankings are computed in parallel.
  2. The two rankings are fused with reciprocal-rank-fusion (RRF) into
     a single candidate pool — semantic + keyword-density both vote.
     This pulls entity-dense chunks (e.g. bullet lists of partner
     companies) that dense alone misses when the surrounding text
     doesn't lexically resemble the query.
  3. If rerank=True, bge-reranker-v2-m3 cross-encodes the (query, doc)
     pair and re-orders. Reranker only sees what fusion surfaced —
     hence the fusion step matters.
  4. Diversification trims to k, capping chunks per document and per
     source site so one page can't monopolise context.

Usage from code:
    from chatbot.retrieve import Retriever
    r = Retriever()
    hits = r.search("What is the CDE at ENSIA?", k=5)
    for h in hits:
        print(h["id"], h["score"], h["metadata"]["topic"])

Pass `rerank=False` to bypass the reranker (useful for ablation).

Usage from CLI (quick smoke-test):
    .venv/bin/python -m chatbot.retrieve "what is the CDE?"
    .venv/bin/python -m chatbot.retrieve "any advice for ENSIA students?" --k 3
    .venv/bin/python -m chatbot.retrieve "C'est quoi le Startup Factory?" --no-rerank

Models are loaded lazily on first .search() call so importing the module
is cheap.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Any

import chromadb
import torch
from rank_bm25 import BM25Okapi
from sentence_transformers import CrossEncoder, SentenceTransformer


def _best_device() -> str:
    """Auto-pick a device: MPS on Apple Silicon, CUDA on Linux+GPU,
    CPU everywhere else (HF free Spaces, generic Linux boxes). Set the
    `ENSIA_DEVICE` env var to override (e.g. force "cpu" on a Mac for
    debugging)."""
    import os
    env = os.environ.get("ENSIA_DEVICE")
    if env:
        return env
    if torch.cuda.is_available():
        return "cuda"
    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "chatbot/chroma_db"
COLLECTION_NAME = "ensia"
MODEL_NAME = "BAAI/bge-m3"
RERANKER_NAME = "BAAI/bge-reranker-v2-m3"
# Candidate pool fed to the reranker AND to the diversification pass.
# Bigger pool gives diversification room to drop near-duplicates from
# the same page/site and still fill k. Reranking 30 cross-encoder
# pairs adds ~50ms on CPU — negligible for the recall it buys.
CANDIDATE_POOL = 30
# Diversification caps applied after rerank (or after dense if no
# rerank). Keeps a single page from monopolising the context.
MAX_PER_DOC = 1   # one chunk per (url | pdf_file | message_id)
MAX_PER_SITE = 2  # at most this many chunks from the same site
# Cap input length the reranker tokenizes per (query, doc) pair. Default
# for bge-reranker-v2-m3 is 8192 — overkill for our chunks and the main
# driver of compute time. 512 keeps full chat messages and the most
# salient slice of long PDF chunks.
RERANK_MAX_TOKENS = 512
# Reciprocal-rank-fusion constant. 60 is the classic value from Cormack
# et al.; tweaking it changes how aggressively top ranks are weighted
# vs. tail. Stick to 60 unless evals say otherwise.
RRF_K = 60
# How many candidates each individual ranker (dense, BM25) contributes
# to the fusion. Slightly bigger than CANDIDATE_POOL so a doc that's
# only ranked by one of the two still has a chance to surface.
PER_RANKER_POOL = 60


_WORD_RE = re.compile(r"\w+", re.UNICODE)


def _tokenize(text: str) -> list[str]:
    """Cheap word-level tokenizer that's friendly to en/fr/ar. We rely
    on \\w+ which Unicode-aware: it keeps Arabic letters, French
    diacritics, digits — everything we need for keyword matching.
    Punctuation, whitespace, and emojis are dropped."""
    return _WORD_RE.findall((text or "").lower())


def _match_where(meta: dict[str, Any] | None, where: dict[str, Any]) -> bool:
    """Tiny subset of Chroma's `where` operators — enough to mirror the
    filters this module emits ($eq on simple fields, $and over clauses).
    Used to post-filter BM25 hits so they obey the same filter the
    chroma query already applied to dense hits."""
    if meta is None:
        return False
    # {$and: [clause, clause, ...]}
    if "$and" in where:
        return all(_match_where(meta, c) for c in where["$and"])
    # {<field>: {$eq: value}} or {<field>: value}
    for field, cond in where.items():
        if field.startswith("$"):
            continue
        if isinstance(cond, dict) and "$eq" in cond:
            if meta.get(field) != cond["$eq"]:
                return False
        elif meta.get(field) != cond:
            return False
    return True


def _rrf_merge(rankings: list[list[str]], k: int = RRF_K) -> dict[str, float]:
    """Reciprocal-rank-fusion. Each ranker contributes 1/(k+rank) per
    doc it ranks; we sum across rankers. Robust to scale differences
    between rankers (cosine ~0–1 vs BM25 unbounded) — only ranks
    matter."""
    scores: dict[str, float] = {}
    for ranked in rankings:
        for rank, doc_id in enumerate(ranked):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank + 1)
    return scores


def _diversify(
    candidates: list[dict[str, Any]],
    k: int,
    max_per_doc: int = MAX_PER_DOC,
    max_per_site: int = MAX_PER_SITE,
) -> list[dict[str, Any]]:
    """Walk candidates in current score order and keep up to k while
    enforcing two caps:
      • max_per_doc per (url | pdf_file | message_id) — same document
        contributes at most this many chunks (default 1).
      • max_per_site per site — at most this many chunks share a site
        key (default 2). Chat messages don't have a site, so they're
        only bounded by the per-doc cap.
    Caller passes a sorted list; this preserves order and just skips
    candidates that exceed a cap."""
    out: list[dict[str, Any]] = []
    doc_count: dict[str, int] = {}
    site_count: dict[str, int] = {}
    for c in candidates:
        md = c.get("metadata") or {}
        # Pick the first non-empty document identifier. Empty means
        # we can't dedup → let it through (rare edge case).
        doc_key = (
            (md.get("url") or "").strip()
            or (md.get("pdf_file") or "").strip()
            or str(md.get("message_id") or "").strip()
        )
        site_key = (md.get("site") or "").strip()
        if doc_key and doc_count.get(doc_key, 0) >= max_per_doc:
            continue
        if site_key and site_count.get(site_key, 0) >= max_per_site:
            continue
        if doc_key:
            doc_count[doc_key] = doc_count.get(doc_key, 0) + 1
        if site_key:
            site_count[site_key] = site_count.get(site_key, 0) + 1
        out.append(c)
        if len(out) >= k:
            break
    return out


class Retriever:
    def __init__(
        self,
        db_path: Path = DB_PATH,
        model_name: str = MODEL_NAME,
        reranker_name: str = RERANKER_NAME,
        embed_device: str | None = None,
        rerank_device: str | None = None,
    ):
        self._client = chromadb.PersistentClient(path=str(db_path))
        self._collection = self._client.get_collection(COLLECTION_NAME)
        self._model_name = model_name
        self._reranker_name = reranker_name
        self._embed_device = embed_device or _best_device()
        self._rerank_device = rerank_device or _best_device()
        self._model: SentenceTransformer | None = None
        self._reranker: CrossEncoder | None = None
        # BM25 index — lazily populated by _bm25_lazy(). We hold the
        # full corpus in memory (~1.6M chars at current scale) so
        # candidates surfaced ONLY by BM25 (not by dense) can still be
        # turned into result dicts without a second chroma round-trip.
        self._bm25: BM25Okapi | None = None
        self._bm25_ids: list[str] = []
        self._bm25_docs: list[str] = []
        self._bm25_metas: list[dict[str, Any]] = []
        self._bm25_idx_by_id: dict[str, int] = {}

    def _model_lazy(self) -> SentenceTransformer:
        if self._model is None:
            self._model = SentenceTransformer(self._model_name, device=self._embed_device)
        return self._model

    def _reranker_lazy(self) -> CrossEncoder:
        if self._reranker is None:
            self._reranker = CrossEncoder(
                self._reranker_name,
                device=self._rerank_device,
                max_length=RERANK_MAX_TOKENS,
            )
        return self._reranker

    def _bm25_lazy(self) -> BM25Okapi:
        """Build the BM25 index over the entire chroma collection on
        first use. ~1s for our corpus; held for the process lifetime."""
        if self._bm25 is None:
            data = self._collection.get(include=["documents", "metadatas"])
            self._bm25_ids = data["ids"]
            self._bm25_docs = data["documents"]
            self._bm25_metas = data["metadatas"]
            self._bm25_idx_by_id = {id_: i for i, id_ in enumerate(self._bm25_ids)}
            tokenized = [_tokenize(d) for d in self._bm25_docs]
            self._bm25 = BM25Okapi(tokenized)
        return self._bm25

    def _rank_one_query(
        self, query: str, where: dict | None, n: int
    ) -> tuple[list[str], list[str], dict[str, dict[str, Any]]]:
        """Dense + BM25 rankings for one query string. Returns
        (dense_ids, bm25_ids, lookup), where lookup maps doc_id → a
        candidate dict (text + metadata + dense_score) for every id the
        dense search returned. BM25-only ids are materialised by the
        caller from the in-memory corpus."""
        embedding = self._model_lazy().encode(
            [query], normalize_embeddings=True, show_progress_bar=False,
        )[0].tolist()
        dense_result = self._collection.query(
            query_embeddings=[embedding],
            n_results=n,
            where=where,
            include=["documents", "metadatas", "distances"],
        )
        dense_ids = list(dense_result["ids"][0])
        lookup: dict[str, dict[str, Any]] = {}
        for i, doc_id in enumerate(dense_ids):
            lookup[doc_id] = {
                "id": doc_id,
                "text": dense_result["documents"][0][i],
                "metadata": dense_result["metadatas"][0][i],
                "dense_score": 1.0 - dense_result["distances"][0][i],
            }

        bm25 = self._bm25_lazy()
        tokens = _tokenize(query)
        bm25_ids: list[str] = []
        if tokens:
            scores = bm25.get_scores(tokens)
            # Argsort descending; keep only positive-scoring docs (zero
            # = no token overlap, not informative).
            order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
            for i in order[:n]:
                if scores[i] <= 0:
                    break
                # Apply chroma-style `where` filter post-hoc so BM25 and
                # dense agree on filtering.
                if where is not None and not _match_where(self._bm25_metas[i], where):
                    continue
                bm25_ids.append(self._bm25_ids[i])
        return dense_ids, bm25_ids, lookup

    def search(
        self,
        query: str,
        k: int = 5,
        *,
        rerank: bool = True,
        candidate_pool: int = CANDIDATE_POOL,
        topic: str | None = None,
        source_type: str | None = None,
        language: str | None = None,
        extra_queries: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Return top-k results sorted by similarity (highest first).

        Pipeline: for the main query (and any `extra_queries`), compute a
        dense + BM25 ranking; RRF-fuse them all; optionally cross-encoder
        rerank against the original query; diversify; return top-k.

        `extra_queries` exists for multilingual expansion: the caller can
        pass e.g. an English translation of an Arabic query so the
        English half of the corpus — which BGE-M3 otherwise ranks below
        same-language chunks — gets a fair vote via RRF.

        Result dict shape:
            {
              "id":          str,
              "score":        float,   # reranker logit if reranked, else RRF score
              "dense_score":  float,   # cosine similarity from dense (0.0 if BM25-only)
              "text":         str,
              "metadata":     dict,
            }
        """
        # ChromaDB filter syntax — "where" needs $eq for single, $and for multi.
        clauses = []
        if topic is not None:
            clauses.append({"topic": {"$eq": topic}})
        if source_type is not None:
            clauses.append({"source_type": {"$eq": source_type}})
        if language is not None:
            clauses.append({"language": {"$eq": language}})
        where = None
        if len(clauses) == 1:
            where = clauses[0]
        elif len(clauses) > 1:
            where = {"$and": clauses}

        # De-dup the query set: original first, then any distinct extras.
        queries = [query]
        for q in extra_queries or []:
            if q and q.strip() and q.strip() != query.strip() and q not in queries:
                queries.append(q)

        # Stage 1: dense + BM25 ranking per query. Pull a wide pool so RRF
        # fusion has range to work with.
        n_per_ranker = max(PER_RANKER_POOL, candidate_pool, k)
        rankings: list[list[str]] = []
        merged_lookup: dict[str, dict[str, Any]] = {}
        for q in queries:
            dense_ids, bm25_ids, lookup = self._rank_one_query(q, where, n_per_ranker)
            rankings.append(dense_ids)
            rankings.append(bm25_ids)
            for doc_id, c in lookup.items():
                # Keep the strongest dense_score seen for this id across
                # the query variants.
                if (
                    doc_id not in merged_lookup
                    or c["dense_score"] > merged_lookup[doc_id]["dense_score"]
                ):
                    merged_lookup[doc_id] = c

        # Stage 2: fuse every ranking with RRF, then materialise the top
        # candidate_pool into candidate dicts. Prefer merged_lookup for
        # text+metadata; fall back to the in-memory BM25 corpus for docs
        # only surfaced lexically.
        fused = _rrf_merge(rankings)
        candidates: list[dict[str, Any]] = []
        for doc_id, fused_score in sorted(fused.items(), key=lambda kv: kv[1], reverse=True):
            if doc_id in merged_lookup:
                c = dict(merged_lookup[doc_id])
            else:
                idx = self._bm25_idx_by_id.get(doc_id)
                if idx is None:
                    continue
                c = {
                    "id": doc_id,
                    "text": self._bm25_docs[idx],
                    "metadata": self._bm25_metas[idx],
                    "dense_score": 0.0,
                }
            c["score"] = fused_score
            candidates.append(c)
            if len(candidates) >= candidate_pool:
                break

        # Stage 3 (optional): cross-encoder rerank on the fused pool. We
        # rerank against the ORIGINAL query — faithful to what the user
        # asked; bge-reranker-v2-m3 is multilingual and scores
        # cross-language pairs fine.
        if rerank and len(candidates) > 1:
            pairs = [(query, c["text"]) for c in candidates]
            rerank_scores = self._reranker_lazy().predict(pairs, show_progress_bar=False)
            for c, s in zip(candidates, rerank_scores):
                c["score"] = float(s)
            candidates.sort(key=lambda c: c["score"], reverse=True)

        # Stage 4 (always): diversify-then-truncate. Walks the ranked
        # list in order and skips candidates that exceed the per-doc /
        # per-site caps. Ensures no single page or website monopolises
        # the final k results.
        return _diversify(candidates, k)


def _format_hit(hit: dict, max_chars: int = 200) -> str:
    md = hit["metadata"]
    kind = md.get("source_type")
    if kind == "chat":
        loc = f"chat msg {md.get('message_id')} | {md.get('topic') or '(no topic)'}"
    elif kind == "pdf":
        loc = f"PDF {md.get('pdf_file')} | chunk {md.get('chunk_index')}"
    elif kind == "external":
        loc = f"web {md.get('site') or '?'} | {md.get('title') or md.get('url') or ''}"
    else:
        loc = f"unknown source ({kind})"
    preview = hit["text"].replace("\n", " ")
    if len(preview) > max_chars:
        preview = preview[:max_chars] + "…"
    return f"  [{hit['score']:.3f}] {hit['id']} ({loc}, lang={md.get('language', '?')})\n         {preview}"


def main():
    parser = argparse.ArgumentParser(description="Quick retrieval smoke-test")
    parser.add_argument("query", help="Query string")
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--no-rerank", action="store_true", help="Skip the cross-encoder reranker")
    parser.add_argument("--topic", default=None)
    parser.add_argument("--source", dest="source_type", default=None,
                        choices=["chat", "pdf", None])
    parser.add_argument("--lang", dest="language", default=None,
                        choices=["en", "ar", "fr", None])
    args = parser.parse_args()

    r = Retriever()
    hits = r.search(
        args.query,
        k=args.k,
        rerank=not args.no_rerank,
        topic=args.topic,
        source_type=args.source_type,
        language=args.language,
    )
    print(f"\nQuery: {args.query!r}")
    print(f"Returned {len(hits)} hits (k={args.k})\n")
    for h in hits:
        print(_format_hit(h))
        print()


if __name__ == "__main__":
    main()
