import { useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { isTerminal, labels, requestDetail, status, submit } from './api';
import type { Analysis } from './api';

interface Evidence { span_id: string; quote: string }
interface Source { span_id: string; paper_id: string; section: string; page: string; text: string }
interface FlowStep { id: string; kind: string; summary: string; evidence: Evidence[] }
interface FlowEdge { from_step_id: string; to_step_id: string; relation: string }
interface Knowledge { id: string; concept: string; from_step_id: string; to_step_id: string;
  necessity_score: number; why_needed: string; short_explanation: string; evidence: Evidence[] }
interface LineageEdge { from_paper_id: string; to_paper_id: string; relationship: string;
  confidence: number; status: string; evidence: Evidence[] }
interface FullResult { pipeline: 'full'; features: Record<string, string>; sources: Source[];
  paper?: { id: string; title: string; authors: string[]; arxiv_version: string; page_count: number; retrieval_source?: string; parser_version?: string };
  lineage?: { status: string; reason?: string; target_id?: string; nodes?: { id: string; title: string }[];
    edges?: LineageEdge[]; missing_reason?: string | null };
  reasoning?: { steps: FlowStep[]; edges: FlowEdge[]; limitations: string[] };
  knowledge?: { items: Knowledge[] }; detail?: { explanation: string; evidence: Evidence[] }; missing_reasons: string[];
}

const kindLabels: Record<string, string> = { problem: '問題', observation: '観察', hypothesis: '仮説',
  approach: '方法', experiment: '実験', result: '結果', interpretation: '解釈', conclusion: '結論' };
const featureLabels: Record<string, string> = { lineage: 'Research Lineage', reasoning: 'Reasoning Flow', knowledge: '前提知識' };
const featureStates: Record<string, string> = { pending: '処理待ち', completed: '生成済み',
  blocked: '検証条件が未達', failed: '生成失敗', insufficient_evidence: '根拠不足', not_implemented: '未実装' };

function EvidenceList({ items, sources }: { items: Evidence[]; sources: Source[] }) {
  return <div className="evidence-list">{items.map((e, i) => {
    const source = sources.find(s => s.span_id === e.span_id);
    return <details key={`${e.span_id}-${i}`}><summary>原文の根拠 {i + 1} · {source?.section ?? '出典'}
      {source?.page && source.page !== 'unknown' ? ` / p.${source.page}` : ' / ページ不明'}</summary>
      <blockquote>{e.quote}</blockquote><small>Source: {e.span_id}</small></details>;
  })}</div>;
}

function Lineage({ result }: { result: FullResult }) {
  const lineage = result.lineage;
  if (!lineage || lineage.status === 'blocked') return <div className="feature-empty"><h3>研究系列はまだ生成されていません</h3>
    <p>{lineage?.reason === 'stage1_quality_audit_pending'
      ? '先行研究の候補順位について品質確認が完了していないため、研究系列の生成を保留しています。'
      : lineage?.reason === 'specter2_contract_not_verified'
        ? '論文の類似度を取得する接続の検証が未完了のため、研究系列の生成を保留しています。'
        : '研究系列を支える十分な根拠を確認できませんでした。'}</p>
    <p>{result.features.reasoning === 'completed' || result.features.knowledge === 'completed' ? '利用可能な論理構造・前提知識の結果は、それぞれのタブで確認できます。' : '現時点で表示できる解析結果はありません。'}</p></div>;
  const edges = lineage.edges ?? [];
  const target = result.paper?.id ?? lineage.target_id ?? 'target';
  const allNodes = [...(lineage.nodes ?? []), { id: target, title: result.paper?.title ?? 'Target' }];
  const supportedEdges = edges.filter(e => e.status !== 'insufficient_evidence');
  function reachesTarget(id: string, seen = new Set<string>()): boolean {
    if (id === target) return true;
    if (seen.has(id)) return false;
    seen.add(id);
    return supportedEdges.some(e => e.from_paper_id === id && reachesTarget(e.to_paper_id, new Set(seen)));
  }
  const nodes = allNodes.filter(n => reachesTarget(n.id));
  const graphEdges = supportedEdges.filter(e => nodes.some(n => n.id === e.from_paper_id));
  function depth(id: string): number {
    if (id === target) return 0;
    return 1 + Math.max(0, ...graphEdges.filter(e => e.from_paper_id === id).map(e => depth(e.to_paper_id)));
  }
  const maxDepth = Math.max(...nodes.map(n => depth(n.id)), 0);
  const positions = new Map(nodes.map(n => {
    const d = depth(n.id); const group = nodes.filter(m => depth(m.id) === d);
    return [n.id, { x: (group.findIndex(m => m.id === n.id) + 1) * 800 / (group.length + 1), y: (maxDepth - d) * 120 + 45 }];
  }));
  return <section><h3>主要な貢献につながる先行研究</h3><p className="fine-print">引用数や類似性だけではなく、原文の根拠に基づく系譜候補です。不確かな関係は区別して表示します。</p>
    <div className="lineage-viewport" tabIndex={0} aria-label="研究系列の図。狭い画面では横にスクロールできます"><svg className="lineage-graph" viewBox={`0 0 800 ${(maxDepth + 1) * 120}`} role="img" aria-label="先行研究から対象論文への研究系列">
      <defs><marker id="lineage-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="#618259" /></marker></defs>
      {graphEdges.map(e => { const a = positions.get(e.from_paper_id)!; const b = positions.get(e.to_paper_id)!;
        return <line key={`${e.from_paper_id}-${e.to_paper_id}`} x1={a.x} y1={a.y + 30} x2={b.x} y2={b.y - 30} stroke="#618259" strokeWidth="2" strokeDasharray={e.status === 'uncertain' ? '5 4' : undefined} markerEnd="url(#lineage-arrow)" />;
      })}
      {nodes.map(n => { const p = positions.get(n.id)!; return <g key={n.id}><rect x={p.x - 155} y={p.y - 30} width="310" height="60" rx="8" fill={n.id === target ? '#e2edda' : '#f4f7ef'} stroke="#a0b48e" /><text x={p.x} y={p.y + 4} textAnchor="middle" fontSize="12" fill="#344e2e">{n.title.length > 42 ? n.title.slice(0, 40) + '…' : n.title}</text></g>; })}
    </svg></div>
    {edges.length === 0 && <p className="detail-note">{lineage.missing_reason ?? '直接的な先行関係を支える十分な根拠がありませんでした。'}</p>}
    {edges.map((e, i) => <article className="live-card" key={`${e.from_paper_id}-${e.to_paper_id}`}><span className={`badge ${e.status === 'supported' ? 'eligible' : 'uncertain'}`}>{e.status === 'supported' ? '根拠あり' : e.status === 'uncertain' ? '不確か' : '根拠不足・主系列から除外'}</span>
      <h4>{i + 1}. {allNodes.find(n => n.id === e.from_paper_id)?.title} → {allNodes.find(n => n.id === e.to_paper_id)?.title}</h4>
      <p>{e.relationship}</p><small>関係スコア：{e.confidence.toFixed(2)}（確率ではありません）</small><EvidenceList items={e.evidence} sources={result.sources} /></article>)}
  </section>;
}

export function LiveApp() {
  const [url, setUrl] = useState(''); const [access, setAccess] = useState('');
  const [jobId, setJobId] = useState<string | null>(() => {
    const id = new URLSearchParams(window.location.hash.slice(1)).get('analysis');
    return id && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(id) ? id : null;
  }); const [analysis, setAnalysis] = useState<Analysis | null>(null);
  const [error, setError] = useState(''); const [sending, setSending] = useState(false);
  const [tab, setTab] = useState('reasoning'); const [retry, setRetry] = useState(0);
  const [concept, setConcept] = useState<string | null>(null);
  const [details, setDetails] = useState<Record<string, { explanation: string; evidence: Evidence[] }>>({});
  const [loadingDetail, setLoadingDetail] = useState<string | null>(null);
  const [detailError, setDetailError] = useState('');
  const detailController = useRef<AbortController | null>(null);
  useEffect(() => () => detailController.current?.abort(), []);
  const active = jobId !== null && (analysis === null || !isTerminal(analysis.status));
  useEffect(() => {
    if (!jobId) return;
    const controller = new AbortController(); let timer: ReturnType<typeof setTimeout> | undefined;
    async function poll() {
      try {
        const data = await status(jobId!, controller.signal, access);
        if (controller.signal.aborted) return;
        setAnalysis(data); setError('');
        if (!isTerminal(data.status)) timer = setTimeout(poll, 1200);
      } catch (e) { if (!controller.signal.aborted) setError(e instanceof Error ? e.message : '通信に失敗しました'); }
    }
    void poll(); return () => { controller.abort(); clearTimeout(timer); };
  }, [jobId, retry, access]);
  async function start(event: FormEvent) {
    event.preventDefault(); detailController.current?.abort(); setDetails({}); setLoadingDetail(null); setSending(true); setError('');
    try {
      const id = await submit(url, undefined, access);
      window.history.replaceState(null, '', `#analysis=${encodeURIComponent(id)}`);
      setJobId(id); setAnalysis(null); setConcept(null);
    }
    catch (e) { setError(e instanceof Error ? e.message : '受付に失敗しました'); }
    finally { setSending(false); }
  }
  async function loadDetail(itemId: string) {
    if (!jobId) return;
    const controller = new AbortController(); detailController.current = controller;
    setLoadingDetail(itemId); setDetailError('');
    try {
      const id = await requestDetail(jobId, itemId, access, controller.signal);
      for (let attempt = 0; attempt < 150; attempt++) {
        if (controller.signal.aborted) return;
        const data = await status(id, controller.signal, access);
        if (isTerminal(data.status)) {
          const r = data.result as unknown as FullResult | null;
          if (!r?.detail) throw new Error('詳細説明の生成に失敗しました。利用可能な短い説明は保持しています。');
          setDetails(previous => ({...previous, [itemId]: r.detail!})); return;
        }
        await new Promise(resolve => setTimeout(resolve, 1200));
      }
      throw new Error('まだ処理中です。時間をおいて再度取得してください。');
    } catch (e) { if (!controller.signal.aborted) setDetailError(e instanceof Error ? e.message : '取得に失敗しました'); }
    finally { if (!controller.signal.aborted) setLoadingDetail(null); }
  }
  const result = analysis?.result && 'pipeline' in analysis.result && analysis.result.pipeline === 'full' ? analysis.result as unknown as FullResult : null;
  const reasoningEvidenceRejected = result?.missing_reasons?.includes('reasoning_evidence_not_supported') ?? false;
  const flow = result?.reasoning; const knowledge = result?.knowledge?.items ?? [];
  return <div className="live-app"><section className="live-hero"><span className="eyebrow">READ WITH CONTEXT AND EVIDENCE</span><h1>論文を、<br />つながりから理解する。</h1><p>研究の背景、論理の流れ、必要な前提知識。<br />公開arXiv論文から、原文の根拠と一緒に読み解きます。</p></section>
    <form className="analysis-form" onSubmit={start}><label htmlFor="live-arxiv">読む論文の arXiv URL</label><div className="analysis-input"><input id="live-arxiv" required type="url" value={url} onChange={e => setUrl(e.target.value)} placeholder="https://arxiv.org/abs/1706.03762" disabled={active || sending} /><button disabled={active || sending}>{sending ? '受付中…' : active ? '解析中…' : '論文を読み解く →'}</button></div>
      <div className="sample-inputs"><span>入力例：</span>{[['Attention', '1706.03762'], ['LoRA', '2106.09685'], ['ReAct', '2210.03629']].map(([name, id]) => <button key={id} type="button" disabled={active || sending} onClick={() => setUrl(`https://arxiv.org/abs/${id}`)}>{name}</button>)}</div>
      <details className="access-code"><summary>招待用アクセスコードを使う場合</summary><input type="password" aria-label="招待用アクセスコード" placeholder="APIキーではありません" value={access} onChange={e => setAccess(e.target.value)} /></details>
      <p className="fine-print">英語の公開arXiv論文が対象です（付録込み50ページ以内）。数分かかる場合があります。原文の根拠を確認できない内容は不確か・根拠不足と表示します。</p></form>
    {error && <div className="notice" role="alert"><div><strong>{error}</strong>{jobId && <button onClick={() => setRetry(v => v + 1)}>状態の取得を再試行</button>}</div></div>}
    {jobId && <section className="job-progress" aria-live="polite"><span className={active ? 'status-dot pulse' : 'status-dot'} /><strong>{analysis ? labels[analysis.status] : '受付済み・状態を確認中'}</strong><span>{active ? analysis?.current_stage ?? '' : ''}</span><small>Job: {jobId}</small></section>}
    {reasoningEvidenceRejected && <div className="notice" role="status"><div><strong>主張と根拠の対応を確認できませんでした</strong><p>選択した原文の根拠では支えきれない、または範囲が曖昧な主張が見つかったため、論理構造とそれに基づく前提知識の表示を止めました。</p></div></div>}
    {analysis?.status === 'failed' && !reasoningEvidenceRejected && <div className="notice"><div><strong>解析を完了できませんでした</strong><p>API設定・取得元の利用制限・対象論文の対応範囲を確認してください。エラーコード：{analysis.error_code ?? 'unknown'}</p></div></div>}
    {result && <section className="live-results"><div className="workspace-head"><div><span className="eyebrow">YOUR PAPER</span><h2>{result.paper?.title ?? '解析結果'}</h2><p>{result.paper?.authors?.slice(0, 4).join(', ')} · {result.paper?.arxiv_version} · {result.paper?.page_count} pages {result.paper?.retrieval_source === 'parsed_cache' ? ' · 取得済み本文を再利用（版の更新確認は未実施）' : ''}</p></div></div>
      {analysis?.status === 'partial_completed' && <div className="notice"><div><strong>一部の機能を完了しました</strong><p>利用できる結果を保持しています。未検証・失敗した機能は完了扱いにしていません。</p></div></div>}
      <nav className="feature-tabs" aria-label="解析機能">{Object.entries(featureLabels).map(([key, label]) => <button key={key} onClick={() => setTab(key)} className={tab === key ? 'active' : ''} aria-pressed={tab === key}><strong>{label}</strong><small>{featureStates[result.features[key]] ?? result.features[key]}</small></button>)}</nav>
      <div className="feature-content">{tab === 'lineage' && <Lineage result={result} />}
        {tab === 'reasoning' && (flow ? <section><h3>問題から結論までの論理</h3>{flow.steps.map((step, i) => <article className="flow-step" key={step.id}><span className="flow-number">{String(i + 1).padStart(2, '0')}</span><div><span className="badge eligible">{kindLabels[step.kind]}</span><h4>{step.summary}</h4><EvidenceList items={step.evidence} sources={result.sources} />
          {flow.edges.filter(e => e.from_step_id === step.id).map(e => <div className="flow-transition" key={`${e.from_step_id}-${e.to_step_id}`}><span>↓ {e.relation} → {flow.steps.find(s => s.id === e.to_step_id)?.summary.slice(0, 45)}</span>
            {knowledge.filter(k => k.from_step_id === e.from_step_id && k.to_step_id === e.to_step_id).map(k => <button key={k.id} onClick={() => { setConcept(k.id); setTab('knowledge'); }}>前提知識を学ぶ：{k.concept} ＋</button>)}</div>)}</div></article>)}{flow.limitations.length > 0 && <div className="detail-note"><strong>読み取り上の限界</strong><ul>{flow.limitations.map((v, i) => <li key={i}>{v}</li>)}</ul></div>}</section> : <div className="feature-empty">論理構造の結果を生成できませんでした。</div>)}
        {tab === 'knowledge' && <section><h3>論理のつながりに必要な前提知識</h3><p className="fine-print">用語集ではなく、あるステップから次のステップを理解するために不可欠と判定した知識だけを表示します。</p>{knowledge.length === 0 ? <p>必要な知識を根拠付きで確認できませんでした。</p> : knowledge.filter(k => !concept || k.id === concept).map(k => <article className="live-card" key={k.id}><span className="eyebrow">LEARN PREREQUISITE</span><h4>{k.concept}</h4><p className="why-needed">なぜ必要？ {k.why_needed}</p><p>{k.short_explanation}</p><EvidenceList items={k.evidence} sources={result.sources} />
        {details[k.id] ? <div className="knowledge-detail"><h4>もう少し詳しく</h4><p>{details[k.id].explanation}</p><EvidenceList items={details[k.id].evidence} sources={result.sources} /></div> : <button className="text-button" disabled={loadingDetail !== null} onClick={() => void loadDetail(k.id)}>{loadingDetail === k.id ? '詳細を生成中…' : 'もう少し詳しく学ぶ ＋'}</button>}</article>)}{detailError && <p role="alert">{detailError}</p>}{concept && <button className="text-button" onClick={() => setConcept(null)}>すべての前提知識を見る</button>}</section>}
      </div><div className="analysis-cost"><small>{analysis?.cost?.simulated ? '模擬費用' : 'API推定費用'}：${analysis?.cost?.estimated_usd?.toFixed(4) ?? '—'} {analysis?.cost?.has_unknown_cost ? '（未確定費用あり）' : ''}</small><small>実行時間：{analysis?.timing?.total_seconds?.toFixed(1) ?? '—'} 秒</small></div>
    </section>}
  </div>;
}
