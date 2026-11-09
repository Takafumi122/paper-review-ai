import asyncio
import json
from contextlib import nullcontext
from types import SimpleNamespace
from typing import Any

import pytest

from paper_review.application import local_embedding_enrichment as enrichment
from paper_review.domain.models import CandidatePaper, CandidateSet, Paper
from paper_review.infrastructure import local_specter2 as local
from paper_review.infrastructure.academic_api.local_cache import LocalArtifactCache


def inputs():
    return Paper(id="target", title="Target", abstract="Target abstract",
                 embedding=[7.0], embedding_model="s2-old"), CandidateSet(
        target_paper_id="target", candidates=[
            CandidatePaper(paper_id="prior", title="Prior", abstract="Prior abstract",
                           resolution_status="resolved", embedding=[8.0], embedding_model="s2-old"),
            CandidatePaper(paper_id="missing", title="No abstract", resolution_status="resolved"),
        ],
    )


def test_local_vectors_cache_identity_input_hash_and_missing_abstract(settings, monkeypatch):
    folder = settings.database_path.parent
    manifest = folder / "manifest.json"
    manifest.write_text("{}")
    cache = LocalArtifactCache(folder / "cache.db")
    calls = []

    class Encoder:
        def encode(self, texts):
            calls.append(texts)
            return [[1.0] + [0.0] * 767 for _ in texts]

    monkeypatch.setattr(enrichment, "load_local_specter2", lambda *args: Encoder())
    target, inventory = inputs()
    first, first_inventory = asyncio.run(enrichment.enrich_local_embeddings(
        target, inventory, manifest, cache,
    ))
    assert first.embedding_model == local.bundle_identity(manifest)
    assert first.embedding_model != "s2-old"
    assert first_inventory.candidates[1].embedding is None
    assert first_inventory.candidates[1].embedding_provenance["missing_reason"] == (
        "embedding_title_or_abstract_missing"
    )
    second, _ = asyncio.run(enrichment.enrich_local_embeddings(target, inventory, manifest, cache))
    assert second.embedding_provenance["cache_hit"] == "true"
    assert len(calls) == 1
    # A changed input or bundle invalidates the cache without touching source objects.
    target.abstract = "Changed abstract"
    asyncio.run(enrichment.enrich_local_embeddings(target, inventory, manifest, cache))
    assert len(calls[-1]) == 1
    manifest.write_text('{"revision":"changed"}')
    asyncio.run(enrichment.enrich_local_embeddings(target, inventory, manifest, cache))
    assert len(calls[-1]) == 2
    assert target.embedding_model == "s2-old"


@pytest.mark.parametrize("vector", [[1.0], [0.0] * 768, [float("nan")] * 768])
def test_invalid_vectors_leave_similarity_missing(settings, monkeypatch, vector):
    folder = settings.database_path.parent
    manifest = folder / "manifest.json"
    manifest.write_text("{}")
    monkeypatch.setattr(enrichment, "load_local_specter2", lambda *args: SimpleNamespace(
        encode=lambda texts: [vector for _ in texts],
    ))
    target, inventory = inputs()
    output, candidates = asyncio.run(enrichment.enrich_local_embeddings(
        target, inventory, manifest, LocalArtifactCache(folder / "cache.db"),
    ))
    assert output.embedding is None
    assert candidates.candidates[0].embedding is None
    assert "local_specter2_inference_unavailable" in candidates.retrieval_warnings


def test_missing_runtime_preserves_inventory_and_does_not_download(settings, monkeypatch):
    folder = settings.database_path.parent
    manifest = folder / "manifest.json"
    manifest.write_text("{}")

    def missing(*args):
        raise ImportError("dummy raw text must not escape")

    monkeypatch.setattr(enrichment, "load_local_specter2", missing)
    target, inventory = inputs()
    output, candidates = asyncio.run(enrichment.enrich_local_embeddings(
        target, inventory, manifest, LocalArtifactCache(folder / "cache.db"),
    ))
    assert output.embedding is None
    assert len(candidates.candidates) == 2
    assert "dummy raw text" not in json.dumps(candidates.model_dump())


def bundle(folder):
    data: dict[str, Any] = {
        "base_repo": "allenai/specter2_base", "adapter_repo": "allenai/specter2",
        "base_revision": "a" * 40, "adapter_revision": "b" * 40,
    }
    for part in ("base", "adapter"):
        directory = folder / part
        directory.mkdir()
        path = directory / "config.json"
        path.write_text("{}")
        data[f"{part}_files"] = {"config.json": local.file_hash(path)}
    manifest = folder / "manifest.json"
    manifest.write_text(json.dumps(data))
    return manifest


def test_bundle_hash_rejects_changes_and_path_escape(settings):
    folder = settings.database_path.parent
    manifest = bundle(folder)
    assert local.verified_bundle(manifest) == (folder / "base", folder / "adapter")
    (folder / "base/config.json").write_text('{"changed":true}')
    with pytest.raises(ValueError, match="hash_mismatch"):
        local.verified_bundle(manifest)
    data = json.loads(manifest.read_text())
    data["base_files"] = {"../outside": "dummy"}
    manifest.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        local.verified_bundle(manifest)


def test_official_recipe_local_paths_eval_cpu_and_cls_pooling(settings, monkeypatch):
    folder = settings.database_path.parent
    manifest = bundle(folder)
    events = []

    class Tensor:
        def __getitem__(self, item):
            assert item == (slice(None), 0, slice(None))
            events.append("cls")
            return self

        def cpu(self):
            return self

        def tolist(self):
            return [[1.0] + [0.0] * 767]

    class Model:
        active_adapters = "proximity"

        def load_adapter(self, path, **kwargs):
            assert path == str(folder / "adapter")
            assert kwargs == {"load_as": "proximity", "set_active": True}

        def to(self, device):
            assert device == "cpu"

        def eval(self):
            events.append("eval")

        def __call__(self, **kwargs):
            return SimpleNamespace(last_hidden_state=Tensor())

    class Tokenizer:
        sep_token = "[SEP]"

        def __call__(self, texts, **kwargs):
            assert texts == ["Title[SEP]Abstract"]
            assert kwargs == {"padding": True, "truncation": True, "return_tensors": "pt",
                              "return_token_type_ids": False, "max_length": 512}
            return {}

    def loader(kind):
        def load(path, **kwargs):
            assert path == str(folder / "base")
            assert kwargs == {"local_files_only": True, "trust_remote_code": False}
            return kind()
        return SimpleNamespace(from_pretrained=load)

    modules = {
        "torch": SimpleNamespace(inference_mode=nullcontext,
                                 use_deterministic_algorithms=lambda _: None),
        "transformers": SimpleNamespace(AutoTokenizer=loader(Tokenizer)),
        "adapters": SimpleNamespace(AutoAdapterModel=loader(Model)),
    }
    monkeypatch.setattr(local.importlib, "import_module", modules.__getitem__)
    monkeypatch.setenv("HF_HOME", "dummy")
    monkeypatch.setenv("HF_HUB_OFFLINE", "0")
    model = local.LocalSpecter2(manifest)
    assert len(model.encode([("Title", "Abstract")])[0]) == 768
    assert events == ["eval", "cls"]
