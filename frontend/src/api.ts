export const statuses = [
  'queued', 'parsing', 'retrieving_candidates', 'ranking_candidates',
  'building_lineage', 'building_reasoning', 'building_knowledge',
  'completed', 'failed', 'partial_completed',
] as const;
export type Status = typeof statuses[number];
export interface Analysis {
  job_id: string;
  status: Status;
  result: { fake: true; message: string } | ({ pipeline: 'full'; features: Record<string, string>; sources: unknown[] } & Record<string, unknown>) | null;
  current_stage?: string;
  cost?: { simulated: boolean; estimated_usd: number; has_unknown_cost: boolean };
  timing?: { total_seconds: number };
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
    throw new Error(response.status === 401 ? '招待用アクセスコードを確認してください。' : response.status === 429 ? '受付上限に達しました。時間をおいてお試しください。' : response.status === 422 ? 'arXiv URLを確認してください。'
      : '処理状況を取得できませんでした。再試行してください。');
  }
  return response.json();
}
function object(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}
export async function submit(url: string, signal?: AbortSignal, accessToken?: string): Promise<string> {
  const data = await json(await fetch('/analyses', {
    method: 'POST', headers: { 'Content-Type': 'application/json', ...(accessToken ? {Authorization: `Bearer ${accessToken}`} : {}) },
    body: JSON.stringify({ arxiv_url: url.trim() }), signal,
  }));
  if (!object(data) || typeof data.job_id !== 'string') throw new Error('応答の形式が不正です。');
  return data.job_id;
}
export async function status(jobId: string, signal?: AbortSignal, accessToken?: string): Promise<Analysis> {
  const data = await json(await fetch(`/analyses/${encodeURIComponent(jobId)}`, { signal, headers: accessToken ? {Authorization: `Bearer ${accessToken}`} : undefined }));
  if (!object(data) || data.job_id !== jobId ||
      !statuses.includes(data.status as Status) ||
      !(data.error_code === null || typeof data.error_code === 'string') ||
      !(data.result === null || (object(data.result) && ((data.result.fake === true && typeof data.result.message === 'string') || (data.result.pipeline === 'full' && object(data.result.features) && Array.isArray(data.result.sources)))))) {
    throw new Error('応答の形式が不正です。');
  }
  return data as unknown as Analysis;
}

export async function requestDetail(jobId: string, itemId: string, accessToken?: string, signal?: AbortSignal): Promise<string> {
  const data = await json(await fetch(`/analyses/${encodeURIComponent(jobId)}/knowledge/${encodeURIComponent(itemId)}/detail`, {
    method: 'POST', signal, headers: accessToken ? {Authorization: `Bearer ${accessToken}`} : undefined,
  }));
  if (!object(data) || typeof data.job_id !== 'string') throw new Error('応答の形式が不正です。');
  return data.job_id;
}
