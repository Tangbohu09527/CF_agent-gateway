'use strict';
/* Synthetic-only regression: a live event arrives after an initial snapshot was
 * read by the real backend, but before that snapshot reaches the real page. */
const {chromium}=require(process.env.CF_PLAYWRIGHT_MODULE||'playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const crypto=require('node:crypto');
const runtime=path.resolve(process.env.CF_CONSOLE_RUNTIME);
const origin=process.env.CF_CONSOLE_ORIGIN||'http://127.0.0.1:18990';
const login=JSON.parse(fs.readFileSync(path.join(runtime,'browser-login.json'),'utf8'));
const peer=JSON.parse(fs.readFileSync(path.join(runtime,'browser-peer.json'),'utf8'));
const out=path.join(runtime,'realtime-race-evidence');fs.mkdirSync(out,{recursive:true});
const marker='合成竞态：初始快照读取后到达的新消息';
const report={synthetic_data:true,actual_page:true,actual_gateway:true,actual_collector:true,page_errors:[],checks:[]};
const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
const deferred=()=>{let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve};};
async function eventually(fn,label,timeout=14000){const end=Date.now()+timeout;do{if(await fn())return;await pause(100);}while(Date.now()<end);throw Error('Timed out: '+label);}
function check(name,value){assert(value,name);report.checks.push({name,passed:true});}
async function main(){
 const browser=await chromium.launch({headless:true,channel:process.env.CF_BROWSER_CHANNEL||'msedge'});
 const context=await browser.newContext({viewport:{width:1440,height:1000}});
 const page=await context.newPage();page.on('pageerror',e=>report.page_errors.push(e.message));
 const snapshotReady=deferred(),snapshotGate=deferred(),deltaReady=deferred(),ackReady=deferred();
 let held=false,deltaCursor=null,postDeltaPolls=0,snapshotReleased=false;
 const read=async endpoint=>{const response=await page.request.get(origin+'/admin/console/'+endpoint);assert.equal(response.status(),200);return response.json();};
 const textMessage=()=>page.locator('#message-list .message-text').filter({hasText:marker});
 try{
  await page.goto(origin+'/console');
  await page.locator('#login-form [name=username]').fill(login.username);
  await page.locator('#login-form [name=password]').fill(login.password);
  await page.locator('#login-form button').click();await page.locator('.conversation-panel').waitFor();
  check('Explicit synthetic demo mode',(await read('state')).mode==='demo');
  await page.locator('[data-page=system]').click();
  await page.locator('[data-live-action=collect]').filter({hasText:'启用仅归档采集'}).click();
  await eventually(async()=>!!(await read('live/demo-primary')).collection.last_success_at,'baseline completion');
  await page.locator('[data-page=chats]').click();await page.locator('.conversation-list').waitFor();
  const chats=await read('conversations?source=demo-primary');
  const chat=chats.items.find(item=>item.username==='wxid_demo_0000');assert(chat);
  const messagePath='/admin/console/conversations/'+encodeURIComponent(chat.id)+'/messages';
  await page.route(url=>url.pathname===messagePath,async route=>{
   const response=await route.fetch();
   if(held)return route.fulfill({response});
   held=true;const old=await response.json();
   check('Held real backend snapshot predates marker',!old.items.some(item=>item.content===marker));
   report.initial_snapshot_total=old.total;report.snapshot_captured_at=new Date().toISOString();
   snapshotReady.resolve();await snapshotGate.promise;
   await route.fulfill({response});
  });
  await page.route(url=>url.pathname==='/admin/console/live/demo-primary',async route=>{
   const url=new URL(route.request().url());
   if(deltaCursor&&url.searchParams.get('after')===deltaCursor){report.cursor_advanced_while_snapshot_held=!!report.cursor_advanced_while_snapshot_held||!snapshotReleased;ackReady.resolve();}
   const response=await route.fetch(),value=await response.json();
   const item=value.items?.find(item=>item.message.content===marker);
   if(item){deltaCursor=value.cursor;report.delta_sequence=item.sequence;report.delta_received_at=new Date().toISOString();}
   else if(deltaCursor)postDeltaPolls++;
   await route.fulfill({response});if(item)deltaReady.resolve();
  });
  await page.locator('[data-conversation="'+chat.id+'"]').click();await snapshotReady.promise;
  check('Initial message panel remains loading',await page.locator('#message-list .initial-loading').count()===1);
  const response=await fetch(peer.url+'/_synthetic/append',{
   method:'POST',headers:{Authorization:'Bearer '+peer.token,'Content-Type':'application/json'},
   body:JSON.stringify({localId:900001,serverId:'synthetic-race-server-900001',chatId:'wxid_demo_0000',sender:'wxid_demo_0000',senderName:'合成竞态发言者',type:1,content:marker,timestamp:new Date().toISOString(),isSelf:false})
  });assert.equal(response.status,200);report.source_available_at=(await response.json()).available_at;
  await Promise.race([deltaReady.promise,pause(14000).then(()=>{throw Error('No collected live marker within 14s');})]);
  await page.locator('[data-conversation="'+chat.id+'"]').filter({hasText:marker}).waitFor();
  // Give the original implementation an opportunity to acknowledge the event.
  // A corrected implementation may defer acknowledgment until snapshot release.
  await Promise.race([ackReady.promise,pause(3500)]);
  report.loading_when_delta_applied=await page.locator('#message-list .initial-loading').count()===1;
  check('Live event applied to conversation list while initial snapshot is held',report.loading_when_delta_applied);
  const durable=await read('conversations/'+encodeURIComponent(chat.id)+'/messages?source=demo-primary&limit=100');
  report.durable_marker_count=durable.items.filter(item=>item.content===marker).length;
  check('Actual collector persisted the marker exactly once',report.durable_marker_count===1);
  snapshotReleased=true;snapshotGate.resolve();
  await page.locator('#message-list .initial-loading').waitFor({state:'detached'});
  await eventually(async()=>postDeltaPolls>=2,'two subsequent incremental polls');
  report.post_delta_empty_polls=postDeltaPolls;
  report.visible_without_refresh=await textMessage().count();
  report.observed_at=new Date().toISOString();
  await page.screenshot({path:path.join(out,'realtime-race-synthetic.png'),fullPage:true});
  if(report.visible_without_refresh!==1){
   // A diagnostic recovery proves this is page loss, not failed persistence.
   await page.locator('[data-conversation="'+chat.id+'"]').click();
   await page.locator('#message-list .initial-loading').waitFor({state:'detached'});
   await textMessage().waitFor();report.visible_after_explicit_reopen=await textMessage().count();
  }
  check('Marker arriving during initial snapshot appears without refresh',report.visible_without_refresh===1);
  check('No JavaScript page errors',report.page_errors.length===0);report.passed=true;
 }catch(error){report.passed=false;report.error=error.message;throw error;}
 finally{
  snapshotGate.resolve();
  for(const name of ['app.js','live.js'])report[name+'_sha256']=crypto.createHash('sha256').update(fs.readFileSync(path.join(__dirname,'../../src/cf_agent_gateway/management_console/static',name))).digest('hex');
  fs.writeFileSync(path.join(out,'realtime-race-result.json'),JSON.stringify(report,null,2));
  await browser.close();
 }
}
main().catch(error=>{console.error(error.stack);process.exitCode=1;});
