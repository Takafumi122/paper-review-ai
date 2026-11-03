# Paper Understanding Agent - Requirements Specification

Status: Canonical MVP requirements. Updated: 2026-09-19.

2026-09-19のユーザーDecisionを反映した要件の正本。
詳細仕様は [Architecture](architecture.md)、[Research Lineage](research-lineage.md)、
[Scoring](scoring-lineage.md)、[LLM Execution](llm-execution-spec.md)、
[Evaluation](evaluation.md)、[Evaluation Papers](thesis-choosing.md) を参照する。
承認済みDecisionと実装上の初期設定・未決事項の区別は
[ADR-0001](../ADR/0001-mvp-decisions.md) に記録する。

## 1. Document Purpose

This document defines the MVP requirements for a paper-understanding support system focused on LLM/NLP research papers.

The product is designed for university students who have introductory knowledge of machine learning and Transformer-based models but have limited experience reading academic papers.

The system does **not** aim to review or score papers.  
Its purpose is limited to helping readers understand:

1. the research background and lineage of the paper,
2. the logical path from problem to conclusion,
3. the minimum prerequisite knowledge required to follow that logic.

---

# 2. Product Vision

> Help students understand where an LLM/NLP paper came from, what it logically demonstrates, and what prerequisite knowledge is necessary to follow its argument.

The core user problem is that beginning paper readers often struggle not because the paper text is impossible to read, but because they lack:

- research context,
- understanding of the logical structure,
- domain-specific prerequisite knowledge.

The product addresses these three barriers directly.

---

# 3. Target User

## 3.1 Primary User

A university student who:

- has introductory machine learning knowledge,
- understands basic Transformer concepts,
- has limited experience reading academic papers,
- wants to read recent LLM/NLP papers,
- struggles to understand research history, specialist terminology, or the logical structure of a paper.

Example persona:

> A student who understands basic machine learning and Transformers but cannot easily determine how a new LLM paper fits into prior research, why its result matters, or which technical concepts are necessary to follow its reasoning.

---

# 4. Scope

## 4.1 Supported Research Domain

MVP scope is limited to:

- Computer Science
- Artificial Intelligence
- NLP
- LLM-centered Generative AI

Examples:

- Transformer
- Large Language Models
- Pre-training
- In-context Learning
- Instruction Tuning
- RLHF
- Preference Optimization
- RAG
- LLM Agents
- Parameter-Efficient Fine-Tuning
- LLM Evaluation

---

## 4.2 Supported Papers

A Target Paper must satisfy all of the following:

1. written in English,
2. available on arXiv,
3. full text can be retrieved,
4. primarily empirical or systems-oriented rather than theory-heavy.

Target input is an arXiv URL. Related/predecessor papers need not be on arXiv.
A related paper without sufficient accessible body text or reliable evidence
must not produce a `supported` Lineage Edge. Private/restricted manuscripts
are not uploaded or sent to the LLM.

---

## 4.3 Out of Scope

MVP does not guarantee support for:

- theory papers dominated by mathematical derivations,
- image-generation papers centered on Diffusion/GAN/VAE,
- speech generation,
- non-CS/AI papers,
- Japanese-language papers,
- paywalled-only papers,
- private or unpublished manuscripts,
- peer-review recommendation generation,
- accept/reject decisions,
- absolute paper quality scoring.

---

# 5. Success State

For MVP, a user is considered successfully supported if they can understand:

1. **Background**  
   Where the target paper sits in its research lineage.

2. **Logic**  
   How the paper moves from problem to conclusion.

3. **Prerequisite Knowledge**  
   Which concepts are essential to follow the reasoning steps.

This is a product-level success definition, not yet a formal user-study metric.

---

# 6. Core MVP Features

The MVP consists of three core features.

1. Research Lineage
2. Reasoning Flow
3. Just-in-Time Background Knowledge

The UI should primarily expose:

```text
Research Lineage
Reasoning Flow
    └── Learn prerequisite
```

Background Knowledge is embedded into Reasoning Flow rather than implemented as a separate primary page.

---

# 7. Research Lineage

## 7.1 Definition

Research Lineage is defined as:

> A sequence of prior works that were directly important in enabling the main contributions of the Target Paper.

Research Lineage is **not** equivalent to:

- all cited papers,
- the most highly cited papers,
- all semantically similar papers,
- all famous papers in the field.

---

## 7.2 Basic Structure

```text
Paper A
   ↓
Remaining Problem / Limitation
   ↓
Paper B
   ↓
Remaining Problem / Limitation
   ↓
Target Paper
```

The lineage should explain the transition between papers, not simply list paper titles.

---

## 7.3 Depth and Size

MVP constraints:

```text
max_depth = 3
max_predecessor_papers = 5
```

The Target Paper is excluded from the five-paper limit. Depth is the maximum
number of edges on any path to the Target Paper, not the number of paper nodes.
Shorten the lineage when sufficient evidence is unavailable.

The system may stop earlier if a shorter lineage sufficiently explains the target contribution.

Do not force the lineage to contain three generations.

---

## 7.4 Branching

Default representation is a single path.

One branch is allowed only when:

> The Target Paper's major contribution explicitly combines two distinct research lines, techniques, or ideas.

Example:

```text
Chain-of-Thought
       ┐
       ├── ReAct
Acting ┘
```

MVP constraints:

```text
max_branch_points = 1
max_incoming_lineages = 2
```

The expected shape is two research lines merging, principally at the Target
Paper. Branching/merging must be justified by the Target's major contribution.
Graph constraints and edge direction are defined in [Research Lineage](research-lineage.md).

Branching must not be used merely because multiple related papers exist.

---

## 7.5 Survey Papers

Survey papers are excluded from the primary lineage by default.

They may be used as supporting external sources but should not normally occupy a lineage node.

---

## 7.6 Dataset Papers

Dataset papers are not treated as Research Lineage nodes in MVP.

They may appear as contextual references.

---

## 7.7 Benchmark Papers

Benchmark papers are not treated as Research Lineage nodes in MVP.

They may appear as contextual references or evaluation context.

---

## 7.8 Same-Year Papers

Determine temporal precedence using the following priority:

```text
1. earliest verifiable public release date
2. arXiv first submitted date
3. publication date
4. publication year
```

Prefer the first date on which the research became publicly available.
Retain date precision and provenance; do not substitute a revised submission
date for first submission. Apply temporal checks to each predecessor edge.

A paper is treated as a predecessor only if it can be confirmed to have appeared before the Target Paper.

If order cannot be established, classify it as:

```text
contemporary_work
```

Do not force a predecessor relationship.

---

## 7.9 Independent / Contemporary Work

Independent work should be shown separately from the main lineage.

Example:

```text
Main Lineage

Paper A
  ↓
Paper B
  ↓
Target Paper


Contemporary / Independent Work

Paper X
Similar problem, different approach
```

Independent work is intended as comparison material, not as part of the causal research lineage.

---

# 8. Research Lineage Candidate Selection

Detailed scoring logic is documented separately in:

```text
docs/scoring-lineage.md
```

The high-level process is:

```text
Target Paper
    ↓
Contribution Extraction
    ↓
Research Problem Extraction
    ↓
Candidate Retrieval
    ↓
Candidate Ranking
    ↓
Contribution Relevance Evaluation
    ↓
Up to 5 Predecessor Papers (Target excluded)
    ↓
Lineage Construction
```

MVP candidate sources:

- Target Paper references
- Semantic Scholar recommendations

Candidate pool target: 30–50 papers; fewer are allowed when retrieval is incomplete.
Extract at most three main Target Contributions. Stage 1 selects at most Top 10:

```text
InitialScore = 0.40 × SemanticSimilarity
             + 0.35 × CitationRelation
             + 0.25 × CitationImportance

FinalScore = 0.40 × AdjustedContributionRelevance
           + 0.25 × SemanticSimilarity
           + 0.20 × CitationRelation
           + 0.15 × CitationImportance
```

All metrics are normalized to [0, 1]. Missing values remain `value=null` with
`missing_reason`, never zero. OpenAlex and NormalizedImpact are excluded from MVP.
Normalization, missing-value aggregation, and the unresolved adjustment policy
are specified in [Scoring](scoring-lineage.md). FinalScore is not EdgeConfidence.

On 2026-11-02 the user approved Stage 1 selection policy `lower-bound-v1`:
select at most Top 10 by the fixed-weight lower bound, then coverage, then canonical ID.
Keep InitialScore, nullable metrics, missing reasons, provenance and the upper bound separately.
All-missing candidates remain unranked. Stage 2 scoring and tie-breaks are unchanged;
the new selection policy does not establish quality acceptance or live readiness.

GPT-5.6 Luna evaluates Contribution Relevance using an integer rubric:

| Score | Meaning |
|---:|---|
| 0 | Target Contributionとほぼ無関係 |
| 1 | 同一研究領域だがLineage上の直接関係は弱い |
| 2 | Target Paperの前提となる関連研究 |
| 3 | Target Paperが解こうとした問題・制約を直接含む重要な先行研究 |
| 4 | Target Contributionの成立に直接つながる中核的先行研究 |

`ContributionRelevance = score / 4`.
`AdjustedContributionRelevance` is a separate field. On 2026-10-30 the user approved
`identity-v1`: it equals `ContributionRelevance` in the initial release. FinalScore
weights remain unchanged; live-contract and quality verification are still required.

Later extension:

- OpenAlex
- Crossref

---

# 9. Research Lineage Confidence

Each lineage edge receives a confidence value.

Confirmed MVP formula:

```text
EdgeConfidence
=
0.60 × ContributionRelationshipScore
+
0.40 × EvidenceConfidence
```

Classification:

```text
0.80 <= score <= 1.00 → supported
0.50 <= score < 0.80  → uncertain
0.00 <= score < 0.50  → insufficient_evidence
```

Use the unrounded score for classification. Evidence Confidence rubric:

| Score | Meaning |
|---:|---|
| 1.00 | 論文本文に直接的なEvidenceがある |
| 0.75 | Introduction / Related Work等から強く裏付けられる |
| 0.50 | 間接的なEvidenceのみ |
| 0.00 | Evidenceを確認できない |

Classify by support for the specific relationship, not by section title alone.
ContributionRelationshipScore evaluates the specific from/to edge, rather than
reusing the candidate's relevance to Target for every edge. Its operational rubric
is defined in [Scoring](scoring-lineage.md).

Hard rule:

```text
EvidenceConfidence == 0
→ insufficient_evidence
```

A strong LLM judgment without evidence must never be displayed as supported.

---

# 10. Research Lineage Evidence

Evidence is attached to the relationship itself, not only to the paper node.

Use both:

```text
LineageNode
LineageEdge
```

Example edge:

```json
{
  "from_paper_id": "paper_a",
  "to_paper_id": "paper_b",
  "relationship": "paper_b addresses a limitation of paper_a",
  "confidence": 0.0,
  "status": "insufficient_evidence",
  "evidence": []
}
```

---

# 11. Reasoning Flow

## 11.1 Purpose

Reasoning Flow structures the paper around:

> Why the conclusion follows from the problem, method, experiments, and results.

It is not intended to be a generic paper summary.

---

## 11.2 Node Types

Each Reasoning Step must use one of:

```text
problem
observation
hypothesis
approach
experiment
result
interpretation
conclusion
```

---

## 11.3 Internal Representation

The internal model is a graph / variable-length step structure.

The UI may render this as a simplified mostly linear flow.

Example:

```text
Problem
   ↓
Observation
   ↓
Approach
   ├── Experiment A
   │      ↓
   │    Result A
   │
   └── Experiment B
          ↓
        Result B
   ↓
Conclusion
```

---

## 11.4 Maximum Steps

```text
max_reasoning_steps = 10
```

If a paper contains many experiments, combine or omit secondary ones when necessary.

---

## 11.5 Multiple Experiments

Separate experiments when doing so clarifies the logical relationship between:

- method,
- evidence,
- result,
- conclusion.

Do not split experiments merely because the paper contains multiple tables.

---

# 12. Reasoning Flow Edges

MVP edge types:

```text
leads_to
evaluates
supports
```

Examples:

```text
Problem
--leads_to-->
Approach

Approach
--evaluates-->
Experiment

Result
--supports-->
Conclusion
```

Data model example:

```json
{
  "from_step_id": "step_3",
  "to_step_id": "step_5",
  "relation": "supports"
}
```

---

# 13. Reasoning Step Evidence

Each Reasoning Step should retain evidence pointing back to the target paper.

Example:

```json
{
  "id": "step_4",
  "type": "result",
  "summary": "...",
  "sources": [
    {
      "paper_id": "...",
      "section": "4. Experiments",
      "page": 7,
      "quote_or_span": "..."
    }
  ]
}
```

---

# 14. Just-in-Time Background Knowledge

## 14.1 Definition of Required Knowledge

A concept is required only if:

> A reader who does not know the concept cannot understand why one Reasoning Step follows from another.

The goal is not to explain every technical term in the paper.

---

## 14.2 Example

Reasoning Step:

```text
Removing recurrence enables more parallel computation during training.
```

Required concepts:

- RNNs process sequence steps sequentially
- what parallel computation means

Not required:

- LSTM gate equations
- GRU internals

---

# 15. Knowledge Necessity Scoring

For each candidate concept, the LLM returns:

```json
{
  "concept": "RNN sequential processing",
  "reasoning_step_id": "step_2",
  "necessary": true,
  "necessity_score": 3,
  "reason": "..."
}
```

Rubric:

| Score | Meaning |
|---:|---|
| 0 | The reasoning can be understood without this concept |
| 1 | Helpful but not necessary |
| 2 | Important to understanding the step |
| 3 | The causal relation is difficult to understand without it |

MVP display rule:

```text
necessity_score == 3
```

Only score-3 concepts are treated as required prerequisite knowledge.

This intentionally favors precision over recall for the first version.

---

# 16. Background Knowledge Output

MVP output:

```text
Concept
Why you need this
Short Explanation
Source
```

Optional detailed explanation is not generated by default.

Detailed explanation is generated only when the user requests it.

This reduces:

- information overload,
- LLM cost,
- unnecessary generation.

---

# 17. Context-Aware Explanation

Background Knowledge explanations should be written in the context of the target paper.

Avoid generic dictionary-style explanations when possible.

Preferred:

> Self-Attention is relevant here because the paper replaces recurrent sequence processing with direct token-to-token interactions.

Less preferred:

> Self-Attention is an attention mechanism introduced in Transformers.

---

# 18. Source and Evidence Model

Claims should retain source-level traceability.

Example:

```json
{
  "claim": "RNNs are difficult to parallelize because sequence states are processed sequentially.",
  "sources": [
    {
      "paper_id": "...",
      "section": "Introduction",
      "page": 2,
      "quote_or_span": "..."
    }
  ]
}
```

---

# 19. Source Priority

Use the following priority order:

```text
1. Target Paper
2. Related Paper
3. Author / Project official source
4. Reliable Academic / Technical source
```

This is the MVP Background Knowledge source priority. Prefer higher-priority
sources when they support the claim. Academic API metadata remains authoritative
for bibliographic fields, but is not sufficient evidence for a causal relationship.
No unlimited Web Search is implemented. Bounded source retrieval is specified in
[Architecture](architecture.md).

---

# 20. Unsupported Claims

The system must not confidently assert unsupported claims about:

- author motivation,
- community consensus,
- research importance,
- causal research relationships.

Allowed statuses:

```text
supported
uncertain
insufficient_evidence
```

If evidence cannot be verified, return uncertainty instead of generating a plausible explanation.

---

# 21. Paper Parsing Scope

MVP parsing requirements:

| Content | Support |
|---|---|
| Main text | Required |
| Section structure | Required |
| References | Required |
| Page numbers | Required when technically available |
| Tables | Partial support |
| Figures | Limited support |
| Equations | Preserve original text / representation where possible |
| Appendix | Supported |
| Footnotes | Low priority |

---

# 22. Figure and Table Limitation

If a claim depends strongly on a table or figure and sufficient evidence cannot be extracted:

```text
status = uncertain
```

The LLM must not reconstruct missing evidence from assumptions.

---

# 23. Academic APIs

## 23.1 MVP

Use:

```text
arXiv
Semantic Scholar
```

---

## 23.2 arXiv Role

Use arXiv for:

- target paper metadata,
- PDF retrieval,
- abstract,
- authors,
- submission date,
- category metadata.

---

## 23.3 Semantic Scholar Role

Use Semantic Scholar for:

- references,
- citation metadata,
- recommendations,
- influential citation metadata where available,
- citation context where available.

---

## 23.4 Later APIs

Not required for initial MVP:

```text
OpenAlex
Crossref
```

OpenAlex may later provide normalized bibliometric metrics.

Crossref may later serve as metadata fallback / DOI resolution.

---

# 24. External IDs

The paper model must support:

```json
{
  "external_ids": {
    "arxiv": null,
    "doi": null,
    "semantic_scholar": null,
    "openalex": null
  }
}
```

This prevents later API integration from requiring a database redesign.

---

# 25. Pipeline Architecture

MVP uses:

> A deterministic workflow with LLMs only where semantic judgment is required.

The workflow itself is controlled and reproducible.

```text
Paper Retrieval
        ↓
Parser                         deterministic
        ↓
Reference Retrieval            deterministic
        ↓
Candidate Retrieval            deterministic
        ↓
Metric Calculation             deterministic
        ↓
Contribution Evaluation        LLM
        ↓
Lineage Construction           LLM
        ↓
Reasoning Extraction           LLM
        ↓
Concept Extraction             LLM
        ↓
Knowledge Generation           LLM
```

Candidate/source retrieval is bounded and controlled by application code.
The project is **not** a fully autonomous agent architecture.

---

# 26. LLM Execution Specification

Detailed LLM execution rules are defined in:

```text
docs/llm-execution-spec.md
```

High-level decisions:

- OpenAI Responses API
- `gpt-5-nano` for low-cost extraction / classification
- `gpt-5.6-luna` for reasoning-heavy tasks
- provider abstraction required
- Structured Output required
- Pydantic validation
- task-specific timeout
- prompt versioning
- prompt hash
- schema version
- pipeline version
- per-paper cost guard

---

# 27. LLM Model Roles

## GPT-5 nano

Use for:

- extraction from retrieved text where needed (API metadata is never guessed),
- contribution candidate extraction,
- research problem extraction,
- concept extraction,
- citation classification,
- evidence extraction,
- short Background Knowledge.

---

## GPT-5.6 Luna

Use for:

- contribution relevance,
- lineage edge evaluation,
- candidate comparison,
- Reasoning Flow generation,
- difficult semantic fallback.

---

# 28. Async Job State Machine

Paper analysis runs asynchronously.

State machine:

```text
queued
  ↓
parsing
  ↓
retrieving_candidates
  ↓
ranking_candidates
  ↓
building_lineage
  ↓
building_reasoning
  ↓
building_knowledge
  ↓
completed
```

Error states:

```text
failed
partial_completed
```

---

# 29. Partial Completion

`partial_completed` is allowed.

Example:

```text
Research Lineage: success
Reasoning Flow: success
Background Knowledge: failed
```

The successful components should remain available.

Do not discard the entire job because one later stage fails.

---

# 30. Retry Semantics

Standardize terminology:

```text
max_attempts = 3
max_retries = 2
```

That means:

- first attempt,
- up to two retries.

Retry rules are defined in detail in `docs/llm-execution-spec.md`.

---

# 31. Cache and Re-Execution

Reuse existing analysis when all of the following match:

```text
Paper
Paper revision / content hash / parser version
Pipeline Version
Prompt Version / prompt hash / schema version
Provider / Model / generation settings
Input source hashes / scoring policy version
```

A change to any relevant execution version allows re-analysis.

Example:

```text
prompt_version = 2.0.0
```

→ previous result does not block a new run.

---

# 32. Evaluation Strategy

Evaluate the three product functions independently.

| Function | Primary Evaluation |
|---|---|
| Research Lineage | relevance of selected predecessors |
| Reasoning Flow | correctness of logical structure |
| JIT Knowledge | precision / recall of necessary concepts |

---

# 33. Research Lineage Evaluation

Compare:

```text
Baseline A:
Citation Count

Baseline B:
Semantic Similarity

Proposed:
Multi-metric Ranking
+
Contribution Relevance
```

Useful metrics:

```text
Precision@K
Recall@K
NDCG@K
Human Evaluation
```

---

# 34. Reasoning Flow Evaluation

Human labels:

```text
correct
partially_correct
incorrect
```

Evaluate:

- Problem accuracy
- Approach accuracy
- Experiment/result mapping
- Result/conclusion distinction
- logical edge correctness
- evidence validity

---

# 35. JIT Knowledge Evaluation

For each concept ask:

> Could a target user understand why this Reasoning Step follows from the previous step without knowing this concept?

Manual labels:

```text
required
optional
unnecessary
```

System output can then be evaluated with precision / recall.

---

# 36. Evaluation Ground Truth

Ground truth is manually annotated for MVP.

This is an internal system-evaluation dataset, not claimed to be an objective scientific gold standard.

For each evaluation paper, create:

## Research Lineage

```json
{
  "required_predecessors": [],
  "acceptable_predecessors": [],
  "incorrect_predecessors": []
}
```

## Reasoning Flow

Manually annotate the major:

```text
Problem
Approach
Experiment
Result
Conclusion
```

plus relevant branches.

## JIT Knowledge

Annotate 5–10 candidate concepts as:

```text
required
optional
unnecessary
```

---

# 37. Evaluation Paper Set

Use the following 10 papers.

| # | Paper | Year | Main Lineage Test |
|---:|---|---:|---|
| 1 | Attention Is All You Need | 2017 | RNN/CNN → Attention → Transformer |
| 2 | Language Models are Few-Shot Learners | 2020 | GPT → Scaling → In-context Learning |
| 3 | Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks | 2020 | Parametric LM + Retrieval |
| 4 | LoRA: Low-Rank Adaptation of Large Language Models | 2021 | Fine-tuning → PEFT |
| 5 | Training language models to follow instructions with human feedback | 2022 | LM → Instruction Tuning → RLHF |
| 6 | Chain-of-Thought Prompting Elicits Reasoning in Large Language Models | 2022 | Few-shot Prompting → Reasoning |
| 7 | ReAct: Synergizing Reasoning and Acting in Language Models | 2022 | Reasoning + Acting |
| 8 | Direct Preference Optimization | 2023 | RLHF → Preference Optimization |
| 9 | QLoRA: Efficient Finetuning of Quantized LLMs | 2023 | LoRA + Quantization |
| 10 | Self-RAG | 2023 | RAG → Adaptive Retrieval + Self-reflection |

Detailed rationale should live in:

```text
docs/thesis-choosing.md
```

---

# 38. UI Requirements

UI and generated explanations are Japanese. Original Evidence quotes are English
and remain verbatim. Preserve proper names, titles, and bibliographic metadata.
English examples in this specification describe structure, not UI copy.

## 38.1 Primary Result View

Show:

```text
Research Lineage
Reasoning Flow
```

Background Knowledge is embedded within Reasoning Flow.

---

## 38.2 Reasoning Step Interaction

Example:

```text
Reasoning Step
    │
    └── Learn prerequisite
```

Clicking a prerequisite shows:

```text
Concept
Why you need this
Short explanation
Source
```

Optional detailed explanation is generated on demand.

---

# 39. Error UX

Do not hide uncertainty.

Examples:

```text
Not enough related papers were retrieved.

Research Lineage confidence: Low
```

Internal / UI states:

```text
supported
uncertain
insufficient_evidence
```

The system must prefer incomplete output over unsupported output.

---

# 40. Non-Functional Requirements

## 40.1 Performance

Initial MVP target:

```text
analysis_time <= 5 minutes
```

This is a soft target to measure and improve, not a hard SLA.

---

## 40.2 LLM Cost

Initial-analysis target per paper:

```text
LLM cost <= USD 1.00
```

Cost must be measurable per analysis job. User-requested detailed JIT Knowledge
generation is recorded separately from initial analysis. Also retain cumulative
paper-level cost across all analyses, retries and on-demand generations.

---

## 40.3 Maximum Paper Size

MVP:

```text
<= 50 pages
```

Papers exceeding this may be rejected or marked unsupported.

---

## 40.4 Concurrent Usage

Target:

```text
personal / portfolio-scale usage
```

No high-scale concurrency requirement for MVP.

---

## 40.5 Availability

No strict SLA.

---

## 40.6 Logging

Log by Pipeline Step.

Required examples:

- start/end time,
- status,
- external API failure,
- LLM tokens,
- estimated LLM cost,
- validation failure,
- retry count.

---

# 41. Security and Data Retention

Target input supports publicly available arXiv papers. Related papers may be non-arXiv.

Rules:

1. Do not support private or access-restricted manuscripts.
2. Retrieved PDF files are not permanently retained.
3. PDFs may exist temporarily during parsing and should be deleted after processing.
4. Only necessary text sections should be sent to the external LLM.
5. Raw paper body text should not be stored in application logs.
6. Metadata and derived structured outputs may be persisted.
7. PaperSection (section, page, text) extracted from public arXiv papers may be
   persisted in the DB for evidence revalidation, Reasoning regeneration,
   prompt-version reruns and on-demand JIT Knowledge.
8. Store revision/content hash and span locations so evidence stays reproducible.
9. Do not interpret this permission as blanket permanent full-text storage of
   non-arXiv sources; default to necessary evidence excerpts and metadata there.
10. Clean temporary PDFs on both success and failure; recover orphaned temporary
    files after crashes. Do not put body text, prompts containing it, or raw
    provider responses into application/error logs.

---

# 42. Repository Documentation Requirements

The repository should make the following understandable to a reviewer:

1. Why the project exists
2. What user problem it solves
3. Why the architecture looks this way
4. How the Research Lineage algorithm works
5. How outputs are evaluated
6. Which parts failed or were improved
7. What is deterministic vs LLM-based

Recommended structure:

```text
README.md

docs/
├── requirements.md
├── architecture.md
├── research-lineage.md
├── scoring-lineage.md
├── llm-execution-spec.md
├── evaluation.md
└── thesis-choosing.md

ADR/
```

---

# 43. README Problem Statement

Suggested product motivation:

> When reading LLM research papers, a reader with only foundational machine-learning knowledge may understand individual sentences while still failing to understand the paper's research context, logical structure, or specialist concepts.
>
> This project supports paper understanding by combining Research Lineage, Reasoning Flow, and Just-in-Time Background Knowledge.

---

# 44. Architecture Rationale

The architecture maps directly to three identified user problems.

```text
User cannot understand the research context
        ↓
Research Lineage


User cannot follow the logical argument
        ↓
Reasoning Flow


User lacks specialist prerequisite knowledge
        ↓
Just-in-Time Background Knowledge
```

This direct mapping should be emphasized in repository documentation.

---

# 45. Core Domain Entities

At minimum, the domain model should include:

```text
Paper
PaperSection
ExternalIds

ResearchLineage
LineageNode
LineageEdge

ReasoningFlow
ReasoningStep
ReasoningEdge

KnowledgeItem

Claim
Source
Evidence

AnalysisJob
LLMExecution
```

Detailed database schema will be defined during system design.

---

# 46. MVP Definition of Done

The MVP is complete when all of the following are possible.

## Paper Input / Parsing

- [ ] Accept an arXiv paper
- [ ] Retrieve the full text
- [ ] Extract sections
- [ ] Extract references
- [ ] Preserve page references when possible

## Research Lineage

- [ ] Extract Target Paper contributions
- [ ] Retrieve candidate predecessor papers
- [ ] Rank candidates
- [ ] Select up to 5 predecessor papers, excluding Target
- [ ] Support paths of at most 3 edges to Target; allow shorter paths
- [ ] Support at most one contribution-justified merge of two incoming lineages
- [ ] Separate contemporary work from predecessors
- [ ] Store LineageEdge evidence
- [ ] Return confidence state

## Reasoning Flow

- [ ] Generate up to 10 Reasoning Steps
- [ ] Use the predefined Step Types
- [ ] Support branching
- [ ] Support the three Edge Types
- [ ] Attach paper evidence to steps

## Just-in-Time Knowledge

- [ ] Extract candidate prerequisite concepts
- [ ] Score concept necessity
- [ ] Display only required concepts in MVP
- [ ] Generate short, paper-context-aware explanations
- [ ] Generate detailed explanation only on demand

## Reliability

- [ ] Every important claim can retain a Source
- [ ] Unsupported relationships can return `uncertain`
- [ ] Missing evidence can return `insufficient_evidence`
- [ ] Figure/table-dependent claims do not get fabricated

## Execution

- [ ] Pipeline runs asynchronously
- [ ] `partial_completed` is supported
- [ ] Cache can reuse identical analysis configurations
- [ ] LLM usage and cost are measurable
- [ ] Initial and on-demand costs are separate; cumulative paper cost is available
- [ ] UI/explanations are Japanese and original Evidence is English
- [ ] Public arXiv section text is reusable in DB; temporary PDFs are deleted

## Evaluation

- [ ] The 10-paper evaluation set exists
- [ ] Manual annotations exist
- [ ] Research Lineage baseline comparison exists
- [ ] Reasoning Flow human evaluation exists
- [ ] JIT Knowledge evaluation exists

---

# 47. Deferred Decisions

Phase 0 technical defaults are documented in [Architecture](architecture.md).
The following do **not** block Phase 0 and may be finalized during their implementation phases:

- database technology,
- queue implementation,
- frontend framework,
- backend framework,
- PDF parsing library,
- graph visualization library,
- deployment environment,
- quality thresholds and annotation procedure before final evaluation.

Redis, Vector DB and Agent Framework are not needed in the initial architecture.
OpenAlex/Crossref integration is outside MVP. Remaining specification gates,
including AdjustedContributionRelevance, are tracked in [ADR-0001](../ADR/0001-mvp-decisions.md).

---

# 48. Next Phase

Requirements definition is sufficiently complete to begin Phase 0 infrastructure.
This does not imply that Stage 2 ranking or quality acceptance is fully settled.

The next phase should proceed in this order:

```text
1. System Architecture
2. Database Schema
3. API Design
4. Repository Structure
5. ADRs for major technology choices
6. Issue / Task Breakdown
7. Implementation
```
