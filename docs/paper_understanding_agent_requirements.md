# 論文理解支援AIエージェント 簡易要件定義書

## 1. 文書概要

本書は、生成AI分野の英語論文を読む学生を対象とした「論文理解支援AIエージェント」のMVP要件を整理するための叩き台である。

本システムは、論文を単純に要約することではなく、以下の3点を支援することを目的とする。

1. その論文が、どのような研究の流れの中で生まれたのかを理解する
2. その論文が、どのような論理の流れによって結論へ到達しているのかを理解する
3. その論理を理解するために必要な前提知識を、必要なタイミングで補う

---

## 2. 背景・課題

生成AI分野の論文を読む学生は、論文本文に書かれている内容だけでは十分に理解できない場合がある。

本プロジェクトでは、主に以下の3つを原因として仮定する。

### 2.1 研究文脈が分からない

対象論文が、

- どのような既存研究を前提としているのか
- 従来手法にはどのような問題があったのか
- その論文が研究の流れの中で何を変更・追加したのか

を把握できない。

### 2.2 結論に至る論理を追えない

論文の各Sectionを読むことはできても、

- 何が問題だったのか
- なぜその手法を採用したのか
- どのような実験を行ったのか
- その結果から何を主張しているのか

という論理的なつながりを整理できない。

### 2.3 前提知識が不足している

論文中に登場する概念・手法・評価指標等について知識が不足しており、論文の論理を途中で追えなくなる。

一方で、論文に関連するすべての基礎知識を事前に学習することは、情報量が多く非効率である。

---

## 3. プロダクトの目的

生成AI分野の論文を読む学生に対して、

> その研究が「どこから生まれ、何を示し、それを理解するために何を知る必要があるか」

を構造化して提示する。

---

## 4. 対象ユーザー

### 4.1 主対象

- 大学生・大学院生
- 生成AI分野の研究・技術に関心がある学生
- 英語論文をある程度読めるが、その研究文脈や技術的前提知識に不安がある読者

### 4.2 想定する前提知識

最低限、以下を想定する。

- Python等のプログラミング経験がある
- 機械学習・深層学習について入門レベルの知識がある
- 英語論文のAbstractやIntroductionをある程度読める

---

## 5. 対象分野

MVPでは対象を以下に限定する。

### 対象

- Computer Science
- Artificial Intelligence
- 特にLLMを中心とした生成AI分野

例：

- Transformer
- Large Language Model
- Pre-training
- Instruction Tuning
- RLHF / Preference Optimization
- RAG
- LLM Agent
- Parameter Efficient Fine-Tuning
- LLM Evaluation

### MVP対象外

初期段階では以下を対象外、または動作保証外とする。

- Diffusion Modelを中心とした画像生成
- 音声生成
- GAN / VAEを中心とした生成モデル
- CS/AI以外の学術分野
- 日本語論文
- 数式理解が中心となる理論論文

---

## 6. MVPの主要機能

MVPでは以下の3機能を中核機能とする。

1. Research Lineage
2. Reasoning Flow
3. Just-in-Time Background Knowledge

---

# 7. 機能要件

## 7.1 論文入力

### 概要

ユーザーが解析対象となる論文を指定する。

### MVP

以下のいずれかを入力可能とする。

- arXiv URL
- PDFファイル

### システムが取得する情報

- タイトル
- 著者
- Abstract
- 各Section
- References
- ページ情報

### 将来候補

- DOI
- Semantic Scholar URL
- ACL Anthology URL
- OpenReview URL

---

## 7.2 論文構造解析

入力された論文を、後続処理で利用可能な構造化データへ変換する。

### 最低限抽出する要素

- Title
- Authors
- Abstract
- Introduction
- Related Work
- Method / Approach
- Experiment
- Results
- Conclusion
- References

### 要件

- Section単位で本文を保持する
- 可能な範囲でページ番号を保持する
- 後続の回答から元の論文箇所へ参照できるようにする

---

# 8. Research Lineage

## 8.1 目的

対象論文が、どのような研究上の流れから生まれたのかを学生が理解できるようにする。

単純な「関連論文一覧」ではなく、対象論文のContributionにつながる研究の系譜を提示する。

---

## 8.2 基本構造

MVPでは以下を基本形とする。

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

---

## 8.3 Research Lineageに含める情報

各論文について、以下を表示する。

- 論文タイトル
- 発表年
- 著者
- その研究の主要Contribution
- 次の研究につながった課題・制約
- Target Paperとの関係
- 情報源

---

## 8.4 論文選択

MVPではResearch Lineageに含める先行研究を3〜5本程度とする。

候補論文は以下から探索する。

- Target PaperのReferences
- Target PaperのRelated Work
- arXiv
- Semantic Scholar等の論文検索サービス
- Web検索

単純な被引用数だけではなく、

> Target Paperの主要Contributionにつながっているか

を基準として選択する。

---

## 8.5 Research Lineage生成フロー

```text
Target Paper
      ↓
Key Contribution Extraction
      ↓
Research Problem Extraction
      ↓
Related Work / References Analysis
      ↓
Candidate Paper Search
      ↓
Candidate Ranking
      ↓
3〜5本を選択
      ↓
Relationship Extraction
      ↓
Research Lineage生成
```

---

## 8.6 MVPでの範囲

MVPでは主に、

```text
Previous Work
      ↓
Unsolved Problem
      ↓
Target Paper
```

までを必須とする。

Target Paper以降のSubsequent Workについては、将来機能とする。

---

# 9. Reasoning Flow

## 9.1 目的

論文の内容を単純に要約するのではなく、

> 「なぜこの結論に到達したのか」

を論理的なステップとして提示する。

---

## 9.2 基本構造

基本形として以下を想定する。

```text
Problem
   ↓
Observation / Hypothesis
   ↓
Approach
   ↓
Experiment
   ↓
Result
   ↓
Interpretation
   ↓
Conclusion
```

ただし、論文によって構造が異なるため、内部では可変長のステップとして扱う。

---

## 9.3 各Stepのデータ

各ステップについて以下を保持する。

- Step Type
- 要約
- 詳細説明
- 前Stepとの関係
- 次Stepとの関係
- 根拠となる論文内Section
- 根拠となるページ

---

## 9.4 想定データ例

```json
{
  "type": "approach",
  "summary": "RNNを使用せずSelf-Attentionのみで系列を処理する",
  "source": {
    "section": "3. Model Architecture",
    "page": 3
  }
}
```

---

# 10. Just-in-Time Background Knowledge

## 10.1 目的

論文を理解するために必要な基礎知識をすべて提示するのではなく、

> 現在理解しようとしているReasoning Stepに必要な知識だけ

を補足する。

---

## 10.2 基本構造

例：

```text
Reasoning Step:
「Attentionの計算量が長文処理のボトルネックになる」
                │
                ├─ Self-Attentionとは？
                └─ なぜ計算量がO(n²)になるのか？
```

---

## 10.3 Knowledge Item

各知識項目について以下を保持する。

- Concept名
- なぜこのConceptを知る必要があるか
- 簡潔な説明
- 詳細説明
- 関連するReasoning Step
- 情報源

---

## 10.4 説明レベル

MVPでは少なくとも簡潔な説明を提供する。

将来的には以下の段階を想定する。

### Quick

1〜3文程度の簡潔な説明。

### Understand

数式を極力使わず、仕組みを説明。

### Deep Dive

数式・アルゴリズム・実装等を含む詳細説明。

---

# 11. Source / Evidence管理

## 11.1 基本方針

システムが生成する事実的な主張について、可能な限り情報源を表示する。

特に、以下を区別する。

```text
[Target Paper]
対象論文本文を根拠とする情報

[Related Paper]
関連論文を根拠とする情報

[External Source]
Web上の外部情報を根拠とする情報
```

---

## 11.2 必須要件

Research Lineage上の以下の情報にはSourceを付与する。

- 先行研究のContribution
- 先行研究のLimitations
- Target Paperとの関係

Reasoning Flowについては、対象論文内のSection・ページを可能な限り示す。

---

## 11.3 禁止事項

エージェントは、根拠を確認できない場合に、

- 著者の意図
- 研究者の問題意識
- 研究コミュニティでの評価

等を事実として断定しない。

根拠が不十分な場合は、

- 不明
- 推定
- 十分な根拠なし

として扱う。

---

# 12. UI要件（MVP）

## 12.1 入力画面

ユーザーが以下を指定できる。

- arXiv URL
- PDF Upload

---

## 12.2 Analysis Status

解析中は以下の状態を表示する。

```text
queued
↓
parsing
↓
analyzing
↓
searching related papers
↓
building lineage
↓
generating reasoning flow
↓
generating background knowledge
↓
completed
```

---

## 12.3 Research Lineage画面

研究の系譜を視覚的に表示する。

例：

```text
[Paper A]
   │
   │ Limitation
   ↓
[Paper B]
   │
   │ Remaining Problem
   ↓
[Target Paper]

Key Contribution:
XXXX
```

---

## 12.4 Reasoning画面

```text
Problem
   ↓
Approach
   ↓
Experiment
   ↓
Result
   ↓
Conclusion
```

各Stepを選択すると詳細を表示できる。

---

## 12.5 Knowledge表示

Reasoning Step上のConceptを選択すると、補足説明を表示する。

例：

```text
Why do I need to know this?

この論文ではAttentionの計算量を削減することが
主要な問題設定となっているためです。

---

Self-Attentionとは？

...
```

---

# 13. Agent / 処理構成

MVPでは以下の役割を想定する。

```text
Target Paper
      │
      ▼
Paper Parser
      │
      ▼
Paper Understanding
      │
      ├───────────────┐
      │               │
      ▼               ▼
Contribution       Reasoning Flow
Extractor           Extractor
      │               │
      ▼               ▼
Research Problem   Concept Extraction
Extractor               │
      │                  ▼
      ▼            Knowledge Generator
Related Paper Search
      │
      ▼
Lineage Builder
```

---

# 14. Agentの責務

## Paper Parser

- PDF / arXivから本文を取得
- Section構造を抽出する

## Contribution Extractor

- Target Paperの主要Contributionを抽出する

## Research Problem Extractor

- Target Paperが解こうとしている研究課題を抽出する

## Related Paper Search

- References / Related Work / Webを探索する
- Research Lineage候補論文を取得する

## Lineage Builder

- 論文間の関係を整理する
- 3〜5本程度のResearch Lineageを生成する

## Reasoning Flow Extractor

- 論文内からProblem → Approach → Result → Conclusion等を抽出する

## Concept Extractor

- 各Reasoning Stepを理解するために必要なConceptを特定する

## Knowledge Generator

- Conceptについて学生向けの説明を生成する

---

# 15. Backend要件

## 15.1 非同期処理

論文解析は長時間かかる可能性があるため、非同期Jobとして扱う。

例：

```http
POST /papers

202 Accepted
```

解析状況は以下から取得する。

```http
GET /papers/{paper_id}
```

---

## 15.2 想定API

```http
POST /papers

GET /papers/{paper_id}

GET /papers/{paper_id}/lineage

GET /papers/{paper_id}/reasoning

GET /papers/{paper_id}/knowledge
```

---

# 16. データモデル（暫定）

主なEntityとして以下を想定する。

```text
Paper
 ├─ Section
 ├─ Reference
 ├─ ResearchLineage
 │    └─ LineageNode
 │
 ├─ ReasoningFlow
 │    └─ ReasoningStep
 │
 └─ KnowledgeItem
```

---

## 16.1 Paper

```text
id
title
authors
abstract
arxiv_id
pdf_url
created_at
```

## 16.2 Section

```text
id
paper_id
title
content
page_start
page_end
```

## 16.3 LineageNode

```text
id
paper_id
related_paper_id
position
relationship
contribution
limitation
source
```

## 16.4 ReasoningStep

```text
id
paper_id
step_type
order
summary
detail
source_section
source_page
```

## 16.5 KnowledgeItem

```text
id
reasoning_step_id
concept
why_needed
short_explanation
detailed_explanation
source
```

---

# 17. 非機能要件

## 17.1 信頼性

- 外部情報には可能な限りSourceを表示する
- 根拠のない断定を避ける
- 取得に失敗した情報を生成で補完しない

## 17.2 再現性

以下を保存できるようにする。

- 使用LLM
- Prompt Version
- 実行日時
- 使用したSource

## 17.3 エラー処理

以下を考慮する。

- PDF解析失敗
- LLM API Timeout
- Rate Limit
- Web検索失敗
- JSON出力失敗

## 17.4 パフォーマンス

MVP段階ではリアルタイム応答を必須とせず、非同期処理を前提とする。

目標処理時間は実装後に測定し、必要に応じて設定する。

## 17.5 コスト

1論文あたりのLLM APIコストを計測可能にする。

---

# 18. 評価要件

MVP完成後、代表的なLLM関連論文を評価セットとして利用する。

例：

- Attention Is All You Need
- BERT
- GPT関連論文
- InstructGPT
- LoRA
- Retrieval-Augmented Generation
- Chain-of-Thought Prompting
- ReAct

※ 評価対象論文は今後確定する。

---

## 18.1 Research Lineage評価

確認項目：

- 重要な先行研究を抽出できているか
- Target Paperとの関係が正しいか
- 不要な論文がLineageに含まれていないか
- Contribution / Limitationの説明に根拠があるか

---

## 18.2 Reasoning Flow評価

確認項目：

- 論文の主要Problemを捉えているか
- Approachを正しく説明しているか
- ResultとConclusionを混同していないか
- 論理的なつながりが妥当か
- 元論文のSourceを参照できるか

---

## 18.3 Background Knowledge評価

確認項目：

- Reasoning Stepを理解するために必要なConceptを抽出できているか
- 不要なConceptを過剰に提示していないか
- 説明が学生に理解可能か
- Reasoning Flowとの関連が明確か

---

# 19. MVPの完成条件

以下を満たした時点でMVP完成とする。

- [ ] arXiv URLまたはPDFから論文を登録できる
- [ ] 論文をSection単位で解析できる
- [ ] Target PaperのContributionを抽出できる
- [ ] 3〜5本程度の先行研究からResearch Lineageを生成できる
- [ ] 各Lineage NodeにSourceを付与できる
- [ ] Reasoning Flowを生成できる
- [ ] Reasoning Stepから元論文Sectionへ遡れる
- [ ] Reasoning Stepごとに必要なBackground Knowledgeを提示できる
- [ ] 非同期Jobとして解析を実行できる
- [ ] 解析結果をDBに保存できる
- [ ] 代表的な複数論文で評価を実施できる

---

# 20. MVPでは実装しないもの

以下は初期MVPから除外する。

- 論文の採否評価
- 査読コメント生成
- Citationの正当性検証
- Subsequent Workの詳細追跡
- 論文同士の自動スコアリング
- 研究の「インパクト」の絶対評価
- 著者の人物紹介
- ユーザーごとの学習履歴
- パーソナライズ
- 全学術分野への対応

---

# 21. 将来機能候補

## Research Lineage拡張

```text
Previous Research
      ↓
Target Paper
      ↓
Subsequent Research
```

まで表示する。

## Contribution Comparison

Target Paperと類似研究について、

- Accuracy
- Model Size
- Training Cost
- Dataset
- Benchmark

等を比較する。

## Interactive Q&A

ユーザーが、

> 「なぜこの研究が必要だったのか？」

> 「このStepが分からない」

等を質問できる。

## 学習支援

ユーザーの理解度に応じてBackground Knowledgeの説明レベルを変更する。

---

# 22. 現時点での未決事項

今後特に検討する必要がある項目。

## Research Lineage

- 「重要な先行研究」をどのように判定するか
- Citation Graphをどの程度利用するか
- Web検索とReferences解析の優先順位
- Lineageを何本まで遡るか
- 複数の研究系譜が存在する場合の表示方法

## Reasoning Flow

- Step Typeを固定するか可変にするか
- ResultとInterpretationをどのように区別するか
- 複数実験が存在する場合の扱い

## Background Knowledge

- 「必要不可欠な知識」をどのように判定するか
- 説明レベルをどこまで提供するか
- Background KnowledgeのSourceを何に限定するか

## 技術

- LLM Provider
- Web Search API
- Paper Search API
- Vector Databaseの必要性
- Agent Frameworkを使用するか
- Backend Framework
- Frontend Framework

---

# 23. MVPの価値仮説

本システムの価値仮説は以下とする。

> 学生が生成AI分野の論文を理解できない主要因は、論文本文そのものの難しさだけではなく、
> 「研究文脈」「論理構造」「前提知識」が不足していることである。

そのため、

1. Research Lineageによる研究文脈の補完
2. Reasoning Flowによる論理構造の可視化
3. Just-in-Time Background Knowledgeによる前提知識の補完

を組み合わせることで、単純な論文要約よりも論文理解を支援できると仮定する。

---

# 24. プロダクト説明（暫定）

> LLMを中心とした生成AI分野の論文を読む学生に対して、
> その論文がどの研究の流れから生まれ、
> どのような論理によって結論へ到達し、
> その過程を理解するために何を知る必要があるかを、
> 論文本文と外部情報から構造化して提示する論文理解支援AIエージェント。
