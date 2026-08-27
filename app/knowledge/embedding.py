"""Dependency-light deterministic embedding for reproducible sample retrieval."""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter

_TOKEN_PATTERN = re.compile(r"\w+", flags=re.UNICODE)


class HashingTextEmbedder:
    """Map Unicode token counts to a normalized, versioned hashing vector."""

    method = "hashed_token_cosine_v1"

    def __init__(self, *, dimensions: int = 128) -> None:
        if not 32 <= dimensions <= 4_096:
            raise ValueError("dimensions must be between 32 and 4096")
        self.dimensions = dimensions

    def embed(self, text: str) -> list[float]:
        tokens = _TOKEN_PATTERN.findall(text.casefold())
        if not tokens:
            raise ValueError("text must contain at least one token")
        vector = [0.0] * self.dimensions
        for token, count in Counter(tokens).items():
            digest = hashlib.blake2b(
                token.encode("utf-8"),
                digest_size=8,
                person=b"multiagt",
            ).digest()
            bucket = int.from_bytes(digest[:4], "big") % self.dimensions
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[bucket] += sign * (1.0 + math.log(count))
        norm = math.sqrt(sum(value * value for value in vector))
        return [value / norm for value in vector]

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        """Embed a bounded batch while preserving deterministic input order."""

        if not texts:
            raise ValueError("embedding inputs must not be empty")
        return [self.embed(text) for text in texts]
