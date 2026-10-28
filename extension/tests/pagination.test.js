"use strict";
const assert = require("node:assert/strict");
require("../url-utils.js");
const rules = require("../pagination.js");
const capture = require("../pagination-capture.js");

for (const [left, right] of [
  ["https://example.com/story.html", "https://example.com/story_2.html"],
  ["https://example.com/content_123456.html", "https://example.com/content_123456_2.html"],
  ["https://example.com/story-2.htm", "https://example.com/story-3.htm"],
  ["https://example.com/story/", "https://example.com/story/page/2"],
  ["https://example.com/story?page=2&id=4", "https://example.com/story?id=4&page=3"],
  ["https://example.com/story?id=4", "https://example.com/story?id=4&PageNo=2&utm_source=page"],
]) assert(rules.nextURL(left, right), `${left} -> ${right}`);
for (const right of ["https://evil.example/story?page=2", "http://example.com/story?page=2", "https://example.com/story?page=4",
  "https://example.com/story?id=other&page=2", "https://example.com/story?offset=20", "https://example.com/story?p=2",
  "https://example.com/story?page=2&page=3", "https://user:secret@example.com/story?page=2", "https://example.com/story#/page/2", "javascript:alert(1)"]) {
  assert.equal(rules.nextURL("https://example.com/story", right), null, right);
}
assert.equal(rules.titleKey("新闻正文（2/3） - 测试站"), rules.titleKey("新闻正文 - 测试站"));
assert.equal(rules.titleKey("真实文章标题【2】--新闻站"), rules.titleKey("真实文章标题--新闻站"));
assert.equal(rules.titleKey("真实文章标题【3】"), rules.titleKey("真实文章标题"));
assert.notEqual(rules.titleKey("报告【2026】"), rules.titleKey("报告【2027】"));
assert(rules.sameArticle({title:"一篇文章第1页"}, {title:"一篇文章第2页"}));
assert(!rules.sameArticle({title:"另一篇文章"}, {title:"完全不同正文"}));
assert(!rules.sameArticle({url:"https://example.com/story",title:"新闻甲",pagination:{canonical:"https://example.com/"}},
  {title:"新闻乙",pagination:{canonical:"https://example.com/"}}), "generic site canonical must not override title mismatch");

function event() { const listeners = new Set(); return { addListener: fn => listeners.add(fn), removeListener: fn => listeners.delete(fn), emit: (...args) => { for(const fn of listeners) fn(...args); } }; }
function page(n, total = 3) {
  return { url: `https://example.com/story?page=${n}`, title:"分页文章测试", raw_text:"独立正文" + n,
    images:[{url:`https://example.com/${n}.png`}], diagnostics:{quality_warnings:[]},
    pagination:{ heading:"分页文章测试",status:n<total?"ready":"none",next_url:n<total?`https://example.com/story?page=${n+1}`:null } };
}
(async () => {
  let source = page(1).url, created = [], removed = [], disconnected = false, total = 3, fail = false, different = false, repeat = false;
  const tabs = new Map();
  global.chrome = {
    runtime:{connect:() => {
      const port = {onMessage:event(),onDisconnect:event(),disconnect:()=>{disconnected=true;port.onDisconnect.emit();},
        postMessage: message => { queueMicrotask(() => {
          if(message.action==="create") { const id=100+created.length; created.push(message.url); tabs.set(id,message.url); port.onMessage.emit({id:message.id,tabId:id}); }
          else { removed.push(message.tabId); tabs.delete(message.tabId); port.onMessage.emit({id:message.id}); }
        }); }};
      return port;
    }},
    tabs:{get:async id => ({url:id===1?source:tabs.get(id),status:"complete"})},
  };
  global.webRagPageCapture={capture:async id=>{
    if(fail) throw new Error("access denied");
    const n=Number(new URL(tabs.get(id)).searchParams.get("page"));
    const value=page(n,total);
    if(different) value.pagination.heading="不是同一篇文章";
    if(repeat) {value.raw_text=page(1).raw_text;value.images=[];}
    return value;
  }};
  const opts = overrides => ({signal:new AbortController().signal,onProgress:()=>{},...overrides});
  let result = await capture.expand(1,page(1),opts());
  assert.equal(result.url,page(1).url);
  assert.equal(result.diagnostics.pagination_page_count,3);
  assert.equal(result.diagnostics.pagination_stop_reason,"complete");
  assert.match(result.raw_text,/第 1 页[\s\S]*独立正文1[\s\S]*第 2 页[\s\S]*独立正文2[\s\S]*第 3 页[\s\S]*独立正文3/);
  assert.equal(result.images.length,3); assert.equal(removed.length,2); assert(disconnected);

  total=20;created=[];removed=[];
  result=await capture.expand(1,page(1,20),opts({limits:{maxPages:100,maxMs:1000000}}));
  assert.equal(result.diagnostics.pagination_page_count,5);assert.equal(created.length,4);
  assert.equal(result.diagnostics.pagination_stop_reason,"page_limit");
  assert(result.diagnostics.quality_warnings.includes("pagination_incomplete"));
  fail=true;
  result=await capture.expand(1,page(1),opts());
  assert.equal(result.raw_text,page(1).raw_text);assert.equal(result.diagnostics.pagination_stop_reason,"page_failed");
  fail=false;different=true;
  result=await capture.expand(1,page(1),opts());
  assert.equal(result.diagnostics.pagination_stop_reason,"different_article");
  different=false;repeat=true;
  result=await capture.expand(1,page(1),opts());
  assert.equal(result.diagnostics.pagination_stop_reason,"repeated_content");
  repeat=false;
  created=[];
  result=await capture.expand(1,{...page(1),raw_text:"x".repeat(499990)},opts());
  assert.equal(result.diagnostics.pagination_stop_reason,"size_limit");assert.equal(created.length,0);
  let identityChecks=0;
  await assert.rejects(capture.expand(1,page(1),opts({guard:async()=>{if(++identityChecks>1)throw new Error("工作空间已变化");}})),/工作空间已变化/);
  const controller=new AbortController();
  await assert.rejects(capture.expand(1,page(1),opts({signal:controller.signal,onProgress:()=>controller.abort()})),/取消/);
  await assert.rejects(capture.expand(1,page(1),opts({guard:async()=>{source="https://example.com/changed";}})),/页面已变化/);
  source=page(1).url;created=[];
  result=await capture.expand(1,page(3),opts({guard:async()=>{source=page(3).url;}}));
  assert.equal(result.diagnostics.pagination_page_count,1);assert.equal(created.length,0);
  assert(result.diagnostics.quality_warnings.includes("pagination_none"));
  console.log("Pagination same-origin/sequential URL rules, identity, bounded merge, repeated content, failures, cancellation and navigation passed");
})().catch(error=>{console.error(error);process.exitCode=1;});
