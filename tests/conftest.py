"""Test fixtures.

Replaces ``opr_mcp.embeddings`` with a deterministic stub so tests don't need to
download a real model. The stub produces unit-norm vectors derived from a hash
of the input — stable across runs but obviously not semantically meaningful.
"""
from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

import numpy as np
import pytest

# Force a stable, in-process model name so the import-time cache key is stable.
os.environ.setdefault("EMBED_MODEL", "stub")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from opr_mcp import embeddings as _emb  # noqa: E402
from opr_mcp.config import EMBED_DIM  # noqa: E402


def _stub_encode(texts, batch_size: int = 32) -> np.ndarray:
    arr = np.zeros((len(texts), EMBED_DIM), dtype=np.float32)
    for i, t in enumerate(texts):
        h = hashlib.blake2b(t.encode("utf-8"), digest_size=64).digest()
        # Repeat hash to fill the dim, map bytes to [-1, 1)
        buf = (h * ((EMBED_DIM // len(h)) + 1))[:EMBED_DIM]
        v = np.frombuffer(buf, dtype=np.uint8).astype(np.float32) / 255.0 * 2 - 1
        n = np.linalg.norm(v)
        arr[i] = v / n if n else v
    return arr


def _stub_encode_one(text: str) -> np.ndarray:
    return _stub_encode([text])[0]


@pytest.fixture(autouse=True)
def _stub_embeddings(monkeypatch):
    monkeypatch.setattr(_emb, "encode", _stub_encode)
    monkeypatch.setattr(_emb, "encode_one", _stub_encode_one)
    yield


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    p = tmp_path / "opr.db"
    monkeypatch.setenv("DB", str(p))
    return p
