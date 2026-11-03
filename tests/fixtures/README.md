# Offline regression inputs

These fixtures let a fresh checkout run the same tests without API keys or a
developer database. They do not establish live provider readiness or independent
scientific ground truth.

Recorded snapshots and tokenizer assets preserve their original bytes through
`.gitattributes` (no line-ending conversion). GitHub groups these generated assets
separately so application code remains reviewable; the files remain available in
the repository for inspection and reproducible tests.

- `recorded-context.json.gz`: the two recorded source packs and parsed sections
  used by citation/page-boundary regressions; minimal source-job records;
  estimated cost/reservation summaries; the accepted LoRA relevance output; and
  frozen offline LoRA inputs. Source papers are
  [Attention v7](https://arxiv.org/abs/1706.03762v7) and
  [LoRA v2](https://arxiv.org/abs/2106.09685v2).
- `evaluation/`: only the 22 historical data files exercised by the regression
  suite. This includes public bibliographic/vector fixtures, citation annotations,
  selected original quotations, and recorded ranking/graph proposals. The RAG
  merge quotations come from [RAG v4](https://arxiv.org/abs/2005.11401v4).
- The minimal `accepted-job.json` contains only the historical ID needed by the
  accounting regression, not the original application's job payload.

No raw PDF, credentials, raw prompts, provider request IDs, private application
database, or account information is included. Recorded model judgments are agent
annotations, not an independent gold set. Historical timestamps and model IDs
describe their original runs, not current provider availability.

The session fixture reconstructs missing offline files in workspace-local
`.work` and ignored `evaluation/reports`. Existing files are never replaced.
Files created by the fixture are removed at session end, so tests do not seed the
normal application or clear existing circuit stops. Production startup does not
import this fixture loader. Tests block real network access.

The application's two Lineage readiness flags remain false regardless of fixture
contents. A fixture's recorded HTTP 200 or synthetic test vector is not evidence
of a new successful request.
