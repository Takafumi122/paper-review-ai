# Deprecated / superseded

2026-09-19: この文書は旧案・履歴資料です。現行仕様は [research-lineage.md](research-lineage.md)、[scoring-lineage.md](scoring-lineage.md) を参照してください。
旧本文を以下にそのまま保存しています。旧本文の重み、API構成、入力範囲、未決事項は現行仕様ではありません。

---

# Research Lineage 論文選定方針・外部API設計メモ

## 1. 目的

本ドキュメントは、論文理解支援AIエージェントにおける `Research Lineage` 機能について、

- 重要な先行研究をどのように探索するか
- どの外部APIを利用するか
- 候補論文をどの指標で評価するか
- 最終的なResearch Lineageをどのように構成するか

を整理するための設計叩き台である。

Research Lineageでは、単純に被引用数の多い論文やReferences上の論文を並べるのではなく、

> Target Paperの主要なContributionにつながった研究上の流れ

を抽出することを目的とする。

---

# 2. 基本方針

重要な先行研究の選定は、単一指標ではなく複数指標を組み合わせて行う。

利用候補の指標は以下。

1. Citation Relation
2. Semantic Relevance
3. Citation Importance
4. Normalized Research Impact
5. Contribution Relevance

これらを用いて候補論文をスコアリングし、最終的に3〜5本程度の重要論文へ絞り込む。

---

# 3. Research Lineageの定義

Research Lineageは単なるCitation Graphではない。

以下のような研究上の流れを表現する。

```text
Previous Research
      ↓
Remaining Problem / Limitation
      ↓
Next Research
      ↓
Remaining Problem / Limitation
      ↓
Target Paper
      ↓
Key Contribution
```

MVPでは主に以下までを対象とする。

```text
Previous Work
      ↓
Unsolved Problem
      ↓
Target Paper
```

Target Paper以降のSubsequent Workは将来機能とする。

---

# 4. 外部API構成

## 4.1 Semantic Scholar API

### 役割

Research Lineage候補論文の探索と、論文間の関係性分析の主軸として使用する。

### 利用候補データ

- Paper Search
- References
- Citations
- Citation Count
- influentialCitationCount
- Citation Context
- Citation Intent
- isInfluential
- SPECTER2 Embedding
- Recommendations API
- Author Metadata

### 主な用途

#### Candidate Generation

Target Paperから以下を取得する。

```text
Target Paper
   ├─ References
   ├─ Recommended Papers
   └─ Related Papers
```

#### Citation Importance

単純に引用されているかではなく、

```text
Target Paper
      ↓
Candidate Paperを
「なぜ引用しているか」
```

を確認する。

可能であれば以下の情報を利用する。

```text
contexts
intents
isInfluential
```

### Semantic Similarity

SPECTER2等のembeddingを利用して、

```text
Target Paper
vs
Candidate Paper
```

の意味的類似度を計算する。

---

# 5. OpenAlex

## 5.1 役割

論文のbibliometricな重要度を補正するために利用する。

### 利用候補データ

- referenced_works
- related_works
- cited_by_count
- publication_year
- Topics
- FWCI
- citation_normalized_percentile
- cited_by_percentile_year

---

## 5.2 Normalized Research Impact

単純なCitation Countだけでは、古い論文が有利になる。

例：

```text
Paper A
year = 2018
citations = 10,000

Paper B
year = 2025
citations = 1,000
```

単純な引用数だけではPaper Aが高く評価される。

そのため、

```text
FWCI
citation_normalized_percentile
cited_by_percentile_year
```

等を利用して、

> 同時期・同分野の中でどの程度影響力があるか

を評価する。

---

# 6. arXiv

## 6.1 役割

原文取得元として利用する。

### 主な用途

- Target Paper PDF取得
- Metadata取得
- Abstract取得
- Author取得
- arXiv category取得
- publication information取得

Research Lineage rankingの主役には使用しない。

---

# 7. Crossref

## 7.1 役割

Metadata補完・正規化のためのfallbackとして利用する。

### 主な用途

- DOI取得
- DOI正規化
- Publication metadata
- References
- is-referenced-by-count

Semantic Scholar / OpenAlexでMetadataが不足した場合に利用する。

---

# 8. APIの役割分担

```text
arXiv
│
│ PDF / Metadata
▼
Target Paper
│
├──────── Semantic Scholar
│          │
│          ├ References
│          ├ Recommendations
│          ├ Citation Context
│          ├ Citation Intent
│          ├ isInfluential
│          └ SPECTER2
│
├──────── OpenAlex
│          │
│          ├ referenced_works
│          ├ related_works
│          ├ FWCI
│          ├ citation percentile
│          └ Topics
│
└──────── Crossref
           │
           └ DOI / Metadata fallback
```

役割は以下。

```text
Semantic Scholar
→ 論文同士の関係性を見る

OpenAlex
→ bibliometricな重要度を見る

arXiv
→ 原文を見る

Crossref
→ metadataを補完する
```

---

# 9. 論文選定に利用する指標

## 9.1 Citation Relation

Target PaperがCandidate Paperを直接引用しているか。

例：

```text
Target Paper
   ↓ cites
Candidate Paper
```

### Candidate Scoreへの利用

直接引用されている場合、高いスコアを与える。

ただしCitation Relationのみで重要論文とは判断しない。

---

# 10. Semantic Relevance

Target PaperとCandidate Paperの内容的な類似性。

例：

```text
embedding(Target)
embedding(Candidate)

↓

cosine similarity
```

Semantic ScholarのSPECTER2等を利用することを検討する。

---

# 11. Citation Importance

Target Paper内でCandidate Paperがどのような文脈で引用されているかを見る。

例：

```text
Background Citation

vs

Methodological Foundation

vs

Result Comparison

vs

Core Prior Work
```

特に、

```text
isInfluential
Citation Intent
Citation Context
```

等が取得できる場合は利用する。

---

# 12. Normalized Research Impact

OpenAlex等から取得。

利用候補：

```text
FWCI
citation_normalized_percentile
cited_by_percentile_year
```

Raw Citation Countよりもこちらを優先する。

---

# 13. Contribution Relevance

外部APIだけでは判断できないため、LLMによって評価する。

Target Paperからまず主要Contributionを抽出する。

例：

```text
Contribution 1
Recurrenceを使わずAttentionのみで系列を処理

Contribution 2
並列化可能なArchitectureを提案

Contribution 3
翻訳性能を改善
```

Candidate Paperについて以下を評価する。

```text
Candidate Paper
      ↓
Target Contributionの前提となった研究か？
      ↓
Candidateが残したProblem / Limitationは何か？
      ↓
Target PaperがそのProblemを解決しているか？
```

---

# 14. Lineage Score

Target Paperを `T`、Candidate Paperを `P` とする。

以下のようなスコアを仮置きする。

```text
LineageScore(T, P)
=
0.25 × CitationRelation
+
0.20 × SemanticSimilarity
+
0.20 × CitationImportance
+
0.15 × NormalizedImpact
+
0.20 × ContributionRelevance
```

## 注意

この重みは暫定値である。

最終的には評価セットを利用し、

```text
Human-selected Lineage
vs
System-generated Lineage
```

を比較しながら調整する。

---

# 15. 候補生成とランキングを分離する

Research Lineage生成は以下の2段階に分ける。

## Phase 1: Candidate Generation

できるだけRecallを高くする。

```text
                 Target Paper
                      │
          ┌───────────┼───────────┐
          ▼           ▼           ▼
     References   S2 Recommend   OpenAlex
          │           │        related_works
          └───────────┼───────────┘
                      ▼
               Candidate Papers
```

候補数の目安：

```text
30〜100 papers
```

MVPでは30〜50本程度を想定する。

---

# 16. Phase 2: Candidate Ranking

候補論文に対して以下を計算する。

```text
Semantic Similarity
Citation Relation
Citation Importance
Normalized Research Impact
```

その後、

```text
Candidate 50 papers
        ↓
Initial Score
        ↓
Top 10
        ↓
LLM-based Contribution Relevance Evaluation
        ↓
Final Score
        ↓
Top 3〜5
```

とする。

高コストなLLM評価はTop 10程度まで絞った後に行う。

---

# 17. Research Lineage生成アルゴリズム（MVP）

## Step 1

Target Paperを解析する。

取得：

```text
Title
Abstract
Introduction
Related Work
Method
Conclusion
References
```

---

## Step 2

Target Paperの主要Contributionを最大3件抽出する。

例：

```json
[
  {
    "id": "c1",
    "description": "Self-Attentionのみで系列モデリングする"
  }
]
```

---

## Step 3

Target Paperが解こうとしているResearch Problemを抽出する。

例：

```json
{
  "problem": "RNNベースモデルは系列処理を逐次実行するため並列化が難しい"
}
```

---

## Step 4

Semantic Scholarから取得する。

```text
References
Recommendations
Citation metadata
```

---

## Step 5

OpenAlexから取得する。

```text
related_works
bibliometric metrics
```

---

## Step 6

候補論文を統合する。

重複除去キー候補：

1. DOI
2. arXiv ID
3. Semantic Scholar Paper ID
4. OpenAlex Work ID
5. normalized title

---

## Step 7

Target Paperより後に発表された論文を除外する。

```text
candidate.publication_year
<=
target.publication_year
```

同年論文についてはpublication dateを確認できる場合のみ厳密比較する。

---

## Step 8

各Candidateについて以下を計算する。

```text
CitationRelation
SemanticSimilarity
CitationImportance
NormalizedImpact
```

---

## Step 9

Initial Scoreを算出する。

---

## Step 10

Top 10 CandidateについてLLMによるContribution Relevance評価を行う。

評価項目：

```text
1. Target PaperのContributionの前提になっているか
2. CandidateがどのProblemを解決したか
3. CandidateにどのLimitationが残ったか
4. Target PaperがそのLimitationを解決しているか
5. Research Lineage上で直接つながるべきか
```

---

## Step 11

Final Lineage Scoreを算出する。

---

## Step 12

Top 3〜5 Paperを選択する。

---

## Step 13

Paper間の順序・関係を生成する。

```text
Paper A
↓
Limitation A
↓
Paper B
↓
Limitation B
↓
Target Paper
```

---

# 18. Candidate Paperデータモデル案

```json
{
  "paper_id": "...",
  "title": "...",
  "year": 2023,
  "authors": [],
  "doi": "...",
  "arxiv_id": "...",

  "citation_relation": 1.0,
  "semantic_similarity": 0.82,
  "citation_importance": 0.76,
  "normalized_impact": 0.91,
  "contribution_relevance": 0.88,

  "lineage_score": 0.84,

  "relationship": {
    "problem": "...",
    "contribution": "...",
    "remaining_limitation": "...",
    "relation_to_target": "..."
  },

  "sources": []
}
```

---

# 19. LLM出力形式案

Contribution Relevance評価はStructured Outputにする。

```json
{
  "candidate_paper_id": "...",
  "relevance": 0.0,
  "is_direct_predecessor": false,
  "related_contribution_ids": ["c1"],
  "candidate_contribution": "...",
  "remaining_problem": "...",
  "target_solution": "...",
  "reason": "...",
  "evidence": []
}
```

---

# 20. Source管理

Research Lineage上の各主張について、元情報を追跡可能にする。

例：

```text
Candidate Contribution
→ Candidate Paper Section 3

Remaining Limitation
→ Candidate Paper Conclusion

Target Relationship
→ Target Paper Related Work

Citation Importance
→ Semantic Scholar Citation Context
```

---

# 21. キャッシュ

外部API呼び出しを削減するため、以下はDB等へキャッシュする。

```text
Paper Metadata
Semantic Scholar Results
OpenAlex Results
Embeddings
Citation Context
LLM Evaluation Results
```

Paper IDやDOIをキーとして再利用する。

---

# 22. API障害時のFallback

## Semantic Scholar取得失敗

```text
OpenAlex
↓
Crossref
```

## OpenAlex取得失敗

Semantic Scholarのcitation count等で暫定評価。

## DOI取得失敗

```text
arXiv ID
Semantic Scholar ID
OpenAlex ID
normalized title
```

で論文を識別する。

---

# 23. MVPで利用する外部サービス

優先度：

## Must

- arXiv
- Semantic Scholar

## Should

- OpenAlex

## Could

- Crossref

---

# 24. 評価方法

Research Lineage選定方式そのものを評価対象とする。

比較候補：

## Baseline A

Citation Countのみ。

```text
Top cited references
```

## Baseline B

Semantic Similarityのみ。

## Proposed

```text
Citation Relation
+
Semantic Similarity
+
Citation Importance
+
Normalized Impact
+
Contribution Relevance
```

---

# 25. 評価指標

人間が作成したResearch LineageをGround Truthとして用意することを検討する。

確認項目：

```text
Precision@K
Recall@K
NDCG
Human Evaluation
```

特に最終的には、

> Research Lineageとして納得できる3〜5本を選べているか

を人間が評価する。

---

# 26. 想定評価セット

LLM関連の代表的な論文を複数利用する。

候補：

```text
Attention Is All You Need
BERT
GPT-related papers
InstructGPT
LoRA
RAG
Chain-of-Thought Prompting
ReAct
```

系譜が異なる論文を含める。

---

# 27. 技術的な問い

本機能では以下を技術的な検証テーマとする。

## Question 1

Citation CountだけでResearch Lineageを構築した場合と、

```text
Multiple Metrics + LLM Contribution Relevance
```

を利用した場合で、どの程度結果が改善するか。

## Question 2

Semantic SimilarityはResearch Lineage選定にどの程度有効か。

## Question 3

Citation Context / Intentは重要先行研究の識別に有効か。

## Question 4

Normalized Citation Impactを利用することで新しい論文を適切に評価できるか。

---

# 28. 未決事項

今後検討する。

## Ranking

- 各Scoreの正規化方法
- Score Weight
- Top K
- 年代補正方法

## Semantic Similarity

- SPECTER2を使用するか
- 独自Embeddingを利用するか

## Citation Importance

- Semantic ScholarのisInfluentialをどの程度信用するか
- Citation ContextをLLMで再評価するか

## Contribution Relevance

- LLM Prompt
- 評価尺度
- Evidenceの取り方
- 1回評価か複数回評価か

## Research Lineage

- 分岐するResearch Lineageの扱い
- 同じProblemに複数Paperが存在する場合の扱い
- Survey Paperを含めるか
- Benchmark Paperを含めるか

---

# 29. 実装優先順位

MVPでは以下の順番で実装する。

```text
1. arXivからTarget Paper取得

2. Semantic ScholarでReferences取得

3. Semantic Scholar Recommendations取得

4. Candidateの重複除去

5. Targetより後のPaperを除外

6. Citation Relation計算

7. Semantic Similarity計算

8. Top Candidate抽出

9. LLMによるContribution Relevance評価

10. Top 3〜5 Paper選択

11. Lineage Relation生成

12. UI表示

13. OpenAlex指標追加

14. Ranking評価
```

OpenAlexによるbibliometric補正は、最初から必須にせず段階的に追加してもよい。

---

# 30. MVP完成条件

- [ ] Target PaperからReferencesを取得できる
- [ ] Semantic Scholar Recommendationsを取得できる
- [ ] Candidate Paperを統合・重複除去できる
- [ ] Target Paperより後の論文を除外できる
- [ ] Semantic Similarityを計算できる
- [ ] Citation Relationを判定できる
- [ ] 上位候補に対してContribution Relevanceを評価できる
- [ ] 最終的に3〜5本のResearch Lineage候補を選択できる
- [ ] Paper間のProblem / Contribution / Limitation関係を表示できる
- [ ] 各主張のSourceを保持できる
- [ ] 複数論文を使って選定結果を評価できる

---

# 31. 設計上の原則

## 31.1 LLMだけに論文探索を任せない

論文候補は外部Academic APIから取得する。

LLMは、

```text
候補生成
```

ではなく主に、

```text
候補間の意味的関係の評価
```

に利用する。

---

## 31.2 Citation Countだけに依存しない

重要度とLineage上の関連性は異なる。

---

## 31.3 Candidate GenerationとRankingを分離する

```text
Recall重視
↓
Candidate Generation

Precision重視
↓
Candidate Ranking
```

とする。

---

## 31.4 高コスト処理は後段に置く

LLMによる詳細評価はTop 10程度へ絞った後に実施する。

---

## 31.5 Sourceを保持する

Research LineageはAIが生成した「もっともらしい研究史」にならないようにする。

すべての重要な関係について、

```text
なぜその関係と判断したか
```

を追跡可能にする。
