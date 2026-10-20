const {chromium}=require(process.env.CF_PLAYWRIGHT_MODULE||'playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path');
const runtime=path.resolve(process.env.CF_PROFILE_B_RUNTIME||'../runtime/demo');
const keys=JSON.parse(fs.readFileSync(path.join(runtime,'demo-secrets.json'),'utf8'));
const native=JSON.parse(fs.readFileSync(path.join(runtime,'native-result.json'),'utf8'));
const origin='http://127.0.0.1:18760';
(async()=>{
 const browser=await chromium.launch({headless:true,...(process.env.CF_BROWSER_CHANNEL?{channel:process.env.CF_BROWSER_CHANNEL}:{})});
 const page=await browser.newPage({viewport:{width:1500,height:1000}}),errors=[],consoleErrors=[];
 page.on('pageerror',e=>errors.push(e.message));page.on('console',m=>{if(m.type()==='error')consoleErrors.push(m.text());});
 const request=async()=>{const r=await page.request.get(origin+'/admin/profile-b/state',{headers:{Authorization:'Bearer '+keys.admin}});assert.equal(r.status(),200);return r.json();};
 const ready=()=>page.waitForFunction(()=>!document.querySelector('#refresh').disabled,{timeout:120000});
 try{
 await page.goto(origin+'/manage');await page.locator('#admin-token').fill(keys.admin);await page.locator('#login-form button').click();await page.locator('#workspace').waitFor({state:'visible'});await ready();
 const s=await request();assert.equal(s.derived.length,4);assert(s.derived.filter(x=>x.kind==='employee').every(x=>x.applied_version===3));
 const old=native.four_profile_disk_evidence.find(x=>x.user_messages.includes('CF_MSG_A1'));
 await page.getByRole('button',{name:'隔离消息验证',exact:true}).click();
 await page.locator('#message-form [name=sender_id]').selectOption('test-a');await page.locator('#message-form [name=conversation_id]').fill('test-a');await page.locator('#message-form [name=text]').fill('CF_MSG_AFTER_RESTART');
 const[r]=await Promise.all([page.waitForResponse(r=>r.url().endsWith('/test-messages'),{timeout:120000}),page.locator('#message-form button').click()]);assert.equal(r.status(),200);const result=await r.json();await ready();assert.equal(result.session_id,old.session_id);assert.equal(result.profile,old.profile);const model=JSON.parse(result.text);assert.deepEqual(model.soul_markers,['CF_SOUL_EMP_V1']);assert(model.history_markers.includes('CF_MSG_A1'));assert(model.history_markers.includes('CF_MSG_READ_ONLY'));assert(!model.history_markers.includes('CF_MSG_NEW_SESSION'));
 await page.getByRole('button',{name:'派生 Profile',exact:true}).click();await page.screenshot({path:path.join(runtime,'browser-evidence','final-profiles.png'),fullPage:true});
 const cleanErrors=[...consoleErrors];assert.deepEqual(errors,[]);assert.deepEqual(cleanErrors,[]);
 await page.route('**/admin/profile-b/state',route=>route.abort('connectionrefused'));await page.getByRole('button',{name:'刷新数据',exact:true}).click();await ready();assert((await page.locator('#notice').textContent()).includes('网络失败'));await page.unroute('**/admin/profile-b/state');await page.getByRole('button',{name:'刷新数据',exact:true}).click();await ready();assert(!(await page.locator('#notice').getAttribute('class')||'').includes('error'));
 const widths=[];for(const width of [1500,800,390]){await page.setViewportSize({width,height:1000});widths.push({width,overflow:await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth)});}assert(widths.every(x=>!x.overflow));
 fs.writeFileSync(path.join(runtime,'restart-result.json'),JSON.stringify({passed:true,real_browser:true,backend_restarted:true,same_hermes_session:true,old_soul_still_pending:true,stored_history_preserved:true,network_error_rendered_and_recovered:true,normal_console_errors:cleanErrors,page_errors:errors,widths,result,state:await request()},null,2));console.log('Restart and page persistence acceptance passed; no normal console errors; network failure/recovery and responsive layout verified.');
 }finally{await browser.close();}
})().catch(e=>{console.error(e.stack);process.exitCode=1;});
