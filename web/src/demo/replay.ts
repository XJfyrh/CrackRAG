// Static replay provider for the public demo build.
//
// The demo has no backend and makes no provider request. Every call the product
// UI would send to `/api/v1/...` is answered from a frozen session captured
// from a real run, in the exact order the product answered it. Nothing here
// may synthesize an answer, a cost, or a citation that was not captured. The
// cited PDF page is fetched as a same-origin static asset.

import {apiUrl, assetUrl, healthUrl} from '../urls';
import {APIError} from '../requests';
import type {ReplayDocument, ReplayQuery, ReplaySession} from './types';

const JSON_HEADERS = {'Content-Type': 'application/json'};
const SSE_HEADERS = {'Content-Type': 'text/event-stream', 'Cache-Control': 'no-store'};

type Answer =
  | {kind: 'json'; status: number; body: unknown}
  | {kind: 'text'; status: number; body: string; contentType: string}
  | {kind: 'sse'; status: number; frames: {event: string; data: unknown}[]}
  | {kind: 'blob'; status: number; url: string};

export class ReplayScope {
  private controller = new AbortController();
  private submitted = 0;
  private current: ReplayQuery | null = null;
  private polls = 0;
  private uploads = 0;
  private readonly documents: ReplayDocument[];

  constructor(private session: ReplaySession) {
    this.documents = session.documents.map(document => ({...document}));
  }

  close() { this.controller.abort(); }

  // Product code reads `scope.active`; keep the exact RequestScope surface.
  get active() { return !this.controller.signal.aborted; }

  private signal(options: RequestInit) {
    const signal = options.signal
      ? AbortSignal.any([this.controller.signal, options.signal])
      : this.controller.signal;
    signal.throwIfAborted();
    return signal;
  }

  async request(path: string, options: RequestInit = {}) {
    const signal = this.signal(options);
    const method = (options.method || 'GET').toUpperCase();
    const answer = this.route(path, method, options, signal);
    const response = await this.respond(answer, signal);
    if (!response.ok) {
      const body = await response.clone().json().catch(() => ({})) as {error?: {reason_code?: string}};
      throw new APIError(response.status, body.error?.reason_code || `HTTP_${response.status}`);
    }
    return response;
  }

  async json<T>(path: string, options: RequestInit = {}): Promise<T> {
    const response = await this.request(path, options);
    return await response.json() as T;
  }

  private route(path: string, method: string, options: RequestInit, signal: AbortSignal): Answer {
    const url = new URL(path, 'http://replay.invalid');
    // Every product path is built with apiUrl()/healthUrl(), so it already
    // carries the deployment base ('/CrackRAG/demo/api/v1/...'). Strip it once
    // and match on the product-relative route.
    const prefix = assetUrl('api/v1');
    const route = url.pathname.startsWith(prefix) ? url.pathname.slice(prefix.length) : url.pathname;

    if (url.pathname === healthUrl() || route === '/healthz') {
      return {kind: 'json', status: 200, body: this.currentHealth()};
    }

    if (route === '/documents' && method === 'GET') {
      return {kind: 'json', status: 200, body: {documents: this.documents}};
    }

    if (route === '/documents' && method === 'POST') {
      // The demo only accepts its own preset uploads; the form is hidden in the UI.
      this.uploads += 1;
      return {kind: 'json', status: 202, body: {document_id: this.documents[Math.min(this.uploads - 1, this.documents.length - 1)].id}};
    }

    const source = /^\/documents\/([^/]+)\/versions\/([^/]+)\/source$/.exec(route);
    if (source) {
      const asset = this.session.sources[`${source[1]}/${source[2]}`];
      if (!asset) return {kind: 'json', status: 404, body: {error: {reason_code: 'NOT_FOUND'}}};
      return {kind: 'blob', status: 200, url: asset};
    }

    const region = /^\/regions\/([^/]+)$/.exec(route);
    if (region) {
      const body = this.findRegion(region[1]);
      if (!body) return {kind: 'json', status: 404, body: {error: {reason_code: 'NOT_FOUND'}}};
      return {kind: 'json', status: 200, body};
    }

    if (route === '/queries' && method === 'GET') {
      const all = this.session.history.queries.map(raw => {
        const item = raw as {id?: string; question?: string; provider?: string; state?: string; answer_status?: string; model_calls?: number; known_estimated_cny?: string};
        const captured = this.session.queries.find(query => this.queryId(query) === item.id);
        const final = captured?.run_steps.at(-1) as {document_version_ids?: string[]} | undefined;
        return {...item, document_version_ids: final?.document_version_ids || []};
      });
      const term = (url.searchParams.get('q') || '').trim().toLocaleLowerCase();
      const filtered = term ? all.filter(item => item.question?.toLocaleLowerCase().includes(term)) : all;
      const summary = {
        total_queries: all.length,
        real_queries: all.filter(item => item.provider === 'deepseek').length,
        zero_model_supported_answers: all.filter(item => item.provider === 'deepseek' && item.state === 'COMPLETED' && item.answer_status === 'SUPPORTED' && item.model_calls === 0).length,
        known_estimated_cny: all.reduce((sum,item) => sum + Number(item.provider === 'deepseek' ? item.known_estimated_cny || 0 : 0), 0).toFixed(8),
        unknown_calls: 0,
      };
      return {kind: 'json', status: 200, body: {queries: filtered, next_cursor: '', summary}};
    }

    if (route === '/queries' && method === 'POST') {
      return this.submit(options, signal);
    }

    const events = /^\/queries\/([^/]+)\/events$/.exec(route);
    if (events) {
      const run = this.currentRun() as {state?: string} | null;
      return {kind: 'sse', status: 200, frames: [{event: 'STATUS', data: {state: run?.state || 'RUNNING'}}]};
    }

    const cancel = /^\/queries\/([^/]+)\/cancel$/.exec(route);
    if (cancel && method === 'POST') {
      return {kind: 'text', status: 200, body: '', contentType: 'application/json'};
    }

    const resume = /^\/queries\/([^/]+)\/jobs\/([^/]+)\/resume$/.exec(route);
    if (resume) {
      return {kind: 'json', status: 200, body: {}};
    }

    const run = /^\/queries\/([^/]+)$/.exec(route);
    if (run) {
      const query = this.session.queries.find(entry => this.queryId(entry) === run[1]);
      if (!query) return {kind: 'json', status: 404, body: {error: {reason_code: 'NOT_FOUND'}}};
      return {kind: 'json', status: 200, body: this.nextRunStep(query)};
    }

    return {kind: 'json', status: 404, body: {error: {reason_code: 'NOT_FOUND'}}};
  }

  private dialogOf(query: ReplayQuery | undefined) {
    return query?.dialog === 'inconclusive' ? 'mock' : 'deepseek';
  }

  /**
   * Regions are shared by every query that cited the same passage, and a quoting
   * query can reference a region captured under another step. Search the whole
   * frozen session so a citation never resolves to a dead link.
   */
  private findRegion(regionId: string) {
    const local = this.current?.regions[regionId];
    if (local) return local;
    for (const query of this.session.queries) {
      if (query.regions[regionId]) return query.regions[regionId];
    }
    return undefined;
  }

  private currentHealth() {
    // The capture switched provider between the mock segment and the paid
    // segment; the badge must follow the same sequence the product showed.
    const query = this.current ?? this.session.queries[Math.min(this.submitted, this.session.queries.length - 1)];
    const health = this.session.healthz[this.dialogOf(query)] as Record<string, unknown>;
    return health;
  }

  private submit(options: RequestInit, signal: AbortSignal): Answer {
    signal.throwIfAborted();
    const query = this.session.queries[this.submitted];
    if (!query) {
      return {kind: 'json', status: 409, body: {error: {reason_code: 'DEMO_SEQUENCE_COMPLETE'}}};
    }
    let request: {question?: string; document_ids?: string[]; build_facts?: boolean; execution_policy?: string};
    try { request = JSON.parse(String(options.body || '{}')); }
    catch { return {kind: 'json', status: 400, body: {error: {reason_code: 'DEMO_STEP_MISMATCH'}}}; }
    const sameDocuments = Array.isArray(request.document_ids) &&
      JSON.stringify([...request.document_ids].sort()) === JSON.stringify([...query.document_ids].sort());
    if (request.question !== query.question || !sameDocuments ||
        !!request.build_facts !== !!query.build_facts ||
        (request.execution_policy || 'HOT_ONLY') !== (query.execution_policy || 'HOT_ONLY')) {
      return {kind: 'json', status: 409, body: {error: {reason_code: 'DEMO_STEP_MISMATCH'}}};
    }
    this.submitted += 1;
    this.current = query;
    this.polls = 0;
    return {kind: 'json', status: 202, body: {query_id: this.queryId(query)}};
  }

  private queryId(query: ReplayQuery) {
    // A capture without run bodies is a broken capture: fail loudly instead of
    // rendering an answer card with an undefined id.
    const last = query.run_steps[query.run_steps.length - 1] as {id?: unknown};
    if (typeof last?.id !== 'string' || !last.id) {
      throw new Error(`replay capture for query ${query.order} has no run id`);
    }
    return last.id;
  }

  private currentRun() {
    if (!this.current) return null;
    const steps = this.current.run_steps;
    return steps[Math.min(this.polls, steps.length - 1)];
  }

  private nextRunStep(query: ReplayQuery) {
    if (this.current === query) {
      const body = this.currentRun();
      this.polls += 1;
      return body;
    }
    return query.run_steps[query.run_steps.length - 1];
  }

  private async respond(answer: Answer, signal: AbortSignal): Promise<Response> {
    switch (answer.kind) {
      case 'json':
        signal.throwIfAborted();
        return new Response(JSON.stringify(answer.body), {status: answer.status, headers: JSON_HEADERS});
      case 'text':
        return new Response(answer.body, {status: answer.status, headers: {'Content-Type': answer.contentType}});
      case 'blob': {
        // Session paths are relative to the deployed base ('samples/x.pdf').
        const response = await fetch(assetUrl(answer.url), {signal});
        signal.throwIfAborted();
        return response;
      }
      case 'sse': {
        const payload = answer.frames
          .map((frame, index) => `id: ${index + 1}\nevent: ${frame.event}\ndata: ${JSON.stringify(frame.data)}\n\n`)
          .join('');
        return new Response(payload, {status: answer.status, headers: SSE_HEADERS});
      }
    }
  }
}
