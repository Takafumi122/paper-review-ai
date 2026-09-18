# LLM Execution Specification

Status: Canonical. Updated: 2026-09-19.
親要件: [requirements.md](requirements.md)。採点: [scoring-lineage.md](scoring-lineage.md)。
モデル・API・検証・versioning・retry・telemetryはユーザー確定。
以下のtask上限やtimeoutは実装用の初期設定であり、性能保証や追加のプロダクト要件ではない。

## 1. Providerとmodel

ProviderはOpenAI、APIはResponses API。モデルIDは `gpt-5-nano` と `gpt-5.6-luna`。
別Provider・別modelへの暗黙の切替はしない。利用不可なら設定エラーを報告する。
実アカウントの利用権限・料金・rate limitはPhase 0/接続実装時の疎通で確認する。
この文書整理ではAPIへの有料リクエストを行っていない。

| Task | Model |
|---|---|
| Contribution候補・Research Problem抽出 | gpt-5-nano |
| 引用・論文種別分類、Evidence候補抽出 | gpt-5-nano |
| Concept抽出・必要度判定、短いKnowledge説明 | gpt-5-nano |
| Contribution Relevance、Candidate比較 | gpt-5.6-luna |
| Edge評価・Lineage構築 | gpt-5.6-luna |
| Reasoning Flow生成 | gpt-5.6-luna |
| オンデマンドKnowledge詳細 | gpt-5-nano（実装初期設定） |

書誌情報はAcademic APIから取得する。metadata欠損をLLMの記憶で埋めない。
nanoからlunaへの意味的fallbackは、task policyで明示された場合だけ同じattempt/予算枠で行う。
初期設定では自動fallbackを無効とし、分類失敗は欠損・部分成功として扱う。

## 2. 依存境界と契約

ApplicationはLLM portを呼び、provider SDKをimportしない。
Gatewayがcontext構築、policy、予算、validation、cache、telemetryを統括する。
OpenAI adapterだけがResponses APIのリクエスト形式・例外・usageを知る。

入力契約:

```text
job_id / paper_id / stage_id / logical_call_id
task_type / provider / model
prompt_version / prompt_hash / schema_version / pipeline_version
context_spans[{source_id, revision, section_id, span_id, text}]
input_hash / generation_settings / output_schema
max_input_tokens / max_output_tokens / attempt_timeout / task_deadline
cost_category / remaining_budget
```

返却契約は `validated_output / usage / provider_request_id / duration / status / error_code`。
業務score・statusはvalidated outputの後にdomain側で決定する。

## 3. Contextと出力

- taskに必要なSection・引用前後・関連する結果箇所を選ぶ。全文PDFを毎回送らない。
- 原文spanと要約を区別する。要約を原文引用として使わない。
- 上限超過はSection/段落単位で選別または分割し、省略・分割を記録する。
- 紙面・span・API取得済みIDはコードが付与し、LLMは入力中のIDを参照する。
- 論文本文・外部資料内の命令文は資料データとして扱い、実行命令にしない。
- 出力は原則strict JSON SchemaのStructured Output。自由文はschema内の文字列として扱う。
- schemaはPydanticモデルを起点にし、providerが対応するJSON Schema範囲に変換する。
- 説明・判断理由は日本語、Evidence原文は英語。原文引用は翻訳で置換しない。
- Temperature等はモデルの対応状況を確認してtask設定へ明示。未対応パラメータを一律送信しない。
  reasoning設定を含む実効設定を記録し、APIのdefault任せで再現性を主張しない。

検証順序:

1. 完了状態を確認。refusal、incomplete、timeoutを通常成功と混同しない。
2. JSON/Pydanticで型、enum、必須field、score範囲を検証。
3. Source/span/Contribution/Step IDの参照整合性を検証。
4. 引用が取得済み原文に一致するか確認。空白・改行の正規化規則を版管理する。
5. 件数・Graph・年代・Confidence hard ruleをdomain規則で検証。
6. 引用が主張を支持するかの意味的判定を保存。構文成功だけでsupportedにしない。

検証に失敗したJSONを正常な解析結果として保存しない。診断は本文を除いたエラー種別・field pathで行う。

## 4. Token budget / timeout（初期設定）

| Task | 入力token/call | 最大出力token/call | timeout/attempt | deadline/logical task |
|---|---:|---:|---:|---:|
| 抽出・分類・Evidence候補 | 8,000 | 2,000 | 30秒 | 90秒 |
| Candidate比較・Contribution評価 | 16,000 | 4,000 | 60秒 | 150秒 |
| Edge判定・Lineage構築 | 16,000 | 4,000 | 60秒 | 150秒 |
| Reasoning Flow | 24,000 | 8,000 | 90秒 | 180秒 |
| Concept・短いKnowledge | 8,000 | 3,000 | 45秒 | 120秒 |
| 要求時の詳細Knowledge | 12,000 | 4,000 | 60秒 | 150秒 |

最大出力tokenにはproviderが課金する推論token消費も考慮する。
budgetはprompt/schema等のoverheadを含めて算定する。入力長超過で勝手に全文送信しない。
分割・batch・fallbackを別名のcallにして予算を回避しない。
Job全体の実装初期上限はinitial analysisでLLM HTTP attempts 30件、
オンデマンド詳細Jobで3件。超えたらpartial_completedまたはfailedと打ち切り理由を残す。

全体の5分はsoft targetであり、300秒到達だけで自動失敗にしない。
stage/task timeoutは無期限待機を防ぐための別の制御。deadlineにはbackoff待ちも含める。
deadlineが先に来たら3 attemptsを使い切る必要はない。

## 5. Retry

```text
max_attempts = 3
max_retries = 2
```

初回＋最大2回。Gatewayを唯一のretry管理者とし、SDKの内蔵retryは無効にする。

| 状況 | 動作 |
|---|---|
| 429・一時的5xx・接続障害 | Retry-Afterを優先、残予算・deadline内で再試行 |
| timeout | 費用不明を記録し、予算予約を残してから再試行可否を判断 |
| JSON/schema/参照validation失敗 | エラー箇所を限定して修復要求。attempt枠を共有 |
| 出力上限到達 | context/要求範囲を縮小して同じ枠で再試行。上限を無断増大しない |
| 401/403・無効model・無効schema/入力 | 設定/入力エラー。自動再試行しない |
| refusal | 失敗理由を表示し、回避目的の再試行をしない |
| Evidence不足・score=0 | 有効な意味的結果。望む回答を得るための再試行をしない |

Retry-Afterがないときの初期backoffは指数1秒・2秒＋0〜0.5秒jitter。
共通のprovider rate limiterを通し、retryもattempt・token・costに含める。
Worker再起動でattempt数を0に戻さない。応答喪失によるprovider側の重複課金を完全に防げるとは仮定しない。

## 6. 費用とtelemetry

ユーザー確定の目標はinitial analysisにつき1 paperあたりUSD 1.00以下。
オンデマンド詳細は別集計。paper累計には全解析Job・再試行・オンデマンドを含める。

```text
cost_category = initial_analysis | jit_detail
initial_job_cost = sum(all attempt costs in the initial job)
detail_job_cost = sum(all attempt costs in the detail job)
paper_cumulative_cost = sum(all job costs for this paper)
```

初期解析の予算guard初期値はUSD 1.00、詳細JobはUSD 0.10とする（調整可能な運用設定）。
呼出前に入力＋最大出力の保守的費用を予約し、既知費用＋未精算予約＋新規予約がguardを超える場合は開始しない。
キャッシュ割引は呼出前には仮定しない。完了後usageと料金表版で精算する。
cache read/write等が別課金ならadapterでその項目も扱う。
推論tokenが既にoutput総量に含まれる場合は二重加算しない。
timeout・応答喪失は `cost_status=unknown` として予約を残す。unknownを0として予算を復活させない。
アカウントの請求確定額を取得したとは主張せず、API usageからの推定費用として表示する。

LLMExecutionに保存する項目:

- paper/job/stage/logical_call/attempt IDs、開始終了、latency、retry理由、cache hit。
- provider、要求model、返却model、request ID、実効generation settings。
- prompt version/hash、schema/pipeline/scoring版、input hash、source IDs。
- input/cached input/output/reasoning tokens（取得できない値はnull）。
- estimated cost、currency、price table version、cost status、予約額。
- validation/refusal/timeoutの状態、本文を含まないerror code。
- 検証済み構造化出力はDBの結果として保持する。本文入りprompt/raw responseをapplication logへ出力しない。

料金表はmodel別の設定として管理し、実装時に公式価格を再確認する。更新時は版を残す。

## 7. Cacheと再実行

キーはpaper revision/content hash、入力Source hashes、parser/pipeline/scoring version、
prompt version/hash、schema version、provider/model、generation settings、language、task type。
異なるTarget contextのKnowledge説明をconcept名だけで使い回さない。
有効なvalidation済み結果のみ再利用。中断・失敗を正常cacheとして扱わない。
短期のAPI障害を永久にキャッシュしない。missing sourceが新たに取得できたら入力hashを変える。

public arXivのSection本文はDBから再利用し、毎回PDFを再取得する必要はない。
provider aliasの中身が将来変わる可能性を考慮し、返却model・実行日時を残す。
同じ設定でもLLM出力の完全一致は保証しない。固定Pipelineは制御の再現性を指す。

## 8. 検証と公式資料

Phase 0ではfake providerでtimeout、3 attempts上限、費用予約、validation、cache、部分成功を検証する。
実接続は別途行い、モデル利用可能性とtoken計測を確認する。

- [OpenAI Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs)
- [GPT-5 nano](https://developers.openai.com/api/docs/models/gpt-5-nano)
- [GPT-5.6 Luna](https://developers.openai.com/api/docs/models/gpt-5.6-luna)

公式仕様は2026-09-19に参照。SDK/APIの具体的引数はadapter実装時にも確認し、モデルは無断変更しない。
