# Evaluation Paper Selection

Status: Canonical. Updated: 2026-09-19.
[requirements.md](requirements.md) の10論文を維持する。ここに示す系列は評価観点であり、
確認済みの正解Edgeやsupported判定ではない。具体的な根拠は人手annotationで確認する。

| # | 論文 | 年（既存要件の表記） | 選定理由・検証観点 |
|---:|---|---:|---|
| 1 | Attention Is All You Need | 2017 | RNN/CNN、Attentionと並列化の関係。古い論文のParser品質、前提知識の選別 |
| 2 | Language Models are Few-Shot Learners | 2020 | GPT、Scaling、In-context Learningの区別。長い論文と複数実験の縮約 |
| 3 | Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks | 2020 | Parametric LMとRetrievalの統合。2系列合流の根拠 |
| 4 | LoRA: Low-Rank Adaptation of Large Language Models | 2021 | Fine-tuning/PEFTからの直接的な前提。低rank知識の必要性 |
| 5 | Training language models to follow instructions with human feedback | 2022 | Instruction tuning/RLHF。手順と評価結果の対応 |
| 6 | Chain-of-Thought Prompting Elicits Reasoning in Large Language Models | 2022 | Few-shotと推論の関係。Observation/Result/Interpretationの区別 |
| 7 | ReAct: Synergizing Reasoning and Acting in Language Models | 2022 | ReasoningとActingの統合。異なる系列を合流させる正当性 |
| 8 | Direct Preference Optimization | 2023 | RLHFからPreference optimizationへの関係。数式を過剰に説明しない範囲 |
| 9 | QLoRA: Efficient Finetuning of Quantized LLMs | 2023 | LoRAと量子化。複合Contributionと必要な背景知識 |
| 10 | Self-RAG | 2023 | RAG、適応的取得、自己評価の関係。分岐Reasoningと背景概念 |

## 選定意図

単一系列、2系列の合流、手法改善、学習方式、推論時の動作、複数実験を含める。
有名さ・被引用数だけで選んだ集合としない。一方で、この10本だけでは未知の新規論文への性能を保証しない。

## Phase 1での確認

- 正式title、arXiv ID、使用version、最初の公開日、その根拠をmanifestに記録。
- この表の年をそのまま公開順序判定の精密な日付として使わない。
- 付録を含むPDFのページ数、全文取得、対象分野・英語・理論偏重でないことを確認。
- 75ページ制限と評価セットの不整合があれば、例外承認または対象版/集合の見直しを行う。
  manifestの記載だけでページ数や版を検証済みとは扱わない。
- 初期のParser/annotation確認はAttention、LoRA、ReActの3本から始める（実装順の提案）。
- 図表依存のEvidence不足、非arXiv先行研究、同年日付不明のケースは別の小さなfixtureでも補う。

評価方法と完成条件は [evaluation.md](evaluation.md) を参照。

## 2026-11-04 評価用PDFの確認

ユーザー依頼の評価条件確定により、GPT-3 v4（75ページ）、InstructGPT v1（68ページ）、
CoT v6（43ページ）、DPO v2（27ページ）、Self-RAG v1（30ページ）を公式PDFで確認した。
CoT/DPOは版を維持してページ数を訂正。Self-RAGは公式arXivの版履歴にv1しか存在しないため、
従来のv2・23ページ指定をv1・30ページへ訂正し、manifest v1.2.0に固定する。
最初の公開日は変更しない。PDF SHA-256と検証過程は
`evaluation/reports/evaluation-sources-2026-11-04/` に記録し、過去の診断は上書きしない。
本文・付録・参考文献が同一ページに混在するため、排他的な本文/付録ページ数は推測せずnullとする。
残る5論文の既存manifest記載を、この検証による再確認済みとは扱わない。
