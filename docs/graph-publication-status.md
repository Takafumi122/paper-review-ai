# 現行Graph公開版の状態 — full-v44

更新日：2026-11-08。実装・公開運用の記録であり、canonical仕様や固定評価基準を置き換えない。

## この版に含む変更

- OpenAlexの書誌照合・references/related worksと、任意CPU版SPECTER2の推論。backendは明示選択し、S2へ自動fallbackしない。固定モデル・runtime・前処理・入力hashでcacheを分離する。
- 参考文献と著者年引用の同定、欠測・出典・日付精度の保持、固定のlower-bound-v1 / identity-v1採点。
- Graphの各辺について、選んだ引用が先行研究固有の貢献と後続研究との関係を支持するか審査する。合流には各系列と統合関係の根拠を要求する。不支持の提案は内部記録へ保持し、公開APIでは辺を返さない。
- Reasoningは主要実験・結果・条件を原文から選択し、各ステップの主張と説明を審査する。同じ論文・版・節の連続区間だけを引用window候補として扱い、後処理で引用を補って合格へ変えない。
- Knowledgeは目的・制約等の必要操作を選び、表示前に各項目自身の選択引用で説明を審査する。他項目の根拠を借りず、cached draftも審査を通す。否定判定・予算不足では未審査説明を表示しない。
- 根拠審査のcheckpoint・logical call ID・費用予約を保持して再開する。成功した機能を残し、失敗を部分成功と区別する。
- 付録込み75ページまで。依存のcertifi/Mako/Pillow/source-map-jsは修正版を保持する。

## 公開状態と品質の区別

実解析：[Railway](https://paper-review-ai-production.up.railway.app/)（招待制）。
記録デモ：[GitHub Pages](https://takafumi122.github.io/paper-review-ai/)（新規解析なし）。

所有者は2026-11-08、数値評価未達を一時許容してGraph機能の本番有効化を指示した。この例外承認は正式な品質合格ではなく、[固定基準](../evaluation/acceptance/criteria-v1.json)は変更していない。全新規deployのreadinessはfalseがdefaultであり、このリポジトリをmergeしてもGraphや本番連携が自動有効化されることはない。

既存本番は2 GB・2 vCPU・1 replica・1 Worker。受付設定は同時1 Job、直近24時間3 Job、初期解析USD0.50/Job、詳細USD0.10/Job。費用は推定で、providerアカウント全体やインフラのhard capではない。

## 直近の本番実行記録

開発時の運用確認としてRAG v4を新規解析した集計：

| 項目 | 結果 |
| --- | --- |
| API投入から終端まで | completed、約252秒 |
| Reasoning / Knowledge | completed / completed |
| Lineage | insufficient_evidence、辺0件 |
| LLM試行・推定費用 | 22試行、USD0.3316018、未確定費用0 |
| container peak memory | 1,196,605,440 bytes |
| 保存結果の再取得 | 一致 |

Graphは辺ありの公開確認を通過していない。未確認の先行研究固有の貢献を、数値評価の免除を理由にsupportedへ変えていない。これは1件の運用記録で、全論文への品質・メモリ上限・費用保証ではない。

開発用のDB、原文、prompt/response、生ログ、過去の評価レポートを公開しないため、本書は集計のみを示す。クリーン環境の回帰テストとCPU実行はPR本文・CIの結果を確認する。新たな公開先で有料Jobを投入する前に予算と停止条件を確定する。

## 残課題

- 現行版の固定10論文評価と、根拠のある辺を表示できる本番実例。未評価・失敗・N/Aを合格へ算入しない。
- Graph根拠選択、主要実験の網羅性、Knowledge Recall等の改善。LLM参照ラベルは独立人手正解とは呼ばない。
- adapters1.3.0がtransformers4.57.xを要求するため、transformersの既知アドバイザリが残る。固定公開モデルの全hash確認、offline/local_files_only、trust_remote_code=falseを維持し、互換修正版またはbackportを検討する。未信頼モデル・任意remote codeをロードさせない。
- Web防御ヘッダー、factory側のfail closed、バックアップ復元・課金hard limit、Actions SHA固定等の運用改善。

[Railway配置](../deployment/RAILWAY.md) / [CPUモデル準備](../deployment/LOCAL-EMBEDDINGS.md) / [評価設計](evaluation.md)。履歴の成功や古いreadiness記述を、この版の正式合格と読み替えない。
