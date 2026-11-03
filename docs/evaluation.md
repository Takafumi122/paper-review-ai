# MVP Evaluation

Status: Canonical evaluation design. Updated: 2026-09-19.
要件: [requirements.md](requirements.md)。対象: [thesis-choosing.md](thesis-choosing.md)。
この文書は評価方法を定義する。実測、アノテーション、品質合格の実績はまだない。

## 1. 評価原則

Research Lineage、Reasoning Flow、JIT Knowledgeを別々に評価する。
内部評価用の人手ラベルであり、客観的な研究史の唯一の正解とは主張しない。
Sourceの実在・支持内容を確認し、LLM自身の採点だけで正解を作らない。
有名な評価論文をモデルが記憶している可能性があるため、原文Evidenceの検証を独立に行う。

## 2. Manifestと人手annotation

各論文にcanonical paper ID、arXiv ID/version、英語title、初公開日とSource、
ページ数（付録込み）、対象範囲適合性、取得日、content hashを記録する。
評価時のAPI応答・候補集合・指標・scoring/model/prompt/schema/pipeline版を固定・記録する。
raw PDFは評価でも処理後削除し、許可されたSection本文とEvidenceで再検証する。

| 機能 | annotation |
|---|---|
| Lineage | required / acceptable / incorrect predecessors、根拠、許容Edge、年代、合流理由 |
| Reasoning | 主要Problem/Approach/Experiment/Result/Conclusion、必要な分岐、根拠、正しい関係 |
| Knowledge | 5〜10候補conceptをrequired / optional / unnecessary、対象Step間、理由、Sourceで分類 |

annotator、日時、rubric版を残す。初期annotationはシステム結果を見る前に作る。
新規候補の未ラベルをincorrectと決めつけず、結果を採点する前に追加レビューする。
単独annotatorの場合はその限界を明記する。評価担当・二者確認の実施可否は最終評価前に決定する。
Ground truthは候補集合に存在するものだけに限定せず、候補取得漏れも検出できるようにする。

## 3. Research Lineage

同一候補集合・同一eligibility条件・同一取得時点で比較する。

- Baseline A: raw citation count降順。
- Baseline B: SemanticSimilarity降順。
- Proposed: InitialScore・選考区間を記録→承認済み`lower-bound-v1`でTop 10→FinalScore。
  旧`observed-renormalized-v1`との比較では同じ原文・候補・指標を固定し、
  selection_policy_versionを記録する。欠損を除く取得coverageと選考方式の効果を分ける。

baselineの欠損はnullとしてunranked、同点はcanonical ID順。欠損率と評価対象coverageを併記する。
Proposedで補正policyが未確定なら評価完了を主張しない。
ranking比較と、Evidence制約を通した最終Graph評価は別々に報告する。

K=3と5を基本とし、以下の運用定義を使う。

```text
relevant = required ∪ acceptable
Precision@K = count(top K ∩ relevant) / K
RequiredRecall@K = count(top K ∩ required) / count(required)
gain(required)=2, gain(acceptable)=1, gain(incorrect)=0
DCG@K = sum((2^gain_i - 1) / log2(i+1), i=1..K)
NDCG@K = DCG@K / IDCG@K
```

出力がK未満の場合は空き順位をgain=0として扱う。加えて返した本数に対するprecisionを報告し、
Evidence不足による適切な棄権と不要な欠落を区別する。
requiredが空ならRecallはN/A、IDCG=0ならNDCGはN/A。0除算や便宜的な満点を使わない。
単にRecall@Kと記載する場合もrequired集合を分母にすることを表記する。
候補生成段階のRequiredRecallも測り、ランキングによる漏れと探索による漏れを分ける。

Graphでは各Edgeの正しさ、引用の支持、時系列、最大本数/深度、合流の正当性、
unsupportedなsupported判定数、適切なuncertain/insufficient_evidenceを評価する。
confidenceは確率とみなさず、score帯ごとの人手妥当率を報告する。

## 4. Reasoning Flow

人手で `correct / partially_correct / incorrect` を付け、各割合と例を報告する。
観点はProblem・Approachの正確性、Experiment/Resultの対応、ResultとConclusionの区別、
論理Edgeの妥当性、主要論理の欠落、根拠の実在と支持。
同じ構造の表現違いを誤りとしない。Step数10以下・type制約は機械検証する。
原文の結果を超えて「証明した」「必ず改善する」等へ強めていないか確認する。

## 5. JIT Knowledge

ターゲットユーザーがそのconceptを知らずにStep A→Bを理解できるかを基準にする。
requiredを正例、optional/unnecessaryを負例として、表示対象（necessity=3）のprecision/recallを算出する。
同義conceptは人手のcanonical labelに合わせ、重複説明を複数正解として数えない。
出力なしのprecision・正例なしのrecallはN/Aとして件数を併記する。
日本語の分かりやすさ、論文文脈との対応、出典の支持、不要な専門知識の増加も確認する。
5〜10候補のannotationで測るrecallはその評価集合内のrecallであり、全概念を網羅した値ではない。
新規出力conceptは追加レビュー後に採点する。

## 6. Reliability / Cost / Latency

- 取得不能、429、timeout、schema不正、refusal、欠損embedding、引用span不正、Worker再起動を検証。
- max_attempts=3 / max_retries=2、retryの二重化防止、予算guard、費用不明の予約保持を確認。
- 成功機能が部分失敗で消えないこと、証拠がない関係をsupportedにしないことを確認。
- 日付不明のcontemporary_work、既知の後発除外、EvidenceConfidence=0のhard ruleを検証。
- PDF削除、public arXivのSection再利用、ログに本文が出ないことを確認。

初回cacheなし解析とcacheあり再利用を分けて報告する。queue wait、worker実行時間、
ユーザー要求からterminalまでの経過時間を別々に測る。
5分soft targetは初回の要求→terminalの経過時間に対して達成件数を報告し、
queue/API待ちによる超過理由を説明する。timeoutやpartialを計測集合から隠さない。
各paperの値、中央値、最大値、目標達成率を報告する。10件で強い統計的一般化をしない。

initial analysisのUSD 1.00目標はretryを含むLLM推定費用で評価する。
JIT詳細費用は別欄、paper累計は全Jobを集計する。unknown費用を0として達成扱いしない。
使用token・価格表版・cache hitと実呼出を分ける。インフラ費用とLLM費用は混同しない。

## 7. DoDと残る判断

必須の構造・信頼性条件は、上限制約遵守、出典追跡、Evidence=0 override、部分成功、
再試行上限、保存方針、費用・時間の可観測性。
指定10論文のmanifest、人手annotation、baseline比較、Reasoning/Knowledge評価レポートを作成する。
50ページ超等の対象外が見つかった場合は黙って除外・短縮せず、方針変更を記録して合意する。

**品質の数値合格閾値・評価担当は未決。** Phase 0の障害ではないがMVP完成判定前に必要。
最終結果を見て合格閾値を都合よく変えない。初期3論文でrubricを整備した後、最終評価前に固定する。
重みやpromptを調整した論文と評価用論文を記録し、同じ10論文を両用した場合はその限界を明記する。
