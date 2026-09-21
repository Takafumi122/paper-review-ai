import { StrictMode, useEffect, useState } from 'react';
import type { FormEvent } from 'react';
import { createRoot } from 'react-dom/client';
import { isTerminal, labels, status, submit } from './api';
import type { Analysis } from './api';
import './style.css';

function App() {
  const [url, setUrl] = useState('');
  const [jobId, setJobId] = useState<string | null>(null);
  const [analysis, setAnalysis] = useState<Analysis | null>(null);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState('');
  const [retry, setRetry] = useState(0);
  const active = jobId !== null && (analysis === null || !isTerminal(analysis.status));

  useEffect(() => {
    if (!jobId) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    async function poll() {
      try {
        const data = await status(jobId!, controller.signal);
        if (controller.signal.aborted) return;
        setAnalysis(data);
        if (!isTerminal(data.status)) timer = setTimeout(poll, 1000);
      } catch (cause) {
        if (!controller.signal.aborted) {
          setError(cause instanceof Error ? cause.message : '通信に失敗しました。');
        }
      }
    }
    void poll();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [jobId, retry]);

  async function start(event: FormEvent) {
    event.preventDefault();
    setSending(true);
    setError('');
    try {
      const id = await submit(url);
      setAnalysis(null);
      setJobId(id);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '通信に失敗しました。');
    } finally {
      setSending(false);
    }
  }

  return <main>
    <h1>論文理解支援</h1>
    <p>Phase 0 動作確認用です。論文の取得・解析は行いません。</p>
    <form onSubmit={start}>
      <label htmlFor="arxiv">arXiv URL</label>
      <input id="arxiv" type="url" required value={url}
        placeholder="https://arxiv.org/abs/1706.03762"
        onChange={event => setUrl(event.target.value)} disabled={sending || active} />
      <button disabled={sending || active}>{sending ? '送信中…' : '動作確認を開始'}</button>
    </form>
    {error && <div role="alert"><p>{error}</p>{active && <button onClick={() => {
      setError(''); setRetry(value => value + 1);
    }}>状態の取得を再試行</button>}</div>}
    {jobId && <section aria-live="polite">
      <h2>{analysis ? labels[analysis.status] : '状態を確認中…'}</h2>
      {analysis?.result && <p>{analysis.result.message}</p>}
      {analysis?.status === 'failed' && <p>完了できませんでした。設定とWorkerの実行状況を確認してください。</p>}
    </section>}
  </main>;
}

createRoot(document.getElementById('root')!).render(<StrictMode><App /></StrictMode>);
