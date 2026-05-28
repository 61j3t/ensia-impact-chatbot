"""Build the ChromaDB index from all chunks (chat + PDFs + OCR).

Embedding model: BAAI/bge-m3 via sentence-transformers. Embeddings are
L2-normalized so the collection's cosine distance is equivalent to
dot-product. BGE-M3 doesn't require any input prefix.

The model is cached under ~/.cache/huggingface after first download.

Run:
    .venv/bin/python -m chatbot.index            # incremental: add missing only
    .venv/bin/python -m chatbot.index --rebuild  # wipe and rebuild
"""

from __future__ import annotations

import argparse
import shutil
import time
from pathlib import Path

import chromadb
from sentence_transformers import SentenceTransformer

from chatbot.chunks import all_chunks

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "chatbot/chroma_db"
COLLECTION_NAME = "ensia"
MODEL_NAME = "BAAI/bge-m3"
EMBED_BATCH = 8  # bge-m3 is ~5x heavier than e5-small; use a smaller batch


def _sanitize(meta: dict) -> dict:
    """ChromaDB rejects None metadata values — convert to empty string."""
    return {k: (v if v is not None else "") for k, v in meta.items()}


def load_model() -> SentenceTransformer:
    from chatbot.retrieve import _best_device
    device = _best_device()
    print(f"Loading {MODEL_NAME} on {device}…")
    t0 = time.time()
    model = SentenceTransformer(MODEL_NAME, device=device)
    print(f"  loaded in {time.time() - t0:.1f}s")
    return model


def build_index(rebuild: bool = False) -> None:
    if rebuild and DB_PATH.exists():
        print(f"Removing existing index at {DB_PATH.relative_to(ROOT)}/")
        shutil.rmtree(DB_PATH)

    chunks = all_chunks()
    print(f"Loaded {len(chunks)} chunks")

    client = chromadb.PersistentClient(path=str(DB_PATH))
    collection = client.get_or_create_collection(
        name=COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"},
    )

    existing_ids = set(collection.get(include=[])["ids"])
    todo = [c for c in chunks if c["id"] not in existing_ids]
    if not todo:
        print(f"Index already has all {len(chunks)} chunks — nothing to do.")
        return
    print(f"Embedding {len(todo)} new chunks (skipping {len(chunks) - len(todo)} already indexed)")

    model = load_model()

    t0 = time.time()
    for batch_start in range(0, len(todo), EMBED_BATCH):
        batch = todo[batch_start : batch_start + EMBED_BATCH]
        texts = [c["text"] for c in batch]
        embeddings = model.encode(
            texts,
            batch_size=EMBED_BATCH,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        collection.add(
            ids=[c["id"] for c in batch],
            embeddings=embeddings.tolist(),
            documents=texts,
            metadatas=[_sanitize(c["metadata"]) for c in batch],
        )
        done = min(batch_start + EMBED_BATCH, len(todo))
        elapsed = time.time() - t0
        rate = done / elapsed if elapsed > 0 else 0
        eta = (len(todo) - done) / rate if rate > 0 else 0
        print(f"  [{done}/{len(todo)}] {rate:.1f} chunks/s, ETA {eta:.0f}s")

    total_count = collection.count()
    print(f"\nDone. Index contains {total_count} chunks at {DB_PATH.relative_to(ROOT)}/")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--rebuild", action="store_true", help="Wipe and rebuild from scratch")
    args = parser.parse_args()
    build_index(rebuild=args.rebuild)
