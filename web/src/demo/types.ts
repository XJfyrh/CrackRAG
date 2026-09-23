// Frozen schemas for the static replay build. The shapes mirror what the
// product API returned at capture time; the replay provider must never
// invent fields the product does not send.

export type ReplayDocument = {
  id: string;
  title: string;
  current_version_id: string;
  state: string;
  indexed_pages: number[];
  requested_pages: number[];
  build_usage: unknown;
};

export type ReplayQuery = {
  /** 1-based position in the captured script. Submission N replays this entry. */
  order: number;
  /** Which beat this query plays in the demo: first answer, build, reuse, abstention. */
  role?: 'abstention' | 'first-answer' | 'build' | 'repeat' | 'paraphrase';
  dialog: 'supported' | 'inconclusive';
  question: string;
  document_ids: string[];
  /** Exact user choices from the captured request; replay rejects mismatches. */
  build_facts?: boolean;
  execution_policy?: 'HOT_ONLY' | 'COLD_ALLOWED';
  /** Successive GET /api/v1/queries/{id} bodies. The last one is terminal. */
  run_steps: unknown[];
  /** Region bodies keyed by region_id, for GET /api/v1/regions/{id}. */
  regions: Record<string, unknown>;
};

export type ReplaySession = {
  schema: 'crackrag-demo-replay-v1';
  captured_at: string;
  product: {release: string; commit: string; provider: string; embedding: string};
  notice: {mode: 'replay'; banner: string; evidence_url: string};
  tenant_label: string;
  healthz: {mock: unknown; deepseek: unknown};
  documents: ReplayDocument[];
  queries: ReplayQuery[];
  history: {queries: unknown[]; next_cursor: string};
  sources: Record<string, string>;
};
