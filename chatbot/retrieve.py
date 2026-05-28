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
# Candidate pool fed to the reranker. Smaller = faster, marginally lower
# recall. With our corpus, 10 is enough — the missed-source @20 cases all
# also missed @10 in the k-sweep.
CANDIDATE_POOL = 10
# Cap input length the reranker tokenizes per (query, doc) pair. Default
# for bge-reranker-v2-m3 is 8192 — overkill for our chunks and the main
# driver of compute time. 512 keeps full chat messages and the most
# salient slice of long PDF chunks.
RERANK_MAX_TOKENS = 512


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

        # Stage 1: dense retrieval. Pull more than k if reranking is on.
        n_dense = max(candidate_pool, k) if rerank else k
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

        if not rerank or len(candidates) <= 1:
            return candidates[:k]

        # Stage 2: cross-encoder rerank.
        pairs = [(query, c["text"]) for c in candidates]
        rerank_scores = self._reranker_lazy().predict(pairs, show_progress_bar=False)
        for c, s in zip(candidates, rerank_scores):
            c["score"] = float(s)
        candidates.sort(key=lambda c: c["score"], reverse=True)
        return candidates[:k]


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
