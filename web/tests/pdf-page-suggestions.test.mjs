import test from 'node:test';
import assert from 'node:assert/strict';
import {financialPageScore,recommendedPages,pageSelectionError} from '../src/pdf-page-suggestions.ts';

test('financial-page suggestion requires both statement terms and numeric evidence',()=>{
 assert.equal(financialPageScore('目录 合并利润表 第40页'),0);
 assert.equal(financialPageScore('合并利润表 营业收入 3,014,989,619.04 营业成本 净利润'),22);
 assert.equal(financialPageScore('营业收入 3,014,989,619.04'),0);
});

test('suggestions rank strongest evidence and return pages in reading order',()=>{
 const scores=[{page:40,score:22},{page:12,score:8},{page:41,score:17},{page:3,score:0}];
 assert.deepEqual(recommendedPages(scores),[12,40,41]);
});

test('page selection fails before upload for long reports and invalid ranges',()=>{
 assert.equal(pageSelectionError('',12),'');
 assert.match(pageSelectionError('',40),/超过 32 页/);
 assert.equal(pageSelectionError('40',40),'');
 assert.match(pageSelectionError('41',40),/不能超过/);
 assert.match(pageSelectionError('4,4',40),/重复/);
 assert.match(pageSelectionError('4-6',40),/英文逗号/);
});
