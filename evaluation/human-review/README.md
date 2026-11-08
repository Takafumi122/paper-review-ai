# 人手評価の準備

状態：**独立した人手レビュー用の資料・未記入フォーム。人手評価者は未定。**
2026-11-04の当初の指示では資料準備までとした。同日その後、ユーザーがLLM参照評価と固定数値基準を採用したため、現在の基準を「未承認」とは扱わない。独立した人手正解が作成済みとも扱わない。
正本は [評価設計](../../docs/evaluation.md)。この資料は正本やADR D2を変更しない。

追記：同日、ユーザーがLLM参照ラベルによる評価と数値基準案を採用した。
現在の固定基準は[criteria-v1.json](../acceptance/criteria-v1.json)。本ファイル以下の
人手向け手順・未記入フォームは将来の独立レビュー用に保持し、LLM評価を人手評価として記入しない。

## 使用する資料

- `criteria-proposal.json`：数値基準の提案。実測値や合格結果ではない。
- `annotation-template.json`：原文を読んで記入する共通フォーム。
- `evaluation/reports/human-review-preparation-2026-11-04/`：manifest v1.2.0に
  固定した10論文の個別フォーム、原文リンク、既存診断への別資料の索引。

原文PDFの版・hashが記録と違う場合は先に差異を解決する。PDFは処理後削除し、
必要な根拠のspan・ページ・短い抜粋と書誌情報で追跡する。原文全文をレビュー記録へ貼らない。

## 評価の手順

1. 担当者・二者確認の範囲を決め、氏名または識別子を記録する。単独評価ならその限界を明記する。
2. 初期3論文（Attention、LoRA、ReAct）で原文を読み、ラベルの境界・判断理由を揃える。
   この段階はrubricの調整用であり、独立した最終テストとは呼ばない。
3. 数値基準とrubricの版・日時・承認者を、最終評価の採点前に固定する。
   現在の基準案は過去の診断を見た後に作られており、事前登録済みとは主張しない。
4. 各論文について、原文だけを見てLineage・Reasoning・Knowledgeの期待内容を記入する。
   システム候補にない先行研究も記録する。既に結果を見た場合は必ず記録し、盲検と呼ばない。
5. 原文ベースのラベルを保存してから、別資料のシステム出力を開く。
   新規候補・概念は追加レビューし、未ラベルを誤りにしない。追加ラベルの経緯も残す。
6. rankingと最終Graphを別に採点し、Reasoning/Knowledgeも独立して評価する。
   失敗・棄権・未取得を対象から消さず、N/Aの理由と評価できた件数を併記する。

同じ10論文に既にprompt/実装の修正を行っているため、10論文全体を未知論文のholdoutとは
扱えない。初期3本と残り7本の内訳も報告し、最終10本の記述的評価と汎化性能を区別する。

## フォームの書き方

`null`は未記入・不明。レビュー済みで「該当なし」と判断した配列だけを`[]`とする。
`reviewer`、`reviewed_at`、`rubric_version`、原文確認、結果の事前閲覧を記入する。

Lineageの`predecessors`には候補ごとに次を記入する。

```json
{
  "paper_id": "人手で確認したcanonical ID",
  "label": "required / acceptable / incorrect のいずれか",
  "reason": "その分類にした原文上の理由",
  "source": {"paper_id": "対象論文ID", "version": "対象版", "page": 1, "span_id": null},
  "allowed_relationships": ["原文が支持する関係の記述"],
  "temporal_check": "確認結果、または不明の理由"
}
```

上記は形式の説明であり、実ラベルではない。例の文字列をそのまま保存しない。
`required`はこの論文の中核的な着想・手法の理解に不可欠、`acceptable`は原文で関係を
支持できる有用な先行研究、`incorrect`は同一性・時系列・内容のいずれかで不適切な候補。
証拠不足・未確認は未判定のまま残し、incorrectに変換しない。

Reasoningの`expected_steps`には`step_id`、`kind`、`claim`、`source`を記入し、
`expected_edges`には`from_step_id`、`to_step_id`、必要な関係・分岐と根拠を記入する。
出力レビューでは各Step/Edgeを`correct / partially_correct / incorrect`で判定する。
表現違いは許容し、実験と結果の対応、結果と結論の区別、主要な抜けを確認する。

Knowledgeの`concepts`には5〜10個の概念について`concept_id`、`canonical_name`、
`label`（`required / optional / unnecessary`）、`from_step_id`、`to_step_id`、
`reason`、`source`を記入する。対象の学生がStep間を理解するために必要かで判断する。
同義語は同じcanonical conceptにまとめる。概念を知らないこと自体を論文の欠点と扱わない。

## 指標と集計上の注意

Lineageのrelevantはrequiredとacceptableの和。Precision@3/5、RequiredRecall@3/5、
NDCG@3/5、候補生成時のRequiredRecallを正本の式で算出する。
空き順位はgain=0、requiredが空のRecallとIDCG=0のNDCGはN/A。
RequiredRecall@5の到達上限は`min(5, required数) / required数`なので、required数が
多い論文へ達成不可能な固定閾値を課さない。返した本数に対するprecisionも別記する。
未ラベルの候補は採点を保留する。

Baseline A（引用数）・B（SemanticSimilarity）・Proposedは同じ候補集合・適格条件・
取得時点で比較し、欠損はunranked、同点はcanonical ID順とする。source変更による
候補取得差とranking差を混ぜない。S2実接続を再開せずに固定snapshotで準備できる範囲を使う。

Graphの誤ったsupported判定、Reasoningの意味を反転させる誤り、出典の捏造などは
重大項目として個別記録する。単なる低confidenceや棄権は誤断定と区別する。
Knowledgeは表示対象のprecisionと、ラベル済みrequired概念のrecallを測る。
出力なしのprecision、正例なしのrecallはN/A。未評価やN/Aを合格へ変換しない。

提案のmacro平均は各論文の有効値を同じ重みで平均し、分母・N/A数・各論文値も示す。
10本の小標本なので強い統計的一般化を行わない。主要3機能の成績を一つの総合点へ潰さない。
