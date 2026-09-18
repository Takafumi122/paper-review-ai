# LLM/NLP Paper Understanding

Transformerや機械学習の基礎は知っていても、新しい論文の研究史・論理構造・専門知識で
つまずく大学生に向けた、LLM/NLP論文の理解支援アプリケーションです。
査読・採否判断・研究の絶対的な品質評価を目的としません。

## MVPの3機能

1. **Research Lineage** — 主要Contributionを直接支える先行研究と、研究間に残った問題を根拠付きで示す。
2. **Reasoning Flow** — ProblemからConclusionまでの論理を、最大10StepのGraphで示す。
3. **Just-in-Time Background Knowledge** — Step間の理解に不可欠な知識だけを、その場で補う。

Targetは英語・arXiv・全文取得可能なLLM/NLP中心の公開論文。原則50ページ以内。
先行研究は非arXivでも対象にできます。先行研究最大5本（Target除外）、Targetまで最大3 Edge、
Contributionが2系列の統合で成立する場合のみ1合流を許します。
UIと生成説明は日本語、Evidence原文は英語です。

## 設計方針

Deterministic Pipeline＋必要箇所のみLLM。Academic APIはarXivとSemantic Scholar。
OpenAI Responses APIで `gpt-5-nano` と `gpt-5.6-luna` を役割別に利用し、
Structured Output・Pydantic validation・出典検証を行います。

初回解析のLLM費用は1 USD/論文、処理時間は5分を目標とし、時間はsoft targetです。
要求時のKnowledge詳細生成は別計上し、論文単位の累計も測定します。
PDFは処理後削除し、公開arXivのSection本文はDBで再利用します。本文はapplication logに出しません。

## 正本ドキュメント

| 文書 | 内容 |
|---|---|
| [Requirements](docs/requirements.md) | 対象・機能・制約・Definition of Done |
| [Architecture](docs/architecture.md) | 最小構成、Domain、Job、API、Repository、実装Phase |
| [Research Lineage](docs/research-lineage.md) | 候補探索、公開順序、GraphとEvidence |
| [Scoring](docs/scoring-lineage.md) | 二段階ランキング、正規化、欠損、Confidence |
| [LLM Execution](docs/llm-execution-spec.md) | モデル、構造化出力、budget、retry、timeout、telemetry |
| [Evaluation](docs/evaluation.md) | 人手annotation、baseline、品質・費用・時間の評価 |
| [Evaluation Papers](docs/thesis-choosing.md) | 10評価論文と選定理由・確認事項 |
| [ADR-0001](ADR/0001-mvp-decisions.md) | 2026-09-19 Decision、補完した設定、未決事項 |

正本は2026-09-19のユーザーDecisionに基づきます。Deprecated文書の古い記述は仕様として使いません。

## 現在の状態と次の段階

文書整理段階です。アプリケーション実装・実測評価はまだありません。
**Phase 0（Skeleton / Infrastructure）を開始可能**です。架空の起動コマンドは掲載せず、
実装後にセットアップ・テスト手順を追加します。

残る判断は、AdjustedContributionRelevanceの補正式（Phase 2前）、品質合格基準・評価体制、
評価論文の版と50ページ制限の整合です。[ADRの未決事項](ADR/0001-mvp-decisions.md) に停止範囲を記載しています。

## 旧文書

重要な情報と検討経緯を残すため、次の文書は削除せず **Deprecated / superseded** としています。

- [初期要件](docs/paper_understanding_agent_requirements.md)
- [旧API・ランキング案](docs/research_lineage_api_and_ranking_design.md)
- [MVP検討メモ](docs/ref/What-to-decide-for-MVP.md)
- [旧要件ファイル](docs/0918-要件定義.md)、[旧Lineageファイル](docs/legacy/Research-Lineage.md)、
  [旧Reasoningファイル](docs/Reasoning%20Flowについて.md)、[旧LLMファイル](docs/llm_usage.md)、
  [旧プロンプト用ファイル](docs/first_prompt.md)、[旧調査用ファイル](docs/ref/findings.md)

将来候補のOpenAlex・Crossref、後続研究追跡、汎用Q&A、学習履歴・個人化はMVPに含めません。
