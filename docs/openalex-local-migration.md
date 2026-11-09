# 現行版の案内

最新はfull-v44 / openalex-local-v7です。[公開状態](graph-publication-status.md)と[現在の配置手順](../deployment/RAILWAY.md)を参照してください。既存本番は数値品質の例外承認で暫定有効化されました。正式品質合格とは区別し、原文による各辺の審査を維持します。

以下の観測・費用・版・未決事項は各実行時点の履歴です。古いページ上限や「本番未変更」の記述を現状として使わないでください。履歴で参照するevaluation/reportsの開発用生レポートはGitへ含めず、現在の集計は上記公開状態に記載しています。

---

# OpenAlex + local SPECTER2 implementation

Status: opt-in implementation; live API/model contract observed, quality not accepted.
Branch: `codex/openalex-local-specter2`.

The user requested implementation of the previously discussed S2 alternative.
On 2026-11-03 the user authorized consecutive live verification and the specification
adoption decision. Canonical requirements now permit this explicitly selected
alternative. The compatibility default remains S2 and there is no automatic fallback.
Production Lineage remains blocked pending quality acceptance and unresolved gates.

## Behavior

- `semantic_scholar` remains the default. Select `openalex_local` explicitly.
- The alternative does not construct an S2 client or change S2 stop records.
- Parsed references are resolved using stable IDs, or exact normalized title plus
  first-author surname and year. A known ID cannot be replaced by title guessing.
- The target's OpenAlex record requires a corroborated arXiv/DOI/OpenAlex ID.
  No assumed arXiv singleton endpoint is used: DOI/OpenAlex filters or bounded
  search followed by arXiv-location verification establish identity. Version v3
  first batches exact arXiv landing-page URLs, including HTTP and HTTPS variants,
  before bounded title discovery.
- OpenAlex references and up to 10 related works supplement parsed references.
  Maximum 50 candidates and 40 HTTP requests per retrieval execution, including
  all lookups and discovery. No retries or redirect following; an error closes
  that client's network window. Existing cached data can still be used.
  The Job's request reservations and stop state persist in SQLite across restarts;
  restarting a retrieval stage does not reset its request budget.
- Unresolved occurrences and reasons are retained. API absence is not proof of
  non-citation. The alternative never asserts reference completeness, so a related
  work's CitationRelation remains null unless a direct citation is confirmed.
- `openalex-local-v2` admits identified papers before unresolved placeholders.
  Original reference outcomes remain available when placeholders exceed the pool.
  Literal-title search removes wildcard/fuzzy characters before discovery; the
  original identity corroboration and fixed scoring rules remain unchanged.
- Dates retain precision and source. OpenAlex's publication date describes its
  primary location; it is not represented as verified first public release.
  Full analysis additionally verifies first submission dates through the arXiv
  Atom API, selecting the unique requested identity/version within batch results.
  These requests share the 40-request retrieval budget, use a separate client
  without OpenAlex authorization, and retain source/hash provenance. Original
  OpenAlex publication dates and citation years remain available independently.
- Version `openalex-local-v4` also normalizes comma-form author names such as
  `Wei, Jason` to surname `Wei` for reference/citation identity. This corrects
  citation context discovery without changing citation importance or weights.
- Abstracts are reconstructed only from contiguous, non-conflicting token indices.
  Missing abstract means missing embedding, not a title-only substitute.
- Local SPECTER2 uses the official base and proximity adapter, title + SEP +
  abstract, truncation at 512 tokens and CLS pooling. It runs CPU batches of 8
  inside the Worker, in eval/inference mode. No vector database or model server.
- Local bundles are content-hashed and inference never downloads model files.
  Missing packages/model, malformed vectors, zero norm, dimension mismatch and
  inference failure preserve nullable similarity and independent reading features.
- Metadata has 24-hour cache TTL (empty results 5 minutes). Embeddings are keyed
  by bundle contents, runtime package versions, preprocessing and input hash.
  SQLite files and model/cache assets stay under `.work`. No raw HTTP body or
  credentials are stored. Normalized public bibliographic metadata is cached.
- Weights, lower-bound-v1 selection, Top 10, citation rubric/max aggregation,
  identity-v1 and Evidence/Graph validators remain unchanged.
- Alternative readiness is separate from S2 readiness. Successful offline tests
  do not enable Contribution/Graph generation for live use.

## Prepare and start

Install the optional runtime only on the machine running local inference:

```sh
UV_CACHE_DIR=.work/uv-cache uv sync --extra local-embeddings
```

ML dependencies are justified solely by the explicitly requested local embedding
inference. They are not installed by default. This CPU implementation has no GPU
requirement; Linux's default PyTorch distribution may nevertheless install CUDA
packages. Size and peak memory must be measured before hosted rollout, especially
on the existing 512 MB demo. The initial implementation did not download weights;
the later authorized live verification prepared and exercised the pinned bundle.

Choose immutable full commit SHAs from the official model repositories' history:

- https://huggingface.co/allenai/specter2_base/commits/main
- https://huggingface.co/allenai/specter2/commits/main

Then explicitly prepare the public model bundle (about 445 MB of model assets):

```sh
UV_CACHE_DIR=.work/uv-cache uv run --no-env-file --extra local-embeddings \
  python scripts/prepare_local_specter2.py --download \
  --base-revision BASE_FULL_COMMIT_SHA --adapter-revision ADAPTER_FULL_COMMIT_SHA
```

The script uses no Hugging Face token. Alternatively place pinned local model
files in `base/` and `adapter/`, then run without `--download` to build the manifest.
Use a new directory for each bundle version. The runtime verifies all file hashes.

With `OPENAI_API_KEY` and `OPENALEX_API_KEY` already supplied externally:

```sh
export PAPER_REVIEW_LOCAL_SPECTER2_MANIFEST="$PWD/.work/models/specter2/manifest.json"
UV_CACHE_DIR=.work/uv-cache uv run --no-env-file --extra local-embeddings \
  python -m paper_review.server --academic-backend openalex_local
```

Do not use `deployment/start_demo.py` for this verification: it explicitly enables
reading-only mode. Check `/health` for backend, model identity and separate readiness.
The previous S2 startup remains available. No `.env` access is required.

Use a separate workspace-local database for alternative verification. Jobs store
their retrieval configuration and reject resumption under changed configuration.
The active-job fingerprint includes alternative model/configuration identity, so
S2, local model revisions and reading-only analyses cannot deduplicate together.

## Live observations (2026-11-03)

OpenAlex authenticated metadata and real CPU inference succeeded. Model pins:
base `3447645e1def9117997203454fa4495937bfbd83`, proximity adapter
`2081559630a80fc5851d8f798a05ba81e9468089`. Runtime: torch 2.15.0,
transformers 4.57.6, adapters 1.3.0. Vectors were finite, nonzero and 768-dimensional.
The active adapter changed output relative to disabling it. Identical batches
repeated exactly; different batch shapes differed by at most 9.54e-7 in the smoke
comparison. Do not require bitwise equality across batch shapes or hardware.

The three-paper retrieval run peaked at 1,140,768,768 bytes (~1.06 GiB RSS) on macOS.
This is not a hosted/Linux or combined API+Worker memory measurement. Do not
assume the existing 512 MB or 1 GiB demo can host this runtime; no deployment was made.

Ten-paper retrieval/embedding diagnostics were attempted, with explicit limits:
GPT-3/InstructGPT exceed the manifest's 50-page allowance, CoT and Self-RAG have
metadata/index-only coverage, DPO cache is v3 rather than the manifest's v2, and
Self-RAG's requested v2 is unavailable (official page lists v1). Do not silently
substitute these for a successful ten-paper full-text evaluation.

Attention and ReAct produced validator-accepted single paths in scoped diagnostics.
LoRA exhausted three Relevance attempts with `relevance_evidence_wrong_candidate`.
These findings and missing core predecessors prevent production quality acceptance.
The local report is `evaluation/reports/openalex-local-live-2026-11-03/RESULTS.md`.

## Follow-up observations (completed 2026-11-04)

The follow-up repaired arXiv ID/date resolution and author surname matching.
Relevance input now includes code-owned candidate-to-source alias options derived
only from sources actually included in the request; evidence validation is unchanged.
All six scoped diagnostics (Attention, LoRA, ReAct, CoT v6, DPO v2 and Self-RAG v1)
completed validator-accepted graphs. All 21 quoted evidence snippets exactly matched
their source spans. This checks grounding mechanics, not independent human quality.
ReAct now includes CoT and Inner Monologue; LoRA includes Intrinsic Dimensionality.
The initial DPO classification failures and a timeout remain recorded alongside
the corrected-input run. Self-RAG v1 remains an explicit diagnostic substitution.

Combined Linux API+Worker measurements used CPU PyTorch and cached public LoRA
source, with live embedding and LLM calls. At 1 GiB the reading features completed,
Lineage remained gated, and memory reached the limit. At 2 GiB measured peak was
1,212,276,736 bytes with no limit hits/OOM, but reasoning stopped at the LLM budget
reservation guard. This does not establish a successful full run at 2 GiB or hosted
capacity. Both temporary containers were removed; no deployment/readiness changed.

Additional used/held LLM cost was USD 0.92792232 of the authorized USD 1, including
a USD 0.036 timeout reservation. The report and independent human-review packet
are in `evaluation/reports/openalex-followup-2026-11-03/`. Historical reports remain
unchanged at the time of that run. D2 quality acceptance and D3 page/version
decisions remained unresolved at its completion.

On 2026-11-04, the user proposed increasing the page limit to 75. Requirements,
the full-analysis gate and manifest v1.1.0 now use 75 pages including appendices.
GPT-3 v4 (75 pages) and InstructGPT v1 (68 pages) are within that limit. This
resolves their page-limit block, not their pending full analyses. No new live LLM
calls accompanied this policy change. Version/page-count discrepancies for other
papers and independent quality acceptance remain pending; earlier reports retain
the 50-page policy applicable when they were written.

The subsequent 2026-11-04 source audit fixed manifest v1.2.0 to CoT v6/43 pages,
DPO v2/27 pages and Self-RAG v1/30 pages, based on official PDFs/version history.
GPT-3 v4/75 pages and InstructGPT v1/68 pages were fully extracted with page-level
source checks. A newly observed alphanumeric bibliography-label defect was fixed
in parser `pdfplumber-0.2.1`; source text and prior reports remain unchanged.
See `evaluation/reports/evaluation-sources-2026-11-04/` for details and limitations.

## Remaining acceptance work

The 2026-11-05 recall repair uses configuration `openalex-local-v5`, so older
retrieval checkpoints cannot resume as if they used the repaired policy. The
resolver reuses unexpired normalized OpenAlex metadata across discovery queries;
stable ID checks and exact normalized full title/first-author/year corroboration
remain required. Discovery queries omit trailing venue prose while validation
continues to use the entire original reference.

An identifier-less OpenAlex record can be treated as an alias only when exactly
one matching record has stable identifiers, at least three complete ordered author
names match exactly after normalization, and full title, year and publication
date agree. Multiple identified records, conflicting dates/authors/DOIs and
insufficient author lists remain ambiguous. The canonical record retains alias
IDs and content hashes in provenance; aliases cannot re-enter the pool as extra
candidates. Shared stable IDs across distinct records retain the existing
ambiguity handling. This does not change scoring, Top-K or Graph-readiness gates.

Paired offline snapshot replay improved required-candidate Recall from 0.8333 to
0.8889 on six labelled papers. The historical 0.75 used different cache conditions
and is not the paired baseline. No new provider calls were made; live freshness,
fresh-query responses and all-ten-paper quality remain unverified. Report:
`evaluation/reports/recall-repair-2026-11-05/`.

Automated tests use mocked HTTP/runtime and do not prove live readiness. Before enabling
`PAPER_REVIEW_OPENALEX_LOCAL_VERIFIED` or `PAPER_REVIEW_OPENALEX_LOCAL_AUDITED`, verify
the real API response and local model contract, then audit Stage 1 coverage/Top 10
with human annotations. Existing S2 flags do not enable the alternative.

Compare embeddings on a fixed candidate set first, then measure retrieval recall
with the new source. Start with Attention/LoRA/ReAct, then the designated ten papers.
Preserve fresh reports and do not overwrite S2 evaluation artifacts. Sources:

- https://help.openalex.org/api/authentication/ (Bearer-header authentication)
- https://help.openalex.org/api/get-single-entities/ (ID/filter lookup)
- https://help.openalex.org/data/works/attributes/ (references, related works, dates, abstract)
- https://huggingface.co/allenai/specter2_base (official inference recipe)
