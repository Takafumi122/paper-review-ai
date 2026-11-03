# Overnight Tasks

## Goal

Phase 0 の実装を可能な限り完了させる。

正本ドキュメント:

- README.md
- docs/requirements.md
- docs/architecture.md
- docs/research-lineage.md
- docs/scoring-lineage.md
- docs/llm-execution-spec.md
- docs/evaluation.md
- docs/thesis-choosing.md
- ADR/0001-mvp-decisions.md

Deprecated / superseded 文書は仕様判断の根拠に使用しない。

---

## Priority 1: Backend foundation

- FastAPI skeleton
- GET /health
- POST /analyses
- GET /analyses/{analysis_id}
- configuration layer
- SQLite
- SQLAlchemy
- Alembic

完了条件:
- migration が成功する
- APIテストが成功する

---

## Priority 2: Job system

- AnalysisJob
- StageExecution
- state machine
- DB-backed queue
- Worker
- fake pipeline

最低限:

queued
→ parsing
→ completed

を実際に動かす。

完了条件:
- POST /analyses
- Worker処理
- GET /analyses/{id}
- completed

まで一周する。

---

## Priority 3: LLM foundation

- LLMProvider interface
- FakeLLMProvider
- model routing configuration
- LLMExecution metadata
- structured-output interface
- fake usage / cost telemetry

実OpenAI APIは呼ばない。

---

## Priority 4: Frontend skeleton

- React
- TypeScript
- Vite
- arXiv URL入力
- POST /analyses
- status polling
- completed表示

見た目の作り込みは不要。

---

## Priority 5: Tests

追加する:

- state transition unit tests
- invalid transition tests
- FakeLLMProvider tests
- config tests
- POST /analyses integration test
- Worker integration test
- GET /analyses/{id} integration test

---

## Priority 6: Quality

Priority 1-5が完了した場合のみ実施。

- lint
- type check
- tests
- migration test
- README起動方法更新
- dead code確認
- obvious bug修正

新機能追加はしない。

---

# Rules

- workspace外を変更しない
- .envを読まない
- API keyを扱わない
- credentialを扱わない
- git pushしない
- git commitしない
- 外部ネットワークを要求しない
- Semantic Scholar APIを呼ばない
- arXiv APIを呼ばない
- OpenAI APIを呼ばない
- Redisを追加しない
- Celeryを追加しない
- Vector DBを追加しない
- Agent frameworkを追加しない
- Kubernetesを追加しない
- Microservices化しない
- 要件にない機能を追加しない
- Deprecated文書を正本として扱わない

仕様判断が必要な場合:

1. 正本文書を再確認
2. 明確なら従う
3. 小さな実装判断なら最小構成を選ぶ
4. Product Requirement変更が必要なら変更しない
5. BLOCKEDとして記録し次へ進む

同じ問題への修正が3回失敗した場合:

- それ以上繰り返さない
- BLOCKEDとして記録
- 次の独立タスクへ進む

---

# Reporting

作業中、以下を

OVERNIGHT_REPORT.md

へ継続的に記録する。

- 完了したタスク
- 変更した主要ファイル
- 実行したテスト
- テスト結果
- 設計判断
- BLOCKED
- 未完了項目

すべてのPriorityが完了した場合:

1. 全テスト
2. type check
3. lint
4. migration確認

を実施する。

完了後は不要なリファクタリングを続けず、終了する。