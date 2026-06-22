"""Retrieve top-k chunks from the ChromaDB index for a given query.

Two-stage retrieval:
  1. Dense retrieval (BGE-M3) returns a candidate pool of CANDIDATE_POOL
     chunks (default 20).
  2. If rerank=True (the default), bge-reranker-v2-m3 cross-encodes each
     (query, candidate) pair and re-orders. The reranker can model the
     query-document relationship more precisely than dot-product on
     pre-computed embeddings.

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
from pathlib import Path
from typing import Any

import chromadb
import torch
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
    ) -> list[dict[str, Any]]:
        """Return top-k results sorted by similarity (highest first).

        With rerank=True (default): fetch `candidate_pool` from dense search,
        then re-score with the cross-encoder reranker, then return top-k.
        With rerank=False: pure dense retrieval, top-k from ChromaDB directly.

        Result dict shape:
            {
              "id":          str,
              "score":        float,   # reranker logit if reranked, else cosine similarity
              "dense_score":  float,   # always present (cosine similarity from dense)
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

        # Stage 1: dense retrieval. Always pull the full candidate pool
        # so the diversification pass has room to drop near-duplicates.
        n_dense = max(candidate_pool, k)
        embedding = self._model_lazy().encode(
            [query],
            normalize_embeddings=True,
            show_progress_bar=False,
        )[0].tolist()
        result = self._collection.query(
            query_embeddings=[embedding],
            n_results=n_dense,
            where=where,
            include=["documents", "metadatas", "distances"],
        )

        candidates = []
        for i in range(len(result["ids"][0])):
            distance = result["distances"][0][i]
            candidates.append({
                "id": result["ids"][0][i],
                "score": 1.0 - distance,           # placeholder, may be overwritten
                "dense_score": 1.0 - distance,
                "text": result["documents"][0][i],
                "metadata": result["metadatas"][0][i],
            })

        # Stage 2 (optional): cross-encoder rerank.
        if rerank and len(candidates) > 1:
            pairs = [(query, c["text"]) for c in candidates]
            rerank_scores = self._reranker_lazy().predict(pairs, show_progress_bar=False)
            for c, s in zip(candidates, rerank_scores):
                c["score"] = float(s)
            candidates.sort(key=lambda c: c["score"], reverse=True)

        # Stage 3 (always): diversify-then-truncate. Walks the ranked
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
