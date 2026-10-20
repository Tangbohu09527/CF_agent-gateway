/* Real page → real Gateway → unmodified official Hermes. Only model/WeChat are substitutes. */
const {chromium}=require(process.env.CF_PLAYWRIGHT_MODULE||'playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const runtime=path.resolve(process.env.CF_PROFILE_B_RUNTIME||'../runtime');
const out=path.join(runtime,'browser-evidence');fs.mkdirSync(out,{recursive:true});
const keys=JSON.parse(fs.readFileSync(path.join(runtime,'demo-secrets.json'),'utf8'));
const origin=process.env.CF_PROFILE_B_ORIGIN||'http://127.0.0.1:18760';
(async()=>{
 const browser=await chromium.launch({headless:true,...(process.env.CF_BROWSER_CHANNEL?{channel:process.env.CF_BROWSER_CHANNEL}:{})});
 const page=await browser.newPage({viewport:{width:1500,height:1000}});
 const errors=[],consoleErrors=[],responses=[];
 page.on('pageerror',e=>errors.push(e.message));
 page.on('console',m=>{if(m.type()==='error')consoleErrors.push(m.text());});
 const state=async()=>{const r=await page.request.get(origin+'/admin/profile-b/state',{headers:{Authorization:'Bearer '+keys.admin}});assert.equal(r.status(),200);return r.json();};
 const tab=async name=>page.getByRole('button',{name,exact:true}).click();
 async function submit(id,endpoint,expected=200){
   const [r]=await Promise.all([page.waitForResponse(r=>r.url().includes('/admin/profile-b/'+endpoint)&&r.request().method()!=='GET',{timeout:120000}),page.locator('#'+id+' button:not([type=button])').click()]);
   const value=await r.json();responses.push({endpoint,status:r.status(),value});
   await page.waitForFunction(()=>!document.querySelector('#refresh').disabled,{timeout:120000});
   assert.equal(r.status(),expected,JSON.stringify(value));return value;
 }
 const fill=async(form,values)=>{for(const[k,v]of Object.entries(values)){const e=page.locator('#'+form+' [name="'+k+'"]');if(typeof v==='boolean')await e.setChecked(v);else if(await e.evaluate(e=>e.tagName)==='SELECT')await e.selectOption(v);else await e.fill(v);}};
 try{
 await page.goto(origin+'/manage');
 await page.locator('#admin-token').fill(keys.admin);await page.locator('#login-form button').click();
 await page.locator('#workspace').waitFor({state:'visible'});await page.waitForFunction(()=>!document.querySelector('#refresh').disabled);
 assert.equal((await state()).employees.length,0,'Use a fresh isolated database for the page acceptance run');
 for(const[name,id,sender]of [['测试员工甲','employee-a','test-a'],['测试员工乙','employee-b','test-b']]){
   await fill('employee-form',{name,employee_id:id,sender_id:sender,bot_allowed:true,group_allowed:true});await submit('employee-form','employees');
   await page.locator('[data-reset="employee-form"]').click();
 }
 await tab('隔离消息验证');
 await fill('message-form',{sender_id:'test-a',conversation_id:'test-a',text:'CF_MSG_UNGROUPED'});
 const denied=await submit('message-form','test-messages',409);assert.equal(denied.detail,'authorized_but_ungrouped');assert.equal((await state()).derived.length,0);
 await tab('分组、群与节点');
 await fill('node-form',{name:'隔离官方 Hermes',connection_ref:'isolated-hermes'});await submit('node-form','nodes');
 await page.locator('#nodes-list button').click();await page.waitForFunction(()=>!document.querySelector('#refresh').disabled,{timeout:120000});
 let s=await state();assert.equal(s.nodes[0].status,'ready');const node=s.nodes[0].id;
 await tab('配置模板');
 for(const[kind,name,soul]of [['employee','员工测试模板','CF_SOUL_EMP_V1\n隔离员工测试行为说明'],['group','群测试模板','CF_SOUL_GROUP_V1\n隔离群测试行为说明']]){
  await fill('template-form',{kind,name,soul});await submit('template-form','templates');await page.locator('[data-reset="template-form"]').click();
 }
 s=await state();const empTemplate=s.templates.find(x=>x.kind==='employee'),roomTemplate=s.templates.find(x=>x.kind==='group');
 await tab('分组、群与节点');
 for(const[kind,name,template_id]of [['employee','员工测试组',empTemplate.id],['group','群测试组',roomTemplate.id]]){
  await fill('group-form',{kind,name,template_id,node_id:node});await submit('group-form','groups');await page.locator('[data-reset="group-form"]').click();
 }
 s=await state();const eg=s.groups.find(x=>x.kind==='employee').id,rg=s.groups.find(x=>x.kind==='group').id;
 await tab('员工管理');
 for(const name of ['测试员工甲','测试员工乙']){await page.locator('#employees-list tr').filter({hasText:name}).getByRole('button',{name:'编辑'}).click();await fill('employee-form',{group_id:eg});await submit('employee-form','employees/');}
 await tab('分组、群与节点');
 for(const[name,id]of [['测试群A','test-room-a'],['测试群B','test-room-b']]){
  await fill('room-form',{name,conversation_id:id,group_id:rg});await submit('room-form','rooms');await page.locator('[data-reset="room-form"]').click();
 }
 await tab('派生 Profile');s=await state();
 for(const[kind,subjects]of [['employee',s.employees],['group',s.rooms]])for(const item of subjects){await fill('derive-form',{kind,subject_id:item.id});await submit('derive-form','profiles/ensure');}
 s=await state();assert.equal(s.derived.length,4);assert(s.derived.every(x=>x.status==='ready'),JSON.stringify(s.derived));assert.equal(new Set(s.derived.map(x=>x.actual_profile)).size,4);
 const original=s.derived.map(x=>[x.id,x.actual_profile]);
 await submit('derive-form','profiles/ensure');assert.deepEqual((await state()).derived.map(x=>[x.id,x.actual_profile]),original);
 await page.screenshot({path:path.join(out,'profiles-created.png'),fullPage:true});
 await tab('隔离消息验证');
 async function message(sender_id,conversation_id,kind,text){await fill('message-form',{sender_id,conversation_id,kind,text});return submit('message-form','test-messages');}
 const a1=await message('test-a','test-a','private','CF_MSG_A1');
 const b1=await message('test-b','test-b','private','CF_MSG_B1');
 const ga1=await message('test-a','test-room-a','group','CF_MSG_GA1');
 const gb1=await message('test-a','test-room-b','group','CF_MSG_GB1');
 const a2=await message('test-a','test-a','private','CF_MSG_A2');assert.equal(a1.session_id,a2.session_id);assert.equal(a1.profile,a2.profile);
 const ga2=await message('test-b','test-room-a','group','CF_MSG_GA2');assert.equal(ga1.session_id,ga2.session_id);assert.notEqual(ga1.profile,a1.profile);assert.notEqual(ga1.profile,gb1.profile);
 assert.deepEqual(JSON.parse(a2.text).history_markers,['CF_MSG_A1','CF_MSG_A2']);
 assert.deepEqual(JSON.parse(ga2.text).history_markers,['CF_MSG_GA1','CF_MSG_GA2']);
 await tab('员工管理');await page.locator('#employees-list tr').filter({hasText:'测试员工乙'}).getByRole('button',{name:'编辑'}).click();await fill('employee-form',{group_allowed:false});await submit('employee-form','employees/');
 await tab('隔离消息验证');await fill('message-form',{sender_id:'test-b',conversation_id:'test-room-a',kind:'group',text:'CF_MSG_GROUP_DENIED'});const groupDenied=await submit('message-form','test-messages',403);assert.equal(groupDenied.detail,'employee_group_permission_denied');
 await message('test-b','test-b','private','CF_MSG_B2');
 await tab('配置模板');await page.locator('#templates-list tr').filter({hasText:'员工测试模板'}).getByRole('button',{name:'编辑'}).click();await fill('template-form',{soul:'CF_SOUL_EMP_V2\n已更新的隔离员工行为说明',language:'en'});await submit('template-form','templates/');
 await tab('派生 Profile');
 for(const item of (await state()).employees){await fill('derive-form',{kind:'employee',subject_id:item.id});await submit('derive-form','profiles/ensure');}
 s=await state();assert(s.derived.filter(x=>x.kind==='employee').every(x=>x.applied_version===2));assert(s.sessions.filter(x=>s.derived.find(d=>d.id===x.derived_id).kind==='employee').every(x=>x.effective_status==='existing_session_pending'));
 await page.screenshot({path:path.join(out,'template-synced-pending-session.png'),fullPage:true});
 await tab('隔离消息验证');const after=await message('test-a','test-a','private','CF_MSG_A3');assert.equal(after.session_id,a1.session_id);assert.deepEqual(JSON.parse(after.text).soul_markers,['CF_SOUL_EMP_V1']);assert.deepEqual(JSON.parse(after.text).history_markers,['CF_MSG_A1','CF_MSG_A2','CF_MSG_A3']);
 await page.reload();await page.locator('#admin-token').fill(keys.admin);await page.locator('#login-form button').click();await page.locator('#workspace').waitFor({state:'visible'});await page.waitForFunction(()=>!document.querySelector('#refresh').disabled);assert.equal((await state()).derived.length,4);
 await tab('员工管理');await page.screenshot({path:path.join(out,'employees.png'),fullPage:true});
 await tab('分组、群与节点');await fill('node-form',{name:'不可达测试节点',connection_ref:'unreachable-test-node'});await submit('node-form','nodes');await page.locator('#nodes-list tr').filter({hasText:'不可达测试节点'}).getByRole('button',{name:'检查能力'}).click();await page.waitForFunction(()=>!document.querySelector('#refresh').disabled,{timeout:120000});assert.equal((await state()).nodes.find(x=>x.connection_ref==='unreachable-test-node').status,'unavailable');assert(await page.locator('#notice').evaluate(e=>e.classList.contains('error')));
 assert.equal((await page.request.post(origin+'/admin/profile-b/nodes',{data:{name:'unauthorized',connection_ref:'isolated-hermes'}})).status(),401);
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
 assert.equal(errors.length,0,JSON.stringify(errors));
 assert(consoleErrors.every(x=>x.includes('Failed to load resource: the server responded with a status of 4')),JSON.stringify(consoleErrors));
 const html=await page.content();assert(!html.includes(keys.admin));assert(!JSON.stringify(await state()).includes(keys.hermes));
 fs.writeFileSync(path.join(out,'result.json'),JSON.stringify({passed:true,test_data:true,model:'deterministic substitute',wechat:'protocol test entry',errors,consoleErrors,responses,state:await state(),routes:{a1,b1,ga1,gb1,a2,ga2,after}},null,2));
 console.log('Browser acceptance passed: four real profiles, persistent routes, template sync and pending old-session effect.');
 }catch(e){await page.screenshot({path:path.join(out,'failure.png'),fullPage:true});fs.writeFileSync(path.join(out,'failure.json'),JSON.stringify({error:e.stack,errors,consoleErrors,responses},null,2));throw e;}finally{await browser.close();}
})().catch(e=>{console.error(e.stack);process.exitCode=1;});
