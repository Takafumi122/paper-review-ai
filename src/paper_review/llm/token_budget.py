"""Offline text-token estimate with explicit overhead; never a provider-exact count.

Bundled o200k_base data and regex originate from OpenAI tiktoken (MIT, adjacent
license). No network access or runtime cache writes are needed. The provider's
usage remains authoritative, including for aliases whose encoding may change.
"""

from __future__ import annotations

import base64
import hashlib
import json
from functools import lru_cache
from pathlib import Path

from tiktoken import Encoding

TOKEN_BUDGET_VERSION = "o200k-base-margin-v1"
ASSET_SHA256 = "446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d"
PATTERN = "|".join(
    [
        r"[^\r\n\p{L}\p{N}]?[\p{Lu}\p{Lt}\p{Lm}\p{Lo}\p{M}]*[\p{Ll}\p{Lm}\p{Lo}\p{M}]+(?i:'s|'t|'re|'ve|'m|'ll|'d)?",
        r"[^\r\n\p{L}\p{N}]?[\p{Lu}\p{Lt}\p{Lm}\p{Lo}\p{M}]+[\p{Ll}\p{Lm}\p{Lo}\p{M}]*(?i:'s|'t|'re|'ve|'m|'ll|'d)?",
        r"\p{N}{1,3}",
        r" ?[^\s\p{L}\p{N}]+[\r\n/]*",
        r"\s*[\r\n]+",
        r"\s+(?!\S)",
        r"\s+",
    ]
)


@lru_cache(maxsize=1)
def encoding() -> Encoding:
    data = Path(__file__).with_name("data").joinpath("o200k_base.tiktoken").read_bytes()
    if hashlib.sha256(data).hexdigest() != ASSET_SHA256:
        raise RuntimeError("tokenizer_asset_hash_mismatch")
    ranks = {
        base64.b64decode(token): int(rank)
        for token, rank in (line.split() for line in data.splitlines() if line)
    }
    return Encoding(
        name="paper-review-o200k-base",
        pat_str=PATTERN,
        mergeable_ranks=ranks,
        special_tokens={"<|endoftext|>": 199999, "<|endofprompt|>": 200018},
    )


def estimate_input_tokens(
    schema: dict, sources: tuple[dict[str, str], ...], instructions: str
) -> int:
    text = json.dumps(
        {"schema": schema, "sources": sources, "instructions": instructions}, ensure_ascii=False
    )
    count = len(encoding().encode(text, disallowed_special=()))
    # Schema/message framing and future alias drift need headroom. This is an
    # estimate, not a mathematical upper bound or provider input-token count.
    return (count * 5 + 3) // 4 + 512
