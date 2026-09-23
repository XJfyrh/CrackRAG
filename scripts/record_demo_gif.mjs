// Records the README demo GIF from the static replay build.
//
// There is no system ffmpeg, and the binary bundled with Playwright is a
// decode-only build (no encoders, no palettegen/paletteuse — see
// docs/demo-site.md), so the GIF is produced without ffmpeg:
//
//   1. An infinite compositor-thread animation keeps the surface producing
//      frames, and CDP `Page.startScreencast` streams JPEG frames of the live
//      page. Playwright's `page.screenshot()` is NOT usable here: it waits for
//      two stable animation frames, so it stalls whenever the page is animating.
//   2. Frames are decimated to a target rate using their real timestamps,
//      decoded (jpeg-js) and written into a GIF (gifenc) with one global
//      palette, so the UI's flat colours stay stable across the loop.
//
// The scenario is driven by the frozen replay session, so what the GIF shows is
// whatever the captured run actually produced — never invented content.
//
// Tool dependencies are pinned in web/package-lock.json so another maintainer
// can regenerate the public animation after npm ci --prefix web.
//
// Usage: node scripts/record_demo_gif.mjs [--base URL] [--out FILE]
//   --dump-frames D  write captured JPEG frames + index to D (for encoder tuning)
//   --from-dump D    skip the browser and re-encode frames previously dumped to D
//   --still          also write tmp/gif-frames/last.png for a README still

import fs from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {createRequire} from 'node:module';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const toolRequire = createRequire(path.join(root, 'web/package.json'));
const {chromium} = toolRequire('@playwright/test');
const {GIFEncoder, quantize, applyPalette} = toolRequire('gifenc');
const jpeg = toolRequire('jpeg-js');

function arg(name, fallback = '') {
  const index = process.argv.indexOf(`--${name}`);
  return index >= 0 && process.argv[index + 1] ? process.argv[index + 1] : fallback;
}
const flag = (name) => process.argv.includes(`--${name}`);

const base = arg('base', 'http://127.0.0.1:18418/CrackRAG/demo/');
const out = arg('out', path.join(root, 'docs/media/demo-loop.gif'));
const speed = Number(arg('speed', '2'));
const targetFps = Number(arg('fps', '4'));
const gifWidth = Number(arg('width', '1100'));
const dumpDir = arg('dump-frames');
const fromDump = arg('from-dump');
const viewport = {width: 1100, height: 570};

const sessionPath = path.join(root, 'web/src/demo/session.json');
const session = JSON.parse(await fs.readFile(sessionPath, 'utf8'));
if (session.queries?.map(({role}) => role).join(',') !== 'first-answer,build,repeat,paraphrase,abstention') {
  throw new Error(`${sessionPath} must contain the verified five-step replay`);
}

const roleOf = (query) => query.role || (query.dialog === 'inconclusive' ? 'abstention' : 'answer');
const beats = session.queries.map((query) => ({role: roleOf(query), question: query.question}));
console.log('beats from session:', beats.map((beat) => beat.role).join(' -> '));

await fs.mkdir(path.dirname(out), {recursive: true});
const browser = await chromium.launch();
const context = await browser.newContext({viewport, deviceScaleFactor: 1, locale: 'zh-CN'});
const page = await context.newPage();
const pageErrors = [];
page.on('pageerror', (error) => pageErrors.push(error.message));

// ---------------------------------------------------------------- frame sink

const client = await context.newCDPSession(page);
const frames = [];
const acks = new Set();
client.on('Page.screencastFrame', (event) => {
  frames.push({ts: Date.now(), data: Buffer.from(event.data, 'base64')});
  acks.add(client.send('Page.screencastFrameAck', {sessionId: event.sessionId}).catch(() => {}));
});
await client.send('Page.startScreencast', {
  format: 'jpeg', quality: 80, maxWidth: viewport.width, maxHeight: viewport.height, everyNthFrame: 1,
});

async function stopCapture() {
  await client.send('Page.stopScreencast').catch(() => {});
  await Promise.all([...acks]);
}

/**
 * Screencast only produces frames while the surface is being painted, so an
 * idle UI yields a slideshow. An infinite compositor-thread animation keeps
 * frames flowing without occupying the main thread or changing what is shown.
 */
async function startPaintKeepalive() {
  await page.evaluate(() => {
    const node = document.createElement('div');
    node.style.cssText = 'position:fixed;left:0;bottom:0;width:3px;height:3px;background:#123b32;' +
      'opacity:0.02;pointer-events:none;z-index:2147483647';
    document.body.append(node);
    node.animate([{transform: 'translateX(0px)'}, {transform: 'translateX(6px)'}],
      {duration: 260, iterations: Infinity});
  });
}

/**
 * Decimates captured frames to the target rate using their real timestamps,
 * then encodes them. GIF delay is centisecond-quantised, so extremely short
 * gaps are merged into the preceding frame.
 */
async function encode() {
  if (!frames.length) return {frames: 0, bytes: 0, playbackSeconds: 0, width: 0, height: 0};
  const minGap = (1000 / targetFps) * speed;
  const kept = [];
  let last = -Infinity;
  let examined = 0;
  for (const frame of frames) {
    examined += 1;
    const gap = frame.ts - last;
    if (examined <= 4) console.log(`  probe #${examined}: ts=${frame.ts} gap=${gap} pass=${gap >= minGap}`);
    if (gap >= minGap) {
      kept.push(frame);
      last = frame.ts;
    }
  }
  console.log(`encode: ${frames.length} captured -> ${kept.length} kept ` +
    `(targetFps=${targetFps}, speed=${speed}, minGap=${Math.round(minGap)}ms; ` +
    `first ts ${frames[0]?.ts} last ts ${frames.at(-1)?.ts})`);

  const probe = jpeg.decode(kept[0].data, {useTArray: true});
  const scale = Math.min(1, gifWidth / probe.width);
  const width = Math.round(probe.width * scale);
  const height = Math.round(probe.height * scale);

  // Palette sample: strided pixels over several frames. Spreading a
  // multi-million element array into push() overflows the stack, so copy into a
  // sized buffer instead.
  const sampleFrames = kept.filter((_, index) => index % Math.max(1, Math.floor(kept.length / 6)) === 0).slice(0, 6);
  const stride = 64 * 4;
  const perFrame = Math.ceil(probe.data.length / stride);
  const sample = new Uint8Array(sampleFrames.length * perFrame * 4);
  let cursor = 0;
  for (const frame of sampleFrames) {
    const rgba = jpeg.decode(frame.data, {useTArray: true}).data;
    for (let index = 0; index + 3 < rgba.length; index += stride) {
      sample[cursor] = rgba[index];
      sample[cursor + 1] = rgba[index + 1];
      sample[cursor + 2] = rgba[index + 2];
      sample[cursor + 3] = 255;
      cursor += 4;
    }
  }
  const palette = quantize(sample.subarray(0, cursor), 256, {format: 'rgb565'});

  const encoder = GIFEncoder();
  const lastTs = kept[kept.length - 1].ts;
  let written = 0;
  let merged = 0;
  for (let index = 0; index < kept.length; index += 1) {
    const next = index + 1 < kept.length ? kept[index + 1].ts : lastTs + minGap;
    const centiseconds = Math.round(Math.max(60, (next - kept[index].ts) / speed) / 10);
    if (centiseconds < 4 && written > 0) {
      merged += 1;
      continue;
    }
    const decoded = jpeg.decode(kept[index].data, {useTArray: true});
    const rgba = resample(decoded.data, decoded.width, decoded.height, width, height);
    encoder.writeFrame(applyPalette(rgba, palette, 'rgb565'), width, height, {
      palette: written === 0 ? palette : undefined,
      delay: centiseconds * 10,
      repeat: 0,
      dispose: 1,
    });
    written += 1;
  }
  encoder.finish();
  const bytes = encoder.bytes();
  await fs.writeFile(out, bytes);
  return {
    frames: written, merged, bytes: bytes.length, width, height,
    playbackSeconds: (lastTs - kept[0].ts) / 1000 / speed,
  };
}

/** Nearest-neighbour box resample; the UI is mostly flat colour, so this is enough. */
function resample(data, sourceWidth, sourceHeight, width, height) {
  if (sourceWidth === width && sourceHeight === height) return new Uint8Array(data);
  const out = new Uint8Array(width * height * 4);
  for (let y = 0; y < height; y += 1) {
    const sourceY = Math.min(sourceHeight - 1, Math.floor((y * sourceHeight) / height));
    for (let x = 0; x < width; x += 1) {
      const sourceX = Math.min(sourceWidth - 1, Math.floor((x * sourceWidth) / width));
      const from = (sourceY * sourceWidth + sourceX) * 4;
      const to = (y * width + x) * 4;
      out[to] = data[from];
      out[to + 1] = data[from + 1];
      out[to + 2] = data[from + 2];
      out[to + 3] = 255;
    }
  }
  return out;
}

// ------------------------------------------------------------------ scenario

async function caption(text) {
  await page.evaluate((value) => {
    let node = document.getElementById('demo-caption');
    if (!node) {
      node = document.createElement('div');
      node.id = 'demo-caption';
      node.style.cssText = [
        'position:fixed', 'left:4%', 'right:4%', 'bottom:14px', 'z-index:10000',
        'background:#123b32f2', 'color:#fff', 'padding:13px 18px', 'border-radius:10px',
        'font:15px/1.55 Inter,"Microsoft YaHei",sans-serif', 'box-shadow:0 4px 18px #0003',
      ].join(';');
      document.body.append(node);
    }
    node.textContent = value;
  }, text);
}
const beat = (seconds) => page.waitForTimeout(seconds * 1000);

/** Scrolling is cosmetic here; a re-render mid-scroll must not fail the recording. */
async function reveal(selector, block = 'start') {
  await page.evaluate(([target, where]) => {
    document.querySelector(target)?.scrollIntoView({block: where, behavior: 'instant'});
  }, [selector, block]).catch(() => {});
  await page.waitForTimeout(400);
}

if (fromDump) {
  const source = path.resolve(root, fromDump);
  const index = JSON.parse(await fs.readFile(path.join(source, 'index.json'), 'utf8'));
  for (const entry of index) {
    frames.push({ts: entry.ts, data: await fs.readFile(path.join(source, entry.file))});
  }
  console.log(`loaded ${frames.length} frames from ${path.relative(root, source)}`);
} else {
  try {
    await page.goto(base, {waitUntil: 'domcontentloaded'});
  await page.getByRole('textbox', {name: '问题', exact: true}).waitFor({state: 'visible', timeout: 30000});
  // Screencast starts before navigation, but the README loop must start on the
  // loaded page rather than a few blank navigation frames.
  frames.length = 0;
  await startPaintKeepalive();
  await caption('年报里一个数字，怎样知道答对了、还能省去重复查询？');
  await beat(2);

  // Beat 1 — the question is answered from the original page.
  await page.locator('.suggestions button').first().click();
  await caption(`① 提问：${beats[0].question}`);
  await beat(2);
  await page.getByRole('button', {name: '查看录制结果', exact: true}).click();
  await page.locator('.answer-verdict').waitFor({state: 'visible', timeout: 60000});
  await reveal('.answer-card');
  await caption('得到数字，也看到来源与这次调用的费用');
  await beat(3);

  // Beat 2 — verify the citation on the original page.
  await page.locator('.source-link').first().click();
  await page.locator('.pdf-preview canvas').waitFor({state: 'visible', timeout: 30000});
  await page.waitForFunction(
    () => document.querySelector('.pdf-preview canvas')?.dataset.status === 'ready',
    null, {timeout: 90000},
  );
  await reveal('.source-card');
  await beat(1);
  await caption('② 点开原报告第 86 页，核对年份列、口径与原文数字');
  await beat(3);
  await reveal('.pdf-preview canvas', 'center');
  await beat(2);
  await page.getByRole('button', {name: '关闭', exact: true}).click();
  await beat(0.5);

  // Beat 3 — explicitly save a validated number for later questions.
  await page.getByRole('button', {name: '② 保存可复用数字'}).click();
  await page.getByRole('button', {name: '查看录制结果', exact: true}).click();
  await page.waitForFunction(() => document.querySelector('.metrics > div:nth-child(2) strong')?.textContent?.trim() === '2 次');
  await reveal('.answer-card');
  await caption('③ 明确保存经过核对的数字；这一步另有调用与费用');
  await beat(3);

  // Beat 4 — repeated and reworded questions now use the saved fact.
  await page.getByRole('button', {name: '③ 再问同一问题'}).click();
  await page.getByRole('button', {name: '查看录制结果', exact: true}).click();
  await page.waitForFunction(() => document.querySelector('.metrics > div:nth-child(2) strong')?.textContent?.trim() === '0 次');
  await reveal('.metrics');
  await caption('④ 再问同一问题：直接复用已核对数字，0 次模型调用');
  await beat(3);
  await page.getByRole('button', {name: '④ 换个问法'}).click();
  await page.getByRole('button', {name: '查看录制结果', exact: true}).click();
  await page.waitForFunction(() => document.querySelector('.metrics > div:nth-child(2) strong')?.textContent?.trim() === '0 次');
  await reveal('.metrics');
  await caption('换一种问法，仍能复用同一条有效事实');
  await beat(2);

  // The final abstention is a separately recorded mock example, labeled as such.
  await page.getByRole('button', {name: '⑤ 看证据不足时的回答'}).click();
  await page.getByRole('button', {name: '查看录制结果', exact: true}).click();
  await page.locator('.answer-verdict[data-status="INCONCLUSIVE"]').waitFor({state: 'visible', timeout: 60000});
  await reveal('.answer-card');
  await caption('⑤ 证据不够就说明缺口，不猜；此段为独立模拟示例');
  await beat(3);
  await caption('点开静态演示，自己核对回答、原页与费用');
  await beat(2);

  if (flag('still')) {
    const frameDir = path.join(root, 'tmp/gif-frames');
    await fs.mkdir(frameDir, {recursive: true});
    await page.screenshot({path: path.join(frameDir, 'last.png'), timeout: 15000}).catch(() => {});
  }
  } finally {
    await stopCapture();
    await context.close();
    await browser.close();
  }
}

if (dumpDir) {
  // Raw frames are large but cheap to keep: they make encoder tuning a
  // re-encode instead of another 40-second recording session.
  const target = path.resolve(root, dumpDir);
  const privateTmp = path.join(root, 'tmp') + path.sep;
  if (!target.startsWith(privateTmp) || target === path.join(root, 'tmp')) {
    throw new Error('--dump-frames must be a subdirectory of the private tmp directory');
  }
  await fs.rm(target, {recursive: true, force: true});
  await fs.mkdir(target, {recursive: true});
  const index = [];
  for (let position = 0; position < frames.length; position += 1) {
    const name = `${String(position).padStart(5, '0')}.jpg`;
    await fs.writeFile(path.join(target, name), frames[position].data);
    index.push({file: name, ts: frames[position].ts});
  }
  await fs.writeFile(path.join(target, 'index.json'), JSON.stringify(index));
  console.log(`dumped ${index.length} frames to ${path.relative(root, target)}`);
}

const encoded = await encode();
const span = frames.length ? (frames.at(-1).ts - frames[0].ts) / 1000 : 0;
const gaps = frames.slice(1).map((frame, index) => frame.ts - frames[index].ts).sort((a, b) => a - b);
console.log(`captured ${frames.length} frames over ${span.toFixed(1)}s ` +
  `(median gap ${gaps.length ? gaps[Math.floor(gaps.length / 2)] : 0}ms)`);
console.log(`wrote ${path.relative(root, out)} · ${encoded.frames} frames` +
  `${encoded.merged ? ` (${encoded.merged} merged)` : ''} · ` +
  `${encoded.width}x${encoded.height} · ${(encoded.bytes / 1e6).toFixed(2)} MB · ` +
  `${encoded.playbackSeconds.toFixed(1)}s playback at ${speed}x`);
if (encoded.bytes > 5_000_000) {
  console.log('WARNING: GIF exceeds the 5 MB public-source limit; raise --speed or lower --width/--fps');
}
if (pageErrors.length) {
  for (const error of pageErrors) console.log('page error:', error);
  process.exitCode = 1;
}
