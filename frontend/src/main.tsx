import { StrictMode, useMemo, useState } from 'react';
import { createRoot } from 'react-dom/client';
import samples from './data/demo.json';
import { eligibilityLabels, filterCandidates, formatMetric } from './demo';
import type { Candidate, DemoPaper } from './demo';
import { LiveApp } from './LiveApp';
import './style.css';

const repository = 'https://github.com/Takafumi122/paper-review-ai';
const previewOnly = import.meta.env.VITE_RECORDED_PREVIEW === 'true';
const papers = samples.papers as DemoPaper[];
const filters = [
  ['all', 'すべて'], ['top10', '参考 Top 10'], ['eligible', '対象候補'],
  ['uncertain', '未確定'], ['excluded', '対象外'], ['probes', '診断候補'],
];

function ExternalLink({ href, children }: { href: string; children: React.ReactNode }) {
  return <a href={href} target="_blank" rel="noreferrer">{children}<span aria-hidden="true"> ↗</span></a>;
}

function Detail({ candidate: c }: { candidate: Candidate | undefined }) {
  if (!c) return <aside className="detail"><span className="eyebrow">PAPER DETAILS</span>
    <h3>候補のタイトルを選択</h3><p>引用元、同定方法、日付の精度とスコアの内訳を確認できます。</p></aside>;
  return <aside className="detail" aria-label="選択した候補の詳細">
    <span className="eyebrow">PAPER DETAILS</span>
    <div className="detail-tags"><span className={`badge ${c.eligibility}`}>{eligibilityLabels[c.eligibility]}</span>
      {c.diagnostic_probe && <span className="badge probe">診断候補</span>}</div>
    <h3>{c.title}</h3>
    <p className="authors">{c.authors.length ? c.authors.join(', ') : '著者情報は未取得'}</p>
    <div className="external-links">
      {c.external_ids.arxiv && <ExternalLink href={`https://arxiv.org/abs/${encodeURIComponent(c.external_ids.arxiv)}`}>arXiv</ExternalLink>}
      {c.resolution_status === 'resolved' && c.external_ids.semantic_scholar &&
        <ExternalLink href={`https://www.semanticscholar.org/paper/${encodeURIComponent(c.external_ids.semantic_scholar)}`}>Semantic Scholar</ExternalLink>}
      {c.external_ids.doi && <ExternalLink href={`https://doi.org/${encodeURIComponent(c.external_ids.doi)}`}>DOI</ExternalLink>}
    </div>
    <dl className="metadata">
      <div><dt>元論文の参考文献番号</dt><dd>{c.reference_orders.map(n => `[${n}]`).join(' ') || '未取得'}</dd></div>
      <div><dt>書誌情報の同定</dt><dd>{c.resolution_status === 'resolved' ? '同定済み' : '未解決'}
        {c.resolution_method && <small>{c.resolution_method}</small>}</dd></div>
      <div><dt>確認できた公開日</dt><dd>{c.date_provenance?.value ?? '未取得'}
        {c.date_provenance && <small>{c.date_provenance.precision === 'year' ? '年精度' : c.date_provenance.precision === 'month' ? '月精度' : '日精度'} · {c.date_provenance.source_id}</small>}</dd></div>
      <div><dt>時系列の判定</dt><dd>{c.temporal_status === 'eligible' ? '対象論文より前' : c.temporal_status === 'later_work' ? '後発研究' : '先行を確認できず'}
        {c.temporal_uncertainty && <small>{c.temporal_uncertainty}</small>}</dd></div>
    </dl>
    {c.exclusion_reason && <p className="detail-note">対象外の理由：{c.exclusion_reason}</p>}
    {c.score && <div className="score-detail"><h4>Stage 1 の内訳 <span>部分スコア</span></h4>
      <dl className="metadata">
        <div><dt>SemanticSimilarity <small>重み 0.40</small></dt><dd>{formatMetric(c.score.semantic_similarity)}</dd></div>
        <div><dt>CitationRelation <small>重み 0.35</small></dt><dd>{formatMetric(c.score.citation_relation)}</dd></div>
        <div><dt>CitationImportance <small>重み 0.25</small></dt><dd>{formatMetric(c.score.citation_importance)}</dd></div>
        <div className="total"><dt>InitialScore / 指標の充足率</dt><dd>{formatMetric(c.score.initial_score)}<small>{Math.round(c.score.coverage * 100)}%</small></dd></div>
      </dl>
      <p className="fine-print">欠損は0にせず、観測できた指標の重みで集計しています。高い値は、系譜上の重要性や論文の品質を保証しません。</p>
    </div>}
    {c.diagnostic_probe && <p className="detail-note">診断項目：{c.diagnostic_probe}。動作確認用の選定であり、科学的な正解ラベルではありません。</p>}
  </aside>;
}

function Explorer() {
  const [paperId, setPaperId] = useState(papers[0].arxiv_id);
  const [filter, setFilter] = useState('all');
  const [query, setQuery] = useState('');
  const [selected, setSelected] = useState<string | null>(null);
  const [showAbout, setShowAbout] = useState(false);
  const paper = papers.find(p => p.arxiv_id === paperId)!;
  const rows = useMemo(() => filterCandidates(paper.candidates, query, filter), [paper, query, filter]);
  const candidate = paper.candidates.find(c => c.id === selected);
  function choose(next: DemoPaper) {
    setPaperId(next.arxiv_id); setQuery(''); setFilter('all'); setSelected(null);
  }
  return <>
    <section className="hero">
      <div><span className="eyebrow">RESEARCH CONTEXT, MADE VISIBLE</span>
        <h1>論文の背景を、<br />たどる。</h1>
        <p>新しい論文は、どんな先行研究の上に立っているのか。<br className="desktop" />引用候補とその根拠を、一つずつ確かめるための研究ノート。</p>
        <div className="hero-status"><span className="status-dot" />記録データで試せる公開プレビュー <span>v0.1</span></div>
      </div>
      <div className="hero-note"><span className="eyebrow">THIS PREVIEW</span><strong>まずは、3本の論文から。</strong>
        <p>候補を検索し、書誌情報・公開順序・スコアの欠損を確認できます。新しい論文のライブ解析は行いません。</p>
        <button className="text-button" onClick={() => setShowAbout(v => !v)} aria-expanded={showAbout}>{showAbout ? '説明を閉じる −' : 'できること・開発状況を見る ＋'}</button>
      </div>
    </section>
    {showAbout && <section className="about-panel" aria-label="開発状況">
      <div><h3>実装済み</h3><p>arXiv取得・PDF構造化、引用候補の同定、決定的なStage 1ランキング、欠損と出典の記録。公開デモでは保存済みの評価結果を表示します。</p></div>
      <div><h3>現在の制限</h3><p>研究系列・推論フロー・前提知識の生成は実装済みです。S2は現在429で停止中のため、研究系列の本番実行は保留しています。この画面は保存済み候補データのプレビューです。</p></div>
      <div><h3>データの扱い</h3><p>2026-10-06の評価スナップショットです。論文本文、PDF、APIキー、Embeddingベクトルはこのデモに含めていません。</p></div>
    </section>}
    <section className="paper-picker" aria-label="対象論文を選択">
      {papers.map((p, i) => <button key={p.arxiv_id} className={`paper-card ${p.arxiv_id === paperId ? 'active' : ''}`} onClick={() => choose(p)} aria-pressed={p.arxiv_id === paperId}>
        <span className="card-top"><span>0{i + 1}</span><span>{p.year}</span></span>
        <strong>{p.short_title}</strong><span className="paper-full-title">{p.title}</span>
        <span className="card-bottom">{p.statistics.distinct_candidate_count} 引用候補 <span aria-hidden="true">↗</span></span>
      </button>)}
    </section>
    <section className="workspace" aria-label="候補論文エクスプローラー">
      <div className="workspace-head"><div><span className="eyebrow">SELECTED PAPER</span><h2>{paper.title}</h2>
        <p>{paper.year} <span>·</span> arXiv:{paper.arxiv_id} <span>·</span> <ExternalLink href={`https://arxiv.org/abs/${paper.arxiv_id}`}>元論文を読む</ExternalLink></p></div>
        <span className="recorded-label">記録データ / {samples.snapshot_date}</span>
      </div>
      <div className="stats-grid">
        <div><strong>{paper.statistics.distinct_candidate_count}<small>本</small></strong><span>独立した候補レコード</span></div>
        <div><strong>{paper.statistics.eligible}<small>本</small></strong><span>時系列・種別を満たす候補</span></div>
        <div><strong>{paper.statistics.resolved_reference_count}<small>/ {paper.statistics.parsed_reference_count}</small></strong><span>同定済みの参考文献</span></div>
        <div><strong>{paper.embedding_available}<small>/ {paper.statistics.eligible}</small></strong><span>SPECTER2取得済み候補</span></div>
      </div>
      <div className="notice"><span className="notice-icon" aria-hidden="true">i</span><div><strong>参考順位です。ランキング品質は未評価です。</strong>
        <p>意味類似度が未取得のため、現在の順位は引用情報だけによる部分スコアです。
          {paper.arxiv_id === '1706.03762' ? '取得時にAPIが429を返しました。' : '429の発生後に取得を停止したため、この論文は未照会です。'}研究系列の確定結果ではありません。</p></div></div>
      <div className="explorer-grid"><div className="candidate-list">
        <div className="list-tools"><label className="search"><span aria-hidden="true">⌕</span><input type="search" aria-label="候補のタイトル・著者を検索" value={query} onChange={e => setQuery(e.target.value)} placeholder="タイトル・著者を検索" /></label>
          <span className="result-count" aria-live="polite">{rows.length} 件</span></div>
        <div className="filters" aria-label="候補の絞り込み">{filters.map(([value, label]) => <button key={value} className={filter === value ? 'selected' : ''} onClick={() => setFilter(value)} aria-pressed={filter === value}>{label}</button>)}</div>
        <div className="list-column-labels"><span>引用候補 / 出典</span><span>参考順位・スコア</span></div>
        {rows.length === 0 ? <div className="empty-state"><h3>一致する候補がありません</h3><p>別のキーワードや絞り込みをお試しください。</p><button onClick={() => { setQuery(''); setFilter('all'); }}>検索をリセット</button></div> :
          <ul className="candidate-rows">{rows.map(c => <li key={c.id} className={selected === c.id ? 'current' : ''}>
            <button className="candidate-button" onClick={() => setSelected(c.id)} aria-pressed={selected === c.id}>
              <span className="candidate-main"><span className="candidate-meta">{c.year ?? '年不明'} <span className={`badge ${c.eligibility}`}>{eligibilityLabels[c.eligibility]}</span>{c.diagnostic_probe && <span className="probe-marker">診断</span>}</span>
                <strong>{c.title}</strong><span className="candidate-authors">{c.authors.slice(0, 2).join(', ') || '著者情報未取得'}{c.authors.length > 2 ? ' et al.' : ''}</span></span>
              <span className="candidate-score"><span>{c.rank !== null ? `#${c.rank.toString().padStart(2, '0')}` : '順位なし'}</span><strong>{c.score ? formatMetric(c.score.initial_score) : '—'}</strong>{c.score && <small>充足率 {Math.round(c.score.coverage * 100)}%</small>}</span>
            </button>
          </li>)}</ul>}
      </div><Detail candidate={candidate} /></div>
    </section>
    <section className="roadmap" aria-label="今後の開発"><div><span className="step-index">01</span><h3>候補と出典を確認</h3><p>このプレビューで操作できます。</p></div><div><span className="step-index">02</span><h3>研究系列をつなぐ</h3><p>実解析画面から実行します。実接続の検証条件が未達の場合は生成を保留します。</p></div><div><span className="step-index">03</span><h3>論理と前提知識へ</h3><p>実解析画面で原文の根拠と一緒に確認します。</p></div></section>
  </>;
}

function App() {
  const [local, setLocal] = useState(previewOnly);
  return <><header className="site-header"><a className="brand" href="./"><span className="brand-mark" aria-hidden="true">p<span>r</span></span><span>Paper Review AI<small>Paper understanding, with evidence.</small></span></a>
    <nav aria-label="メインナビゲーション">{!previewOnly && <button className="text-button" onClick={() => setLocal(v => !v)}>{local ? '論文を解析する' : '記録データを見る'}</button>}<ExternalLink href={repository}>GitHub</ExternalLink></nav></header>
    <main className={local ? 'local-demo' : ''}>{local ? <Explorer /> : <LiveApp />}</main>
    <footer className="site-footer"><span>Paper Review AI <span>—</span> 論文を評価するのではなく、理解を支える。</span><div><span>根拠を確認できない関係は確定しません</span><ExternalLink href={`${repository}/blob/main/README.md`}>開発状況・起動方法</ExternalLink></div></footer>
  </>;
}

createRoot(document.getElementById('root')!).render(<StrictMode><App /></StrictMode>);
