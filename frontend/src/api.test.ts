import { test } from 'node:test';
import assert from 'node:assert/strict';
import { submit, status, isTerminal } from './api.ts';

test('POST request and polling response contract', async () => {
  const original = globalThis.fetch;
  globalThis.fetch = async (url, init) => {
    if (init?.method === 'POST') {
      assert.equal(url, '/analyses');
      assert.deepEqual(JSON.parse(init.body as string), { arxiv_url: 'https://arxiv.org/abs/1706.03762' });
      return Response.json({ job_id: 'test' }, { status: 202 });
    }
    assert.equal(url, '/analyses/test');
    return Response.json({ job_id: 'test', status: 'completed', error_code: null,
      result: { fake: true, message: '完了' } });
  };
  try {
    assert.equal(await submit(' https://arxiv.org/abs/1706.03762 '), 'test');
    assert.equal((await status('test')).status, 'completed');
    assert.equal(isTerminal('parsing'), false);
    for (const state of ['completed', 'failed', 'partial_completed'] as const) assert.ok(isTerminal(state));
  } finally { globalThis.fetch = original; }
});

test('reject HTTP errors and malformed responses', async () => {
  const original = globalThis.fetch;
  try {
    globalThis.fetch = async () => Response.json({}, { status: 422 });
    await assert.rejects(submit('bad'), /arXiv URL/);
    globalThis.fetch = async () => Response.json({ job_id: 'test', status: 'invented' });
    await assert.rejects(status('test'), /形式/);
  } finally { globalThis.fetch = original; }
});
