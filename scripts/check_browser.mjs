// Reproducible browser acceptance against the real mock application.
// Optional --video records a narrated-by-captions 3–5 minute demonstration.
import fs from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {createRequire} from 'node:module';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const require=createRequire(path.join(root,'web/package.json'));
const {chromium,expect}=require('@playwright/test');
const base=process.env.CRACKRAG_URL||'http://127.0.0.1:18086';
const token=(await fs.readFile(process.env.CRACKRAG_TOKEN_FILE||path.join(root,'.release/secrets/access_token'),'utf8')).trim();
const output=path.join(root,'output/playwright');await fs.mkdir(output,{recursive:true});
const record=process.argv.includes('--video');
const recoveryArg=process.argv.indexOf('--recovery-evidence');
const recoveryFolder=recoveryArg>=0?process.argv[recoveryArg+1]:null;
if(recoveryArg>=0&&!recoveryFolder)throw new Error('--recovery-evidence requires a completed private demo directory');
async function api(route){const response=await fetch(base+route,{headers:{Authorization:`Bearer ${token}`}});if(!response.ok)throw new Error(`HTTP ${response.status} on ${route}`);return response.json()}
if((await api('/healthz')).provider!=='mock')throw new Error('MOCK_ONLY: refusing browser submissions to a paid target');
const browser=await chromium.launch({headless:!process.argv.includes('--headed')});
const context=await browser.newContext({viewport:{width:1440,height:1000},locale:'zh-CN',...(record?{recordVideo:{dir:output,size:{width:1440,height:1000}}}:{})});
const page=await context.newPage();
const errors=[],posts=[],results=[];
page.on('pageerror',error=>errors.push(error.message));
page.on('request',request=>{if(request.method()==='POST'&&new URL(request.url()).pathname==='/api/v1/queries')posts.push(Date.now())});
const started=Date.now();
async function caption(text,seconds=18){
 if(!record)return;
 await page.evaluate(text=>{let e=document.getElementById('demo-caption');if(!e){e=document.createElement('div');e.id='demo-caption';e.style.cssText='position:fixed;left:8%;right:8%;bottom:18px;z-index:10000;background:#123b32ee;color:white;padding:15px 22px;border-radius:10px;font:17px/1.6 sans-serif;box-shadow:0 3px 14px #0003';document.body.append(e)}e.textContent=text},text);
 await page.waitForTimeout(seconds*1000);
}
async function upload(name){
 await page.getByLabel('选择 PDF').setInputFiles(path.join(root,'web/public/samples',name));
 await page.getByRole('textbox',{name:'页码范围'}).fill('1');
 const received=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/v1/documents'&&r.request().method()==='POST');
 await page.getByRole('button',{name:'上传文档',exact:true}).click();
 const response=await received;if(response.status()!==202)throw new Error('upload failed');
 const created=await response.json();
 await expect.poll(async()=>{const d=(await api('/api/v1/documents')).documents.find(d=>d.id===created.document_id);return d?.state},{timeout:120000}).toBe('READY');
 await expect(page.getByRole('checkbox',{name,exact:true}).first()).toBeEnabled({timeout:10000});
}
async function ask(button,expected){
 await page.getByRole('button',{name:button,exact:true}).click();
 const received=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/v1/queries'&&r.request().method()==='POST');
 await page.getByRole('button',{name:'查阅并回答',exact:true}).click();
 const response=await received;if(response.status()!==202)throw new Error(`query rejected ${response.status()}`);
 const created=await response.json();
 await expect(page.locator('.run-id')).toContainText(created.query_id);
 await expect(page.locator('.answer-verdict')).toHaveAttribute('data-status',expected,{timeout:120000});
 await expect.poll(async()=>{const r=await api('/api/v1/queries/'+created.query_id);return {state:r.state,pending:r.diagnostics.m3.pending_jobs||0}},{timeout:120000}).toEqual({state:'COMPLETED',pending:0});
 const run=await api('/api/v1/queries/'+created.query_id);results.push({id:run.id,status:expected,calls:run.calls.length,coverage:run.answer.evidence_summary.structured_coverage.status});
 return run;
}
try{
 await page.goto(base);
 await page.getByRole('textbox',{name:'本机访问令牌'}).fill(token);
 await page.getByRole('button',{name:'进入工作区',exact:true}).click();
 await expect(page.getByText('新查询：模拟模式',{exact:true})).toBeVisible();
 await caption('CrackRAG v0.1.0｜这段视频使用默认 mock，展示真实上传、验证、事实构建与复用流程。真实模型质量和费用另有验收报告。',20);
 await upload('financial.pdf');
 await caption('1 / 导入自制 PDF。样例包含明确主体、合并口径、单位和年份列；通过正常解析入库，没有预置正式事实。',20);
 const first=await ask('① 首问','SUPPORTED');
 expect(first.calls.length).toBeGreaterThan(0);expect(first.answer.evidence_summary.reused_facts).toHaveLength(0);
 await page.locator('.answer-card').scrollIntoViewIfNeeded();
 await page.screenshot({path:path.join(output,'01-supported.png')});
 await caption('2 / 首问查阅原文。模型提出声明，Go 重新验证来源语义后生成答案。通过回答校验，并不会自动发布正式事实。',22);
 await page.locator('.source-link').first().click();
 await expect(page.getByRole('heading',{name:'来源核验 · 第 1 页'})).toBeVisible();
 await expect(page.getByLabel('PDF 来源页 1')).toHaveAttribute('data-status','ready',{timeout:20000});
 await page.locator('.source-card').scrollIntoViewIfNeeded();
 await page.screenshot({path:path.join(output,'02-source.png')});
 await caption('3 / 打开来源。可以查看逐字引文、区域原文、页码与原 PDF；答案不是只给一串不可核对的文本。',20);
 await page.getByRole('button',{name:'关闭',exact:true}).click();
 const built=await ask('② 显式构建','SUPPORTED');
 expect(built.diagnostics.m3.jobs.some(j=>j.state==='COMMITTED')).toBeTruthy();
 await page.getByRole('heading',{name:'后台事实构建',exact:true}).scrollIntoViewIfNeeded();
 await expect(page.getByText('已发布通过验证的事实',{exact:true})).toBeVisible();
 await page.screenshot({path:path.join(output,'03-built.png')});
 await caption('4 / 显式后台构建使用 COLD_ALLOWED，真实模式会额外计费。候选先隔离保存，再验证和原子发布；回答与发布分别记录。',22);
 const reused=await ask('③ 复用查询','SUPPORTED');
 expect(reused.calls).toHaveLength(0);expect(reused.answer.evidence_summary.structured_coverage.status).toBe('FULL');
 await page.locator('.answer-card').scrollIntoViewIfNeeded();
 await page.screenshot({path:path.join(output,'04-reuse.png')});
 await caption('5 / 同一需求再次查询：FULL 命中有效事实，零模型调用。事实 ID、验证报告与来源仍可追溯；这不等于所有序列一定降本。',22);
 const postCount=posts.length;
 await page.reload();await expect(page.locator('.run-id')).toContainText(reused.id,{timeout:15000});
 expect(posts.length).toBe(postCount);
 await caption('6 / 刷新页面恢复原 Run，没有重新提问。最近查询可以切换历史，后台终态和账本仍沿用原查询。',18);
 await upload('ambiguous.pdf');
 await ask('④ 缺证据示例','INCONCLUSIVE');
 await page.locator('.answer-card').scrollIntoViewIfNeeded();
 await expect(page.getByText('证据缺口',{exact:true})).toBeVisible();
 await page.screenshot({path:path.join(output,'05-abstention.png')});
 await caption('7 / 缺少口径附注的样例保持 INCONCLUSIVE。系统解释证据缺口，不把页面上的数字直接当成受支持结论。',22);
 const beforeHistory=posts.length;
 await page.locator('.history-item').filter({hasText:'0 次模拟调用'}).first().click();
 await expect(page.locator('.run-id')).toContainText(reused.id);
 expect(posts.length).toBe(beforeHistory);
 await page.setViewportSize({width:390,height:844});
 expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1)).toBeTruthy();
 await page.screenshot({path:path.join(output,'06-mobile.png'),fullPage:true});
 await page.setViewportSize({width:1440,height:1000});
 if(recoveryFolder){
  const proof=JSON.parse(await fs.readFile(path.join(recoveryFolder,'proof.json'),'utf8'));
  const saved=JSON.parse(await fs.readFile(path.join(recoveryFolder,'before.json'),'utf8'));
  if(proof.status!=='PASS'||proof.paid_calls!==0||proof.calls_before!==proof.calls_after||proof.full_reuse_model_calls!==0||proof.release_manifest_sha256!==(await api('/healthz')).release_manifest_sha256)throw new Error('Recovery proof does not match this mock release');
  await page.getByRole('button',{name:'刷新',exact:true}).click();
  const list=await api('/api/v1/queries?limit=20');
  const index=list.queries.findIndex(r=>r.id===saved.run.id);
  if(index<0)throw new Error('Run recovery demonstration just before recording so its Run remains on the first history page');
  await expect(page.locator('.history-item')).toHaveCount(list.queries.length);
  const noPosts=posts.length;
  await page.locator('.history-item').nth(index).click();
  await expect(page.locator('.run-id')).toContainText(saved.run.id);
  await expect(page.getByText('已发布通过验证的事实',{exact:true})).toBeVisible();
  await page.getByRole('heading',{name:'后台事实构建',exact:true}).scrollIntoViewIfNeeded();
  await page.screenshot({path:path.join(output,'07-recovery.png')});
  await caption(`8 / 查看刚完成的受控恢复演练：候选保存后终止主实例，并重启 Redis；备用实例接续同一个任务。调用数保持 ${proof.calls_before} → ${proof.calls_after}，恢复后复用零模型调用。命令与核对记录随版本说明提供。`,24);
  expect(posts.length).toBe(noPosts);
  results.push({recovery_run:saved.run.id,calls_before:proof.calls_before,calls_after:proof.calls_after,full_reuse_model_calls:0});
 }
 await caption('支持范围：明确实体、年度和合并口径的财务标量。复杂跨页表格、扫描 OCR 与任意问答不在首版范围。源码、运行步骤与真实评测证据随版本交付。',22);
 expect(errors).toEqual([]);
 await fs.writeFile(path.join(output,'browser-acceptance.json'),JSON.stringify({version:'release-browser-acceptance-v1',provider:'mock',results,query_posts:posts.length,refresh_history_created_posts:0,page_errors:errors,video:record,elapsed_seconds:(Date.now()-started)/1000},null,2));
 console.log('PASS: browser upload, supported answer, source, explicit build, zero-call reuse, refusal, refresh/history, mobile layout');
}finally{await context.close();await browser.close()}
