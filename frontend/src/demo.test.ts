import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { filterCandidates, formatMetric } from './demo.ts';
import type { DemoPaper } from './demo.ts';

const bundle = JSON.parse(readFileSync(new URL('./data/demo.json', import.meta.url), 'utf8'));
const papers = bundle.papers as DemoPaper[];

test('recorded demo preserves all candidates and diagnostic probes without live success claims', () => {
  assert.equal(bundle.live_analysis, false);
  assert.equal(bundle.stage1_quality_evaluable, false);
  assert.deepEqual(papers.map(p => p.candidates.length), [40, 61, 35]);
  assert.deepEqual(papers.map(p => filterCandidates(p.candidates, '', 'probes').length), [5, 4, 5]);
  for (const paper of papers) {
    assert.equal(paper.embedding_available, 0);
    assert.equal(filterCandidates(paper.candidates, '', 'top10').length, 10);
    assert.ok(paper.candidates.filter(c => c.score).every(c => c.score!.semantic_similarity === null));
  }
});

test('search and filters compose without changing stored ranking', () => {
  const lora = papers[1];
  const before = JSON.stringify(lora);
  const prefix = filterCandidates(lora.candidates, '  PREFIX ', 'probes');
  assert.equal(prefix.length, 1);
  assert.equal(prefix[0].eligibility, 'uncertain');
  assert.equal(prefix[0].rank, null);
  assert.equal(prefix[0].date_provenance?.precision, 'year');
  assert.equal(filterCandidates(lora.candidates, 'nonexistentxyz', 'all').length, 0);
  assert.equal(JSON.stringify(lora), before);
});

test('observed zero is displayed separately from missing', () => {
  assert.equal(formatMetric(0), '0.000');
  assert.equal(formatMetric(null), '未取得');
  assert.equal(formatMetric(undefined), '未取得');
});
