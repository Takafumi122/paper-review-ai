export const statuses = [
  'queued', 'parsing', 'retrieving_candidates', 'ranking_candidates',
  'building_lineage', 'building_reasoning', 'building_knowledge',
  'completed', 'failed', 'partial_completed',
] as const;
export type Status = typeof statuses[number];
export interface Analysis {
  job_id: string;
  status: Status;
  result: { fake: true; message: string } | null;
  error_code: string | null;
}
export const labels: Record<Status, string> = {
  queued: '待機中', parsing: '処理中', retrieving_candidates: '候補取得中',
  ranking_candidates: '候補評価中', building_lineage: '研究系列の構築中',
  building_reasoning: '論理構造の構築中', building_knowledge: '前提知識の生成中',
  completed: '完了', failed: '処理に失敗しました', partial_completed: '一部完了',
};
export function isTerminal(status: Status): boolean {
  return ['completed', 'failed', 'partial_completed'].includes(status);
}
async function json(response: Response): Promise<unknown> {
  if (!response.ok) {
    throw new Error(response.status === 422 ? 'arXiv URLを確認してください。'
      : '処理状況を取得できませんでした。再試行してください。');
  }
  return response.json();
}
function object(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}
export async function submit(url: string, signal?: AbortSignal): Promise<string> {
  const data = await json(await fetch('/analyses', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ arxiv_url: url.trim() }), signal,
  }));
  if (!object(data) || typeof data.job_id !== 'string') throw new Error('応答の形式が不正です。');
  return data.job_id;
}
export async function status(jobId: string, signal?: AbortSignal): Promise<Analysis> {
  const data = await json(await fetch(`/analyses/${encodeURIComponent(jobId)}`, { signal }));
  if (!object(data) || data.job_id !== jobId ||
      !statuses.includes(data.status as Status) ||
      !(data.error_code === null || typeof data.error_code === 'string') ||
      !(data.result === null || (object(data.result) && data.result.fake === true &&
        typeof data.result.message === 'string'))) {
    throw new Error('応答の形式が不正です。');
  }
  return data as unknown as Analysis;
}
