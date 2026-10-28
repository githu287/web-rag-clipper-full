"use strict";
const assert=require("node:assert/strict");
const fs=require("node:fs"),path=require("node:path"),vm=require("node:vm");
require("../url-utils.js");
const rules=require("../pagination.js");
const event=()=>{const listeners=new Set();return{addListener:f=>listeners.add(f),emit:(...args)=>{for(const f of listeners)f(...args);}};};
async function settle(){for(let i=0;i<30;i++)await Promise.resolve();}

(async()=>{
  const tabs=new Map([[1,{id:1,url:"https://example.com/story?page=1",windowId:2,active:true}]]),removed=[];
  let next=10,delayCreate=null,saved={webRagPaginationTabs:[]};
  const chrome={runtime:{id:"fixture",getURL:p=>"chrome-extension://fixture/"+p,onConnect:event()},
    storage:{session:{get:async()=>saved,set:async data=>{saved=JSON.parse(JSON.stringify(data));}}},
    tabs:{onActivated:event(),get:async id=>{if(!tabs.has(id))throw Error("closed");return {...tabs.get(id)};},
      create:async options=>{if(delayCreate)await delayCreate;const tab={id:next++,...options};tabs.set(tab.id,tab);return tab;},
      remove:async id=>{removed.push(id);tabs.delete(id);}}};
  const context=vm.createContext({chrome,webRagPagination:rules,URL,Map,Set,Number,Array,Promise,console,setTimeout});
  vm.runInContext(fs.readFileSync(path.join(__dirname,"../pagination-tabs.js"),"utf8"),context);
  await settle();
  function port(){const p={name:"web-rag-pagination",sender:{id:"fixture",url:"chrome-extension://fixture/sidepanel.html"},onMessage:event(),onDisconnect:event(),responses:[],postMessage:m=>p.responses.push(m)};chrome.runtime.onConnect.emit(p);return p;}
  const create={id:1,action:"create",sourceTabId:1,sourceUrl:tabs.get(1).url,previousUrl:tabs.get(1).url,url:"https://example.com/story?page=2"};
  let p=port();p.onMessage.emit(create);await settle();
  assert.equal(p.responses[0].tabId,10);assert.equal(tabs.get(10).active,false);
  p.onMessage.emit({id:2,action:"release",tabId:1});await settle();assert(tabs.has(1),"may only release owned tabs");
  p.onDisconnect.emit();await settle();assert(!tabs.has(10));assert(tabs.has(1));
  let finish;delayCreate=new Promise(resolve=>{finish=resolve;});
  p=port();p.onMessage.emit(create);await settle();p.onDisconnect.emit();finish();await settle();
  assert(!tabs.has(11),"late create must be removed after owner disconnects");delayCreate=null;
  p=port();p.onMessage.emit(create);await settle();
  tabs.get(12).active=true;chrome.tabs.onActivated.emit({tabId:12});await settle();p.onDisconnect.emit();await settle();
  assert(tabs.has(12),"user-claimed tab must remain open");
  assert(p.responses.some(m=>m.event==="claimed"));
  p=port();p.onMessage.emit({...create,url:"https://evil.example/story?page=2"});await settle();
  assert(p.responses[0].error);assert.equal(next,13);
  p.onDisconnect.emit();
  saved={webRagPaginationTabs:[{id:15,claimed:false},{id:16,claimed:true},{id:17,claimed:false}]};
  for(const id of [15,16,17])tabs.set(id,{id,active:id===17});
  vm.runInContext(fs.readFileSync(path.join(__dirname,"../pagination-tabs.js"),"utf8"),vm.createContext({chrome,webRagPagination:rules,URL,Map,Set,Number,Array,Promise,console,setTimeout}));
  await settle();
  assert(!tabs.has(15),"worker recovery cleans abandoned inactive tabs");
  assert(tabs.has(16)&&tabs.has(17),"worker recovery preserves claimed or active tabs");
  assert.equal(saved.webRagPaginationTabs.length,0);
  console.log("Temporary pagination tabs: ownership, inactive creation, disconnect, late-create cleanup, user takeover and origin rejection passed");
})().catch(error=>{console.error(error);process.exitCode=1;});
