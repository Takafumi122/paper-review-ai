# Research Lineage

Status: Canonical. Updated: 2026-09-19.
要件の正本は [requirements.md](requirements.md)。数値計算は [scoring-lineage.md](scoring-lineage.md)。

## 1. 目的と範囲

Target Paperの主要Contributionを成立させる上で直接重要だった先行研究の系列を示す。
被引用数順・類似論文一覧・引用Graphの全表示を目的にしない。
Contribution、残された問題・制約、後続論文が何を変えたかを、確認済みSourceとともに説明する。

Targetは英語・arXiv・全文取得可能な対象分野の論文。候補は非arXivでもよい。
十分な本文または信頼できるEvidenceがない関係をsupportedにしない。
Surveyは原則主系列から除外し、Dataset/Benchmark論文は主系列から除外する。
それらの参考文献・Knowledge Sourceとしての利用は禁止しない。
Surveyの例外を自動判断で増やさず、MVPの自動選択では除外を標準とする。

## 2. Graph制約

```text
max_predecessor_papers = 5  # Targetは含めない
max_depth = 3              # 各先行研究からTargetまでの最長パスのEdge数
max_branch_points = 1
max_incoming_lineages = 2
```

- Edge方向は先行研究 → それを基礎とする後続研究。
- Targetは終端。主系列の全NodeはTargetへ到達する。循環・自己Edge・重複Edgeを禁止。
- 基本は一本道。上限は埋める目標ではない。一本道なら深度制約により先行研究は最大3本。
- Targetの主要Contributionが異なる2系列を明示的に統合する場合だけ、合流点を1か所許す。
- 実装上は入次数2のNodeを合流点として数える。その他のNodeの入次数は最大1、
  非Targetの出次数は1。任意の分岐・再合流Graphには拡大しない。
- 主な形状はTargetでの合流。途中Nodeでの合流を表示する場合もTarget Contributionへの
  明示的な統合関係と各EdgeのEvidenceを必要とし、同じ深度・本数上限を適用する。
- Evidence不足なら短縮する。先行研究0本＋不足理由も有効な解析結果。

例：`A → B → C → Target` は深度3。
`A → B → C → D → Target` は深度4のため不許可。
`A → B → Target` と `C → D → E → Target` の合流は、先行研究5本・深度3。
この形状自体が許容されても、統合の証拠がなければ分岐を許さない。

## 3. 公開順序

優先する根拠は、最初に確認できる公開日、arXiv first submitted date、publication date、publication year。
原稿の更新日を初公開日に使わない。APIや公式資料から取得し、LLMに日付を補完させない。

各日付に `kind / value / precision / source_id` を保存する。
日精度ならその日、月精度なら月初〜月末、年精度なら年初〜年末の区間として比較する。
先行研究Aの最遅可能日がBの最早可能日より前の場合にのみAの先行を確認できる。
同日・区間重複・根拠の競合で順序不明なら `contemporary_work` とする。
同じ年でも日付が十分なら比較可能。明確に後発なら `later_work` として主系列から除外。
単に古いだけでは影響関係の証拠にはならない。

独立研究は因果系列から分離する。同時期研究の別枠表示は「本当に同時発表」の断定ではなく、
順序未確認を含む分類であることをUIに示す。

## 4. 候補探索と絞り込み

1. Targetを取得・構造化し、nanoで根拠付き主要Contributionを最大3件とProblemを抽出。
2. Target ReferencesとSemantic Scholar Recommendationsを取得。
3. DOI、arXiv ID、Semantic Scholar IDで統合。IDが矛盾したら自動結合しない。
   正規化タイトルは照合候補の手掛かりに限定し、単独で同一論文と断定しない。
4. 日付・論文種別を確認し、主系列候補／同時期／後発／対象外を分離。
5. 30〜50候補を目安にInitialScoreを計算し、最大Top 10をluna評価へ渡す。
6. 候補のContribution、制約、Targetとの関係に必要な根拠を取得し、Contribution Relevanceを評価。
7. 確定した補正policyでFinalScoreを計算して系列候補を選ぶ。
   AdjustedContributionRelevance未確定の間はこの処理を本番有効化しない。
8. 各候補間のEdgeを別途評価し、Graph制約とEvidenceを検証して最大5先行研究を出力。

取得元、取得日時、候補除外理由、raw metric、欠損理由、score内訳を保存する。
Academic API障害時にOpenAlex/Crossrefへ切り替えない。取得済みReferences/cacheで縮退し、
不足を明示する。候補名・書誌情報をLLMの記憶から補わない。

### 制限付き追加取得（実装初期設定）

Targetへの関連性だけではA→Bを証明できないため、選択候補のReferences・本文を必要時に取得する。
初期設定はJob全体で候補50件、lunaで詳細評価する候補10件、候補本文取得10件、
Academic APIのHTTP attempts 40件（再試行・ページングを含む）を上限とする。
追加候補も同じ総枠を使い、追加のTop 10枠を作らない。上限と打ち切り理由を保存する。
選択済み系列の不足Edgeを優先し、無制限な探索・全候補の全組比較はしない。
取得制限は品質・時間の計測後に設定版を更新できるが、Graph制約は変更しない。

## 5. EvidenceとConfidence

Sourceは取得した文書/APIの版、URL、取得日時、hashを示す。
EvidenceはSource中のspan、Section、ページ、英語引用とClaimとの対応を示す。
LLMには取得済みspan IDを渡し、存在しないID・ページ・引用を作らせない。

- 原文一致の検証と、引用が関係を裏付けるかの意味的検証を分離する。
- A→Bの関係をTargetへの関連scoreで代用しない。
- EvidenceConfidence、ContributionRelationshipScore、EdgeConfidence、statusを保存する。
- EvidenceConfidenceが0なら、他のscoreに関係なくinsufficient_evidence。
- uncertainは不確実な候補関係として区別して表示する。insufficient_evidenceを主系列の
  接続として使わず、不足理由として返す。
- 図表の不足で関係を検証できない場合、数値だけでsupportedにしない。

計算・閾値は [Scoring](scoring-lineage.md) を参照。UIは日本語、原文引用は英語。

## 6. 受入観点

Targetを除く本数、最長パス、1合流・2系列、循環、公開順序、非arXiv候補、Evidence欠如、
引用先ID不明、S2障害、深度不足で短縮する例を検証する。
品質評価は [evaluation.md](evaluation.md) に従い、少ない出力を誤って失敗扱いしない。
