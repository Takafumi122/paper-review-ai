"""CPU SPECTER2 inference from an explicitly prepared, content-verified local bundle."""

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import json
import math
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from paper_review.config import ROOT

PREPROCESSING = "title-sep-abstract-cls-512-v1"


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def bundle_identity(manifest: Path | None) -> str:
    if manifest is None or not manifest.is_file():
        return "local-specter2:unconfigured"
    versions = {}
    for package in ("torch", "transformers", "adapters"):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = "uninstalled"
    runtime = hashlib.sha256(json.dumps(versions, sort_keys=True).encode()).hexdigest()
    return "local-specter2:" + file_hash(manifest) + ":" + PREPROCESSING + ":" + runtime


def verified_bundle(manifest: Path) -> tuple[Path, Path]:
    directory = (ROOT / ".work").resolve()
    if not manifest.resolve().is_relative_to(directory) or manifest.name.startswith(".env"):
        raise ValueError("specter2_bundle_must_be_workspace_local")
    data = json.loads(manifest.read_text())
    if (data.get("base_repo") != "allenai/specter2_base"
            or data.get("adapter_repo") != "allenai/specter2"):
        raise ValueError("specter2_bundle_model_mismatch")
    for part in ("base", "adapter"):
        if not re.fullmatch(r"[a-f0-9]{40}", data.get(f"{part}_revision", "")):
            raise ValueError("specter2_bundle_revision_unpinned")
        if not data.get(f"{part}_files"):
            raise ValueError("specter2_bundle_empty")
        folder = (manifest.parent / part).resolve()
        if not folder.is_relative_to(directory):
            raise ValueError("specter2_bundle_invalid_path")
        actual = {str(p.relative_to(folder)) for p in folder.rglob("*") if p.is_file()
                  and ".cache" not in p.relative_to(folder).parts}
        if actual != set(data[f"{part}_files"]):
            raise ValueError("specter2_bundle_files_changed")
        for name, digest in data[f"{part}_files"].items():
            path = (folder / name).resolve()
            if not path.is_relative_to(folder) or path.name.startswith(".env"):
                raise ValueError("specter2_bundle_invalid_path")
            if file_hash(path) != digest:
                raise ValueError("specter2_bundle_hash_mismatch")
    return manifest.parent / "base", manifest.parent / "adapter"


class LocalSpecter2:
    def __init__(self, manifest: Path):
        base, adapter = verified_bundle(manifest)
        os.environ["HF_HOME"] = str(ROOT / ".work/huggingface")
        os.environ["HF_HUB_OFFLINE"] = "1"
        # Optional imports keep ML runtimes out of the default application install.
        self.torch: Any = importlib.import_module("torch")
        transformers: Any = importlib.import_module("transformers")
        adapters: Any = importlib.import_module("adapters")
        self.tokenizer = transformers.AutoTokenizer.from_pretrained(
            str(base), local_files_only=True, trust_remote_code=False,
        )
        self.model = adapters.AutoAdapterModel.from_pretrained(
            str(base), local_files_only=True, trust_remote_code=False,
        )
        self.model.load_adapter(str(adapter), load_as="proximity", set_active=True)
        if not self.model.active_adapters:
            raise ValueError("specter2_proximity_adapter_not_active")
        self.model.to("cpu")
        self.model.eval()
        self.torch.use_deterministic_algorithms(True)
        self.model_id = bundle_identity(manifest)

    def encode(self, texts: list[tuple[str, str]]) -> list[list[float]]:
        vectors = []
        for offset in range(0, len(texts), 8):
            batch = [title + self.tokenizer.sep_token + abstract
                     for title, abstract in texts[offset:offset + 8]]
            inputs = self.tokenizer(batch, padding=True, truncation=True, return_tensors="pt",
                                    return_token_type_ids=False, max_length=512)
            with self.torch.inference_mode():
                output = self.model(**inputs)
            vectors.extend(output.last_hidden_state[:, 0, :].cpu().tolist())
        return vectors


@lru_cache(maxsize=1)
def load_local_specter2(manifest: Path, identity: str) -> LocalSpecter2:
    model = LocalSpecter2(manifest)
    if model.model_id != identity:
        raise ValueError("specter2_bundle_changed_during_load")
    return model


def valid_vector(value: object, *, dimension: int = 768) -> bool:
    return (isinstance(value, list) and len(value) == dimension
            and all(type(v) in {int, float} and math.isfinite(v) for v in value)
            and any(value))
