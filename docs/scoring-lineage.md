# Research Lineage Scoring

Status: Canonical. Updated: 2026-09-19. Scoring policy draft version: 1.
ユーザー確定の重み・Contribution/Evidence rubricと、今回補完した正規化規則を区別する。
AdjustedContributionRelevanceの補正だけは未決であり、Stage 2の本番利用を止める。
Graph仕様は [research-lineage.md](research-lineage.md)、未決事項は [ADR-0001](../ADR/0001-mvp-decisions.md)。

## 1. 確定した二段階score

```text
InitialScore = 0.40 × SemanticSimilarity
             + 0.35 × CitationRelation
             + 0.25 × CitationImportance

FinalScore = 0.40 × AdjustedContributionRelevance
           + 0.25 × SemanticSimilarity
           + 0.20 × CitationRelation
           + 0.15 × CitationImportance
```

Stage 1は最大Top 10。Stage 2の順位はGraph構築への入力であり、上位を自動でEdge接続しない。
OpenAlexはMVPで呼ばず、NormalizedImpactはその追加時まで使用しない。
Raw citation countはbaseline用にのみ保持し、これらの式へ追加しない。

## 2. Metricの表現

```json
{
  "value": null,
  "missing_reason": "embedding_unavailable",
  "source_ids": [],
  "normalization_version": "1"
}
```

valueは有限な[0,1]またはnull。nullならmissing_reason必須、観測値ならmissing_reason=null。
NaN、無限大、範囲外のLLM scoreを黙ってclampせずvalidation failureとする。
既知の0と不明は異なる。例：観測した無関係は0、取得できず関係不明はnull。

## 3. 正規化（実装規則）

### SemanticSimilarity

同一model/version・同一次元のSPECTER2 vectorsをSemantic Scholarから取得できる場合に計算する。
追加embedding providerや自動的な別modelへの切替はMVPに含めない。

```text
cos = dot(target, candidate) / (norm(target) * norm(candidate))
SemanticSimilarity = (clip(cos, -1, 1) + 1) / 2
```

clipは浮動小数の丸め誤差にのみ使用する。vector欠如・ゼロnorm・次元/model不一致はnull。
候補集合ごとのmin-max正規化は行わない。同じ組のscoreが候補追加だけで変わらないようにする。
Similarityが高いことは研究上の直接関係を意味しない。

### CitationRelation

Target→Candidateの直接引用が解決済みReferencesまたは引用本文で確認できれば1。
TargetのReferencesを完全取得・照合でき、当該Candidateの非引用を確認できれば0。
APIの部分取得、未解決Reference、同一性不明等で非引用を確認できなければnull。
Recommendationsに存在するだけでは直接引用の有無を決めない。

### CitationImportance

引用文脈の重要性を次の固定rubricで0〜1に変換する。全文LLM評価はせず、既存の引用文脈を
nanoで分類し、分類結果からコードで値を決める。Source/spanを必須とする。

| 観測した分類 | 値 |
|---|---:|
| 中核的先行研究・方法的基盤として明示的に使用/拡張 | 1.00 |
| 手法選択や問題設定を具体的に動機付ける関連研究 | 0.75 |
| 結果・手法の比較対象 | 0.50 |
| 一般的な背景・関連研究の列挙 | 0.25 |
| 非引用であることを確認済み | 0.00 |
| 文脈不足・取得失敗・引用先未解決 | null |

複数の確認済み引用文脈は最大値を採用し、各分類と採用理由を残す。
文脈がない場合、当該Target→Candidateの `isInfluential=true` は0.75のproxyとして使用可能。
`isInfluential=false` は重要性0の証拠ではないので、それ単独ではnull。
APIのintentだけ、論文全体のinfluentialCitationCountだけではこの値を確定しない。
proxy使用と文脈の網羅性を記録し、評価時に集計する。

### ContributionRelevance（ユーザー確定）

GPT-5.6 LunaがTargetの主要ContributionとCandidateについて整数を返す。

| score | 判断 |
|---:|---|
| 0 | Target Contributionとほぼ無関係 |
| 1 | 同一研究領域だがLineage上の直接関係は弱い |
| 2 | Target Paperの前提となる関連研究 |
| 3 | Target Paperが解こうとした問題・制約を直接含む重要な先行研究 |
| 4 | Target Contributionの成立に直接つながる中核的先行研究 |

`ContributionRelevance = score / 4`。関連するContribution IDs、日本語の判断理由、
Evidence IDsを保存する。情報不足はscore=0ではなくscore=null。
複数Contributionについては候補全体のrubricを1回評価し、どのContributionを支えるかを列挙する。

### AdjustedContributionRelevance（未決）

DecisionはFinalScoreにこの名称を用いるが、補正式を定義していない。
ContributionRelevanceそのもの、EvidenceConfidenceとの積、重複性の補正などは同じ結果にならない。
勝手にいずれかを採用しない。

- フィールドは[0,1]またはnull、`adjustment_policy_version`を持つ。
- 未確定時はnull、missing_reason=`adjustment_policy_unresolved`。
- **仕様未確定は観測欠損と区別する。この状態でFinalScoreを残り3指標から計算してはならない。**
- `FinalScore=null / status=blocked_configuration` とし、Phase 0ではfake出力のみ利用可能。
- Phase 2前にpolicyを承認・版固定し、計算例と評価ケースを追加する。

## 4. 欠損値と順位（実装規則）

全指標がある場合は上記の確定式をそのまま使う。
通常の取得欠損では観測できた指標の重み合計をcoverageとして保持し、次で集計する。

```text
O = observed metric indices
coverage = sum(w[i] for i in O)
score = sum(w[i] * value[i] for i in O) / coverage   # coverage > 0
score = null                                     # coverage == 0
```

欠損を0で埋めず、観測値間の相対重みを維持する。scoreには`complete / partial / unavailable`
を添付する。partial scoreはcoverageが異なるため、完全観測と同等の確実性を意味しない。
この欠損対応はユーザー確定の重みそのものを変更するものではない。

Stage 1はscore降順→coverage降順→canonical paper ID辞書順で最大10件を選ぶ。
scoreがnullの候補は順位対象外とし、不足数をLLMの推測で埋めない。
raw値・coverage・欠損パターンを記録し、低coverage候補が有利になる偏りを評価する。
観測欠損を解消する取得は既定の探索・attempt枠内だけで行う。

Stage 2は補正policyが確定し、ContributionRelevanceとAdjustedContributionRelevanceが
取得できた候補だけ順位対象とする。補正値が不明なら他の3指標だけでFinalScoreを作らない。
その他の指標の通常欠損には同じcoverage式を適用する。同点はInitialScore降順
（nullは最後）→coverage降順→canonical IDで解消する。
順位計算に表示用の丸め値を使わない。

例：S=0.8、C=1、I=nullならInitialScore=(0.32+0.35)/0.75=0.893333…、coverage=0.75。
S=0.8、C=1、I=0.5ならInitialScore=0.795、coverage=1。
全欠損はnullであり、0ではない。

## 5. EdgeConfidence（ランキングとは別）

### ContributionRelationshipScore（実装rubric）

lunaで各A→Bの組を評価する。上のContribution Relevance rubricのTargetを当該Bに置き換え、
0:ほぼ無関係、1:同領域のみ、2:Bの前提、3:Bが扱う問題・制約を直接含む、
4:BのContribution成立に直接つながる中核的先行研究、として `score/4` にする。
これはTargetに対するCandidate ranking scoreのコピーではない。Edgeごとに根拠と理由を持つ。
判定失敗ならnullとし、確認できないscoreを作らない。

### EvidenceConfidence（ユーザー確定）

| 値 | 判断 |
|---:|---|
| 1.00 | 論文本文に直接的なEvidenceがある |
| 0.75 | Introduction / Related Work等から強く裏付けられる |
| 0.50 | 間接的なEvidenceのみ |
| 0.00 | Evidenceを確認できない |

Section名だけで値を決めない。Introductionでも当該関係を直接述べた引用なら1.00、
単なる言及なら0.75とは限らない。Sourceの存在と引用の支持内容の両方を確認する。
複合的な関係Claimは分割して検証し、必須Claimの最小EvidenceConfidenceをEdgeに採用する。

```text
EdgeConfidence = 0.60 × ContributionRelationshipScore + 0.40 × EvidenceConfidence

supported:             0.80 <= EdgeConfidence <= 1.00
uncertain:             0.50 <= EdgeConfidence < 0.80
insufficient_evidence: 0.00 <= EdgeConfidence < 0.50
```

必ず未丸め値で分類する。EvidenceConfidence==0なら式の結果に関係なくinsufficient_evidence。
一方の評価失敗でscoreがnullならEdgeConfidenceもnull、状態はinsufficient_evidenceにして
`evaluation_unavailable`を残す。nullを数値0に変えない。
公開順序・引用span検証・対象範囲の制約違反を高scoreで上書きしない。

例：Relationship=1、Evidence=0なら数式上0.6でもinsufficient_evidence。
Relationship=1、Evidence=0.5なら0.8だが、引用が実在し関係を支持することの検証は依然必須。
Relationship=0.75、Evidence=0.75なら0.75、uncertain。
これらの値は校正済みの確率ではなく、固定rubricによる判断scoreである。

## 6. 検証ケース

全観測、部分欠損、全欠損、既知0、ゼロvector、矛盾ID、同点、0.5/0.8境界、
Evidence=0 override、補正policy未確定、LLM失敗を検証する。
重み変更・rubric変更・欠損policy変更はscoring policy versionを更新し、cacheを無効化する。
