# 最新版の案内

現在はfull-v44の暫定公開版です。[最新の公開状態・検証範囲・未達](graph-publication-status.md)を参照してください。以下は2026-11-03時点の履歴であり、現在の配置・readiness・テスト件数を示しません。

---

# 公開準備版の範囲 — 2026-11-03

## 今回共有するもの

LLM/NLP論文読解を支援する実装、キー不要の候補プレビュー、ローカル起動手順、
ネットワークを使わない回帰テストを共有する。有料ホスト契約や実解析デモのデプロイは今回の範囲外。

公開URL（記録プレビューのみ）：https://takafumi122.github.io/paper-review-ai/
リポジトリ：https://github.com/Takafumi122/paper-review-ai （旧名paper-reading-assistantからリダイレクト）

| 対象 | 確認できた範囲 | 残る制限 |
|---|---|---|
| 取得・構造化 | arXiv URL検証、PDF解析、版・引用・出典の保持 | 新規取得の429・失敗があり、全URLの取得は保証しない |
| Reasoning Flow | 保存済みAttention・LoRA・ReAct等で生成・主張と根拠の照合を実施 | 独立goldによる正式な品質合格は未確定 |
| 前提知識 | 概念の仕組み、論文とのつながり、短文・詳細の照合を実装・検証 | 全論文での説明品質を保証しない |
| Research Lineage | 保存済みLoRAで現在の生成処理がAghajanyan→LoRAの単一路を返した。原文・候補・年代・構造を再確認 | S2実接続は停止中。本番の2つのreadinessフラグはfalse |
| 合流の根拠 | RAG原文では同じ主要貢献への2系列統合を支持。LoRAの並列した着想・動機だけの案は不採用 | 固定原文に対する局所的検証であり、候補取得からの一連の成功ではない |
| 記録プレビュー | 3論文の候補を検索・絞り込み、年代・出典・スコア欠損を確認できる。GitHub Pagesで公開済み | 保存済み候補の表示。新規論文の解析や完成した研究史ではない |

現在はfull-v35 / merge-grounding-v2 / lower-bound-v1。
Stage 2補正は承認済みidentity-v1で、重みは正本を維持する。

## S2を保留している理由

2026-11-03のAPIキー付き最小metadata GETも1回目で429だった。
embedding取得や後続のLoRA検証はその窓では送らず停止した。
その後、接続の切り分けを保留するユーザー判断に基づき、保存済みデータでLoRAの検証を完了した。
これはS2が復旧したことを意味しない。

停止履歴を消したり、キーなし送信・別providerへの切替をしたりせず、
公開デモでも学術取得を保留する起動wrapperを使う。
S2の追加試行は、この公開準備には含めない。

## 公開対象と除外対象

- 公開対象：アプリ・テスト・ロックファイル・migration・配置設定・仕様・手順。
- テスト用：回帰検証に必要な記録済みの出典・候補・引用・ベクトル・判定のみをfixtureとして収録。
  [fixtureの説明](../tests/fixtures/README.md)のとおり、開発者のDBではなく最小限の再生データを使う。
- 除外：`.env`、キー、`.work`、開発用DB、raw PDF、ログ、依存関係の実体、
  `evaluation/reports`以下の通常の実行履歴。過去の実行レポートを一括公開しない。
- キー不要のフロントエンドプレビューには、論文本文・キー・embeddingベクトルを含めない。

`.env`はローカルに保持したままGitの追跡から外す。
既存履歴の`.env`についてはblob metadataが空ファイルであることを確認し、内容は読まない。
公開前チェックは現在のGit選択ファイルと履歴を走査し、検出内容そのものを出力しない。

## クリーン環境での再現性

最初のクリーンコピーでは18テストが開発用DBに依存して失敗した。
その依存を記録済みfixtureで再現できるよう修正し、同じ回帰テストを省略せず通す。
テストのために作ったDB・停止状態・記録ファイルは終了時に削除し、既存ファイルは置き換えない。
本番コードからテストfixtureを読み込む経路はない。

コミット済みの`9d9d25b`から新規cloneを作り、開発用DB・`.env`・既存の仮想環境を
コピーせずに検証した。Python依存関係41パッケージとフロントエンド依存関係22パッケージを
ロックと取得済みパッケージキャッシュから新規インストールできた。

| 確認 | 結果 |
|---|---|
| pytest（CIと同じDB環境変数） | **638 passed**、既存deprecation warning 2件 |
| Ruff / Pyright | 成功 / 0 errors |
| Alembic upgrade / check | 新規workspace内DBで成功 |
| frontend test / build | 6 tests成功 / build成功 |
| 公開前チェック | 選択ファイル・全Git履歴とも検出事項0。dotenv内容は読まず検査 |
| Fakeの受付→Worker実行→結果取得 | HTTP 202→completed、simulated=true。実解析ではない |
| ブラウザ初期表示 | HTML・JS・CSS配信と入力画面の表示を確認 |

CIのDB環境変数に影響されていた設定テストも、defaultと明示指定の双方を検証するように修正した。
テストの省略や許容値の引下げは行っていない。
検証用APIは127.0.0.1の別portで起動し、確認後に停止した。
今回の検証で外部論文取得・S2・有料LLM呼出は0回。

取得・構造化、候補選考、fixture、根拠付き読解、UI、API/Worker、operator検証、
配置/CI、公開説明を別コミットへ分割した。生成データはGitHubの差分で区別し、
元バイトを保持する設定とした。大きな変更はCommit単位で確認できる。
READMEとこの文書への検証結果の追記は、上記コード検証後の文書変更である。

## GitHub Pagesの記録プレビュー（2026-11-03公開）

PR #1（merge commit `42af0ba`）とPR #2（merge commit `19fc698`）をmerge方式で`main`へ統合し、
機能別コミットを保持した。PR #2は記録専用ビルド、Pages workflow、リポジトリ名変更後のリンク更新を含む。

- 配信内容：`VITE_RECORDED_PREVIEW=true` でビルドした `frontend/dist` のみ。
  2026-10-06スナップショットの3論文（Attention・LoRA・ReAct）の保存済み候補。
- 提供しないもの：URL入力、新規論文の解析、Research Lineage・Reasoning Flow・前提知識の生成、
  API・Worker・DB。APIキー・論文本文・PDF・embeddingベクトルは含まない。
- 配信workflow：`.github/workflows/pages.yml`。公開前チェック、frontend 6 tests、記録専用ビルドの後に
  GitHub Pagesへデプロイする。`main`の`frontend/`またはworkflow変更、手動実行で起動する。

| 確認 | 結果 |
|---|---|
| PR #2のChecks（push / pull_request） | 両方成功 |
| ローカル検証（PR #2） | pytest 638 passed（既存warning 2件）、Ruff成功、Pyright 0 errors、frontend 6 tests・記録専用build成功、公開前チェック検出0 |
| Pages workflow（run 41590816537） | build・deployとも成功 |
| 公開URLの配信 | HTML・`./assets/`配下のJS・CSSがサブパスで200。JSはローカルの記録専用buildとバイト一致 |
| ヘッドレスChromeでの操作 | Explorerが初期表示。URL入力欄と実解析への切替ボタンなし。論文切替、検索、5種の絞り込み、候補詳細とスコア内訳、開発状況パネルが動作。JS例外・consoleエラー0 |

ブラウザが自動で要求するホスト直下の`/favicon.ico`は404（アプリはfaviconを定義していない）。
アプリ資産の404ではない。公開URLでの確認は保存済みデータの表示確認であり、
実解析・S2復旧・説明品質の合格を示すものではない。今回の公開作業で外部論文取得・S2・有料LLM呼出は0回。

## 次の公開工程

実解析デモには有料または別途承認したホスト、HTTPS・招待コード・永続SQLite・利用上限が必要で、
ホスト上の再起動後の永続化と、承認した予算内の実解析を最後に確認する。今回は実施していない。
