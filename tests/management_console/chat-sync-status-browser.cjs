'use strict';
/* Synthetic transport faults only; the page, Gateway, collector and archive are real. */
const assert=require('node:assert/strict');
const path=require('node:path');

module.exports=async function chatSyncStatus({page,check,pause,eventually,nav,openChat,out}) {
 const pattern=url=>url.pathname==='/admin/console/live/demo-primary';
 const archived=new Date(Date.now()-120000).toISOString();
 const previousSuccess=new Date(Date.now()-60000).toISOString();
 let mode='blocked',frozenCheck=null,seen=0;
 const handler=async route=>{
  if(mode==='connection_failed'){seen++;return route.abort('failed');}
  const response=await route.fetch();
  const value=await response.json();
  const checked=frozenCheck||new Date().toISOString();
  value.collection={...value.collection,state:['aging','no_archive'].includes(mode)?'running':mode,
   enabled:mode!=='disabled',last_check_at:checked,
   last_success_at:mode==='blocked'?previousSuccess:checked,last_message_at:mode==='no_archive'?null:archived,
   last_error:mode==='blocked'?'source_message_database_unreadable':null};
  value.diagnostics={...value.diagnostics,ttl_seconds:mode==='aging'?2:20};
  seen++;
  await route.fulfill({response,json:value});
 };
 await page.route(pattern,handler);
 const banner=page.locator('#chat-sync-status');
 const state=async value=>{await eventually(async()=>await banner.getAttribute('data-state')===value);};
 try {
  await eventually(async()=>seen>0);
  await banner.waitFor({state:'visible',timeout:3500});
  await state('blocked');
  check('Chat pane exposes blocked synchronization while retaining archived records',
   await banner.innerText().then(t=>t.includes('消息同步受阻')&&t.includes('当前展示已归档记录')&&t.includes('微信消息库读取失败')));
  check('Chat pane separates actual archive, last check and successful empty checks',
   await banner.locator('[data-sync-time=archive]').getAttribute('datetime')===archived&&
   await banner.locator('[data-sync-time=success]').getAttribute('datetime')===previousSuccess&&
   await banner.locator('[data-sync-time=check]').getAttribute('datetime')!==previousSuccess);
  for(const width of [1440,430]){
   await page.setViewportSize({width,height:1000});
   await page.screenshot({path:path.join(out,'chat-sync-blocked-'+width+'-synthetic.png'),fullPage:true});
  }
  await page.setViewportSize({width:1440,height:1000});

  const count=await page.locator('#message-list [data-message-id]').count();
  await page.locator('#message-filter [name=q]').fill('合成未提交筛选');
  await page.locator('#message-list').evaluate(e=>{e.dataset.statusPreservation='same-node';e.scrollTop=0;});
  const scroll=await page.locator('#message-list').evaluate(e=>e.scrollTop);
  mode='running';
  await state('running');
  check('Successful empty collection checks never claim a new message was received',
   await banner.innerText().then(t=>t.includes('采集检查正常')&&!t.includes('收到新消息'))&&
   await banner.locator('[data-sync-time=archive]').getAttribute('datetime')===archived);
  check('Status recovery preserves message DOM, reading position and current filter',
   await page.locator('#message-list').getAttribute('data-status-preservation')==='same-node'&&
   await page.locator('#message-list [data-message-id]').count()===count&&
   Math.abs(await page.locator('#message-list').evaluate(e=>e.scrollTop)-scroll)<2&&
   await page.locator('#message-filter [name=q]').inputValue()==='合成未提交筛选');
  mode='no_archive';
  await eventually(async()=>await banner.locator('[data-sync-time=archive]').getAttribute('datetime')===null);
  check('Successful collection with no archived message has no invented receipt time',
   await banner.getAttribute('data-state')==='running'&&
   await banner.locator('[data-sync-time=archive]').innerText()==='未取得实际归档时间');
  mode='unknown';await state('unknown');
  check('A fresh unknown result is not described as an expired check',await banner.innerText().then(t=>t.includes('无法确认当前采集状态')&&!t.includes('检查已过期')));

  mode='partial';await state('partial');
  check('Partial collection has a distinct warning',await banner.innerText().then(t=>t.includes('消息同步部分受阻')));
  mode='disabled';await state('disabled');
  check('Disabled collection is described as stopped rather than a fault',await banner.innerText().then(t=>t.includes('消息同步已停用')));

  mode='aging';frozenCheck=new Date().toISOString();await state('running');
  await state('unknown');
  check('An expired collection check becomes unknown without changing the archive timestamp',
   await banner.innerText().then(t=>t.includes('检查已过期'))&&
   await banner.locator('[data-sync-time=archive]').getAttribute('datetime')===archived);
  frozenCheck=null;mode='blocked';await state('blocked');
  mode='connection_failed';await state('unknown');
  check('Increment transport failure is visible in the chat pane',await banner.innerText().then(t=>t.includes('增量连接失败')));
  mode='running';await state('running');
  check('Fresh returned collection evidence recovers the warning');

  for(const [width,height] of [[1440,1000],[1920,1080],[900,950],[430,900]]){
   await page.setViewportSize({width,height});
   await pause(100);
   check('Chat status fits '+width+'px without document overflow',await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth&&document.documentElement.scrollHeight<=innerHeight+1));
   await page.screenshot({path:path.join(out,'chat-sync-status-'+width+'-synthetic.png'),fullPage:true});
  }
  await page.setViewportSize({width:1440,height:1000});
  await banner.locator('[data-sync-diagnostics]').click();
  await page.locator('#live-system').waitFor();
  check('View diagnostics opens the existing system status page',await page.locator('[data-page=system]').getAttribute('aria-current')==='page');
  await nav('chats');await openChat('demo-primary');
  await page.locator('#source-select').selectOption('demo-secondary');
  await page.locator('#reading-panel .empty-reading').waitFor();
  check('Changing source removes the old source archive time',
   await page.locator('#reading-panel time[datetime="'+archived+'"]').count()===0);
  await page.locator('#source-select').selectOption('demo-primary');
  await page.locator('#reading-panel .empty-reading').waitFor();
  await openChat('demo-primary');
  await state('running');
 } finally {
  await page.unroute(pattern,handler);
 }
 assert.equal(await page.locator('#message-filter [name=q]').inputValue(),'');
};
