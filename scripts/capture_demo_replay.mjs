// Captures a real CrackRAG run into a frozen replay session for the static
// demo site. It drives the product UI exactly as a person would, records the
// responses the product returned, and writes two artifacts:
//
//   --out-raw     full capture, for the private evidence directory. Not published.
//   --out-public  sanitized session bundled into the demo build.
//
// It never generates an answer, a citation or a cost: everything is copied from
// the running product. The paid phase is explicit and must be enabled by the
// operator before phase 2; the script refuses to submit to a mock target when a
// paid phase was requested.
//
// Usage:
//   node scripts/capture_demo_replay.mjs \
//     --base http://127.0.0.1:18086 \
//     --token-file .release/secrets/access_token \
//     --phase mock|live|publish --out-raw <private checkpoint path> \
//     --annual-report-pdf <path to the full annual report PDF> \
//     --annual-page 86 --annual-year 2024

import fs from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {createRequire} from 'node:module';
import {execFile} from 'node:child_process';
import {promisify} from 'node:util';

const runTool = promisify(execFile);

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const require = createRequire(path.join(root, 'web/package.json'));
const {chromium} = require('@playwright/test');

function arg(name, fallback = '') {
  const index = process.argv.indexOf(`--${name}`);
  return index >= 0 && process.argv[index + 1] ? process.argv[index + 1] : fallback;
}
const flag = (name) => process.argv.includes(`--${name}`);

const base = arg('base', 'http://127.0.0.1:18086');
const tokenFile = arg('token-file', path.join(root, '.release/secrets/access_token'));
const rawOut = arg('out-raw', path.join(root, 'evidence', `demo-replay-${new Date().toISOString().replace(/[:.]/g, '-')}.json`));
const publicOut = arg('out-public', path.join(root, 'web/src/demo/session.json'));
const assetDir = arg('asset-dir', path.join(root, 'web/public/samples'));
const annualPdf = arg('annual-report-pdf');
const annualPage = Number(arg('annual-page', '86'));
const annualYear = arg('annual-year', '2024');
// The two phases need different server modes, which is an operator action
// between them. Separating the phases into resumable runs means the paid phase
// starts immediately after `live enable`, while the price snapshot is fresh.
const phaseArg = arg('phase');
if (!['mock', 'live', 'publish'].includes(phaseArg)) {
  throw new Error('Specify one explicit --phase mock, --phase live, or --phase publish; paid capture never follows mock automatically');
}
const phasesToRun = phaseArg === 'publish' ? [] : [phaseArg];

const token = phasesToRun.length ? (await fs.readFile(tokenFile, 'utf8')).trim() : '';
const capture = {schema: 'crackrag-demo-capture-v1', started_at: new Date().toISOString(), base, phases: [], documents: [], queries: [], submissions: [], healthz: {}, uploaded: []};
if (phaseArg === 'publish') {
  Object.assign(capture, JSON.parse(await fs.readFile(rawOut, 'utf8')));
}
if (phasesToRun.includes('live') && !phasesToRun.includes('mock')) {
  const previous = JSON.parse(await fs.readFile(rawOut, 'utf8').catch(() => 'null'));
  if (!previous) throw new Error(`--phase live needs the mock phase first: run --phase mock --out-raw ${rawOut}`);
  if (previous.phases?.some((entry) => entry.name === 'live-answer-and-reuse')) {
    throw new Error(`${rawOut} already contains a live phase; refusing to capture it twice`);
  }
  if (previous.queries?.some((entry) => entry.label?.startsWith('live-'))) {
    throw new Error(`${rawOut} contains a partial paid phase; inspect it before any new paid request`);
  }
  Object.assign(capture, previous, {phases: previous.phases || []});
  console.log(`resuming: ${capture.queries.length} queries and ${capture.documents.length} documents already captured`);
}
const browser = phasesToRun.length ? await chromium.launch({headless: !flag('headed')}) : null;
const pageErrors = [];
let context;
let page;

async function api(route) {
  const response = await fetch(base + route, {headers: {Authorization: `Bearer ${token}`}});
  if (!response.ok) throw new Error(`HTTP ${response.status} on ${route}`);
  return response.json();
}

/** Records what the product answered, not what we hoped it would answer. */
async function snapshotRun(queryId) {
  const run = await api(`/api/v1/queries/${queryId}`);
  const regions = {};
  for (const source of run.answer?.evidence_summary?.sources || []) {
    regions[source.region_id] = await api(`/api/v1/regions/${source.region_id}`);
  }
  return {run, regions};
}

async function recordHistory() {
  return await api('/api/v1/queries?limit=20');
}

async function recordSources() {
  const titles = {};
  for (const document of (await api('/api/v1/documents')).documents) {
    titles[`${document.id}/${document.current_version_id}`] = document.title;
  }
  return titles;
}

/** A fresh context per phase: a stale session token must never skip the login step. */
async function openWorkspace() {
  context = await browser.newContext({viewport: {width: 1440, height: 1000}, locale: 'zh-CN'});
  page = await context.newPage();
  page.on('pageerror', (error) => pageErrors.push(error.message));
  await page.goto(base);
  const tokenBox = page.getByRole('textbox', {name: '本机访问令牌'});
  if (await tokenBox.count()) {
    await tokenBox.fill(token);
    await page.getByRole('button', {name: '进入工作区', exact: true}).click();
  }
  await page.getByRole('textbox', {name: '问题', exact: true}).waitFor({state: 'visible', timeout: 30000});
}

async function waitForVerdict(expected, timeoutMs = 600000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const status = await page.locator('.answer-verdict').getAttribute('data-status').catch(() => null);
    if (status === expected) return;
    await page.waitForTimeout(1000);
  }
  throw new Error(`verdict did not reach ${expected}`);
}

async function waitForTerminal(queryId, timeoutMs = 600000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const run = await api(`/api/v1/queries/${queryId}`);
    const pending = run.diagnostics?.m3?.pending_jobs || 0;
    const reserved = (run.calls || []).some((call) => call.state === 'RESERVED');
    if (['COMPLETED', 'FAILED', 'CANCELLED', 'TIMED_OUT', 'INTERRUPTED'].includes(run.state) && !pending && !reserved) return run;
    await page.waitForTimeout(1500);
  }
  throw new Error(`run ${queryId} did not reach a settled terminal state`);
}

async function uploadDocument(filePath, pages, year, label) {
  await page.getByLabel('选择 PDF').setInputFiles(filePath);
  if ((pages || year) && !(await page.locator('.upload-options').evaluate(element => element.open)))
    await page.locator('.upload-options summary').click();
  if (pages) await page.getByRole('textbox', {name: '页码范围'}).fill(String(pages));
  if (year) await page.getByRole('textbox', {name: '报告年份'}).fill(String(year));
  const received = page.waitForResponse((r) => new URL(r.url()).pathname === '/api/v1/documents' && r.request().method() === 'POST');
  await page.getByRole('button', {name: '上传文档', exact: true}).click();
  const response = await received;
  if (response.status() !== 202) throw new Error(`upload rejected with ${response.status()}`);
  const created = await response.json();
  const deadline = Date.now() + 600000;
  while (Date.now() < deadline) {
    const document = (await api('/api/v1/documents')).documents.find((d) => d.id === created.document_id);
    if (document?.state === 'READY') {
      console.log(`uploaded ${label}: ${document.title} (pages ${document.indexed_pages.join(',')})`);
      if (!capture.uploaded.includes(document.id)) capture.uploaded.push(document.id);
      return document;
    }
    if (document?.state === 'FAILED') throw new Error(`parsing failed for ${label}: ${document.error_json?.reason_code}`);
    await page.waitForTimeout(2000);
  }
  throw new Error(`document ${label} was not indexed in time`);
}

/** Submits one question and records every response the product produced. */
async function ask({question, buildFacts = false, policy, document, expectedVerdict, label}) {
  if (document) {
    // Titles are not unique once a workspace has earlier uploads of the same
    // file, so select the row by its document id.
    const checkbox = page.locator(`article[data-document-id="${document.id}"] input[type=checkbox]`);
    if (!(await checkbox.isChecked())) await checkbox.check();
  }
  await page.getByRole('textbox', {name: '问题', exact: true}).fill(question);
  const build = page.getByRole('checkbox', {name: '为以后提问保存数字'});
  if ((await build.isChecked()) !== buildFacts) await build.setChecked(buildFacts);
  if (buildFacts && policy) await page.getByRole('combobox', {name: '何时保存供后续复用'}).selectOption(policy);
  const received = page.waitForResponse((r) => new URL(r.url()).pathname === '/api/v1/queries' && r.request().method() === 'POST');
  await page.getByRole('button', {name: '查阅并回答', exact: true}).click();
  const response = await received;
  if (response.status() !== 202) throw new Error(`query rejected with ${response.status()}`);
  const created = await response.json();
  await waitForVerdict(expectedVerdict);
  await waitForTerminal(created.query_id);
  const captured = await snapshotRun(created.query_id);
  const request = response.request().postDataJSON();
  capture.submissions.push({label, request, query_id: created.query_id});
  capture.queries.push({label, query_id: created.query_id, request, ...captured});
  await fs.mkdir(path.dirname(rawOut), {recursive: true});
  await fs.writeFile(rawOut, JSON.stringify(capture, null, 2));
  const summary = captured.run.answer?.evidence_summary || {};
  console.log(`captured ${label}: ${captured.run.state} verdict=${captured.run.answer?.answer_validation?.status || summary.validation?.status || 'n/a'} calls=${(captured.run.calls || []).length} coverage=${summary.structured_coverage?.status}`);
  return captured.run;
}

/** Copies the exact bytes the product served back into the demo assets. */
async function freezeSourcePdf(documentId, versionId, filename, citedPage = 0) {
  const response = await fetch(`${base}/api/v1/documents/${documentId}/versions/${versionId}/source`, {headers: {Authorization: `Bearer ${token}`}});
  if (!response.ok) throw new Error(`source download failed: ${response.status}`);
  const bytes = Buffer.from(await response.arrayBuffer());
  await fs.mkdir(assetDir, {recursive: true});
  if (citedPage) {
    // Preserve the complete upload only in ignored private tmp; the public
    // replay needs a single cited page. PDFSource renders it as page 1 while
    // the UI continues to show the original report's printed page number.
    const scratch = path.join(root, 'tmp', 'demo-replay-source.pdf');
    const pagePattern = path.join(root, 'tmp', 'demo-replay-page-%d.pdf');
    await fs.mkdir(path.dirname(scratch), {recursive: true});
    await fs.writeFile(scratch, bytes);
    try {
      await runTool('pdfseparate', ['-f', String(citedPage), '-l', String(citedPage), scratch, pagePattern]);
    } catch (error) {
      throw new Error(`cannot extract cited page ${citedPage}; install Poppler pdfseparate (${error.message})`);
    }
    await fs.copyFile(path.join(root, 'tmp', `demo-replay-page-${citedPage}.pdf`), path.join(assetDir, filename));
  } else {
    await fs.writeFile(path.join(assetDir, filename), bytes);
  }
  const size = (await fs.stat(path.join(assetDir, filename))).size;
  console.log(`froze source asset samples/${filename} (${size} bytes${citedPage?', one cited page':''})`);
  return `samples/${filename}`;
}

async function phase(name, expectedProvider, run) {
  const health = await api('/healthz');
  if (health.provider !== expectedProvider) {
    throw new Error(`phase ${name} needs provider=${expectedProvider} but the server reports ${health.provider}. ` +
      `Switch the deployment (./crackrag mock | ./crackrag live enable) before continuing.`);
  }
  capture.healthz[expectedProvider] = health;
  console.log(`--- phase ${name} (provider=${health.provider}) ---`);
  await openWorkspace();
  const result = await run();
  capture.phases.push({name, provider: health.provider, finished_at: new Date().toISOString()});
  return result;
}

try {
  // Phase 1 — mock: the abstention path, no cost. Runs first so the paid session
  // (whose price snapshot expires after 24h) is prepared immediately before the
  // phase that actually spends.
  if (phasesToRun.includes('mock')) {
    const ambiguous = await phase('mock-abstention', 'mock', async () => {
      const document = await uploadDocument(path.join(root, 'web/public/samples/ambiguous.pdf'), '1', '', 'ambiguous.pdf');
      await ask({question: '样例控股2024年净利润是多少？', document: document, expectedVerdict: 'INCONCLUSIVE', label: 'mock-abstention'});
      return document;
    });
    capture.documents.push({...ambiguous, replay_asset: null, phase: 'mock'});
    await context.close();
    for (const document of capture.documents) {
      document.replay_asset = await freezeSourcePdf(document.id, document.current_version_id, 'ambiguous.pdf');
    }
    capture.history = await recordHistory();
    await fs.mkdir(path.dirname(rawOut), {recursive: true});
    await fs.writeFile(rawOut, JSON.stringify(capture, null, 2));
    console.log(`mock phase written to ${path.relative(root, rawOut)}`);
  }

  if (phasesToRun.includes('live')) {
    if (!annualPdf) throw new Error('--annual-report-pdf is required for the paid phase');

    // Phase 2 — paid: one real answer, one explicit build, two zero-call reuses.
    await phase('live-answer-and-reuse', 'deepseek', async () => {
      const document = await uploadDocument(annualPdf, annualPage, annualYear, 'annual report');
      const question = arg('question', `松霖科技${annualYear}年合并口径营业收入是多少人民币元？`);
      const paraphrase = arg('paraphrase', `请给出松霖科技${annualYear}年度合并营业收入，单位为人民币元。`);
      await ask({question, document: document, expectedVerdict: 'SUPPORTED', label: 'live-first-answer'});
      await ask({question, buildFacts: true, policy: 'COLD_ALLOWED', document: document, expectedVerdict: 'SUPPORTED', label: 'live-explicit-build'});
      await ask({question, document: document, expectedVerdict: 'SUPPORTED', label: 'live-repeat'});
      await ask({question: paraphrase, document: document, expectedVerdict: 'SUPPORTED', label: 'live-paraphrase'});
      return document;
    });

    // Freeze exactly the bytes the product served for the documents this
    // capture uploaded. The tenant may hold earlier sessions' documents, and
    // the replay must not inherit them.
    const documents = (await api('/api/v1/documents')).documents
      .filter((document) => capture.uploaded.includes(document.id));
    const merged = documents.map((document) => {
      const previous = capture.documents.find((candidate) => candidate.id === document.id);
      return {
        ...document,
        phase: document.title === 'ambiguous.pdf' ? 'mock' : 'live',
        replay_asset: previous?.replay_asset || null,
      };
    });
    for (const document of merged) {
      const filename = document.title === 'ambiguous.pdf' ? 'ambiguous.pdf' : 'demo-annual-report.pdf';
      document.replay_asset = await freezeSourcePdf(document.id, document.current_version_id, filename,
        filename === 'demo-annual-report.pdf' ? annualPage : 0);
    }
    capture.documents = merged;
    capture.history = await recordHistory();
    capture.page_errors = pageErrors;
    await fs.mkdir(path.dirname(rawOut), {recursive: true});
    await fs.writeFile(rawOut, JSON.stringify(capture, null, 2));
  }
} finally {
  await context?.close().catch(() => {});
  await browser?.close();
}

if (phaseArg !== 'publish') {
  if (pageErrors.length) throw new Error(`capture had ${pageErrors.length} browser errors; inspect the private checkpoint`);
  console.log(`private checkpoint -> ${path.relative(root, rawOut)} (${capture.queries.length} queries); run --phase publish only after review`);
  process.exit(0);
}

// ---------------------------------------------------------------- publication

const byLabel = (label) => capture.queries.find((entry) => entry.label === label);
const order = ['live-first-answer', 'live-explicit-build', 'live-repeat', 'live-paraphrase', 'mock-abstention'];
// The GIF recorder and any future demo build key their captions off these roles.
const roleOf = {
  'mock-abstention': 'abstention',
  'live-first-answer': 'first-answer',
  'live-explicit-build': 'build',
  'live-repeat': 'repeat',
  'live-paraphrase': 'paraphrase',
};
const assetByDocumentVersion = {};
for (const document of capture.documents) {
  assetByDocumentVersion[`${document.id}/${document.current_version_id}`] = document.replay_asset;
}

function stripInternal(value) {
  if (Array.isArray(value)) return value.map(stripInternal);
  if (value && typeof value === 'object') {
    const out = {};
    for (const [key, entry] of Object.entries(value)) {
      if (['tenant_id', 'trace_id', 'owner_instance_id', 'config_version', 'request_id'].includes(key)) continue;
      out[key] = stripInternal(entry);
    }
    return out;
  }
  return value;
}

/** Replay only the stored terminal response; never manufacture progress events. */
function runSteps(run) {
  return [stripInternal(run)];
}

const session = {
  schema: 'crackrag-demo-replay-v1',
  captured_at: capture.started_at,
  product: {
    release: arg('release', 'v0.1.0'),
    commit: capture.healthz.deepseek?.release_manifest_sha256 || capture.healthz.mock?.release_manifest_sha256 || 'unknown',
    provider: capture.healthz.deepseek?.provider || 'unknown',
    embedding: capture.healthz.deepseek?.embedding_version || capture.healthz.mock?.embedding_version || 'unknown',
  },
  notice: {
    mode: 'replay',
    banner: (() => {
      const live = byLabel('live-first-answer')?.run;
      const reuse = byLabel('live-repeat')?.run;
      const liveSteps = order.filter((label) => label.startsWith('live-')).map(byLabel).filter(Boolean);
      const calls = liveSteps.flatMap((entry) => entry.run.calls || []).length;
      const cost = liveSteps
        .reduce((sum, entry) => sum + Number(entry.run.cost?.known_estimated_subtotal || 0), 0);
      return `真实 DeepSeek 段共 ${calls} 次模型调用、已知用量费用估算 ¥${cost.toFixed(8)}；` +
        `重复提问 ${(reuse?.calls || []).length} 次模型调用。最后的缺证据示例使用模拟模式，未计入真实费用。`;
    })(),
    evidence_url: 'https://github.com/XJfyrh/CrackRAG/releases/tag/v0.1.0',
  },
  tenant_label: 'demo',
  healthz: capture.healthz,
  documents: capture.documents.map((document) => ({
    id: document.id,
    title: document.title,
    current_version_id: document.current_version_id,
    state: 'READY',
    indexed_pages: document.indexed_pages,
    requested_pages: document.requested_pages,
    build_usage: null,
  })),
  queries: order.map(byLabel).filter(Boolean).map((entry, index) => ({
    order: index + 1,
    role: roleOf[entry.label],
    dialog: entry.label === 'mock-abstention' ? 'inconclusive' : 'supported',
    question: entry.run.question,
    document_ids: entry.request?.document_ids || [entry.run.answer?.evidence_summary?.sources?.[0]?.document_id].filter(Boolean),
    build_facts: !!entry.request?.build_facts,
    execution_policy: entry.request?.execution_policy || 'HOT_ONLY',
    run_steps: runSteps(entry.run),
    regions: Object.fromEntries(Object.entries(entry.regions).map(([regionId, value]) => {
      const region = stripInternal(value);
      return [regionId, {...region, source_url: region.source_url?.split('/api/v1')[1]
        ? `/api/v1${region.source_url.split('/api/v1')[1]}` : region.source_url,
        replay_page: entry.label === 'mock-abstention' ? region.page : 1, context: null}];
    })),
  })),
  history: {
    // Only the runs this capture created: the tenant keeps earlier sessions and
    // the replay panel must not show queries whose answers are not replayed.
    queries: (capture.history?.queries || []).filter((item) => capture.queries.some((entry) => entry.query_id === item.id)),
    next_cursor: '',
  },
  sources: assetByDocumentVersion,
};

await fs.mkdir(path.dirname(rawOut), {recursive: true});
await fs.writeFile(rawOut, JSON.stringify(capture, null, 2));
await fs.writeFile(publicOut, JSON.stringify(session, null, 2));

const serialized = JSON.stringify(session);
for (const forbidden of ['tenant_id', 'trace_id', 'sk-', 'C:\\Users', 'C:/Users']) {
  if (serialized.includes(forbidden)) throw new Error(`sanitized session still contains ${forbidden}`);
}
console.log(`raw capture  -> ${path.relative(root, rawOut)}`);
console.log(`demo session -> ${path.relative(root, publicOut)} (${Buffer.byteLength(serialized)} bytes)`);
console.log(`captured ${session.queries.length} queries, ${session.documents.length} documents, ${Object.keys(session.sources).length} sources`);
console.log(`page errors: ${pageErrors.length ? pageErrors.join(' | ') : 'none'}`);
