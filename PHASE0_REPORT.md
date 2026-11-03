# Phase 0 Completion Report: Foundation & Infrastructure

Date: 2026-09-19  
Branch: `codex/overnight-phase0`  
Python: `3.12.12`  
Status: **Phase 0 Complete — Ready for Phase 1**

---

## 1. Phase 0 Goal

Phase 0 の目的は、今後の論文取得、PDF解析、Research Lineage、Reasoning Flow、Just-in-Time Background Knowledge の実装に向けた**オフライン実行基盤・インフラストラクチャ・検証骨格を確立すること**です。

本物の外部API（arXiv / Semantic Scholar / OpenAI）や重厚なアルゴリズムは実装せず、決定論的なモック（Fake）パイプラインを用いて以下の疎通と信頼性を検証・完了させることがゴールでした。

- FastAPI による非同期ジョブ受付とポーリングAPI
- SQLite + SQLAlchemy + Alembic による永続化とスキーマ管理
- AnalysisJob の厳格な状態マシン制御
- ホスト単一実行・lease・heartbeat・checkpoint を備えた専用 Worker
- LLMProvider 抽象化、タスク別ルーティング、厳格な Pydantic 出力検証、テレメトリ、予算事前予約
- React + TypeScript + Vite によるフロントエンド骨格（ポーリング、エラー処理、結果表示）
- 単体テスト・統合テスト・型チェック・Linter・マイグレーション整合性の確立

---

## 2. Implemented Components

既存実装および今回の引き継ぎで検証・整備したコンポーネント一覧です。

| レイヤー / コンポーネント | 実装ファイル | 概要 |
|---|---|---|
| **設定管理** | `src/paper_review/config.py` | 非秘密設定のみを対象とする Pydantic Settings。`.env` 自動読込禁止、ワークスペース内 `.db` 強制、`fake` プロバイダ検証。 |
| **ドメイン状態管理** | `src/paper_review/domain/jobs.py` | `JobStatus`（10状態）、厳格な `transition()` 関数、正規表現による arXiv URL 検証・正規化 (`arxiv_identity`)。 |
| **データベースモデル** | `src/paper_review/infrastructure/database/models.py` | `Paper`, `AnalysisJob`, `StageExecution`, `LLMExecution` の SQLAlchemy 2.0 モデル定義。アクティブジョブ部分一意インデックス。 |
| **セッション・接続** | `src/paper_review/infrastructure/database/session.py` | SQLite WALモード、外部キー有効化、`PRAGMA busy_timeout=5000`、`BEGIN IMMEDIATE` による直列化トランザクション。 |
| **マイグレーション** | `migrations/env.py`, `migrations/versions/0001_foundation.py`, `alembic.ini` | 固定DDLによる初期スキーマ作成。SQLAlchemyモデルとの完全一致（ドリフトゼロ）。 |
| **API バックエンド** | `src/paper_review/api/app.py`, `main.py` | `GET /health`, `POST /analyses` (202 Accepted, 同一fingerprint共有), `GET /analyses/{job_id}` (ステータス・費用・時間取得)。 |
| **非同期 Worker** | `src/paper_review/worker.py` | ホスト全体で同時実行1件の排他的claim、lease / heartbeat によるフェンシング、再起動時の checkpoint 再開、CLI (`--once`)。 |
| **LLM Gateway** | `src/paper_review/llm/gateway.py` | 最大3 attempts（初回+2回retry）、指数バックオフ、タスク別予算事前予約、タイムアウト時unknown費用保持、Pydanticバリデーション修復、キャッシュ。 |
| **LLM Provider** | `src/paper_review/llm/provider.py` | `LLMProvider` Protocol、タスク別 `TaskPolicy`（14タスクのモデルルーティング）、決定論的 `FakeLLMProvider`。 |
| **可観測性・ログ** | `src/paper_review/infrastructure/observability.py` | 構造化JSONテレメトリログ。機密・論文本文・生プロンプトを出力しない厳格なサニタイズ。 |
| **フロントエンド** | `frontend/src/main.tsx`, `api.ts`, `vite.config.ts` | React 19 + TypeScript + Vite。arXiv URL送信、重複防止、1秒ポーリング、日本語UI、エラー再試行、Viteプロキシ。 |
| **テストスイート** | `tests/unit/`, `tests/integration/`, `frontend/src/api.test.ts` | 状態遷移、設定、LLM Gateway、API、Worker並行制御・フェンシング、Alembicライフサイクル、フロントエンド契約テスト。 |

---

## 3. Architecture Decisions

ADR-0001 および正本ドキュメントに基づき、以下の設計判断を遵守・実装しました。

1. **モノリス別プロセスアーキテクチャ**
   - APIとWorkerは同じコードベースを共有し、別プロセスで起動する最小構成。
   - Redis, Celery, Vector DB, Agent Framework, Kubernetes などの外部ミドルウェアは導入しない。
2. **SQLite + WAL + BEGIN IMMEDIATE によるキューイング**
   - SQLite単一ファイルでDBキューを実装。`BEGIN IMMEDIATE` でclaim処理を直列化し、ホスト全体で同時実行1件を担保。
3. **Lease, Heartbeat, Checkpoint による耐障害性**
   - Workerはジョブにlease期限を設定し、処理中バックグラウンドタスクで定期的にheartbeatを更新。
   - Worker異常終了時は別Workerが期限切れジョブを回収。保存済みcheckpointとattempt数を保持し、冪等に再開。
4. **決定論的パイプラインとLLM境界**
   - LLM自身が次のツールや実行フローを自律決定するエージェントループは採用せず、Pythonコードによる固定パイプラインを採用。
   - LLM GatewayがPydantic構造化出力バリデーションを実施し、不正データはリトライ枠内で修復要求。
5. **テレメトリとデータ機密保護**
   - 論文本文、生プロンプト、生LLMレスポンスはアプリケーションログに出力しない。
   - 構造化JSONログには識別ID、イベント種別、トークン数、推定コスト、エラーコードのみを出力。
6. **偽装のないシミュレーテッド費用**
   - Phase 0 のLLM費用は公式料金表と分離した固定レート（`fake-v1`）による推定値とし、実課金が発生しているかのような偽装を行わない。

---

## 4. Validation Results

以下のバリデーションをすべて実行し、合格を確認しました。

| コマンド | 検証内容 | 結果 | 詳細 |
|---|---|---|---|
| `uv run python --version` | Python環境統一 | **PASS** | `Python 3.12.12` |
| `uv run alembic upgrade head` | マイグレーション適用 | **PASS** | リビジョン `0001 (head)` 適用完了 |
| `uv run alembic check` | スキーマドリフト検出 | **PASS** | `No new upgrade operations detected.` (完全一致) |
| `uv run pytest -q` | バックエンド全テスト | **PASS** | **82 passed**, 2 warnings (1.72s) |
| `uv run ruff check src tests main.py migrations` | コード静的解析・Linter | **PASS** | `All checks passed!` (エラー・警告 0) |
| `uv run pyright` | 型チェック (Python 3.12) | **PASS** | `0 errors, 0 warnings, 0 informations` |
| `npm --prefix frontend test` | フロントエンド契約テスト | **PASS** | **2 passed** (Node.js test runner) |
| `npm --prefix frontend run build` | フロントエンド本番ビルド | **PASS** | `tsc --noEmit && vite build` 成功 |
| エンドツーエンド実行テスト | API → Queue → Worker → Polling | **PASS** | POST 202 → queued → claim → fake LLM → completed を確認 |

---

## 5. Known Warnings

現時点で以下の Warning が確認されていますが、FastAPI / Starlette の上流ライブラリ仕様に起因するものであり、Phase 0 の動作を阻害するものではありません。

1. `StarletteDeprecationWarning: Using 'httpx' with 'starlette.testclient' is deprecated; install 'httpx2' instead.`
   - FastAPI 0.136 の TestClient 内部での警告。
2. `DeprecationWarning: The anyio.abc.BlockingPortal alias is deprecated, use anyio.from_thread.BlockingPortal instead.`
   - Starlette の TestClient 終了処理内での anyio 警告。

---

## 6. Deferred Items (意図的に延期した項目)

以下は正本仕様に基づき、Phase 1 以降で順次実装される項目です。Phase 0 では意図的に対象外としています。

- **arXiv API クライアント & PDF ダウンローダ**: Phase 1 で実装。
- **PDF パーサー (pdfplumber)**: Section抽出・文献抽出・ページ位置取得。Phase 1 で実装。
- **一時PDFのライフサイクル管理**: 処理後削除・異常終了時クリーンアップ。Phase 1 で実装。
- **Semantic Scholar API クライアント**: 引用情報・レコメンデーション取得。Phase 2 で実装。
- **OpenAI Responses API 実アダプター**: `gpt-5-nano` / `gpt-5.6-luna` 接続。Phase 2 / Phase 3 で実装。
- **Research Lineage アルゴリズム**: 2段階候補ランキング、合流判定、Confidence計算。Phase 2 で実装。
- **Reasoning Flow 生成**: 最大10ステップ、8種ノード、3種エッジ。Phase 3 で実装。
- **JIT Background Knowledge**: 必要度スコアリング（score=3のみ採用）、文脈解説。Phase 4 で実装。
- **UI 詳細画面**: SVG による Lineage / Reasoning Flow のグラフ描画。Phase 6 で実装。

---

## 7. Phase 0 Definition of Done (達成度照合)

| DoD 項目 | 要求仕様 | 実装状況 | 判定 |
|---|---|---|:---:|
| **API Skeleton** | `GET /health`, `POST /analyses`, `GET /analyses/{id}` | `src/paper_review/api/app.py` に実装済み。arXiv URL 正規化・重複ジョブ共有・422/404処理完備。 | **DONE** |
| **Configuration** | Pydantic による設定、`.env` 読込禁止、workspace 内 DB 強制 | `src/paper_review/config.py` に実装済み。バリデータ完備。 | **DONE** |
| **DB & Migration** | SQLite (WAL/FK/busy_timeout), SQLAlchemy, Alembic | `models.py`, `session.py`, `0001_foundation.py` に実装済み。`alembic check` ドリフトなし。 | **DONE** |
| **Job State Machine** | 10状態、遷移バリデーション、fake短縮パス | `src/paper_review/domain/jobs.py` に実装済み。unit test 網羅。 | **DONE** |
| **DB-backed Worker** | 原子性 claim、lease、heartbeat、checkpoint 再開 | `src/paper_review/worker.py` に実装済み。マルチプロセス・別Worker再開検証済み。 | **DONE** |
| **LLM Abstraction** | Protocol, Fake Provider, ルーティング, Pydantic 出力 | `provider.py`, `gateway.py` に実装済み。14タスクのモデル・トークン・タイムアウト定義済み。 | **DONE** |
| **LLM Telemetry & Guard** | 3 attempts, 予算事前予約, unknown 保持, サニタイズログ | `gateway.py`, `observability.py` に実装済み。本文漏洩防止テスト合格。 | **DONE** |
| **Frontend Skeleton** | React 19, TypeScript, Vite, 入力, ポーリング, 状態表示 | `frontend/` に実装済み。test/build パス。 | **DONE** |
| **All Tests & Linters** | pytest 82件、Ruff、Pyright、npm test、npm build | すべてエラー 0 / 警告 0 で通過。 | **DONE** |

**結論: Phase 0 の Definition of Done は 100% 達成されています。**

---

## 8. Remaining Issues (未決事項と引き継ぎ課題)

ADR-0001 に記載された未決事項（Decision Gates）および実装上の注意点です。これらは Phase 0 の完了を妨げるものではありませんが、後続フェーズの着手前に留意する必要があります。

1. **D1: AdjustedContributionRelevance の補正式（Phase 2 Stage 2 前に確定要）**
   - 現在は未決（`null` / `blocked_configuration`）。Phase 2 の Stage 2 候補ランキング実装前に補正式の確定が必要です。
2. **D2: 品質の数値合格閾値および評価体制（Phase 5 最終評価前に確定要）**
   - 人手アノテーションおよび合否判定の閾値基準。
3. **D3: 10評価論文の版と50ページ制限の整合性（Phase 1 マニフェスト作成時に確認要）**
   - 付録込みで50ページを超過する論文（GPT-3等）の取り扱い方針を Phase 1 開始時に確認。
4. **パッケージ実行時の `PYTHONPATH=src` 不要化（対応完了）**
   - PEP 517 / PEP 621 に準拠した Hatchling ビルド設定（`[build-system]`, `[tool.hatch.build.targets.wheel]`）を導入し、`uv sync` で `paper_review` が通常パッケージとして認識されるように整備しました。これにより `PYTHONPATH=src` なしで `uv run uvicorn main:app` および `uv run python -m paper_review.worker` が実行可能です。

---

## 9. Phase 1 Readiness (Phase 1 開始可否)

### 判定: **READY TO PROCEED TO PHASE 1 (Phase 1 開始可能)**

**理由**:
- Phase 0 の全目標（API、永続化、キュー、Worker、状態マシン、Fake LLM、テレメトリ、UI骨格、テスト）が完全に実装・検証されています。
- テスト82件、Linter、型チェック、ビルド、スキーマチェックのすべてがグリーンです。
- 非同期ジョブ投入から Worker 処理、フロントエンドのポーリング表示までが一気通貫で動作することを確認済みです。
- D1〜D3の未決事項は Phase 0 を妨げず、Phase 1（PDF取得・解析）への移行にも影響しません。

---

## 10. Recommended Next Issues (次に着手すべきIssue)

Phase 1（Retrieval / Parsing）に向けて、以下の順序でのタスク着手を推奨します。

1. **Issue 1-1: 評価対象10論文のマニフェスト作成と50ページ制約の検証 (D3対応)**
   - `docs/thesis-choosing.md` の10論文について、対象arXivバージョン、ページ数、付録の有無を確認し、マニフェストファイル（`evaluation/manifest/`）を定義する。
2. **Issue 1-2: arXiv API クライアントおよび PDF 取得・一時ファイル管理の実装**
   - HTTPX を用いた arXiv API クライアント、レート制限対応、PDF の一時ダウンロードと処理後・異常終了時の安全な削除機構（`finally` および起動時残骸クリーンアップ）の実装。
3. **Issue 1-3: PDF パーサーアダプター (pdfplumber) の実装と評価**
   - 本文、Section構造、文献一覧（References）、ページ番号の抽出。代表論文での抽出品質検証。
4. **Issue 1-4: PaperRevision / PaperSection / Reference の DB モデル・Alembic マイグレーション**
   - 抽出したセクションテキストを永続化し、後続のLLM解析・再検証に再利用するためのDBスキーマ拡張（`migrations/versions/0002_parsing.py`）。
5. **Issue 1-5: Worker パイプラインへの Parsing ステージ本実装の組み込み**
   - Fake パイプラインの短縮遷移を解除し、`queued` → `parsing`（PDF取得・パース・セクション保存）の実行フローを確立。
