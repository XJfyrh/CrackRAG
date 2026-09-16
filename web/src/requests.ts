export class APIError extends Error {
  constructor(readonly status: number, readonly reason: string) {
    super(reason);
  }
}

// A tenant change closes this scope before React unmounts its UI.
// Current responses and follow-up requests must still belong to this scope.
export class RequestScope {
  private controller = new AbortController();
  constructor(private token: string) {}
  get active() { return !this.controller.signal.aborted; }
  close() { this.controller.abort(); }

  async request(path: string, options: RequestInit = {}) {
    const signal = options.signal
      ? AbortSignal.any([this.controller.signal, options.signal])
      : this.controller.signal;
    signal.throwIfAborted();
    const response = await fetch(path, {
      ...options, signal,
      headers: {Authorization: `Bearer ${this.token}`, ...options.headers},
    });
    signal.throwIfAborted();
    if (!response.ok) {
      let body;
      try { body = await response.json(); } catch { body = {}; }
      signal.throwIfAborted();
      throw new APIError(response.status, body?.error?.reason_code || `HTTP_${response.status}`);
    }
    return response;
  }

  async json<T>(path: string, options: RequestInit = {}): Promise<T> {
    const response = await this.request(path, options);
    const value = await response.json();
    this.controller.signal.throwIfAborted();
    options.signal?.throwIfAborted();
    return value;
  }
}

export function delay(ms: number, signal: AbortSignal) {
  return new Promise<void>((resolve, reject) => {
    signal.throwIfAborted();
    const abort = () => { clearTimeout(timer); reject(signal.reason); };
    const timer = setTimeout(() => { signal.removeEventListener('abort', abort); resolve(); }, ms);
    signal.addEventListener('abort', abort, {once: true});
  });
}
