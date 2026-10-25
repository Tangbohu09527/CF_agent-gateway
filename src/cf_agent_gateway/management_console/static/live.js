'use strict';
window.CFConsoleLive = (() => {
 let context=null, timer=null, generation=0, cursor='', pending=0, lastSnapshot=null;
 let flight=null, refreshRequested=false, renderVersion=0, receivedAt=null, acceptedAt=null;
 const activeActions=new Set();
 const current=(ctx,gen,src)=>context===ctx&&generation===gen&&(!src||ctx.getSource()===src);
 const html=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
 const time=v=>v?new Date(v).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false}):'尚未检查';
 const words={online:'微信在线',offline:'微信离线',unknown:'微信状态未知',running:'采集运行',partial:'采集运行 · 存在资料缺口',disabled:'采集已停用',blocked:'采集受阻',stale:'采集检查过期',waiting_scan:'等待扫码',phone_confirm:'等待手机确认',connecting:'正在连接登录',verifying:'复核账号与资料',expired:'二维码已过期',failed:'登录失败',not_started:'尚未发起登录',already_online_preserved:'已在线，保留登录',available:'可用',unavailable:'不可用'};
 const reason={app_not_running:'微信进程未运行',logged_out:'尚未登录',session_expired:'登录失效',check_expired:'检查已过期',not_checked:'尚未检查',source_connection_failed:'微信接口连接失败',source_account_mismatch:'账号不符，已阻止接管',auth_observation_unrecognized:'当前微信界面未识别，不能确认在线；请检查登录页',management_channel_not_configured:'管理通道未配置',management_channel_unavailable:'管理通道不可达',target_login_identity_guard_not_verified:'目标微信缺少已验证的账号保护，受控扫码尚未接通',wechat_not_verified_online:'微信尚未确认在线',archive_source_check_failed:'消息读取未完成，请检查上游接口',collector_internal_error:'归档检查异常，请查看候选服务状态'};
 Object.assign(reason,{source_identity_unverified:'当前活动账号未核验，停止读取',source_messages_connection_failed:'会话消息接口超时或连接失败',source_messages_read_failed:'会话消息接口读取失败',source_messages_contract_invalid:'消息接口返回结构不符',source_messages_all_failed:'当前会话消息均未读到',candidate_commit_failed:'候选数据库提交失败',invalid_message_identity_or_schema:'消息身份或结构不完整'});
 reason.source_message_database_unreadable='微信消息库读取失败，采集受阻；登录在线不代表消息可读';
 const text=v=>reason[v]||words[v]||v||'未提供';
 const stages={login:'微信进程与真实登录',ownership:'采集所有权',upstream:'上游消息读取',normalization:'规范化与身份',persistence:'候选数据库提交'};
 const stageWords={normal:'正常',partial:'部分受阻',blocked:'受阻',error:'异常',disabled:'停用',unknown:'未知'};
 function chainHTML(trace){const rows=Object.entries(stages).map(([key,label])=>{const value=trace?.stages?.[key]||{},expired=!value.observed_at||Date.now()>Date.parse(value.observed_at)+(trace?.ttl_seconds||20)*1000;return `<div class="status-row"><div><h3>${html(label)} · ${html(expired?'未知 / 检查过期':stageWords[value.state]||'未知')}</h3><p>${html(text(value.reason))} · 观察 ${html(time(value.observed_at))} · 最近正常 ${html(time(value.last_success_at))}${value.failed_conversations?' · 读取失败会话 '+html(value.failed_conversations):''}${value.invalid_rows?' · 身份不完整 '+html(value.invalid_rows):''}</p></div></div>`;}).join('');const incidents=(trace?.incidents||[]).map(x=>`<p>${html(stages[x.stage]||x.stage)} · ${html(text(x.reason))} · ${x.recovered_at?'历史已恢复':'最近观察异常'} · ${html(x.count)} 次 · 首次 ${html(time(x.first_at))} · 最近 ${html(time(x.last_at))}${x.recovered_at?' · 恢复 '+html(time(x.recovered_at)):''}</p>`).join('')||'<p>尚无已记录的故障；这不代表缺失的历史消息已恢复。</p>';return `<div class="surface section-gap"><div class="surface-title"><h2>消息链路</h2><button id="copy-chain-diagnostic">复制诊断摘要</button></div><p>检查到期显示未知；最近检查成功与收到新消息分别记录。此处不启动服务，也不触发 AI。</p>${rows}<div class="status-row"><div><h3>当前浏览器增量</h3><p>本页最近接受响应 ${html(time(acceptedAt))} · 最近收到响应 ${html(time(receivedAt))}。仅代表当前页面；后端返回不等于其他终端已显示。</p></div></div><details><summary>近期故障记录</summary>${incidents}</details></div>`;}
 function stop(){generation++;renderVersion++;clearTimeout(timer);timer=null;context=null;cursor='';pending=0;lastSnapshot=null;flight=null;refreshRequested=false;receivedAt=null;acceptedAt=null;document.querySelector('#new-message-notice')?.remove();document.querySelector('#controlled-qr')?.replaceChildren();}
 async function start(ctx){stop();context=ctx;const gen=generation;await tick(gen);}
 function reset(){if(context){const ctx=context;return start(ctx);}}
 function lamp(bot){const element=document.querySelector('#bot-live');if(!element)return;const expires=Date.parse(bot?.expires_at),stale=!Number.isFinite(expires)||Date.now()>expires,state=stale||!['online','offline','unknown'].includes(bot?.state)?'unknown':bot.state;element.dataset.state=state;element.querySelector('span').textContent=words[state]||words.unknown;element.title=`${text(stale?'check_expired':bot.reason)}；检查：${time(bot?.observed_at)}；来源：真实认证/进程观察（演示模式为协议替身）`;}
 function schedule(gen,delay){if(!context||gen!==generation)return;clearTimeout(timer);timer=setTimeout(()=>{timer=null;void tick(gen);},delay);}
 async function tick(gen){
  const ctx=context;if(!ctx||gen!==generation)return;
  if(flight?.gen===gen){refreshRequested=true;return flight.promise;}
  clearTimeout(timer);timer=null;
  const src=ctx.getSource();if(!src){schedule(gen,2000);return;}
  const operation={gen,promise:null};flight=operation;
  operation.promise=(async()=>{
   let delay=2000;
   try{
    const value=await ctx.api('live/'+encodeURIComponent(src)+'?'+new URLSearchParams({after:cursor,limit:100}));
    if(!current(ctx,gen,src))return;
    receivedAt=new Date().toISOString();
    lastSnapshot=value;lamp(value.bot);
    // Commit the durable cursor only after this page accepts its changes.
    if(value.items.length&&await ctx.applyChanges(value.items)===false){delay=500;return;}
    if(!current(ctx,gen,src))return;
    cursor=value.cursor;
    acceptedAt=new Date().toISOString();
    const root=document.querySelector('#live-system');if(root)await renderSystem(root,gen,value);
    delay=value.has_more?50:2000;
   }catch(error){
    if(!current(ctx,gen,src))return;
    lamp(lastSnapshot?.bot);
    const connection=document.querySelector('#live-connection');if(connection)connection.textContent='增量连接失败，正在等待重连；已显示记录保留。';
    delay=4000;
   }finally{
    if(flight===operation)flight=null;
    if(current(ctx,gen,src)){const again=refreshRequested;refreshRequested=false;schedule(gen,again?0:delay);}
   }
  })();
  return operation.promise;
 }
 function staleTick(){if(context)lamp(lastSnapshot?.bot);}
 setInterval(staleTick,1000);
 function notice(count){pending+=count;let button=document.querySelector('#new-message-notice');if(!button){const list=document.querySelector('#message-list');if(!list)return;button=document.createElement('button');button.id='new-message-notice';button.className='new-message-notice';list.after(button);}button.textContent=`有 ${pending} 条新消息 · 查看最新`;const ctx=context,gen=generation;button.setAttribute('aria-live','polite');button.onclick=async()=>{if(!current(ctx,gen))return;clearNotice();await ctx.latest();};}
 function clearNotice(){pending=0;document.querySelector('#new-message-notice')?.remove();}
 async function action(button,path){
  const ctx=context,gen=generation,src=ctx?.getSource(),key=src+':'+path;
  if(!ctx||activeActions.has(key))return;
  activeActions.add(key);button.disabled=true;button.setAttribute('aria-busy','true');
  try{const result=await ctx.api(path,'POST',{});if(current(ctx,gen,src)){if(result.error||['failed','unsupported','unavailable','rejected'].includes(result.state))throw new Error('受控操作未完成：'+text(result.error||result.state));await tick(gen);}}
  catch(error){if(current(ctx,gen,src))ctx.toast(error.message,true);}
  finally{activeActions.delete(key);if(button.isConnected&&current(ctx,gen,src)){button.disabled=false;button.removeAttribute('aria-busy');}}
 }
 async function renderSystem(root,gen=generation,snapshot=lastSnapshot){const ctx=context;if(!ctx||!root)return;const src=ctx.getSource(),version=++renderVersion;let services,system,nodes,files;try{[services,system,nodes,files]=await Promise.all([ctx.api('services/'+encodeURIComponent(src)),ctx.api('live/system/snapshot'),ctx.api('nodes'),ctx.api('nodes/filebrowser-status')]);}catch(error){if(current(ctx,gen,src)&&root.isConnected&&version===renderVersion)root.textContent='运行状态读取失败：'+error.message;return;}if(!current(ctx,gen,src)||!root.isConnected||version!==renderVersion)return;const bot=snapshot?.bot||{},collect=snapshot?.collection||{},controller=services.controller||{},login=services.login||{};
 const capability=n=>n?.check?.expires_at&&Date.parse(n.check.expires_at)<=Date.now()?'检查已过期':text(n?.check?.status||'待检查');
 const tasks=system.archive_tasks||{},taskSource=tasks.sources?.[src],taskExpired=!tasks.observed_at||Date.now()>Date.parse(tasks.observed_at)+(tasks.ttl_seconds||20)*1000;
 const counts=values=>Object.entries(values||{}).map(([key,value])=>text(key)+' '+value).join('、')||'无记录';
 const taskSummary=taskExpired?'统计未知/已过期':taskSource?.state==='available'?'派发分状态：'+counts(taskSource.dispatch)+'；投递分状态：'+counts(taskSource.delivery):'该来源只读档案统计未取得';
 const row=(label,value)=>`<div class="status-row"><div><h3>${html(label)}</h3><p>${html(value)}</p></div></div>`;
 root.innerHTML=`<div class="surface"><div class="surface-title"><h2>微信运行与受控操作</h2><span id="live-connection">增量连接持续检查</span></div><p>微信登录、资料读取、消息采集、AI派发和投递分别判断。查看此页不启动任何服务。</p>${row(words[bot.state]||'微信状态未知',`${text(bot.reason)} · 检查 ${time(bot.observed_at)} · 有效 ${bot.ttl_seconds||20} 秒`)}${row('仅归档采集',`${text(collect.state)} · 最近检查 ${time(collect.last_check_at)} · 最近成功 ${time(collect.last_success_at)} · 最近收到消息 ${time(collect.last_message_at)}${collect.last_error?' · '+text(collect.last_error):''}`)}<p class="coverage">${html((collect.coverage||'尚未取得采集范围')+(collect.data_quality?.known_gap?' 已知资料缺口；本次窗口有 '+(collect.data_quality.skipped_invalid_rows||0)+' 条记录身份/格式不完整，未猜测身份或进入AI。':''))}</p>${row('管理通道',`${text(controller.state)} · 微信服务 ${text(controller.wechat_service||'unknown')} · 检查 ${time(controller.observed_at)}${controller.error?' · '+text(controller.error):''}`)}<div class="actions live-actions"><button data-live-action="check">立即检查微信</button><button data-live-action="service" ${!services.capabilities.start_service?'disabled':''}>检查并启动微信服务</button><button data-live-action="app" ${!services.capabilities.start_app?'disabled':''}>检查并启动微信进程</button><button data-live-action="login" ${!services.capabilities.login?'disabled':''}>登录 / 获取二维码</button><button data-live-action="collect">${collect.enabled?'停止仅归档采集':'启用仅归档采集'}</button></div><p class="help-text">启动保持已运行服务与现有登录；采集不启用AI、不发送、不下载附件原件。${html(services.capabilities.reason?text(services.capabilities.reason):'受控登录保留预期账号，不允许换号。')}</p><div id="controlled-login-state">${html(text(login.state))}${login.error?' · '+html(text(login.error)):''}</div><div id="controlled-qr"></div></div><div class="surface section-gap"><div class="surface-title"><h2>Gateway</h2></div>${row('管理API与候选数据库',`${system.gateway.management_api} · ${system.gateway.candidate_database} · 检查 ${time(system.observed_at)}`)}${row('正式采集 / 派发 / 投递',JSON.stringify(controller.workers||{state:'正式运行状态未取得'}))}${row('运行版本',system.gateway.revision)}${row('权限边界','候选配置尚未作用于正式派发；正式权限未核验、未迁移。')}${row('在途与任务统计',taskSummary+' · 检查 '+time(tasks.observed_at)+' · 来源：只读消息档案，累计完成数不代表在途')}</div><div class="surface section-gap"><div class="surface-title"><h2>文件系统</h2></div>${row('FileBrowser只读检查',`${text(files.state||files.status||'unknown')} · 检查 ${time(files.observed_at||files.checked_at)} · ${text(files.error||files.reason||'')}`)}${row('空间 / 挂载 / 可写能力','未采集；普通状态检查不向正式目录写文件。')}</div><div class="surface section-gap"><div class="surface-title"><h2>AI主机</h2></div>${(nodes.items||[]).map(n=>row(n.name,`来源 ${n.source||'本管理台'} · 业务 ${capability(n.business)} · Profile管理 ${capability(n.management)} · 详细分项见AI节点页`)).join('')||row('尚无可展示节点','已有配置需由服务器按受保护引用登记，不表示正式电脑不存在。')}</div>`;
 root.insertAdjacentHTML('beforeend',chainHTML(snapshot?.diagnostics));
 root.querySelector('#copy-chain-diagnostic').onclick=async()=>{try{await navigator.clipboard.writeText(JSON.stringify({version:system.gateway.revision,observed_at:snapshot?.diagnostics?.observed_at,collection:{state:collect.state,last_check_at:collect.last_check_at,last_success_at:collect.last_success_at,last_error:collect.last_error},stages:snapshot?.diagnostics?.stages||{},incidents:snapshot?.diagnostics?.incidents||[],current_browser:{received_at:receivedAt,accepted_at:acceptedAt}},null,2));ctx.toast('已复制脱敏诊断摘要');}catch{ctx.toast('浏览器未允许复制，请查看消息链路中的检查时间',true);}};
 root.querySelectorAll('[data-live-action]').forEach(button=>{button.onclick=()=>{const kind=button.dataset.liveAction;const path=kind==='check'?`live/${src}/check`:kind==='collect'?`live/${src}/collection/${collect.enabled?'stop':'start'}`:`services/${src}/${kind==='service'?'start-service':kind==='app'?'start-app':'login'}`;action(button,path);};const kind=button.dataset.liveAction,path=kind==='check'?'live/'+src+'/check':kind==='collect'?'live/'+src+'/collection/'+(collect.enabled?'stop':'start'):'services/'+src+'/'+(kind==='service'?'start-service':kind==='app'?'start-app':'login');if(activeActions.has(src+':'+path)){button.disabled=true;button.setAttribute('aria-busy','true');}});
 if(login.qr_available){try{const qr=await ctx.api('services/'+src+'/login/qr');if(!current(ctx,gen,src)||!root.isConnected||version!==renderVersion)return;const image=document.createElement('img');image.alt='当前Bot登录二维码，仅在此管理页面显示';image.className='controlled-qr';if(/^data:image\/png;base64,[A-Za-z0-9+/=]+$/.test(qr.data_url)){image.src=qr.data_url;root.querySelector('#controlled-qr').replaceChildren(image);}}catch{if(current(ctx,gen,src)&&root.isConnected&&version===renderVersion)root.querySelector('#controlled-qr').textContent='二维码已不可用，请重新检查。';}}
 }
 return {start,stop,reset,notice,clearNotice,renderSystem};
})();
