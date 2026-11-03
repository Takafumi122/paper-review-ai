# Paper Review AI Project Rules

## Source of truth

na判断では以下を正本とする。

* docs/requirements.md
* docs/architecture.md
* docs/research-lineage.md
* docs/scoring-lineage.md
* docs/llm-execution-spec.md
* docs/evaluation.md
* docs/thesis-choosing.md
* ADR/0001-mvp-decisions.md

Deprecated / superseded 文書が正本と矛盾する場合、正本を優先する。

## Engineering principles

* MVPを優先する
* YAGNIを守る
* 不要な抽象化を追加しない
* Microservices化しない
* Redis / Celery / Kafkaを追加しない
* Vector Databaseを追加しない
* Agent Frameworkを追加しない
* Kubernetesを追加しない

LLMをworkflow controllerとして使用せず、
Deterministic Pipeline + semantic judgmentが必要な箇所のみLLM
というArchitectureを維持する。

## Python

* Python 3.12
* uvで依存管理
* Python commandは原則 `uv run` 経由
* type hintsを使用
* Pydanticを使用
* pytest
* ruff
* pyright

## Frontend

* React
* TypeScript
* Vite

Phase 0ではUIデザインの作り込みを行わない。

## Security

* .envを読まない
* credentialsを扱わない
* secretsをrepositoryへ保存しない
* paper bodyをapplication logへ出さない
* git pushしない
* 明示されない限りgit commitしない

## Scope

現在のPhase外の機能を先回りして実装しない。

Requirement変更が必要な場合は、
勝手に変更せず問題として報告する。

テストを通すためにRequirementを弱めない。

## Verification

変更後は該当する範囲について可能な限り以下を実行する。

* pytest
* ruff
* pyright
* Alembic migration verification
* frontend tests
* frontend build

既存の失敗を隠したりskipへ変更して完了扱いにしない。
