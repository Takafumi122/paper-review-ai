# Paper Review AI

LLM/NLP論文を、**Research Lineage・Reasoning Flow・必要な前提知識**から理解するためのアプリです。説明には英語原文の引用を対応付けます。採否判断や、論文の絶対的な品質評価は行いません。

**開発中の暫定公開版です。** 現在の処理版は `full-v44`、Graphの根拠検証は `edge-grounding-v3` です。

- [Railwayの実解析アプリ](https://paper-review-ai-production.up.railway.app/)：招待コードが必要です。OpenAlex＋CPU版SPECTER2で候補取得し、生成した説明を選択引用で審査します。
- [GitHub Pagesの記録プレビュー](https://takafumi122.github.io/paper-review-ai/)：APIキー不要の保存済み候補表示です。新規解析は行いません。

Graphの本番有効化は、2026-11-08の所有者による数値品質未達の一時許容に基づきます。正式な品質合格ではありません。引用が先行研究固有の貢献と後続研究との関係を支えなければ、辺を表示しません。[現在の公開状態・未達と検証範囲](docs/graph-publication-status.md)を確認してください。

## できることと制限

| 機能 | 現在の状態 |
|---|---|
| arXiv URLの受付・全文構造化 | 実装済み。取得元の429や取得失敗では処理が制限されます |
| Reasoning Flow | 原文に対応する推論ステップを生成し、主張と引用を照合します |
| 前提知識 | ステップ間に必要な概念と短い説明、オンデマンド詳細を生成・照合します |
| Research Lineage | OpenAlex＋自前SPECTER2を明示選択でき、Graphを原文で審査します。所有者の例外承認で暫定有効化中。根拠不足の辺は表示しません |
| キー不要の記録プレビュー | 3論文の保存済み候補を検索・絞り込みし、出典・年代・欠損・スコア内訳を確認できます。[公開URL](https://takafumi122.github.io/paper-review-ai/)で閲覧可能 |

`semantic_scholar`は互換defaultとして残りますが、本番は明示選択の`openalex_local`を使用し、S2を呼びません。障害時に別backendへ自動切替しません。対象は英語の公開arXiv論文・付録込み75ページ以内です。3機能すべての完走や説明品質の正式合格は保証しません。

[現在の検証済み範囲と残課題](docs/graph-publication-status.md)に、実行成功・根拠審査・品質合格を区別して記載しています。[以前の公開記録](docs/release-status.md)は履歴です。

## まず記録プレビューを試す（APIキー不要）

ブラウザで https://takafumi122.github.io/paper-review-ai/ を開くと、保存済み候補のExplorerが直接表示されます。論文の切替・検索・絞り込み・候補詳細を操作できます。2026-10-06の評価スナップショットであり、URL入力や新規解析の画面はありません。`main` の `frontend/` 変更時に `.github/workflows/pages.yml` が記録専用ビルド（`VITE_RECORDED_PREVIEW=true`）を作り、`frontend/dist` だけを配信します。バックエンドやAPIキーは使いません。

ローカルで同じプレビューを開く場合、必要なもの：Python 3.12、[uv](https://docs.astral.sh/uv/)、Node.js 24、npm。

```sh
git clone https://github.com/Takafumi122/paper-review-ai.git
cd paper-review-ai
npm --prefix frontend ci --no-audit --no-fund
npm --prefix frontend run dev
```

表示されたローカルURLを開き、ヘッダーの **「記録データを見る」** を選びます。候補データのプレビューであり、新しい論文の解析は行いません。

## 実解析をローカルで起動する

```sh
UV_NO_ENV_FILE=1 uv sync --locked
npm --prefix frontend ci --no-audit --no-fund
npm --prefix frontend run build
```

起動するターミナルへ、次の環境変数を外部から設定します。キーをコマンド引数、チャット、Git、ログへ書かないでください。

| 環境変数 | 用途 |
|---|---|
| `OPENAI_API_KEY` | 説明生成・根拠照合 |
| `OPENALEX_API_KEY` | `openalex_local`の候補取得用。公式モデルと任意CPU依存の準備は[こちら](deployment/LOCAL-EMBEDDINGS.md) |
| `SEMANTIC_SCHOLAR_API_KEY` または `S2_API_KEY` | 互換defaultのS2 backendを選択する場合のみ必要 |
| `PAPER_REVIEW_ACCESS_TOKEN` | 共有時の招待コード。ローカルのみでは任意 |

まず読解機能だけを起動する場合、学術APIキーとembeddingモデルは不要です。

```sh
PAPER_REVIEW_INITIAL_BUDGET_USD=0.50 \
uv run --no-env-file python -m paper_review.server --reading-only
```

候補取得・GraphのためのOpenAlex構成は、[固定モデルとCPUイメージの準備](deployment/LOCAL-EMBEDDINGS.md)に従い、`--academic-backend openalex_local`を明示して起動します。

[ローカルアプリ](http://127.0.0.1:8000)を開きます。マイグレーション・FastAPI・DB-backed Workerを起動します。終了はCtrl+C。既定DBは `.work/app.db`、更新後は再起動が必要です。上記コマンドは `.env` を読み込みません。

新規環境ではreadinessをfalseから開始します。OpenAlex用は`PAPER_REVIEW_OPENALEX_LOCAL_VERIFIED`と`PAPER_REVIEW_OPENALEX_LOCAL_AUDITED`で、S2用のフラグとは独立です。実API/model確認と公開判断を省略して有効化しないでください。既存本番の例外承認を、新しい環境や正式な品質合格へ流用しません。

外部APIを使わずJobの受付・実行だけ確認する場合は、デフォルトのFake設定で以下を別ターミナルで実行できます。APIキーやプロバイダー設定を渡す必要はありません。

```sh
uv run --no-env-file uvicorn main:app --host 127.0.0.1 --port 8000
uv run --no-env-file python -m paper_review.worker --mode phase0_fake
```

Fake結果は論文解析の結果ではありません。`--operator-checks` は開発者向けの履歴依存診断です。公開ホストでは有効にしません。

## 検証（APIキー・開発用DB不要）

```sh
UV_NO_ENV_FILE=1 UV_CACHE_DIR=.work/uv-cache uv sync --locked
UV_CACHE_DIR=.work/uv-cache uv run --no-env-file pytest -q
UV_CACHE_DIR=.work/uv-cache uv run --no-env-file ruff check src tests scripts main.py migrations
UV_CACHE_DIR=.work/uv-cache uv run --no-env-file pyright
mkdir -p .work
PAPER_REVIEW_DATABASE_PATH="$PWD/.work/validation.db" uv run --no-env-file alembic upgrade head
PAPER_REVIEW_DATABASE_PATH="$PWD/.work/validation.db" uv run --no-env-file alembic check
npm --prefix frontend test
npm --prefix frontend run build
python3 scripts/check_publication.py --tracked --history
```

テストは実ネットワークを禁止します。必要な回帰入力は[テスト用fixture](tests/fixtures/README.md)から再構成し、開発者のDBや認証情報を使いません。テストが作った再生データは終了時に削除され、既存データや停止記録は置き換えません。GitHub Actionsの `Checks` も同じ検証を行います。自動デプロイは、`main` の記録専用フロントエンドをGitHub Pagesへ配信する `pages.yml` だけで、API・Workerはデプロイしません。

## ブラウザデモの共有

静的な記録プレビューはフロントエンドのビルドだけで配信でき、https://takafumi122.github.io/paper-review-ai/ で公開済みです。実解析にはPython API・常駐Worker・永続SQLiteが必要で、GitHub Pagesだけでは動きません。

[配置手順](deployment/README.md)と[Railway手順](deployment/RAILWAY.md)を用意しています。既存のRailway本番は稼働中です。GitHubからの自動デプロイは停止しており、PRのmergeだけでは本番は更新されません。GitHub Pagesは引き続き記録プレビュー専用です。

共有する実解析デモにはHTTPS、招待コード、永続ディスクが必要です。プロバイダーのキーはホストのSecret設定に置き、フロントエンドへ渡しません。既定で初期解析USD 1.00、詳細USD 0.10の予約予算を適用します。費用は推定で、応答喪失による不明額は保持します。設定defaultは待機・実行中10件、直近24時間25件です。既存本番は初期解析USD 0.50/Job・詳細USD 0.10/Job、同時1 Job・直近24時間3 Jobへ絞っています。詳細生成も受付Job数に算入します。プロバイダーやインフラ全体の請求上限ではありません。

招待コードは共有デモ用で、利用者ごとのデータ分離はありません。開発者の `.work`、`.env`、DB、ログをデモへコピーしないでください。

## 設計と仕様

Python 3.12 / FastAPI / Pydantic / SQLAlchemy / Alembic / SQLite、React / TypeScript / Vite。

[要件](docs/requirements.md)・[構成](docs/architecture.md)・[研究系列](docs/research-lineage.md)・[スコア](docs/scoring-lineage.md)・[LLM実行](docs/llm-execution-spec.md)・[評価](docs/evaluation.md)・[ADR](ADR/0001-mvp-decisions.md)を正本とします。後続のユーザー決定に基づく現在のモデル設定は `src/paper_review/llm/provider.py`、推定価格設定は `src/paper_review/llm/pricing.py` にあります。過去の初期モデル表や記録済み判定を、現在の利用可能性・料金・品質の保証として扱いません。
