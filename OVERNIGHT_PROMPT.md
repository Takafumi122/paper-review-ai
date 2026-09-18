OVERNIGHT_TASKS.md を読み、記載されているタスクをPriority順に実行してください。

作業開始前に、正本として指定されているRequirements / Architecture / ADRを読み、現在のrepositoryの実装状態との差分を確認してください。

その後は、人間からの追加確認を待たず、自律的に作業を進めてください。

各タスクについて、

1. 現状を調査する
2. 最小の実装方針を決める
3. 実装する
4. 関連テストを実行する
5. 失敗した場合は原因を調査して修正する
6. 完了条件を確認する
7. OVERNIGHT_REPORT.md を更新する
8. 次のPriorityへ進む

を繰り返してください。

重要な制約:

* OVERNIGHT_TASKS.md に記載された範囲を超えて機能追加しない
* 正本ドキュメントのProduct Requirementを変更しない
* workspace外を変更しない
* .env、APIキー、credentialを読み書きしない
* git push / git commitを行わない
* 外部ネットワークアクセスを要求しない
* Browser / Computer Use等に依存しない
* 実OpenAI API、arXiv API、Semantic Scholar APIを呼ばない
* 同一問題への修正が3回失敗したらBLOCKEDとして記録し、その問題に依存しない次の作業へ進む
* テストを通すためだけに要件を弱めない
* 大規模なリファクタリングを目的化しない
* 不必要な抽象化や新規依存を追加しない

作業状況を OVERNIGHT_REPORT.md に継続的に残してください。最後だけにまとめて書くのではなく、主要なタスクが完了するたびに更新してください。

Priorityがすべて完了した場合は、全テスト、lint、type check、migration確認を行い、最終結果を OVERNIGHT_REPORT.md にまとめて終了してください。

タスク完了後、朝まで時間が残っていても、不要な機能追加やリファクタリングを開始しないでください。
