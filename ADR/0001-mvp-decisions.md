# ADR-0001: MVP Decisionの正本と実装境界

Date: 2026-09-19.
Status: User decisions accepted; implementation defaults documented; remaining gates explicit.

## 背景

初期要件、外部API設計案、検討メモに重複・古い記述があり、詳細実行仕様が空だった。
Requirements Review後のユーザーDecisionを正式要件として反映する。
旧本文は削除せずDeprecated / supersededの注記と移行先を付ける。

## 確定したDecisionと反映先

| Decision | 内容 | 詳細の正本 |
|---|---|---|
| 1 | 先行研究5本、Target除外、最大3 Edge、1合流・2系列、論文種別除外 | [Research Lineage](../docs/research-lineage.md) |
| 2 | 確認できる最初の公開日優先、順序不明はcontemporary_work | [Research Lineage](../docs/research-lineage.md) |
| 3 | Targetは英語/arXiv/全文、Relatedは非arXiv可、根拠不足をsupportedにしない | [Requirements](../docs/requirements.md) |
| 4 | 指定二段階重み、Top 10、0〜1、欠損null、OpenAlex除外 | [Scoring](../docs/scoring-lineage.md) |
| 5 | lunaのContribution rubric 0〜4、score/4 | [Scoring](../docs/scoring-lineage.md) |
| 6 | Evidence Confidence 1/0.75/0.5/0 | [Scoring](../docs/scoring-lineage.md) |
| 7 | Edge Confidence 0.6/0.4、0.8/0.5閾値、Evidence=0 override | [Scoring](../docs/scoring-lineage.md) |
| 8 | PDF削除、public arXiv SectionはDB永続化可、本文ログ禁止 | [Architecture](../docs/architecture.md) |
| 9 | Knowledge Sourceの4段階優先、無制限Web Searchなし | [Architecture](../docs/architecture.md) |
| 10 | UI/説明は日本語、Evidence原文は英語 | [Requirements](../docs/requirements.md) |
| 11 | 初回1 USD目標、追加詳細別計上、paper累計 | [LLM execution](../docs/llm-execution-spec.md) |
| 12 | 5分soft target、可観測性優先 | [Evaluation](../docs/evaluation.md) |
| 13 | max_attempts=3 / max_retries=2 | [LLM execution](../docs/llm-execution-spec.md) |
| 14 | 指定2モデル、Responses API、abstraction、構造化・検証・version・budget・telemetry | [LLM execution](../docs/llm-execution-spec.md) |
| 15 | README＋7 docs＋ADRを正本化、旧文書は保存 | [README](../README.md) |

## 正本の責任範囲

requirementsはプロダクト要件、個別docsは詳細仕様を定義する。
ユーザーDecisionと矛盾する実装初期設定を採用しない。
古い文書にのみ存在する将来案は自動的にMVP要件へ昇格させない。
旧案のOpenAlex/Crossref fallback、必ず3〜5本生成、PDF Upload、未確定評価集合は採用しない。

## 今回補完した実装規則・初期設定

以下はユーザーから数値指定されたものではなく、設計文書を実装可能にする補完である。
変更時は対応する設定/policy versionと検証結果を記録する。

- cosineの[0,1]変換、CitationRelation/Importanceのrubric、coverageによる欠損集計、同点順序。
- ContributionRelationshipScoreは各Edgeの後続論文に対して0〜4 rubricを適用しscore/4。
- 年月日精度を区間比較し、合流を入次数で数えるGraph検証。
- SQLite＋Worker等の技術構成、Parser第一候補、API契約。
- task token/timeout、attempt総上限、予算guard、cache TTL、外部資料取得上限。
- 非arXiv全文の恒久保存は行わず必要Evidence抜粋・metadataに限定する標準動作。

## 未決事項と停止範囲

| ID | 未決事項 | 必要時期 | 現在の扱い |
|---|---|---|---|
| D1 | AdjustedContributionRelevanceの補正式 | 2026-10-30解決 | ユーザー承認：identity-v1（ContributionRelevanceと同値）。既定FinalScore重みを維持 |
| D2 | 品質の数値合格閾値、評価担当・二者確認の扱い | 2026-11-04方針確定 | ユーザーがLLM参照ラベルと提示済み数値基準を採用。追加API費用なしで既存出力を評価。独立人手評価とは呼ばず、不足coverageを合格にしない |
| D3 | 10評価論文の版・付録込み75ページ制限との整合 | Phase 1 manifest作成時 | 2026-11-04：上限75ページを採用。GPT-3 v4（75）・InstructGPT v1（68）・CoT v6（43）・DPO v2（27）・Self-RAG v1（30）を公式PDFで確認しmanifest v1.2.0に固定。残る5論文の再検証・品質合格を意味しない |

D1は単なる正規化方法ではなく意味のあるscore変更なので、Evidenceとの積などを勝手に設定しない。
通常の欠損処理でD1を回避することも禁止する。

Parser最終選定、Knowledge用公式/学術URL allowlist、API credentials/modelアクセス確認、
deployment・外部公開時の認証は実装時の確認事項。Phase 0はローカル/fakeで進められる。
allowlist未設定なら外部Web取得なし。非arXiv全文の永久保存を将来必要とする場合は別途判断する。

## Phase 0 readiness

**開始可能。** API、DB、migration、永続queue、Worker、型契約、fake LLM、ログの実装に必要な境界がある。
Phase 0完了は、fake Jobの投入→実行→結果取得、再起動復旧、schema不正/timeout/予算の検証で判定する。
D1〜D3はこれらの基盤を妨げない。Phase 2やMVP完成が無条件に承認済みという意味ではない。
今回の変更は文書のみであり、Phase 0のコード実装は開始していない。

## 2026-10-30 Decision — D1 identity-v1

ユーザーが「初版は補正なしで確定する」を承認。
AdjustedContributionRelevance = ContributionRelevance、policy版はidentity-v1とする。
過去の未決記録は当時の経緯として保持する。モデル・API・Stage 1品質の検証を
省略する承認ではなく、欠損やEvidence不足を0やsupportedへ変換するものでもない。

## 2026-11-02 Decision — Stage 1 lower-bound-v1

ユーザーが方式Cを採用し、正本・本番実装・検証へ進めることを明示的に承認した。
Stage 1は固定重みのスコア下限降順→coverage降順→canonical ID順で最大Top 10を選ぶ。
InitialScore、coverage、null、欠損理由とSourceは保持し、下限・上限・選考policy版を別に記録する。
全欠損は下限0でも順位対象外。旧結果は上書きせず、再現用に旧policyを明示する。
重み、引用roleの最大集約、Top-K、Stage 2のFinalScore・tie-break、identity-v1は変更しない。
本判断は品質合格・S2停止解除・Lineage本番有効化の承認ではない。

## 2026-11-03 Decision — OpenAlex + local SPECTER2 alternative

ユーザーがS2代替構成の実装と、接続・実推論・代表論文・指定10論文への診断・仕様採用判断を
なるべく連続して行うことを承認した。従来Decision 4のOpenAlex除外を、この明示選択の取得経路に
限って拡張する。NormalizedImpact、Crossref fallback、重み・Top-K・引用policyの変更は含まない。

`openalex_local`はOpenAlex書誌/引用情報と公式SPECTER2の固定base/proximity adapterを使用する。
互換defaultはS2とし、自動fallbackは行わない。候補保持policy `openalex-local-v2`では未解決
placeholderが同定済み候補を排除しないようにし、元のReference解決結果と候補50件上限を保持する。
自前推論の任意ML runtimeはこの目的に限って追加する。モデル資産と全cacheはworkspace内に置く。

本採用は構成を選べることの承認であり、品質合格や本番Lineage有効化の承認ではない。
実接続・実model契約とStage 1品質を分離し、D2/D3、引用不整合、取得不足を成功で上書きしない。
2026-11-03の`.env`読み込み許可は今回の会話限りであり、恒久的なsecurity規則の変更ではない。

## 2026-11-04 Decision — LLM参照ラベルと数値基準

ユーザーが「LLMの採点で正解を作る」と、提示済みの数値基準案を明示採用した。
固定基準は`evaluation/acceptance/criteria-v1.json`。既存出力を参照するLLM評価として
採点者・原文Evidence・snapshot hash・事前閲覧を記録し、独立人手正解とは呼ばない。
追加API費用は使わず、既存出力に欠ける版・機能・baselineを成功で埋めない。
Railway公開はユーザー指定だが、品質合格・容量検証・credential送信承認は区別して実施する。
