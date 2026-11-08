# Railway: full-v44の配置と運用

更新日2026-11-08。既存本番は[公開アプリ](https://paper-review-ai-production.up.railway.app/)で稼働中。Graphは所有者による数値品質未達の一時許容で暫定有効化されています。[品質未達・運用確認の集計](../docs/graph-publication-status.md)を参照してください。新しいdeployの品質合格や、有料検証の包括承認を意味しません。

## 公開元と自動連携

GitHubの旧codex/reading-web連携と自動deployment triggerは解除済みです。既存本番は検証したCLIスナップショットを使用しており、GitHubへのpush/mergeでは自動変更されません。公開するGit SHA、source/image hash、固定モデルhash、DB path、設定、切り戻し先を別々に記録してください。

このPRは現行実装をmainから再現可能にするための同期です。実行用codeと公開説明を含み、開発用DB、モデル実体、評価の生レポートやキーは含みません。自動デプロイ再開は、保護されたmain・必須CI・モデル/容量確認と公開判断を揃えてから行います。

## Imageと容量

```sh
docker build --build-arg LOCAL_EMBEDDINGS=true -t paper-review-ai:graph-publication .
```

Dockerfileのdefaultは任意ML依存なし。OpenAlex＋自前embeddingを使う場合だけCPUイメージを選択します。Railwayでは`LOCAL_EMBEDDINGS=true`のservice variableで対応するDocker build ARGを渡します。キーをbuild ARGへ設定しません。

既存本番はmemory limit 2 GB、2 vCPU、1 replica、1 Worker。直近のRAG実解析peakは約1.20 GBでした。全論文の最大値を保証せず、1 GBで動くとは扱いません。容量と継続課金の変更は別判断とし、無断でplan/resourceを増やしません。

## 永続volumeと起動

volumeを`/app/.work`へmountし、次をstart commandに設定します。

```text
python deployment/start_railway.py
```

`RAILWAY_RUN_UID=0`はmount rootの初期化だけに使います。launcherはroot所有権をUID/GID10001、mode0700へ設定し、補助groupを外して権限を落とした後にモデル準備・migration・API/Workerを起動します。再帰chownはしません。volumeはbuild/pre-deployではmountされないため、migrationとモデル準備をそこへ移さないでください。

新規環境のDB例は`/app/.work/app.db`。既存本番の配置記録は`/app/.work/graph-v44-2026-11-08.db`です。再deployで既存のDB変数やvolumeを変更・削除しないでください。ローカル検証DBを本番へuploadしません。

## Variables

secretは所有者がRailwayへ直接設定し、チャットやGitへ貼りません。

| 変数 | 用途・設定 |
| --- | --- |
| OPENAI_API_KEY | secret、LLM provider |
| OPENALEX_API_KEY | secret、候補取得 |
| PAPER_REVIEW_ACCESS_TOKEN | secret、招待コード。公開bindでは必須 |
| LOCAL_EMBEDDINGS | true、CPU依存をbuild |
| RAILWAY_RUN_UID | 0、初期化後は10001へdrop |
| PAPER_REVIEW_PIPELINE_MODE | full |
| PAPER_REVIEW_READING_ONLY | false、OpenAlex候補取得を使う場合 |
| PAPER_REVIEW_ACADEMIC_BACKEND | openalex_local |
| PAPER_REVIEW_LOCAL_SPECTER2_MANIFEST | /app/.work/models/specter2/manifest.json |
| PAPER_REVIEW_PREPARE_LOCAL_SPECTER2 | trueで固定公開モデルを起動時に準備・再検証 |
| PAPER_REVIEW_OPENALEX_LOCAL_VERIFIED | 新規環境はfalse、API/model確認後の公開判断で変更 |
| PAPER_REVIEW_OPENALEX_LOCAL_AUDITED | 新規環境はfalse、品質判定または明示した例外承認が必要 |
| PAPER_REVIEW_DATABASE_PATH | 既存DBを維持。新規環境のみ上記例を使用 |
| PAPER_REVIEW_INITIAL_BUDGET_USD | 0.50、既存本番の初期解析上限 |
| PAPER_REVIEW_DETAIL_BUDGET_USD | 0.10、詳細生成の別上限 |
| PAPER_REVIEW_MAX_QUEUED_JOBS | 1 |
| PAPER_REVIEW_MAX_DAILY_JOBS | 3、直近24時間の詳細を含む受付Job数 |
| PAPER_REVIEW_RELEASE_CANARY | false、通常運用で自動有料解析を開始しない |
| OMP_NUM_THREADS / MKL_NUM_THREADS | 2 / 2 |
| TOKENIZERS_PARALLELISM | false |

既存本番のOpenAlex両フラグtrueは明示した暫定公開の例外です。固定評価の合格とは呼びません。S2用readinessフラグと停止履歴は独立であり、この変更に伴って解除しません。`PAPER_REVIEW_ENABLE_PROVIDER_CHECKS`はunset、公開時に`--operator-checks`を使いません。frontend buildで`VITE_RECORDED_PREVIEW`は設定しません。

費用予約・unknown保留・Job試行上限はアプリ側のガードです。providerアカウント全体の請求やインフラのhard capではありません。招待コードは共有方式で、個人別の認可はありません。

## 固定モデル

[CPUモデル準備](LOCAL-EMBEDDINGS.md)に従います。launcherの明示選択は、固定された公開repo/revisionだけをHugging Face認証なしで取得します。既存manifestを上書きせず、全ファイルhashと既知manifest hashを照合し、一致しなければ起動を拒否します。推論時はoffline/local_files_onlyです。

起動時の取得には240秒の期限があるためhealthcheckは`/health`、待機300秒を例とします。過去の起動失敗を重複deployで積み増さず、既存の稼働版と候補を区別してください。

## 課金なしの公開前確認

```sh
UV_CACHE_DIR=.work/uv-cache uv run --no-env-file \
  python scripts/verify_demo_http.py https://YOUR-HOST.up.railway.app
```

full-v44 / edge-grounding-v3、未認証の解析/結果取得401、運用専用API非公開、frontendのHTML/JS/CSSを検査します。不正な入力以外の解析POSTは行いません。healthのbackend・モデルidentity・readiness・予算も確認してください。公開domainをsourceやnetwork設定変更後に再確認し、従来URLの到達性を維持します。

本番のキーを外へ取り出してテストする必要はありません。認証付き成功経路と実論文の解析は別の確認で、予算・試行数・通信障害/認証/429時の停止条件を確定してから行います。前回の完走を新しい版の有料解析へ流用して「確認済み」とはしません。

`verify_railway_graph.py`は過去の1件限定release canary用です。`PAPER_REVIEW_RELEASE_CANARY=true`が明示されたときだけ起動し、排他的markerで重複投入を止めます。通常公開で有効化しないでください。否定された根拠判定を変えるための再試行にも使いません。

## 切り戻し

1. 新規Job投入を止め、実行中Jobと費用保留を確認する。
2. Graph重大根拠誤り、OOM、解析失敗等があれば、OpenAlex両フラグと自動canaryをfalseにし、同じDB/volumeを保持して候補取得構成へ戻す。
3. 起動に問題があれば、確認済みの直前image/sourceと設定へ戻す。古いsnapshotには既知の依存脆弱性がある場合があるため、長期運用へ戻したことにはしない。
4. health・認証・公開domain・既存結果・新規受付停止/再開を確認して記録する。

根拠不足による0辺は有効な棄権で、辺を強制公開しません。公開の可否と品質の正式合格を別に報告します。
