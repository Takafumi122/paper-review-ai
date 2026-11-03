export interface Candidate {
  id: string;
  title: string;
  authors: string[];
  year: number | null;
  external_ids: { arxiv: string | null; doi: string | null; semantic_scholar: string | null };
  eligibility: string;
  exclusion_reason: string | null;
  temporal_status: string;
  temporal_uncertainty: string | null;
  date_provenance: { kind: string; value: string; precision: string; source_id: string } | null;
  resolution_status: string;
  resolution_method: string | null;
  reference_orders: number[];
  is_influential_citation: boolean | null;
  diagnostic_probe: string | null;
  rank: number | null;
  score: {
    initial_score: number | null;
    semantic_similarity: number | null;
    citation_relation: number | null;
    citation_importance: number | null;
    coverage: number;
    status: string;
    missing_reasons: Record<string, string>;
  } | null;
}

export interface DemoPaper {
  arxiv_id: string;
  short_title: string;
  title: string;
  year: string;
  published_at: string;
  statistics: {
    parsed_reference_count: number; resolved_reference_count: number;
    unresolved_reference_count: number; distinct_candidate_count: number; eligible: number;
  };
  embedding_available: number;
  embedding_missing_reason: string;
  candidates: Candidate[];
}

export function filterCandidates(candidates: Candidate[], query: string, filter: string): Candidate[] {
  const text = query.trim().toLowerCase();
  return candidates.filter(c => {
    const matches = !text || `${c.title} ${c.authors.join(' ')} ${c.year ?? ''}`.toLowerCase().includes(text);
    const category = filter === 'all' ||
      (filter === 'top10' ? c.rank !== null && c.rank <= 10 :
        filter === 'probes' ? c.diagnostic_probe !== null : c.eligibility === filter);
    return matches && category;
  });
}

export function formatMetric(value: number | null | undefined): string {
  return value === null || value === undefined ? '未取得' : value.toFixed(3);
}

export const eligibilityLabels: Record<string, string> = {
  eligible: '対象候補', uncertain: '順序など未確定', excluded: '対象外',
};
