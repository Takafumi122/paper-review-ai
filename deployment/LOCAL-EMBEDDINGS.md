# Optional CPU image for local SPECTER2

The default image keeps metadata-only deployments free of ML dependencies.
Build the optional CPU runtime with:

```sh
docker build --build-arg LOCAL_EMBEDDINGS=true -t paper-review-ai:local-runtime .
```

The pinned torch/adapters/transformers/numpy versions match the Linux evaluation.
This build does not copy models, databases or credentials. The Docker context
allowlist includes only the existing model-preparation script from `scripts/`.
On Railway, a `LOCAL_EMBEDDINGS=true` service variable supplies the declared
Dockerfile build argument ([Railway Dockerfile documentation](https://docs.railway.com/builds/dockerfiles)).
The existing production service explicitly uses this CPU build. New deployments remain opt-in. See [the current rollout status](../docs/graph-publication-status.md).

The Railway launcher can prepare and verify the pinned public bundle on the mounted
volume when `PAPER_REVIEW_PREPARE_LOCAL_SPECTER2=true` is explicitly configured.
It does so after dropping to UID10001; no SSH key or provider key is used for the
public model download. Alternatively, an operator can run the following as the
application user (UID10001), with `/app` as the working directory:

```sh
python scripts/prepare_local_specter2.py \
  --directory /app/.work/models/specter2 \
  --base-revision 3447645e1def9117997203454fa4495937bfbd83 \
  --adapter-revision 2081559630a80fc5851d8f798a05ba81e9468089 \
  --download
```

This downloads only public model assets with Hugging Face authentication disabled.
The script fails if the destination already has a manifest; inspect it rather
than overwriting an existing bundle. A model revision change requires a new
bundle directory. The manifest records all file hashes; the runtime revalidates
them and requires an active proximity adapter before inference.

The volume is mounted at runtime, so do not prepare it during image build or a
pre-deploy step. The source bundle is approximately hundreds of MB and requires
both disk space and outbound public download access. No developer `.env`, local
DB or cached paper text needs to be uploaded. The owner supplies provider and
invitation secrets directly through Railway Variables.

After preparation, configure
`PAPER_REVIEW_LOCAL_SPECTER2_MANIFEST=/app/.work/models/specter2/manifest.json`.
The retrieval fingerprint includes the bundle/runtime identity. Keep
`PAPER_REVIEW_OPENALEX_LOCAL_VERIFIED=false` and
`PAPER_REVIEW_OPENALEX_LOCAL_AUDITED=false` until the release-quality decision is
accepted. A runtime build or embedding smoke test does not open these gates.

The existing production service now has a 2-GB limit and one Worker. The latest
RAG operational canary peaked at 1,196,605,440 bytes; that is one paper, not a
qualified maximum for every paper. Do not deploy this runtime into a 1-GB limit.
The current status and quality waiver are documented in
[the publication summary](../docs/graph-publication-status.md).

The CPU runtime retains transformers4.57.6 because adapters1.3.0 requires that
series. Known advisories remain; arbitrary models/remote code must not be loaded.
The pinned and content-verified model is a mitigation, not a claim that all
runtime vulnerabilities are fixed. certifi/Mako/Pillow/source-map-js are updated
through the published dependency locks/constraints.
