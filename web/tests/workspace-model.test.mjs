import test from 'node:test';
import assert from 'node:assert/strict';
import {expandFollowup,comparablePrior,formatCny} from '../src/workspace-model.ts';

test('follow-up only replaces an explicit supported field',()=>{
 const previous='松霖科技2024年合并口径营业收入是多少人民币元？';
 assert.equal(expandFollowup(previous,'那 2023 年呢？'),'松霖科技2023年合并口径营业收入是多少人民币元？');
 assert.equal(expandFollowup(previous,'那营业成本呢？'),'松霖科技2024年合并口径营业成本是多少人民币元？');
 assert.equal(expandFollowup(previous,'那去年呢？'),null);
 assert.equal(expandFollowup('说说这家公司','那 2023 年呢？'),null);
 assert.equal(expandFollowup('松霖科技2024年和2023年营业收入如何？','那 2022 年呢？'),null);
 assert.equal(expandFollowup('松霖科技2024年营业收入与营业成本如何？','那净利润呢？'),null);
 assert.equal(expandFollowup(previous,'2023年营业收入是多少？'),null);
});

test('fee comparison requires the same version, question and earlier observed call',()=>{
 const run={id:'now',question:'松霖科技2024年营业收入是多少？',created_at:'2026-09-16T13:31:00Z',provider:'deepseek',document_version_ids:['version-a'],calls:[],answer:{evidence_summary:{reused_facts:[{fact_id:'fact'}]}}};
 const history=[
  {id:'different-version',question:run.question,provider:'deepseek',created_at:'2026-09-16T13:29:00Z',model_calls:1,known_estimated_cny:'0.01',document_version_ids:['version-b']},
  {id:'future',question:run.question,provider:'deepseek',created_at:'2026-09-16T13:35:00Z',model_calls:1,known_estimated_cny:'0.01',document_version_ids:['version-a']},
  {id:'earlier',question:run.question,provider:'deepseek',created_at:'2026-09-16T13:30:00Z',model_calls:1,known_estimated_cny:'0.008119',document_version_ids:['version-a']},
 ];
 assert.equal(comparablePrior(run,history)?.id,'earlier');
 assert.equal(comparablePrior({...run,calls:[{}]},history),undefined);
});

test('unknown cost stays unknown',()=>{
 assert.equal(formatCny(null),'费用待核对');
 assert.equal(formatCny('not-a-number'),'费用待核对');
 assert.equal(formatCny('0.008119'),'¥0.008119');
 assert.equal(formatCny('0.00989692'),'¥0.00989692');
});
