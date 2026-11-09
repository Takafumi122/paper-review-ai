"""Prepare a pinned public SPECTER2 bundle; downloading requires --download."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import re
from pathlib import Path

from paper_review.config import ROOT
from paper_review.infrastructure.local_specter2 import file_hash, verified_bundle


def revision(value: str) -> str:
    if not re.fullmatch(r"[a-f0-9]{40}", value):
        raise argparse.ArgumentTypeError("use a full immutable 40-character commit SHA")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=ROOT / ".work/models/specter2")
    parser.add_argument("--base-revision", type=revision, required=True)
    parser.add_argument("--adapter-revision", type=revision, required=True)
    parser.add_argument("--download", action="store_true")
    args = parser.parse_args()
    directory = args.directory.resolve()
    if not directory.is_relative_to((ROOT / ".work").resolve()):
        parser.error("model bundle must stay inside workspace .work")
    manifest = directory / "manifest.json"
    if manifest.exists():
        parser.error("bundle already exists; choose a new directory for a new version")
    os.environ["HF_HOME"] = str(ROOT / ".work/huggingface")
    data = {}
    for part, repo in (("base", "allenai/specter2_base"), ("adapter", "allenai/specter2")):
        folder = directory / part
        if args.download:
            hub = importlib.import_module("huggingface_hub")
            hub.snapshot_download(
                repo_id=repo, revision=getattr(args, f"{part}_revision"),
                local_dir=str(folder), cache_dir=str(ROOT / ".work/huggingface/hub"),
                token=False,
                allow_patterns=["*.json", "*.bin", "*.safetensors", "vocab.txt"],
            )
        if not folder.is_dir():
            parser.error("model files missing; prepare files or explicitly use --download")
        data[f"{part}_repo"] = repo
        data[f"{part}_revision"] = getattr(args, f"{part}_revision")
        files = {}
        for path in sorted(folder.rglob("*")):
            if not path.is_file() or ".cache" in path.relative_to(folder).parts:
                continue
            if not path.resolve().is_relative_to(folder) or path.name.startswith(".env"):
                parser.error("model bundle contains an invalid path")
            files[str(path.relative_to(folder))] = file_hash(path)
        data[f"{part}_files"] = files
    with manifest.open("x") as saved:
        json.dump(data, saved, indent=2, sort_keys=True)
    verified_bundle(manifest)
    print(json.dumps({"status": "prepared", "manifest": str(manifest),
                      "live_inference_verified": False}))


if __name__ == "__main__":
    main()
