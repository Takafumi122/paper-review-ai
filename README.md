# LLM/NLP Paper Understanding

Transformerや機械学習の基礎は知っていても、新しい論文の研究史・論理構造・専門知識で
つまずく大学生に向けた、LLM/NLP論文の理解支援アプリケーションです。
査読・採否判断・研究の絶対的な品質評価を目的としません。

## プロジェクトの目的 (Project Purpose)

LLM/NLP分野の論文を読む初学者が、個々の文は読めても「研究の歴史的背景」「問題から結論に至る論理構造」「理解に必要な前提知識」をつかめずに挫折する問題を解決します。

## 対象ユーザー (Target User)

- 機械学習の基礎および Transformer の基本概念を理解している大学生・初学者。
- 近年のLLM/NLP論文を読みたいが、先行研究とのつながり、専門用語の前提、論理展開の追跡に苦労している読者。

## MVPの3機能

1. **Research Lineage** — 主要Contributionを直接支える先行研究と、研究間に残った問題を根拠付きで示す。
2. **Reasoning Flow** — ProblemからConclusionまでの論理を、最大10StepのGraphで示す。
3. **Just-in-Time Background Knowledge** — Step間の理解に不可欠な知識だけを、その場で補う。

Targetは英語・arXiv・全文取得可能なLLM/NLP中心の公開論文（原則50ページ以内）。
先行研究は非arXivでも対象にできます。先行研究最大5本（Target除外）、Targetまで最大3 Edge、
Contributionが2系列の統合で成立する場合のみ1合流を許します。
UIと生成説明は日本語、Evidence原文は英語です。

## 現在の実装状態 (Current Status)

**Phase 0（基盤スケルトン / Infrastructure）が完了した状態**です。
本物の論文取得やLLM解析は行わず、モック（Fake）パイプラインを用いた非同期ジョブ実行、状態管理、耐障害性、UIポーリングの基盤が検証済みです。

### Phase 0で実装済みのもの

- **FastAPI バックエンド**:
  - `GET /health`（疎通確認）
  - `POST /analyses`（arXiv URL受付・正規化・重複排除・202 Accepted返却）
  - `GET /analyses/{job_id}`（ステータス・疑似コスト・実行時間・機能別状態取得）
- **SQLite + SQLAlchemy + Alembic**:
  - テーブル: `papers`, `analysis_jobs`, `stage_executions`, `llm_executions`
  - WALモード、外部キー制約、busy timeout設定
  - `BEGIN IMMEDIATE` によるトランザクション直列化（Claim競合・予算予約保護）
  - Alembic による厳格なマイグレーション管理（DDL固定・ドリフト検出）
- **AnalysisJob 状態マシン**:
  - `queued` → `parsing` → `retrieving_candidates` → `ranking_candidates` → `building_lineage` → `building_reasoning` → `building_knowledge` → `completed`
  - 終端/エラー状態: `completed`, `failed`, `partial_completed`
  - Phase 0 fakeパイプライン短縮遷移（`parsing` → `completed`）
- **DB-backed Worker**:
  - ホスト全体で同時実行1件の排他claim制御
  - lease / heartbeat によるタイムアウト監視と期限切れジョブ回収
  - stage checkpointによる再起動時の再実行冪等性と進捗保持
- **LLM Provider 抽象化 & Telemetry**:
  - `LLMProvider` Protocol および `FakeLLMProvider`（決定論的モック）
  - Pydantic strict Structured Output検証
  - タスク別モデルルーティング（`gpt-5-nano` / `gpt-5.6-luna`、token上限、timeout、deadline）
  - 最大3 attempts（初回 + 最大2回retry）の永続化
  - 予算事前予約（Budget reservation）、タイムアウト時のunknown費用保持
  - 機密・論文本文をログに出力しない構造化JSONログ（`observability.py`）
- **React + TypeScript + Vite フロントエンド**:
  - arXiv URL入力フォーム、送信、重複送信防止
  - 1秒間隔のステータスpolling
  - 完了、一部完了、失敗状態の日本語表示およびポーリング再試行UI
- **テスト基盤**:
  - 状態遷移・不正遷移・設定バリデーション
  - LLM Gateway（修復、キャッシュ、予算、リトライ、タイムアウト、クリーンアップ）
  - API・Worker並行実行・フェンシング・マルチプロセス実行
  - Alembicマイグレーションライフサイクル・スキーマ不整合テスト

## 技術スタック (Tech Stack)

- **言語**: Python 3.12, TypeScript 5.9
- **Backend**: FastAPI 0.136, Pydantic v2, Uvicorn 0.48
- **Database / ORM**: SQLite (WAL), SQLAlchemy 2.0, Alembic 1.16
- **Frontend**: React 19, Vite 7
- **パッケージマネージャ / ツール**: uv, npm, ruff, pyright, pytest

## リポジトリ構成 (Repository Structure)

```text
paper-review-ai/
├── src/
│   └── paper_review/
│       ├── api/                # FastAPIエンドポイント (app.py)
│       ├── config.py           # 非秘密設定管理 (Pydantic Settings)
│       ├── domain/             # ドメインロジック・ジョブ状態マシン (jobs.py)
│       ├── infrastructure/
│       │   ├── database/       # SQLAlchemyモデル・セッション管理 (models.py, session.py)
│       │   └── observability.py# 構造化JSONテレメトリログ
│       ├── llm/                # LLM抽象化・Gateway・Fake Provider (gateway.py, provider.py)
│       └── worker.py           # DBキューワーカー (単一実行・lease・checkpoint)
├── frontend/
│   ├── src/                    # React UIコンポーネント・APIクライアント・テスト
│   ├── package.json
│   └── vite.config.ts          # APIリバースプロキシ設定
├── migrations/                 # Alembicマイグレーションスクリプト
│   └── versions/
│       └── 0001_foundation.py
├── tests/
│   ├── conftest.py             # オフライン保護・DBテストフィクスチャ
│   ├── integration/            # API、Worker、Alembicテスト
│   └── unit/                   # 状態遷移、設定、LLMゲートウェイテスト
├── docs/                       # 正本設計仕様書群
├── ADR/                        # アーキテクチャ決定記録 (ADR-0001)
├── alembic.ini
├── main.py                     # Uvicornエントリーポイント
├── pyproject.toml
└── uv.lock
```

## ローカル環境セットアップ (Local Setup)

Python 3.12 および Node.js が必要です。

```bash
# Python依存関係の同期
uv sync

# フロントエンド依存関係のインストール
npm --prefix frontend install
```

## データベース・マイグレーション (Alembic Migration)

```bash
# マイグレーションの適用
uv run alembic upgrade head

# 現在のリビジョン確認
uv run alembic current

# モデルとDBスキーマの整合性確認
uv run alembic check
```

## アプリケーションの起動方法

各コンポーネントを別ターミナルで起動します。

### 1. Backend 起動

```bash
uv run uvicorn main:app --host 127.0.0.1 --port 8000
```

### 2. Worker 起動

```bash
# 常駐ポーリング実行
uv run python -m paper_review.worker

# 1ジョブのみ処理して終了する場合
uv run python -m paper_review.worker --once
```

### 3. Frontend 起動

```bash
npm --prefix frontend run dev
```

起動後、ブラウザで `http://127.0.0.1:5173` にアクセスします。

## 検証・テストコマンド (Validation)

```bash
# Pythonバージョンの確認
uv run python --version

# マイグレーション確認
uv run alembic upgrade head

# バックエンドテスト (pytest)
uv run pytest -q

# バックエンド Linter (Ruff)
uv run ruff check src tests main.py migrations

# バックエンド 型チェック (Pyright)
uv run pyright

# フロントエンド テスト
npm --prefix frontend test

# フロントエンド ビルド
npm --prefix frontend run build
```

## Phase 0 では未実装のもの (Out of Scope for Phase 0)

Phase 0 は動作基盤と検証骨格の確立を目的としており、以下は意図的に未実装です。

- 実 arXiv API / Semantic Scholar API との通信
- 本物の PDF 取得・ダウンロードおよび PDF パーサー（pdfplumber 等）
- 本物の OpenAI Responses API クライアント
- Research Lineage の候補探索・2段階ランキング・Graph構築アルゴリズム
- Reasoning Flow の論理ステップ生成
- Just-in-Time Background Knowledge の必要度判定・解説生成
- 外部 Web 検索 / ドキュメント取得

## Phase 1 以降との境界 (Roadmap & Boundaries)

| Phase | 内容 | 境界と達成要件 |
|---|---|---|
| **Phase 0 (完了)** | **Skeleton / Infrastructure** | API → DB queue → Fake Worker → ポーリング、再開、テレメトリログ、Fake LLM検証 |
| **Phase 1 (次工程)** | **Retrieval / Parsing** | arXiv PDF取得、Section/References構造化抽出、ページ位置保持、一時PDF削除 |
| **Phase 2** | **Research Lineage** | 補正式確定、S2候補探索、2段階スコアリング、最大5本・3Edge・1合流Lineage |
| **Phase 3** | **Reasoning Flow** | 最大10Stepの論理構造グラフ、3種のエッジ、原文Evidence紐付け |
| **Phase 4** | **JIT Knowledge** | 必要度3の概念抽出、文脈依存解説、要求時詳細生成、別費用集計 |
| **Phase 5** | **Evaluation / Reliability** | 10評価論文ベンチマーク、人手評価データ照合、障害・再開・費用検証 |
| **Phase 6** | **UI Refinement** | 日本語UI・英語Evidence表示、分岐グラフSVG描画、不確実性UI |

## 正本ドキュメントへの導線

| 文書 | 内容 |
|---|---|
| [Requirements](docs/requirements.md) | プロダクト要件、対象・機能・制約・Definition of Done |
| [Architecture](docs/architecture.md) | 最小構成、Domain、Job、API、Repository、実装Phase |
| [Research Lineage](docs/research-lineage.md) | 候補探索、公開順序、GraphとEvidence |
| [Scoring](docs/scoring-lineage.md) | 二段階ランキング、正規化、欠損、Confidence |
| [LLM Execution](docs/llm-execution-spec.md) | モデル、構造化出力、budget、retry、timeout、telemetry |
| [Evaluation](docs/evaluation.md) | 人手annotation、baseline、品質・費用・時間の評価 |
| [Evaluation Papers](docs/thesis-choosing.md) | 10評価論文と選定理由・確認事項 |
| [ADR-0001](ADR/0001-mvp-decisions.md) | 2026-09-19 Decision、補完した設定、未決事項 (D1〜D3) |
| [Phase 0 Report](PHASE0_REPORT.md) | Phase 0 完了報告書、コンポーネント一覧、Validation結果 |

正本は2026-09-19のユーザーDecisionに基づきます。Deprecated文書の古い記述は仕様として使用しません。
