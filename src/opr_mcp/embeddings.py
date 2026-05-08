from __future__ import annotations

import struct
from collections.abc import Iterable
from functools import lru_cache

import numpy as np

from .config import EMBED_DIM, embed_model_name


@lru_cache(maxsize=1)
def _model():
    from sentence_transformers import SentenceTransformer

    name = embed_model_name()
    model = SentenceTransformer(name)
    dim = model.get_sentence_embedding_dimension()
    if dim != EMBED_DIM:
        raise RuntimeError(
            f"Embedding model {name!r} produces {dim}-dim vectors, "
            f"but the schema is fixed at {EMBED_DIM}. Use a 384-dim model or rebuild the DB."
        )
    return model


def encode(texts: Iterable[str], batch_size: int = 32) -> np.ndarray:
    texts = list(texts)
    if not texts:
        return np.zeros((0, EMBED_DIM), dtype=np.float32)
    model = _model()
    vecs = model.encode(
        texts,
        batch_size=batch_size,
        normalize_embeddings=True,
        convert_to_numpy=True,
        show_progress_bar=False,
    )
    return vecs.astype(np.float32, copy=False)


def encode_one(text: str) -> np.ndarray:
    return encode([text])[0]


def to_blob(vec: np.ndarray) -> bytes:
    """Pack a float32 vector into the binary format sqlite-vec expects."""
    if vec.dtype != np.float32:
        vec = vec.astype(np.float32)
    return struct.pack(f"{len(vec)}f", *vec.tolist())
