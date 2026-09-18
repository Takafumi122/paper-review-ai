1. プロダクト境界・対象ユーザー
現状「生成AI論文を読む学生」までは決まっている。
もう一段具体化する。
- MLには入門している論文初心者を対象とします。
- 扱う論文の分野はLLM/NLP中心とします。
- 対象論文は、
1. 英語
2. arXiv
3. 本文取得可能
なものとします。
- 理論論文（数式が中心のもの）は対象外とします。
- 目的は、論文読者のための読解支援に限定します。
- 成功状態は、その論文の「背景・論理・前提知識」を把握できる状態に仮置きします。
ユーザー像をさらに具体化すると、
> Transformerや機械学習の基礎は知っているが、新しいLLM論文を読んだときに研究史や細かい専門知識がわからない大学生
となります。

2. Research Lineageの「正解」を定義する。
現在は、
PaperA -->Problem --> PaperB --> Problem --> Target Paper
ですが、何を持って「Lineage」と呼ぶのかを明示化したい。
ここでは、一旦
> Target Paperの主要Contributionを成立させる上で直接的に重要だった先行研究の系列
と定義します。
| 論点          | 答え                 |
| ----------- | ------------------ |
| 何世代遡るか      | 2世代 / 3世代          |
| 最大Paper数    | 3〜5本               |
| 分岐を許すか      | 1本の一本道 / Graph     |
| Surveyを含めるか | 原則除外            |
| Dataset論文   | Lineageとしては扱わない      |
| Benchmark論文 | Lineageとして扱かわない      |
| 同年論文        | 発表時期をどう扱うか         |
| 独立研究        | 同時期に別系統で出た研究も比較材料として出力したい |
MVPでは、
> 最大5本、基本は一本道、必要な場合のみ1回分岐
させます。
3. Research Lineageのスコア仕様
`scoring-lineage.md`に記載。

4. Reasoning Flowのデータ構造
ここもコードを書く前に決める必要があります。
現状、
Problem
↓
Approach
↓
Experiment
↓
Result
↓
Conclusion
ですが、実際には論文によってかなり異なります。
例えば、
Problem
↓
Observation
↓
Method A
├ Experiment A
│  ↓
│ Result A
│
└ Experiment B
   ↓
   Result B
↓
Conclusion
のようになる可能性があります。
従ってUIは、一本道に見せる。
いっぽう内部データでは、Graphまたは可変長Stepとします。

Step Typeを、
- Problem
- Observation
- hypothesis
- approach
- experiment
- result
- interpretation
- conclusion
から選ばせる方式とします。
分岐には対応させる。
Step数は最大10個にする。
複数Experimentは論理関係が明確になる形で分離する。

5. Just-In-Time Knowledgeの「必要」の定義
例えばTransformer論文なら、
- self-Attention
- Query / key / Value
- Softmax
- Embedding
- Positional Encoding
- BLEU
- RNN
- CNN
- Cross Entropy
- GPU parallelization
など大量に関連知識があり、これを全て説明するとJust-In-Timeにはならない。
そこで、
> その知識を知らない場合、Reasioning Stepの因果関係を理解できなくなるConcept
だけを必須knowledgeとします。
例えば、
Reasoning Step: 「RNNをのぞくことで学習を並列化した」
であれば、
必須：
- RNNが逐次処理であること
- 並列計算とは何か
不要：
- LSTMのGate数式
- GRUの詳細
である。
ここでさらに、説明は論文の文脈に沿ったものであるとより良い。
この判定をLLMにさせるならば、
```plaintext
Is this concept necessary to understand
why Step B follows from step A?
```
のように問えることになる。

6. Background Knowledgeの説明仕様
「何を説明するか」に加えて、「どう説明するか」も必要である。
例えばMVPで、
```plaintext
Concept
Why you need thi
Short Explanation
Optional Detail
Source
```
で十分である。
ここで、説明を最初から3段階生成するのではなく、
**Short explanation**
だけを事前に生成しておいて、
詳細はユーザーが押した場合に生成する仕様とします。

7. Claim単位でSourceを持つ
ここでは一旦、
```json
{
    "claim": "RNNは逐次処理のため並列化が難しい",
    "source": [
    {
        "paper_id": "...",
        "section": "Introduction",
        "page": 2,
        "quote_or_span": "..."
    }
    ]
}
```
とします。
Research Lineageでも、
Paper A --> Paper BというEdgeそのものにEvidenceを持たせます。

LineageNodeだけでなくLineageEdgeも
Entityとして持つイメージです。
またSourceの優先順位は、
1. 論文本文
2. 著者公式ページ / Project Page
3. Academic API metadeta
4. 信頼性の高い外部資料
5. その他Web
とします。

8. 論文解析の対応範囲について
PDF parsingはかなり事故りやすい。
そのためどこまで対応するかを決める必要がある。
MVPであれば、
| 内容         | 対応      |
| ---------- | ------- |
| 本文         | 必須      |
| Section    | 必須      |
| References | 必須      |
| ページ番号      | 可能な限り必須 |
| Table      | 一部対応    |
| Figure     | MVPでは限定 |
| 数式         | 原文保持程度  |
| Appendix   | 対応      |
| Footnote   | 優先度低    |
とする。
また、
> 表・図に強く依存する主張は、十分なEvidenceを取得できない場合 `uncertain`とする
仕様とします。
9. 外部APIの正式な役割
現状は、
- Semantic Scholar
- OpenAlex
- arXiv
- Crossref
が候補にある。
ここからMVPでは、
arXiv + Semantic Scholar
だけを作り、
後からOpenAlexを追加します。
理由は、最初から複数Academic APIを統合した場合
- DOI
- arXiv ID
- Semantic Scholar ID
- OpenAlex ID
のIdentity Resolutionだけでもかなり作業量が増えるため。
ただしデータモデル上は最初から
`external_ids`を持たせておく。

10. AgentかPipeLineか
ここは重要。
MVPでは全てを自律Agentにはせずに、
```plaintext
Deterministic Pipeline
+
必要箇所だけLLM
```
という構成でいきます。
Paper取得
↓
Parser                         deterministic
↓
References取得                 deterministic
↓
Candidate Retrieval            deterministic
↓
Metric計算                     deterministic
↓
Contribution Evaluation        LLM
↓
Lineage Construction           LLM
↓
Reasoning Extraction           LLM
↓
Concept Extraction             LLM
といった形です。
つまり、**Agentic Searchは使うが、Workflow自体は制御されたPipeline**
とします。
これは信頼性・テスト可能性・説明可能性の側面からも有益です。

11. LLMの仕様
最低限次を決める
| 論点                   | 決めること        |
| -------------------- | ------------ |
| Model                | 何を使うか        |
| Provider abstraction | 後から交換可能にするか  |
| Structured Output    | JSON Schema  |
| Temperature          | 低めに固定するか     |
| Retry                | 何回か          |
| Timeout              | 何秒か          |
| Prompt Version       | 保存するか        |
| Token budget         | Stepごとに制限するか |


またポートフォリオとしては、
- model
- prompt_version
- input_hash
- output
を保存する。
12. 非同期Jobの仕様
queued
parsing
analyzing
...
completed
としたが、これをState Machineとして決めておく。
1. queue
2. parsing
3. retrieving_candidates
4. ranking_candidates
5. building_lineage
6. building_reasoning
7. building_knowledge
8. completed
とします。
また、エラーなら`failed`を返す。
一部だけ失敗してしまった場合、例えば
Research Lineage は成功したがknowledge生成が失敗した場合は`partial_completed`を許す。

13. 再実行・キャッシュ仕様
同じ論文を何度も解析するとAPI代がかかってしまう。
そのため、
Paper + Pipeline + prompt Version + model
が同じならキャッシュを使います。
一方、
prompt_version=2
となったら再解析可能にします。

14. 評価設計
3機能を別々に評価する。
| 機能               | 評価                    |
| ---------------- | --------------------- |
| Research Lineage | 選択論文の妥当性              |
| Reasoning Flow   | 論理構造の正確性              |
| JIT Knowledge    | 必要知識のPrecision/Recall |

Research Lineageなら、
Citation Count baseline vs Semantic Similarity baseline vs Proposed Method
の比較ができる。
Reasoning Flowならば、人間が
- correct
- Partially correct
- incorrect
をつけられる。
JIT Knowledgeなら、
> これがなくてもStepを理解できるか？
という評価基準が使える。

15. 評価用論文セット
「有名だから」だけではなく、異なる種類のResearch Lineageをテストできるかを基準としている。
| # | 論文 | 年 | 評価したいLineage |
| -: | :-- | -: | :-- |
| 1 | **Attention Is All You Need** | 2017 | RNN/CNN → Attention → Transformer |
| 2 | **Language Models are Few-Shot Learners (GPT-3)** | 2020 | GPT系・Scaling・In-context Learning |
| 3 | **Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks** | 2020 | Parametric LM + Retrieval |
| 4 | **LoRA: Low-Rank Adaptation of Large Language Models** | 2021 | Fine-tuning → PEFT |
| 5 | **Training language models to follow instructions with human feedback (InstructGPT)** | 2022 | LM → Instruction tuning → RLHF |
| 6 | **Chain-of-Thought Prompting Elicits Reasoning in Large Language Models** | 2022 | Few-shot prompting → Reasoning |
| 7 | **ReAct: Synergizing Reasoning and Acting in Language Models** | 2022 | CoT + Acting/Tool use |
| 8 | **Direct Preference Optimization** | 2023 | RLHF → Preference Optimization |
| 9 | **QLoRA: Efficient Finetuning of Quantized LLMs** | 2023 | LoRA + Quantization |
| 10 | **Self-RAG** | 2023 | RAG → Adaptive retrieval + Self-reflection |
詳しい選定理由は `docs/thesis-choosing`にある。

16. UIで何を最初に見せるか
Research Lineage / Reasoning / Knowledgeを全部一度に見せると情報量が多くなってしまう。
- Research Lineage
- Reasoning Flow
を最初に見せて、
background knowledgeはreasoning Flowの中に埋め込みます。
つまり、knowledgeを独立した3ページ目にするのではなく、
Reasoning Step
   │
   └── Learn prerequisite
とします。

17. エラー時対応・UX
かなり重要だが忘れやすいところ
Related papersを十分取得できませんでした
Research Lineage confidence: Low
と表示するなど。
AIに無理やりLineageを作らせないことが重要。
以下の状態が必要となる。
```plaintext
supported
undertain
insufficient_evidence
```
18. 非機能要件
解析時間：5分以内
LLMコスト：1論文1ドル以内
Paperサイズ：50ページ以内
同時実行：個人利用レベル
Availability: 厳密なSLAなし
ログ： Pipeline Step単位
API再試行： 最大2回
とします。
重要なのは**測定できるようにすること**です。

19. セキュリティ・データ保持
MVPでも
- アップデートされたPDFを保持するか
- どれくらい保存するか
- 外部LLMへ本文を送るか
- ログに本文を残すか
を決めておくべき。
MVPでは、
> 公開済み論文のみを対象とし、非公開（有料の論文）については外部LLMへ本文を送らない。また、アップデートされたPDFデータは保持しない。
という設定で行こうと思います。

20. Githubポートフォリオとして必要な要件
Repositoryから以下がわかる状態にする。
- なぜ作ったか
自分がLLM分野の論文を読む際に全く背景知識がない状態からだとその論文の文脈が分からずその論文自体の価値がわからない。
また、論文を読み進めるにあたり専門用語が頻出することから読み進めるのが困難である・論理の筋を捉えるのに苦戦する。
という課題を解決するために作成した。
- どんな問題を解くか
機械学習の基礎知識がある程度で論文をあまり読んだことのない大学生が、その論文の背景知識や論理構造・専門的な用語群に苦戦せずに情報をインプットできる。
- なぜこのArchitectureか
必要な機能を
・専門的な背景知識の情報支援
・Research Lineage（その論文がどのような研究系統に属しているのか）を知ることができる
・
に絞った。
それぞれに対応する機能を実装するためにこのArchitectureとなった。
- どんなアルゴリズムか
・

`README.md`
`docs/requirements.md`
`docs/architecture.md`
`docs/research-lineage.md`
`docs/evaluation.md`
`ADR/`
があるといい。