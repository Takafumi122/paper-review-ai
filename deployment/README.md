# 配置する構成

2026-11-08：現行版はfull-v44。実解析は[既存Railway](https://paper-review-ai-production.up.railway.app/)で招待制の暫定公開中です。[公開状態と未達](../docs/graph-publication-status.md)を確認してください。

同じコードベースのFastAPI・DB-backed Worker各1プロセスとReact静的build、永続SQLiteを単一ホストに置きます。1 replica / 1 Workerを維持し、SQLiteを複数ホストへ共有しません。

- [Railwayの起動・永続化・認証・切り戻し](RAILWAY.md)
- [固定SPECTER2モデルと任意CPUイメージ](LOCAL-EMBEDDINGS.md)
- [Reading-onlyモード](READING_WEB.md)：候補取得・Graphを実行しない独立モード
- [以前の費用比較](LOW_COST_OPTIONS.md)：調査時点の履歴で、現在の料金保証や契約指示ではありません

既存本番は2 GB・2 vCPU・1 replica。GitHub sourceと自動デプロイを解除済みで、このPRやmainの更新だけではRailwayを切り替えません。保護mainと実行image/modelの対応を確定してから、別途公開する版を選びます。GitHub Pagesは記録専用frontendの別配信です。

providerキーと招待コードは所有者がホストのVariablesへ設定します。フロントエンド、build引数、コマンド引数、Git、ログへ入れず、開発者の.env・DB・.workをuploadしません。既存本番のデータ・設定を、新cloneの検証DBや初期値で置き換えないでください。
