const $=s=>document.querySelector(s), $$=s=>[...document.querySelectorAll(s)];
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const api=async(path,options={})=>{if(options.body&&typeof options.body!=='string'){options.body=JSON.stringify(options.body);options.headers={'Content-Type':'application/json',...(options.headers||{})}}const r=await fetch(path,options);const type=r.headers.get('content-type')||'';const data=type.includes('json')?await r.json():await r.text();if(!r.ok)throw new Error(data.detail||data.message||JSON.stringify(data));return data};
// 软件窗口（Edge --app）没有地址栏和标签页，target="_blank" 只会在同一个
// 无地址栏的窗口里再开一层，看着就像「浏览器嵌进了软件界面」。这种窗口里的
// 外部链接一律交给系统默认浏览器；浏览器标签页模式本来就是真浏览器，不拦。
// 标记由托盘开窗时带上的 ?app=1 决定，不是靠 UA 猜。
const APP_WIN=new URLSearchParams(location.search).get('app')==='1';
if(APP_WIN)document.addEventListener('click',event=>{
  const link=event.target instanceof Element?event.target.closest('a[target="_blank"]'):null;
  if(!link||!link.href)return;
  event.preventDefault();event.stopPropagation();
  api('/api/system/open-url',{method:'POST',body:{url:link.href}}).catch(err=>toast(err.message));
},true);
// 离线页在自己的 iframe 里，上面那个顶层监听看不到它。页面会把外链 postMessage
// 过来（见 archive._reader_html），这里接住再交给系统浏览器——绝不让 iframe 就地
// 跳到微信的错误页，那边没有后退按钮，用户只能把整个阅读页叉掉。
window.addEventListener('message',event=>{
  if(event.origin!==location.origin)return;
  const data=event.data;
  if(!data||data.type!=='radar:open-url'||typeof data.url!=='string')return;
  if(!/^https?:\/\//i.test(data.url))return;
  if(APP_WIN){api('/api/system/open-url',{method:'POST',body:{url:data.url}}).catch(err=>toast(err.message));return}
  window.open(data.url,'_blank','noopener');
});
// 兜底：万一页面还是被导航走了（浏览器错误页），把它拉回本地快照。
// 首次 about:blank 也会触发 load，所以只在真正加载过离线页之后才复位。
function guardArchiveFrame(frame){
  if(!frame||frame.dataset.guarded)return;
  frame.dataset.guarded='1';
  frame.addEventListener('load',()=>{
    let inside=false;
    try{inside=frame.contentWindow.location.pathname.startsWith('/api/items/')}catch(e){inside=false}
    if(inside){frame.dataset.ready='1';return}
    if(!frame.dataset.ready)return;
    const src=frame.dataset.src||frame.getAttribute('src');
    if(src)frame.src=src;
  });
}
/* 提示条：必须挂在「最上层 modal」的子树里，否则等于没提示。
   阅读工作台与精读页都是 showModal()，它们进 top layer 之后，留在 body 里的浮层
   会被遮罩压在下面——用户点完按钮什么也看不见，反馈就成了「点了没反应」。
   老 WebView2 没有 Popover API 时 `[popover]` 一直是 display:none，所以还要能退化成
   普通定位元素，光靠 showPopover() 会给出一片安静。 */
function toastHome(el){
  const home=(window.topModal&&window.topModal())||document.body;
  if(el.parentElement===home)return;
  const shown=el.matches(':popover-open');
  if(shown){try{el.hidePopover()}catch(e){}}
  home.appendChild(el);
  if(shown){try{el.showPopover()}catch(e){}}
}
const toast=message=>{
  const el=$('#toast');if(!el)return;
  toastHome(el);
  el.textContent=message;
  if(typeof el.showPopover==='function'){
    if(!el.matches(':popover-open')){try{el.showPopover()}catch(e){el.removeAttribute('popover')}}
  }else if(el.hasAttribute('popover')){
    el.removeAttribute('popover');       // 没有 Popover API：交给 .show 类控制显隐
  }
  el.classList.add('show');
  clearTimeout(el._hideTimer);
  el._hideTimer=setTimeout(()=>el.classList.remove('show'),3600);
};
// 模态关掉时提示条还留在它的子树里，会跟着一起消失；搬回 body，下次触发再挂回去。
document.addEventListener('close',event=>{
  const el=$('#toast');
  if(el&&event.target&&event.target.contains&&event.target.contains(el))document.body.appendChild(el);
},true);
const time=value=>value?new Date(value).toLocaleString('zh-CN',{hour12:false}):'—';
const bool=value=>value?'true':'false';
let currentView='dashboard', currentPapers=[];
let taskFilter='active',taskRefreshTimer=null;
const TASK_LABELS={poll_source:'采集检查',archive_item:'归档全文',papers_item:'论文下载与解析',analyze_item:'WorkBuddy 精读',flush_batch:'整理待发送汇报',deliver_batch:'发送汇报'};
const TASK_STATES={READY:'排队中',LEASED:'执行中',RETRY_WAIT:'等待重试',SUCCEEDED:'已完成',DEAD:'失败',CANCELLED:'已取消'};
const TASK_PROGRESS={READY:12,LEASED:62,RETRY_WAIT:32,SUCCEEDED:100,DEAD:100,CANCELLED:0};
/* 左下角的「数据位置」必须现取现显示：写死在 HTML 里会让新电脑上的安装包
   也显示开发机那台 E 盘，看着像装错了地方。点击可复制，方便按路径找文件。 */
function applyStoragePath(dir){
  const code=$('#storagePath');
  if(!code||!dir||code.dataset.path===dir)return;
  code.dataset.path=dir;code.textContent=dir;code.title=dir+'\n点击复制路径';
  const copy=async()=>{
    try{await navigator.clipboard.writeText(dir);toast('数据目录已复制')}catch(e){toast(dir)}
  };
  code.onclick=copy;
}
async function loadHealth(){try{const h=await api('/api/health');applyStoragePath(h.data_dir);window.__radarDataDir=h.data_dir||'';$('#healthDot').className='dot '+(h.worker_alive?'online':'offline');$('#healthText').textContent=h.worker_alive?`后台运行中 · ${h.jobs_pending} 个任务待处理`:'后台 Worker 未连接';$('#taskPendingBadge').textContent=h.jobs_pending;$('#taskQueueBtn').classList.toggle('alert',h.jobs_dead>0);const cards=[['全部资料',h.items],['文章',h.articles],['视频',h.videos],['已下载论文',h.papers_downloaded],['待人工论文',h.papers_needed],['异常 / 死信',`${h.errors+h.source_errors} / ${h.jobs_dead}`]];$('#stats').innerHTML=cards.map(([k,v])=>`<div class="stat"><strong>${v}</strong><span>${k}</span></div>`).join('');if($('#taskDrawer').classList.contains('open'))loadTaskQueue().catch(()=>{})}catch(e){$('#healthDot').className='dot offline';$('#healthText').textContent='管理进程未连接'}}
function openTaskDrawer(){taskFilter='active';$('#taskDrawer').classList.add('open');$('#taskBackdrop').classList.add('open');$('#taskDrawer').setAttribute('aria-hidden','false');loadTaskQueue().catch(e=>{$('#taskList').innerHTML=`<div class="task-empty">${esc(e.message)}</div>`})}
function closeTaskDrawer(){$('#taskDrawer').classList.remove('open');$('#taskBackdrop').classList.remove('open');$('#taskDrawer').setAttribute('aria-hidden','true')}
let taskSelected=new Set();
async function loadTaskQueue(){const data=await api('/api/jobs/summary?limit=180');const c=data.counts;window.taskCounts=c;const cards=[['进行中',data.active],['排队',c.ready],['执行中',c.running],['待重试',c.retry],['失败',c.failed]];$('#taskSummary').innerHTML=cards.map(([k,v])=>`<div class="task-mini"><b>${v}</b><span>${k}</span></div>`).join('');const states=taskFilter==='active'?['READY','LEASED','RETRY_WAIT']:taskFilter==='failed'?['DEAD']:taskFilter==='done'?['SUCCEEDED','CANCELLED']:null;const rows=data.jobs.filter(j=>!states||states.includes(j.state));
const alive=new Set(data.jobs.map(j=>j.id));
for(const id of [...taskSelected])if(!alive.has(id))taskSelected.delete(id);
const list=$('#taskList'),scrollTop=list.scrollTop,openIds=new Set([...list.querySelectorAll('.task-item')].filter(x=>x.querySelector('details[open]')).map(x=>x.dataset.jobId));list.innerHTML=rows.map(j=>{const cls=j.state==='DEAD'?'failed':j.state==='RETRY_WAIT'?'retry':j.state==='READY'?'ready':'';const progress=TASK_PROGRESS[j.state]??0;const detail=JSON.stringify(j.payload||{});const pick=j.state==='DEAD'?`<label class="task-pick" title="选中后可批量重试或删除"><input type="checkbox" data-task-pick="${j.id}"${taskSelected.has(j.id)?' checked':''}></label>`:'';return `<article class="task-item ${cls}" data-job-id="${j.id}"><div class="task-item-head">${pick}<div class="task-item-title"><div class="task-kind">${esc(TASK_LABELS[j.kind]||j.kind)}</div><div class="task-target">${esc(j.target_title||'系统任务')}</div></div><span class="badge ${j.state==='SUCCEEDED'?'ok':j.state==='DEAD'?'error':j.state==='LEASED'?'wait':''}">${esc(TASK_STATES[j.state]||j.state)}</span></div><div class="task-progress"><i style="width:${progress}%"></i></div><div class="task-meta"><span>尝试 ${j.attempts}/${j.max_attempts}</span><span>优先级 ${j.priority}</span><span>更新 ${time(j.updated_at)}</span></div>${j.last_error?`<div class="task-error">${esc(j.last_error)}</div>`:''}${j.state==='DEAD'?`<div class="actions" style="margin-top:7px"><button class="primary" data-job-diagnostic="${j.id}">查看诊断</button>${j.retryable?`<button data-job-retry="${j.id}">临时错误重试</button>`:''}</div>`:''}<details><summary>任务详情</summary><pre>${esc('任务ID：'+j.id+'\n类型：'+(TASK_LABELS[j.kind]||j.kind)+'\n计划：'+time(j.run_at)+'\n参数：'+detail)}</pre></details></article>`}).join('')||`<div class="task-empty">当前筛选下没有任务。可切换「失败」或「最近完成」查看历史。</div>`;list.scrollTop=scrollTop;for(const item of list.querySelectorAll('.task-item')){if(openIds.has(item.dataset.jobId)){const d=item.querySelector('details');if(d)d.open=true}}$('#taskUpdated').textContent='更新于 '+new Date().toLocaleTimeString('zh-CN',{hour12:false});
$('#taskBulkBar').hidden=taskFilter!=='failed';
$('#taskSelectedInfo').textContent=`已选 ${taskSelected.size} 条`;
const boxes=$$('#taskList [data-task-pick]');
$('#taskSelectAll').checked=boxes.length>0&&boxes.every(x=>x.checked);
$('#taskSelectAll').disabled=!boxes.length;
$('#taskRetrySelected').disabled=!taskSelected.size;
$('#taskDeleteSelected').disabled=!taskSelected.size;
$('#taskCleanFailed').disabled=!(c.failed>0);
const groups=taskFilter==='failed'?(data.failed_groups||[]):[];
const hint=$('#taskFailHint');
hint.hidden=!groups.length;
hint.innerHTML=groups.length?`<b>失败原因 Top</b>${groups.map(g=>`<span><i>${g.count}</i> ${esc((g.error||'').slice(0,72))}</span>`).join('')}`:''}
$('#taskQueueBtn').onclick=openTaskDrawer;$('#taskClose').onclick=closeTaskDrawer;$('#taskBackdrop').onclick=closeTaskDrawer;$('#taskRefresh').onclick=()=>loadTaskQueue().catch(e=>toast(e.message));document.addEventListener('keydown',e=>{if(e.key==='Escape')closeTaskDrawer()});document.querySelectorAll('[data-task-filter]').forEach(b=>b.onclick=()=>{taskFilter=b.dataset.taskFilter;document.querySelectorAll('[data-task-filter]').forEach(x=>x.classList.toggle('primary',x===b));loadTaskQueue().catch(e=>toast(e.message))});
function syncTaskPicked(){const boxes=$$('#taskList [data-task-pick]');$('#taskSelectedInfo').textContent=`已选 ${taskSelected.size} 条`;$('#taskSelectAll').checked=boxes.length>0&&boxes.every(x=>x.checked);$('#taskRetrySelected').disabled=!taskSelected.size;$('#taskDeleteSelected').disabled=!taskSelected.size}
$('#taskList').addEventListener('change',e=>{const box=e.target.closest('[data-task-pick]');if(!box)return;box.checked?taskSelected.add(box.dataset.taskPick):taskSelected.delete(box.dataset.taskPick);syncTaskPicked()});
$('#taskSelectAll').onchange=e=>{for(const box of $$('#taskList [data-task-pick]')){box.checked=e.target.checked;e.target.checked?taskSelected.add(box.dataset.taskPick):taskSelected.delete(box.dataset.taskPick)}syncTaskPicked()};
async function bulkJobAction(action,allFailed=false){const ids=[...taskSelected];if(!allFailed&&!ids.length)return toast('先勾选失败任务');const r=await api('/api/jobs/bulk',{method:'POST',body:{action,ids,all_failed:allFailed}});taskSelected.clear();toast(action==='retry'?`已重新排队 ${r.updated} 个任务`:`已删除 ${r.updated} 条失败记录`);await loadTaskQueue();loadHealth().catch(()=>{})}
$('#taskRetrySelected').onclick=()=>bulkJobAction('retry').catch(e=>toast(e.message));
$('#taskDeleteSelected').onclick=()=>{if(!taskSelected.size)return toast('先勾选失败任务');if(!confirm(`删除选中的 ${taskSelected.size} 条失败记录？\n\n只删任务记录，不动资料库里的内容和已归档的正文。`))return;bulkJobAction('delete').catch(e=>toast(e.message))};
$('#taskCleanFailed').onclick=()=>{const n=(window.taskCounts||{}).failed||0;if(!n)return toast('当前没有失败任务');if(!confirm(`删除全部 ${n} 条失败任务记录？\n\n只删任务记录，不动资料库里的内容和已归档的正文。`))return;bulkJobAction('delete',true).catch(e=>toast(e.message))};
async function loadEvents(){try{const rows=await api('/api/events?limit=60');$('#events').innerHTML=rows.map(x=>`<div class="event"><time>${time(x.created_at)}</time><span class="badge ${x.type.includes('error')?'error':''}">${esc(x.type)}</span><span>${esc(x.message)}</span></div>`).join('')||'<p class="muted">还没有运行事件</p>'}catch(e){toast(e.message)}}
/* ---------- 采集来源 ----------
   这一页只回答一个问题：装了哪些来源、这一趟跑得怎么样。
   新增与改配置都收进弹层，列表里只留状态卡片和动作——以前两个大表单摆在最上面，
   真正的「哪个来源挂了」被挤到屏幕外面去了。 */
const SOURCE_META={
  wechat_public_local:{label:'本机公众号消息库',short:'公众号',icon:'公',note:'零 API 费用'},
  wechat_local:{label:'本机微信聊天窗口',short:'聊天',icon:'聊',note:'只读单个窗口'},
  jsonl:{label:'本地分享桥',short:'分享桥',icon:'桥',note:'手机分享落到文件'},
  rss:{label:'RSS / Atom',short:'RSS',icon:'R',note:'订阅源'},
  bilibili_datax:{label:'B站（DataX）',short:'B站',icon:'B',note:'按次计费'},
  wechat_datax:{label:'公众号（DataX）',short:'DataX',icon:'D',note:'按次计费'},
  wecom_kf:{label:'企业微信客服',short:'企微',icon:'企',note:'接收转发'},
};
const SOURCE_EXAMPLES={
  rss:{url:'https://example.com/feed.xml',kind:'article'},
  wechat_public_local:{start_at_now:true,use_catalog:true,max_rows_per_poll:1000},
  wechat_local:{target_wxid:'对方的微信号',target_name:'聊天窗口名称',history_mode:'index_only'},
  jsonl:{path:'data/share_inbox.jsonl',start_at_end:false},
  wecom_kf:{corp_id:'ww...',open_kf_id:'wk...',start_at_now:true},
  bilibili_datax:{queries:['基因编辑 研究进展','蛋白质结构预测'],up_ids:[],sort:'latest',min_views:100,exclude_keywords:['课程','广告'],max_price_per_call:'0.01'},
  wechat_datax:{accounts:[{name:'公众号准确名称',id:'gh_原始编号'}],max_price_per_call:'0.05',initial_backfill_days:30,overlap_hours:24,max_pages_per_account_per_run:3,adaptive_polling:true},
};
const SOURCE_HINTS={
  rss:'url 是订阅地址；kind 填 article 或 video。RSSHub 的公众号路由也填在这里。',
  wechat_public_local:'use_catalog=true 表示只读「公众号库」里勾选的号；start_at_now=true 只追新，不回扫历史。',
  wechat_local:'只适配已验证的微信 3.x；history_mode=index_only 表示历史只建索引，新转发才抓全文。',
  jsonl:'path 是收件箱文件的绝对路径，start_at_end=true 表示只看启动之后新追加的行。',
  wecom_kf:'corp_id / open_kf_id 在企业微信后台拿；Secret 在「设置 → 参数设置」里单独保存。',
  bilibili_datax:'queries 是关键词，up_ids 是 UP 主 mid，两个填一个就够；max_price_per_call 是单次上限。',
  wechat_datax:'需要准确的账号名和 gh 编号；余额不足时接口返回 402，来源会被自动停用。',
};
let sourceRows=[],sourceQuery='',sourceFilter='',sourceEditId=null,sourceDraftType='rss';
function sourceMeta(type){return SOURCE_META[type]||{label:type||'未知类型',short:type||'其他',icon:'·',note:'自定义采集器'}}
function sourceState(x){return x.last_error?'error':(x.enabled?'on':'off')}
function ago(iso){
  if(!iso)return '还没成功过';
  const t=new Date(iso).getTime();
  if(!t)return '—';
  const sec=Math.max(0,(Date.now()-t)/1000);
  if(sec<90)return '刚刚';
  if(sec<3600)return Math.round(sec/60)+' 分钟前';
  if(sec<86400)return Math.round(sec/3600)+' 小时前';
  return Math.round(sec/86400)+' 天前';
}
function shortPath(value){const t=String(value||'');return t.length>34?'…'+t.slice(-32):t}
function sourceMetrics(x){
  const cells=[[x.poll_seconds+' 秒','检查间隔'],[ago(x.last_success_at),'上次成功']];
  const cfg=x.config||{};
  if(x.type==='wechat_public_local'||x.type==='wechat_datax'){
    cells.push([`${x.catalog_enabled||0} / ${x.catalog_total||0}`,'启用 / 库内总数']);
  }else if(x.type==='jsonl'){
    cells.push([cfg.path?shortPath(cfg.path):'没配路径','监听的收件箱']);
  }else if(x.type==='rss'){
    cells.push([cfg.url?shortPath(cfg.url):'没配地址','订阅地址']);
  }else if(x.type==='bilibili_datax'){
    cells.push([`${(cfg.queries||[]).length} 关键词 · ${(cfg.up_ids||[]).length} UP`,'监测范围']);
  }else if(x.type==='wecom_kf'){
    cells.push([cfg.open_kf_id||'没配客服账号','open_kf_id']);
  }else if(x.type==='wechat_local'){
    cells.push([cfg.target_name||cfg.target_wxid||'没配窗口','监测的聊天窗口']);
  }else{
    cells.push([Object.keys(cfg).length+' 项','配置项']);
  }
  return cells;
}
function sourceCard(x){
  const meta=sourceMeta(x.type),state=sourceState(x);
  const badge=state==='error'?`<span class="badge error">需要处理</span>`:state==='on'?`<span class="badge ok">运行中</span>`:`<span class="badge wait">已停用</span>`;
  const count=x.error_count>1?`<span class="badge wait" title="连续失败次数">连续 ${x.error_count} 次</span>`:'';
  const links=x.type==='wechat_public_local'&&x.catalog_linked?`<span class="badge ok">联动公众号库</span>`:'';
  const actions=[`<button class="primary" type="button" data-src-run="${x.id}">立即检查</button>`,
                 `<button type="button" data-src-edit="${x.id}">配置</button>`];
  if(x.type==='wechat_local')actions.push(`<button type="button" data-src-archive="${x.id}">历史全文归档</button>`);
  if(['wechat_public_local','wechat_datax'].includes(x.type))actions.push(`<button type="button" data-src-goto="wechat">去公众号库</button>`);
  return `<article class="src-card" data-state="${state}" data-src-card="${x.id}">
    <div class="src-card-head">
      <span class="src-icon" aria-hidden="true">${esc(meta.icon)}</span>
      <div class="src-title">
        <h3>${esc(x.name)}</h3>
        <div class="src-tags"><span class="badge">${esc(meta.label)}</span>${badge}${links}${count}</div>
      </div>
    </div>
    <p class="src-cap">${esc(x.capability||meta.note)}</p>
    <div class="src-metrics">${sourceMetrics(x).map(([v,k])=>`<div><b title="${esc(v)}">${esc(v)}</b><span>${esc(k)}</span></div>`).join('')}</div>
    ${x.last_error?`<div class="src-error">${esc(x.last_error)}</div>`:''}
    <div class="src-actions">${actions.join('')}
      <label class="switch" title="${x.enabled?'停用后不再调度，已归档的资料都还在':'重新开始调度'}"><input type="checkbox" data-src-toggle="${x.id}"${x.enabled?' checked':''}><span class="track"></span><span class="muted">${x.enabled?'启用':'停用'}</span></label>
    </div>
    <details class="src-raw"><summary>查看配置</summary><pre>${esc(JSON.stringify({type:x.type,poll_seconds:x.poll_seconds,config:x.config},null,2))}</pre></details>
  </article>`;
}
function renderSourceStats(){
  const on=sourceRows.filter(x=>x.enabled).length;
  const bad=sourceRows.filter(x=>x.last_error).length;
  const latest=sourceRows.map(x=>x.last_success_at).filter(Boolean).sort().pop();
  const cards=[['运行中',on],['已停用',sourceRows.length-on],['需要处理',bad],['最近成功',latest?ago(latest):'—']];
  $('#sourceStats').innerHTML=cards.map(([k,v])=>`<div class="stat"><strong>${esc(String(v))}</strong><span>${esc(k)}</span></div>`).join('');
  const alert=$('#sourceAlert');
  const broken=sourceRows.filter(x=>x.last_error);
  if(!broken.length){alert.hidden=true;alert.innerHTML='';return}
  alert.hidden=false;
  alert.innerHTML=`<b>${broken.length} 个来源需要你看一眼</b><ul>${broken.slice(0,4).map(x=>`<li>${esc(x.name)}：${esc((x.last_error||'').slice(0,120))}</li>`).join('')}</ul>
    <small>来源停用不会删除任何已归档的资料；修好后把卡片上的开关打开就会继续跑。</small>
    <button type="button" id="sourceOnlyBad">只看有故障的</button>`;
  const btn=$('#sourceOnlyBad');
  if(btn)btn.onclick=()=>{$('#srcFilter').value='error';sourceFilter='error';renderSources()};
}
function renderSources(){
  renderSourceStats();
  const q=sourceQuery.toLowerCase();
  const rows=sourceRows.filter(x=>{
    if(sourceFilter&&sourceState(x)!==sourceFilter)return false;
    if(!q)return true;
    return (x.name+' '+x.type+' '+sourceMeta(x.type).label+' '+JSON.stringify(x.config||{})).toLowerCase().includes(q);
  });
  $('#sourcesList').innerHTML=rows.map(sourceCard).join('')||
    `<div class="panel muted">${sourceRows.length?'没有符合筛选条件的来源。':'还没有来源。点右上角「新增来源」加一个 RSS 或分享桥。'}</div>`;
  $('#srcCount').textContent=sourceRows.length?`显示 ${rows.length} / ${sourceRows.length} 个来源`:'';
}
async function loadSources(){
  sourceRows=await api('/api/sources');
  renderSources();
}
function paintSourceTypes(){
  const grid=$('#sourceTypeGrid');
  if(!grid)return;
  grid.innerHTML=Object.entries(SOURCE_META).map(([type,meta])=>
    `<button type="button" class="src-type${type===sourceDraftType?' on':''}" data-src-type="${type}">
       <b>${esc(meta.label)}</b><span>${esc(meta.note)}</span>
     </button>`).join('');
}
function sourceExample(type){
  const base=SOURCE_EXAMPLES[type]||{};
  // share_inbox.jsonl 的真实位置随安装目录变，示例里不能写死某一台机器的盘符。
  if(type==='jsonl'&&window.__radarDataDir){
    return {...base,path:String(window.__radarDataDir).replace(/\\/g,'/')+'/share_inbox.jsonl'};
  }
  return base;
}
function applySourceDraft(type){
  sourceDraftType=type;
  $('#srcConfig').value=JSON.stringify(sourceExample(type),null,2);
  $('#srcPoll').value=type==='jsonl'?2:(['wecom_kf','wechat_local','wechat_public_local'].includes(type)?60:300);
  $('#srcConfigHint').textContent=SOURCE_HINTS[type]||'';
}
function openSourceDialog(source){
  sourceEditId=source?source.id:null;
  const dlg=$('#sourceDialog');
  $('#sourceDialogTitle').textContent=source?'配置来源':'新增来源';
  $('#sourceDialogLead').textContent=source
    ? '改完保存立刻生效。停用不会删除已归档的资料；类型不能改，要换类型请新建一个再停掉旧的。'
    : '先选一个类型，配置模板会自动填好，按需改几个字就能用。';
  $('#sourceTypeGrid').hidden=!!source;
  $('#srcEnabledLabel').hidden=!!source;
  $('#srcResult').textContent='';
  if(source){
    sourceDraftType=source.type;
    $('#srcName').value=source.name;
    $('#srcPoll').value=source.poll_seconds;
    $('#srcConfig').value=JSON.stringify(source.config||{},null,2);
    $('#srcConfigHint').textContent='类型：'+sourceMeta(source.type).label;
    paintSourceTypes();
  }else{
    $('#srcName').value='';
    applySourceDraft('rss');
    paintSourceTypes();
  }
  if(!dlg.open)dlg.showModal();
}
async function setSourceEnabled(id,enabled){
  await api('/api/sources/'+id,{method:'PATCH',body:{enabled}});
  toast(enabled?'已启用，下一轮开始调度':'已停用，已归档的资料都还在');
  await loadSources();
}
async function runSourceNow(id){
  await api('/api/sources/'+id+'/run',{method:'POST'});
  toast('已排入「立即检查」，几秒后在任务队列里能看到');
  loadHealth().catch(()=>{});
}
$('#srcQ').oninput=()=>{sourceQuery=$('#srcQ').value.trim();renderSources()};
$('#srcFilter').onchange=()=>{sourceFilter=$('#srcFilter').value;renderSources()};
$('#gotoSources').onclick=()=>switchView('sources');
$('#srcRefresh').onclick=()=>{loadSources().then(()=>toast('已刷新来源状态')).catch(e=>toast(e.message))};
$('#srcAdd').onclick=()=>openSourceDialog(null);
$('#sourceDialogClose').onclick=()=>$('#sourceDialog').close();
$('#sourceTypeGrid').onclick=e=>{
  const btn=e.target.closest('[data-src-type]');
  if(!btn)return;
  applySourceDraft(btn.dataset.srcType);
  paintSourceTypes();
};
$('#srcSave').onclick=async()=>{
  const name=$('#srcName').value.trim();
  if(!name)return toast('先给这个来源起个名字');
  let config;
  try{config=JSON.parse($('#srcConfig').value||'{}')}catch(e){
    $('#srcResult').textContent='配置不是合法 JSON：'+e.message;
    return;
  }
  const poll=Math.max(2,Number($('#srcPoll').value)||300);
  const button=$('#srcSave');
  button.disabled=true;
  try{
    if(sourceEditId){
      await api('/api/sources/'+sourceEditId,{method:'PATCH',body:{name,poll_seconds:poll,config}});
      toast('已保存，下一轮按新配置跑');
    }else{
      await api('/api/sources',{method:'POST',body:{name,type:sourceDraftType,poll_seconds:poll,config,enabled:$('#srcEnabled').value==='1'}});
      toast('已创建，后台会自动检查');
    }
    $('#sourceDialog').close();
    await loadSources();
  }catch(e){
    $('#srcResult').textContent=e.message;
  }finally{
    button.disabled=false;
  }
};
$('#sourcesList').addEventListener('click',async e=>{
  const run=e.target.closest('[data-src-run]');
  if(run)return runSourceNow(run.dataset.srcRun).catch(err=>toast(err.message));
  const edit=e.target.closest('[data-src-edit]');
  if(edit){
    const row=sourceRows.find(x=>x.id===edit.dataset.srcEdit);
    if(row)openSourceDialog(row);
    return;
  }
  const archive=e.target.closest('[data-src-archive]');
  if(archive)return api('/api/sources/'+archive.dataset.srcArchive+'/archive-history',{method:'POST'})
    .then(r=>toast(r.queued?`已开始归档，剩余 ${r.remaining} 条`:'没有待归档的条目'))
    .catch(err=>toast(err.message));
  const go=e.target.closest('[data-src-goto]');
  if(go)return switchView(go.dataset.srcGoto);
});
$('#sourcesList').addEventListener('change',e=>{
  const box=e.target.closest('[data-src-toggle]');
  if(!box)return;
  setSourceEnabled(box.dataset.srcToggle,box.checked).catch(err=>{toast(err.message);box.checked=!box.checked});
});
const WA_LABELS={LIFE_MOLECULAR:'生命科学基础',STRUCT_BIO:'结构生物学',CHEM_BIOL:'化学生物学',SYNTH_BIO:'合成生物学',OMICS_BIOINFO:'生信与组学',NEURO:'神经科学',BIOMED:'生物医学临床',CHEM_MAT:'化学与材料',PHYS_MATH:'物理数学天文',CS_AI:'计算机与AI',ENG_TECH:'工程与微流控',INTERDISCIPLINARY:'跨学科',SCI_COMM:'期刊与科学媒体',ACADEMIC_TOOL:'科研工具服务',CAMPUS_EDU:'校园教育',SOCIAL_HUMANITIES:'人文社科',LIFE_ENTERTAINMENT:'生活娱乐动漫',OTHER:'待判断'};
let waAll=[],waFiltered=[],waPage=0,waLoaded=false;const waSelected=new Set(),WA_PAGE_SIZE=60;
async function loadWechatAccounts(){const data=await api('/api/wechat/accounts?limit=2000');waAll=data.items;const s=data.stats||{};const cards=[['收录总数',s.total||0],['已启用',s.enabled||0],['CORE',s.core||0],['OPTIONAL',s.optional||0],['EXCLUDE',s.excluded||0],['已停用',(s.total||0)-(s.enabled||0)]];$('#wechatStats').innerHTML=cards.map(([k,v])=>`<div class="stat"><strong>${v}</strong><span>${k}</span></div>`).join('');if(!waLoaded){$('#waCategory').innerHTML='<option value="">全部分类</option>'+Object.entries(WA_LABELS).map(([k,v])=>`<option value="${k}">${v}</option>`).join('');waLoaded=true}applyWaFilter()}
async function syncLocalAccounts(){const b=$('#waSyncLocal');b.disabled=true;b.textContent='同步中…';try{const d=await api('/api/wechat/accounts/sync-local',{method:'POST',body:{}});toast(`本机关注 ${d.found} 个：新增 ${d.inserted}、更新 ${d.updated}${d.merged?`、归并 ${d.merged}`:''}`);await loadWechatAccounts()}finally{b.disabled=false;b.textContent='同步本机关注'}}
function applyWaFilter(){const q=$('#waQ').value.trim().toLowerCase(),cat=$('#waCategory').value,fit=$('#waFit').value,en=$('#waEnabled').value;waFiltered=waAll.filter(x=>(!q||(x.name+' '+x.description+' '+x.reason+' '+(x.recent_article?x.recent_article.title:'')+' '+WA_LABELS[x.category_code]).toLowerCase().includes(q))&&(!cat||x.category_code===cat)&&(!fit||x.fit===fit)&&(!en||String(x.enabled)===en));waPage=0;renderWechat()}
function renderWechat(){const pages=Math.max(1,Math.ceil(waFiltered.length/WA_PAGE_SIZE));waPage=Math.min(waPage,pages-1);const rows=waFiltered.slice(waPage*WA_PAGE_SIZE,(waPage+1)*WA_PAGE_SIZE);$('#waRows').innerHTML=rows.map(x=>`<tr><td><input type="checkbox" data-wa-check="${x.id}" ${waSelected.has(x.id)?'checked':''}></td><td><b class="wa-open" data-wa-open="${x.id}" title="点开看它最近的推文">${esc(x.name)}</b>${x.account_id?`<div class="update">${esc(x.account_id)}</div>`:''}</td><td><span class="badge">${esc(WA_LABELS[x.category_code]||x.category_code)}</span></td><td><span class="badge ${x.fit.toLowerCase()}">${x.fit}</span></td><td><span class="badge ${x.enabled?'ok':'wait'}">${x.enabled?'已启用':'已停用'}</span></td><td><span class="reason">${esc(x.reason||'—')}</span><br><span class="recent">${x.recent_article?`最近：${esc((x.recent_article.title||'').slice(0,70))} · ${time(x.recent_article.discovered_at)}`:'<span class="muted">本站还没收录它的文章</span>'}</span>${x.description?`<br><small class="muted">简介：${esc(x.description.slice(0,56))}</small>`:''}</td></tr>`).join('')||'<tr><td colspan="6" class="muted">没有匹配的公众号</td></tr>';$('#waPageInfo').textContent=`第 ${waPage+1}/${pages} 页 · 筛选结果 ${waFiltered.length} 个`;$('#waSelectedInfo').textContent=`已勾选 ${waSelected.size} 个`;$('#waHeaderCheck').checked=rows.length>0&&rows.every(x=>waSelected.has(x.id));$('#waPrev').disabled=waPage<=0;$('#waNext').disabled=waPage>=pages-1}
async function setWaEnabled(enabled){if(!waSelected.size)return toast('请先勾选公众号');const r=await api('/api/wechat/accounts/bulk',{method:'POST',body:{ids:[...waSelected],enabled}});toast(`已${enabled?'启用':'停用'} ${r.updated} 个公众号`+(enabled&&r.classified?`，自动分类 ${r.classified} 个`:'')+(enabled&&r.backfill_queued?`，${r.backfill_queued} 个补录任务已排队`:''));await loadWechatAccounts();if(enabled&&r.backfill_queued)setTimeout(()=>loadWechatAccounts().catch(()=>{}),8000)}
$('#waQ').oninput=applyWaFilter;$('#waSearch').onclick=applyWaFilter;$('#waCategory').onchange=applyWaFilter;$('#waFit').onchange=applyWaFilter;$('#waEnabled').onchange=applyWaFilter;$('#waPrev').onclick=()=>{waPage--;renderWechat()};$('#waNext').onclick=()=>{waPage++;renderWechat()};$('#waEnable').onclick=()=>setWaEnabled(true).catch(e=>toast(e.message));$('#waDisable').onclick=()=>setWaEnabled(false).catch(e=>toast(e.message));$('#waCheckPage').onclick=()=>{waFiltered.slice(waPage*WA_PAGE_SIZE,(waPage+1)*WA_PAGE_SIZE).forEach(x=>waSelected.add(x.id));renderWechat()};$('#waUncheckPage').onclick=()=>{waSelected.clear();renderWechat()};$('#waCheckAll').onclick=()=>{waFiltered.forEach(x=>waSelected.add(x.id));renderWechat()};$('#waSyncLocal').onclick=()=>syncLocalAccounts().catch(e=>toast(e.message));$('#waHeaderCheck').onchange=e=>{waFiltered.slice(waPage*WA_PAGE_SIZE,(waPage+1)*WA_PAGE_SIZE).forEach(x=>e.target.checked?waSelected.add(x.id):waSelected.delete(x.id));renderWechat()};
$('#waRows').addEventListener('change',e=>{if(e.target.matches('[data-wa-check]')){const id=+e.target.dataset.waCheck;e.target.checked?waSelected.add(id):waSelected.delete(id);$('#waSelectedInfo').textContent=`已勾选 ${waSelected.size} 个`}});

/* ---------- 公众号近期推文面板 ----------
   数据来自本机微信的公众号消息库（PublicMsg.db），所以能看到很多还没进雷达的推文，
   那正是「补录」的来源。补录＝建条目＋排队抓正文，排除＝记住原因并可选地建一条
   该号专属的过滤规则（以后同类标题在入库那一刻就被挡住）。 */
let waPanelAcc=null,waPanelItems=[],waPanelRules=[],waPanelOffset=0,waPanelDays=365,waPanelHasMore=false;
let waPollToken=0,waOpenedAt=0,waPollTimer=null;
const waPanelSelected=new Set();
let waPresets=[],waExcludeTargets=[];
const WA_PANEL_PAGE=24;

const waStateTag={archived:'<span class="wa-tag ok">已收录</span>',index_only:'<span class="wa-tag info">仅索引</span>',
  excluded:'<span class="wa-tag warn">已排除</span>',hint:'<span class="wa-tag wait">仅摘要</span>',new:''};

function waCard(item){
  const state=item.state||'new';
  // 封面走本机代理：微信图床有防盗链，浏览器直连只会拿到"不可引用"的占位图。
  const cover=item.cover?`<img class="wa-cover-img" src="/api/wechat/cover?u=${encodeURIComponent(item.cover)}" alt="" loading="lazy" onerror="this.remove()">`:'';
  const initial=`<span class="wa-initial">${esc((item.title||'#').slice(0,1))}</span>`;
  const detail=item.detail||{};
  let note='';
  if(state==='excluded')note=`<div class="wa-note">${esc(detail.reason||'已排除')}</div>`;
  else if(state==='hint')note='<div class="wa-note">微信只保留了这条会话摘要，正文与链接没有落盘，无法补录。</div>';
  else if(item.rule_note)note=`<div class="wa-note">按现有规则不会收录：${esc(item.rule_note)}</div>`;
  const tools=[];
  if(state==='new')tools.push('<button type="button" data-wa-act="import">补录</button>');
  if(state==='index_only')tools.push('<button type="button" data-wa-act="import">补全文</button>');
  if(state==='archived'&&detail.item_id)tools.push('<button type="button" data-wa-act="go">在资料库</button>');
  if(state==='excluded')tools.push('<button type="button" data-wa-act="restore">恢复</button>');
  if(state!=='excluded'&&state!=='hint')tools.push('<button type="button" class="danger" data-wa-act="exclude">排除</button>');
  const picked=waPanelSelected.has(item.canonical_url)?' picked':'';
  const title=item.url
    ? `<a class="wa-title" href="${esc(item.url)}" target="_blank" rel="noreferrer" title="${esc(item.summary||'')}">${esc(item.title)}</a>`
    : `<span class="wa-title" title="${esc(item.summary||'')}">${esc(item.title)}</span>`;
  return `<article class="wa-card${picked}" data-state="${state}" data-wa-key="${esc(item.canonical_url)}">
    <div class="wa-cover ${item.cover?'':'noimg'}">${initial}${cover}<span class="wa-pick">✓</span>${waStateTag[state]||''}</div>
    <div class="wa-body">
      ${title}
      ${note}
      <div class="wa-meta"><span>${time(item.published_at).slice(0,10)}</span></div>
      <div class="wa-tools">${tools.join('')}</div>
    </div>
  </article>`;
}

async function openWaPanel(id){
  const acc=waAll.find(x=>x.id===id);
  if(!acc)return;
  waPanelAcc=acc;waPanelSelected.clear();waPanelOffset=0;waPanelItems=[];waPanelRules=[];
  waPollToken++;                       // 换号就把上一个号的轮询作废
  waOpenedAt=Date.now();
  const cat=$('#waPanelCategory');
  cat.innerHTML=Object.entries(WA_LABELS).map(([k,v])=>`<option value="${k}">${esc(v)}</option>`).join('');
  cat.value=acc.category_code||'OTHER';
  $('#waPanelFit').value=acc.fit||'OPTIONAL';
  $('#waPanelEnabled').checked=!!acc.enabled;
  $('#waPanelName').textContent=acc.name;
  $('#waPanelMeta').innerHTML=`${acc.account_id?`<span class="badge">${esc(acc.account_id)}</span>`:''}<span>${esc(acc.reason||'')}</span>`;
  $('#waPanelStatus').textContent='正在打开…';
  $('#waPanelSub').textContent='';
  $('#waArticlesGrid').innerHTML=waSkeleton();
  $('#waPanelMore').hidden=true;$('#waPanelCount').textContent='';
  syncWaPanelPicked();
  if(!$('#waArticles').open)$('#waArticles').showModal();
  await loadWaPanel(true);
}
function closeWaPanel(){waPollToken++;const d=$('#waArticles');if(d.open)d.close()}
$('#waPanelRefresh').onclick=()=>loadWaPanel(true,true).catch(e=>toast(e.message));

/* 详情是两段式的：服务端先给缓存（几十毫秒就回来），重活排在后台线程。
   这边看到 refreshing 就每 1.2 秒再问一次，直到列表补齐；期间显示已经等了
   几秒，免得"没反应"被误解成卡死。 */
function waSkeleton(){
  return Array.from({length:3},()=>`<article class="wa-card wa-skel">
    <div class="wa-cover noimg"><span class="wa-skel-box"></span></div>
    <div class="wa-body"><span class="wa-skel-line w80"></span><span class="wa-skel-line w60"></span><span class="wa-skel-line w40"></span></div>
  </article>`).join('');
}

function stopWaPoll(){if(waPollTimer){clearTimeout(waPollTimer);waPollTimer=null}}
function scheduleWaPoll(token){
  stopWaPoll();
  waPollTimer=setTimeout(()=>{
    if(token!==waPollToken)return;
    loadWaPanel(true,false,true).catch(()=>{});
  },1200);
}

function waAgo(iso){
  if(!iso)return '';
  const t=new Date(iso).getTime();
  if(!Number.isFinite(t))return '';
  const s=Math.max(0,(Date.now()-t)/1000);
  if(s<90)return '刚刚';
  if(s<3600)return `${Math.round(s/60)} 分钟前`;
  if(s<86400)return `${Math.round(s/3600)} 小时前`;
  return `${Math.round(s/86400)} 天前`;
}

function waSubLine(d){
  const s=d.sources||{},bits=[];
  if(d.channel)bits.push(`通道 ${d.channel}`);
  if(s.wechat_running===false)bits.push('微信当前未运行，只能看本地归档');
  else if(s.wechat_running)bits.push('微信运行中');
  if(s.db_synced_at)bits.push(`微信库 ${waAgo(s.db_synced_at.replace(' ','T'))}同步`);
  if(s.in_contacts===false&&s.wechat_running)bits.push('本机关注列表里没有这个号');
  if(s.newest_item_display){
    const days=s.days_since;
    bits.push(`最后一条 ${s.newest_item_display.slice(0,10)}${Number.isFinite(days)&&days>=7?`（${days} 天前，本机微信里没有更新的记录）`:''}`);
  }else if(s.hint_at_display){
    bits.push(`微信 ${s.hint_at_display} 收到过推送，但正文没落盘`);
  }
  if(d.cached&&d.cache_age!==undefined)bits.push(`本机缓存 ${Math.max(0,Math.round((d.cache_age||0)/60))} 分钟前`);
  return bits.join(' · ');
}

async function loadWaPanel(reset,refresh,poll){
  if(!waPanelAcc)return;
  const token=waPollToken;
  if(reset&&!poll){waPanelOffset=0;waPanelItems=[];}
  const more=$('#waPanelMore');more.hidden=true;more.disabled=true;
  const offset=poll?0:waPanelOffset;
  try{
    let url=`/api/wechat/accounts/${waPanelAcc.id}/articles?limit=${WA_PANEL_PAGE}&offset=${offset}&days=${waPanelDays}`;
    if(refresh)url+='&refresh=1';
    const data=await api(url);
    if(token!==waPollToken)return;                 // 期间换了号，丢弃这次结果
    if(data.error){
      $('#waPanelStatus').textContent=data.error;
      $('#waPanelSub').textContent='';
      if(reset||poll)$('#waArticlesGrid').innerHTML='<div class="wa-empty">这台电脑上暂时看不到它的推文</div>';
      stopWaPoll();
      return;
    }
    const list=poll?(data.items||[]):(reset?(data.items||[]):waPanelItems.concat(data.items||[]));
    waPanelItems=list;
    waPanelRules=data.rules||[];
    waPanelHasMore=!!data.has_more;
    waPanelOffset=(poll||reset)?WA_PANEL_PAGE:offset+(data.items||[]).length;
    const bits=[`共 ${data.total} 篇`];
    bits.push(`资料库已收录 ${waPanelItems.filter(x=>x.state==='archived'||x.state==='index_only').length} 篇`);
    if(waPanelRules.length)bits.push(`本号排除规则 ${waPanelRules.length} 条`);
    if(data.memory_cards)bits.push(`盒子卡片 ${data.memory_cards} 条`);
    if(poll)bits.push(`已等 ${Math.round((Date.now()-waOpenedAt)/1000)} 秒`);
    if(data.category_note){
      bits.push(data.category_note);
      setTimeout(()=>loadWechatAccounts().catch(()=>{}),0);
    }
    let note=data.note||'';
    if(!note&&data.refreshing)note='首次读取要扫一遍微信进程内存，通常几秒；读到之后会自己刷新，不用关掉重开。';
    $('#waPanelStatus').textContent=[note,...bits].filter(Boolean).join(' · ');
    $('#waPanelSub').textContent=waSubLine(data);
    if(data.account&&data.account.category_code){
      waPanelAcc.category_code=data.account.category_code;
      const pick=$('#waPanelCategory');
      if(pick&&[...pick.options].some(o=>o.value===data.account.category_code))pick.value=data.account.category_code;
    }
    if(data.refreshing&&!waPanelItems.length){
      if(poll)$('#waPanelStatus').textContent=`正在读取本机微信… 已等 ${Math.round((Date.now()-waOpenedAt)/1000)} 秒`;
      $('#waArticlesGrid').innerHTML=waSkeleton();
    }else{
      $('#waArticlesGrid').innerHTML=waPanelItems.map(waCard).join('')
        ||`<div class="wa-empty">${esc(note||'这段时间里它没有推文')}</div>`;
    }
    more.hidden=!(waPanelHasMore&&!data.refreshing);
    $('#waPanelCount').textContent=waPanelHasMore?`已显示 ${waPanelItems.length} / ${data.total} 篇`:`共 ${waPanelItems.length} 篇`;
    if(data.refreshing)scheduleWaPoll(token); else stopWaPoll();
  }catch(e){
    if(token===waPollToken)$('#waPanelStatus').textContent=e.message;
    stopWaPoll();
  }finally{
    more.disabled=false;
  }
}

function syncWaPanelPicked(){
  for(const card of $$('#waArticlesGrid .wa-card')){
    card.classList.toggle('picked',waPanelSelected.has(card.dataset.waKey));
  }
  $('#waPanelSelected').textContent=`已选 ${waPanelSelected.size} 篇`;
}
function toggleWaPick(key){
  waPanelSelected.has(key)?waPanelSelected.delete(key):waPanelSelected.add(key);
  syncWaPanelPicked();
}

async function waImport(articles){
  if(!articles.length)return toast('先勾选要补录的推文');
  try{
    const r=await api(`/api/wechat/accounts/${waPanelAcc.id}/articles/import`,{method:'POST',body:{articles}});
    toast(`已补录 ${r.created} 篇，正在排队抓正文`+(r.existing?`（${r.existing} 篇本来就有）`:''));
    waPanelSelected.clear();
    await loadWaPanel(true);
    loadHealth().catch(()=>{});
  }catch(e){toast(e.message)}
}
async function waRestore(articles){
  try{
    const r=await api(`/api/wechat/accounts/${waPanelAcc.id}/articles/restore`,{method:'POST',body:{articles}});
    toast(`已恢复 ${r.restored} 篇`);
    waPanelSelected.clear();
    await loadWaPanel(true);
  }catch(e){toast(e.message)}
}

async function openWaExclude(articles){
  if(!articles.length)return toast('先勾选要排除的推文');
  waExcludeTargets=articles;
  if(!waPresets.length){
    try{waPresets=(await api('/api/wechat/exclude-presets')).presets||[]}catch(e){waPresets=[]}
  }
  const sel=$('#waExcludePreset');
  sel.innerHTML=waPresets.map((p,i)=>`<option value="${i}">${esc(p.label)}</option>`).join('')||'<option value="0">手动排除</option>';
  sel.value='0';
  applyWaPreset();
  $('#waExcludeTitle').textContent=`排除 ${articles.length} 篇 · ${waPanelAcc.name}`;
  $('#waExcludeCount').textContent=waPanelRules.length
    ?`本号现有 ${waPanelRules.length} 条排除规则：${waPanelRules.map(r=>`${r.patterns}（命中 ${r.hits}）`).join('；')}`
    :'本号还没有排除规则。';
  $('#waExcludeDisable').checked=false;
  if(!$('#waExcludeDialog').open)$('#waExcludeDialog').showModal();
}
function applyWaPreset(){
  const preset=waPresets[+$('#waExcludePreset').value]||{keywords:[]};
  $('#waExcludeKeywords').value=(preset.keywords||[]).join('、');
}

$('#waPanelClose').onclick=closeWaPanel;
// Esc 关窗不经过 closeWaPanel，补一个 close 事件把轮询停掉，否则它会一直问下去。
$('#waArticles').addEventListener('close',()=>{waPollToken++;stopWaPoll()});
$('#waPanelMore').onclick=()=>loadWaPanel(false).catch(e=>toast(e.message));
$('#waPanelRange').onchange=e=>{waPanelDays=+e.target.value;waPanelSelected.clear();loadWaPanel(true).catch(e=>toast(e.message))};
$('#waPanelSelectNew').onclick=()=>{waPanelItems.filter(x=>x.state==='new'||x.state==='index_only').forEach(x=>waPanelSelected.add(x.canonical_url));syncWaPanelPicked()};
$('#waPanelSelectNone').onclick=()=>{waPanelSelected.clear();syncWaPanelPicked()};
$('#waPanelImport').onclick=()=>waImport(waPanelItems.filter(x=>waPanelSelected.has(x.canonical_url)));
$('#waPanelExclude').onclick=()=>openWaExclude(waPanelItems.filter(x=>waPanelSelected.has(x.canonical_url)));
$('#waArticlesGrid').addEventListener('click',e=>{
  const card=e.target.closest('.wa-card');
  if(!card||!waPanelAcc)return;
  const item=waPanelItems.find(x=>x.canonical_url===card.dataset.waKey);
  if(!item)return;
  const act=e.target.closest('[data-wa-act]');
  if(act){
    const name=act.dataset.waAct;
    if(name==='import')waImport([item]);
    else if(name==='exclude')openWaExclude([item]);
    else if(name==='restore')waRestore([item]);
    else if(name==='go'&&item.detail&&item.detail.item_id)showItem(item.detail.item_id).catch(err=>toast(err.message));
    return;
  }
  if(e.target.closest('a'))return;
  toggleWaPick(item.canonical_url);
});
$('#waPanelCategory').onchange=async e=>{
  if(!waPanelAcc)return;
  try{
    await api(`/api/wechat/accounts/${waPanelAcc.id}`,{method:'PATCH',body:{category_code:e.target.value}});
    waPanelAcc.category_code=e.target.value;
    toast(`分类已改为 ${WA_LABELS[e.target.value]||e.target.value}`);
    loadWechatAccounts().catch(()=>{});
  }catch(err){toast(err.message)}
};
$('#waPanelFit').onchange=async e=>{
  if(!waPanelAcc)return;
  try{
    await api(`/api/wechat/accounts/${waPanelAcc.id}`,{method:'PATCH',body:{fit:e.target.value}});
    waPanelAcc.fit=e.target.value;
    toast(`建议已改为 ${e.target.value}`);
    loadWechatAccounts().catch(()=>{});
  }catch(err){toast(err.message)}
};
$('#waPanelEnabled').onchange=async e=>{
  if(!waPanelAcc)return;
  try{
    const r=await api(`/api/wechat/accounts/${waPanelAcc.id}`,{method:'PATCH',body:{enabled:e.target.checked}});
    waPanelAcc.enabled=e.target.checked?1:0;
    if(e.target.checked){
      if(r.category_code){waPanelAcc.category_code=r.category_code;$('#waPanelCategory').value=r.category_code}
      toast('已启用'+(r.category_note?`；${r.category_note}`:'')+(r.backfill_queued?'；正在补录最近推文':''));
      // 补录在后台跑，几秒后刷新列表才能看到「最近」那一列和自动分类结果。
      setTimeout(()=>loadWechatAccounts().catch(()=>{}),7000);
    }else{
      toast('已停止监测这个号');
      loadWechatAccounts().catch(()=>{});
    }
  }catch(err){toast(err.message)}
};
$('#waExcludePreset').onchange=applyWaPreset;
$('#waExcludeClose').onclick=$('#waExcludeCancel').onclick=()=>$('#waExcludeDialog').close();
$('#waExcludeOk').onclick=async()=>{
  const keywords=$('#waExcludeKeywords').value.split(/[\n,，、;；|]+/).map(x=>x.trim()).filter(Boolean);
  const mode=$('#waExcludeMode').value;
  if(mode==='rule'&&!keywords.length)return toast('留一个关键词，否则以后同类还是会进来');
  const preset=waPresets[+$('#waExcludePreset').value]||{};
  try{
    const r=await api(`/api/wechat/accounts/${waPanelAcc.id}/articles/exclude`,{method:'POST',body:{
      articles:waExcludeTargets,mode,reason:preset.label||'手动排除',keywords,
      disable_account:$('#waExcludeDisable').checked}});
    $('#waExcludeDialog').close();
    toast(`已排除 ${r.excluded} 篇`+(r.rule_id?'，以后同类不再收录':''));
    if(r.hit_existing)toast(`顺手把已收录的 ${r.hit_existing} 篇也排除了`);
    waPanelSelected.clear();
    await loadWaPanel(true);
    loadWechatAccounts().catch(()=>{});
  }catch(e){toast(e.message)}
};
$('#waRows').addEventListener('click',e=>{
  const target=e.target.closest('[data-wa-open]');
  if(target)openWaPanel(+target.dataset.waOpen);
});

let listRefreshTimer=null;
function itemStage(x){
  if(x.status==='discovered'){
    if(x.processing_policy==='history_index_only') return {badge:'仅索引',text:'仅保存了索引，尚未抓取正文；在详情里点「让软件去找原文」即可补全文'};
    return {badge:'待归档',text:'排队等待归档…'};
  }
  if(x.status==='archived') return {badge:'已归档',text:'正文与图片已保存，等待精读'};
  if(x.status==='ready'){
    const mode=x.analysis_mode||(x.analysis||{}).analysis_mode;
    if(mode==='record_only') return {badge:'图文短记',text:x.summary||'内容主要在图与短文本身，已按原样记录'};
    return {badge:'已精读',text:x.summary||''};
  }
  if(x.status==='filtered'){
    const mode=x.analysis_mode||(x.analysis||{}).analysis_mode;
    if(mode==='record_only') return {badge:'图文短记',text:x.summary||''};
    return {badge:'低相关',text:x.summary||'本地初筛相关度不足，未调用云模型；详情可强制精读'};
  }
  if(x.status==='insufficient') return {badge:'无法详细归档',text:x.summary||'没有拿到足够原文，未做猜测性精读'};
  if(x.status==='error') return {badge:'失败',text:x.error||''};
  return {badge:x.status,text:''};
}
let itemPage=1,itemPageSize=50,itemTotal=0,itemCollections=[],ideaCollections=[];
const itemPicked=new Set();
function daysAgoIso(value){const n=Number(value||0);if(!n)return '';const d=new Date();d.setHours(0,0,0,0);d.setDate(d.getDate()-(n-1));return d.toISOString()}
async function loadCollections(scope){try{const rows=await api('/api/collections?scope='+scope);if(scope==='library'){itemCollections=rows;window.__itemCollections=rows}else{ideaCollections=rows;window.__ideaCollections=rows}syncCollectionButton(scope);return rows}catch(e){return []}}
function collectionName(scope,id){const list=scope==='library'?itemCollections:ideaCollections;const hit=list.find(c=>c.id===id);return hit?hit.name:''}
function nameKey(value){return (value||'').toLowerCase().replace(/[\s\-_·、,，.。:：()（）\[\]【】"'“”]/g,'')}
function editDistance(a,b){
  if(a===b)return 0;
  const m=a.length,n=b.length;
  if(!m)return n;if(!n)return m;
  let prev=Array.from({length:n+1},(_,i)=>i),cur=new Array(n+1);
  for(let i=1;i<=m;i++){cur[0]=i;for(let j=1;j<=n;j++){cur[j]=Math.min(prev[j]+1,cur[j-1]+1,prev[j-1]+(a[i-1]===b[j-1]?0:1))}const swap=prev;prev=cur;cur=swap}
  return prev[n];
}
/* 判断「这个名字是不是已经有一个了」。
   互相包含（「抗体」⊂「抗体工程」）算，差一个字也算——但只对四个字以上的名字，
   否则「抗体」和「抗菌」会被判成同一个，反而阻止用户建新夹子。 */
function findSimilarCollections(name,list){
  const key=nameKey(name);
  if(!key)return [];
  return list.filter(c=>{
    const other=nameKey(c.name);
    if(!other)return false;
    if(other===key)return true;
    if(key.length>=2&&other.length>=2&&(key.includes(other)||other.includes(key)))return true;
    return Math.min(key.length,other.length)>=4&&editDistance(key,other)<=1;
  }).slice(0,3);
}
/* 筛选收藏夹不再用 <select>：选项一多就翻不动，也没地方放搜索框和排序。
   现在的交互是「一个按钮 → 弹层里搜索/排序/点一行」，和「收藏到」保持同一套设计。
   旧实现还有两个真 bug：搜不到时把第 0 个 option 的文案改成「没有匹配的收藏夹」，
   下一次渲染又把这段文案当标题读回去，于是「全部收藏夹」被永久顶掉。 */
let libraryCollectionFilter='',ideaCollectionFilter='';
let collectionPickScope='library';
function collectionFilterValue(scope){return scope==='library'?libraryCollectionFilter:ideaCollectionFilter}
function collectionFilterLabel(scope){
  const value=collectionFilterValue(scope);
  if(value==='__none__')return '未收藏';
  if(!value)return '全部收藏夹';
  const list=scope==='library'?itemCollections:ideaCollections;
  const hit=list.find(c=>c.id===value);
  return hit?hit.name:'全部收藏夹';
}
function syncCollectionButton(scope){
  const label=$(scope==='library'?'#searchCollectionLabel':'#ideaCollectionLabel');
  if(!label)return;
  label.textContent=collectionFilterLabel(scope);
  const button=label.closest('button');
  if(button)button.classList.toggle('active',!!collectionFilterValue(scope));
  if($('#collectionPick').open&&collectionPickScope===scope)renderCollectionPick();
}
function sortCollectionRows(rows,mode){
  const copy=(rows||[]).slice();
  if(mode==='count')copy.sort((a,b)=>(b.count||0)-(a.count||0)||String(a.name).localeCompare(String(b.name),'zh'));
  else if(mode==='name')copy.sort((a,b)=>String(a.name).localeCompare(String(b.name),'zh'));
  else copy.sort((a,b)=>String(b.last_used||b.created_at||'').localeCompare(String(a.last_used||a.created_at||'')));
  return copy;
}
function renderCollectionPick(){
  const scope=collectionPickScope;
  const list=scope==='library'?itemCollections:ideaCollections;
  const word=($('#collectionPickSearch')?.value||'').trim().toLowerCase();
  const mode=$('#collectionPickSort')?.value||'recent';
  const current=collectionFilterValue(scope);
  const matched=sortCollectionRows(word?list.filter(c=>(c.name||'').toLowerCase().includes(word)):list,mode);
  const fixed=[
    {id:'',name:'全部收藏夹',count:null,hint:'不按收藏夹限制'},
    {id:'__none__',name:'未收藏',count:null,hint:'还没放进任何收藏夹'},
  ];
  const head=fixed.map(row=>`<button type="button" class="collection-pick-row${row.id===current?' current':''}" data-pick-collection="${esc(row.id)}"><span>${esc(row.name)}</span><small class="muted">${esc(row.hint)}</small><b>${row.id===current?'✓':''}</b></button>`).join('');
  const body=matched.map(c=>`<button type="button" class="collection-pick-row${c.id===current?' current':''}" data-pick-collection="${esc(c.id)}"><span>${esc(c.name)}</span><small class="muted">${c.count||0} 条${c.last_used?` · 最近 ${time(c.last_used).slice(0,10)}`:''}</small><b>${c.id===current?'✓':''}</b></button>`).join('');
  const empty=word&&!matched.length?`<div class="collection-pick-empty">没有匹配「${esc(word)}」的收藏夹。<br>可以换个词，或到「管理收藏夹」里新建。</div>`:'';
  $('#collectionPickList').innerHTML=head+body+empty;
  $('#collectionPickHint').textContent=word
    ? `在 ${list.length} 个收藏夹里搜到 ${matched.length} 个`
    : `共 ${list.length} 个收藏夹 · 点一行按它筛选`;
}
function positionCollectionPop(pop,anchor){
  const rect=anchor.getBoundingClientRect();
  const width=pop.offsetWidth||320,height=pop.offsetHeight||260;
  const left=Math.min(window.innerWidth-width-12,Math.max(12,rect.left));
  let top=rect.bottom+8;
  if(top+height>window.innerHeight-12)top=Math.max(12,rect.top-height-8);
  pop.style.left=left+'px';
  pop.style.top=top+'px';
}
async function openCollectionPick(scope,anchor){
  collectionPickScope=scope;
  if($('#collectionPickSearch'))$('#collectionPickSearch').value='';
  if($('#collectionPickSort'))$('#collectionPickSort').value='recent';
  $('#collectionPickTitle').textContent=scope==='library'?'按收藏夹筛选资料':'按收藏夹筛选 Ideas 与笔记';
  await loadCollections(scope);
  renderCollectionPick();
  const pop=$('#collectionPick');
  if(pop.open)pop.close();
  pop.showModal();
  positionCollectionPop(pop,anchor);
  $('#collectionPickSearch').focus();
}
function closeCollectionPick(){const pop=$('#collectionPick');if(pop&&pop.open)pop.close()}
function applyCollectionFilter(scope,value){
  if(scope==='library')libraryCollectionFilter=value||'';
  else ideaCollectionFilter=value||'';
  syncCollectionButton(scope);
  closeCollectionPick();
  if(scope==='library')reloadItems();
  else loadIdeas().catch(e=>toast(e.message));
}
function collectionManageRows(scope){
  const list=scope==='library'?itemCollections:ideaCollections;
  const word=($('#collectionDialogSearch')?.value||'').trim().toLowerCase();
  const mode=$('#collectionDialogSort')?.value||'recent';
  return sortCollectionRows(word?list.filter(c=>(c.name||'').toLowerCase().includes(word)):list,mode);
}
function scheduleListRefresh(){if(listRefreshTimer)return;listRefreshTimer=setTimeout(()=>{listRefreshTimer=null;if(currentView==='library')loadItems().catch(()=>{})},4000)}
async function loadItems(){
  const params=new URLSearchParams({q:$('#searchQ').value.trim(),kind:$('#searchKind').value,status:$('#searchStatus').value,limit:itemPageSize,offset:(itemPage-1)*itemPageSize});
  const since=daysAgoIso($('#searchRange').value);
  if(since)params.set('since',since);
  const collection=libraryCollectionFilter;
  if(collection)params.set('collection',collection);
  if($('#searchExcluded').checked)params.set('include_excluded','true');
  if($('#hideLow').checked)params.set('hide_low','true');
  const data=await api('/api/items?'+params.toString());
  itemTotal=data.total||0;
  const rows=data.items||[];
  const pages=Math.max(1,Math.ceil(itemTotal/itemPageSize));
  if(itemPage>pages&&itemTotal){itemPage=pages;return loadItems()}
  $('#itemsList').innerHTML=rows.map(x=>{
    const tags=JSON.parse(x.tags_json||'[]'),stage=itemStage(x);
    const jobBadge=x.active_job?` <span class="badge wait"><span class="spin"></span>${x.active_job==='analyze'?'精读中':x.active_job==='verify'?'等微信放行':'归档中'}</span>`:'';
    const chips=(x.collections||[]).map(id=>{const name=collectionName('library',id);return name?`<span class="collection-chip">${esc(name)}</span>`:''}).join('');
    const excluded=x.excluded_at?` <span class="badge error" title="${esc(x.exclusion_reason||'')}">${x.exclusion_reason&&x.exclusion_reason.startsWith('手动删除')?'已删除':'已过滤'}</span>`:'';
    const picked=itemPicked.has(x.id)?' checked':'';
    return `<article class="item${picked?' picked':''}" data-item="${x.id}"><div><h3><label class="pick" title="选中后可批量删除"><input type="checkbox" data-pick="${x.id}"${picked}></label><span>${esc(x.title||x.url)}</span></h3><div><span class="badge">${x.kind==='video'?'视频':'文章'}</span> <span class="badge ${x.status==='ready'?'ok':x.status==='error'?'error':x.status==='discovered'?'wait':''}">${esc(stage.badge)}</span> <span class="badge">${esc(x.category)}</span>${excluded}${jobBadge}${chips?` <span class="collection-chips">${chips}</span>`:''}</div><p>${esc(x.summary||x.error||stage.text)}</p><small>${esc(x.author)} · ${time(x.published_at||x.discovered_at)} · ${tags.map(esc).join(' / ')}</small></div><div class="score"><button class="star ${(x.collections||[]).length?'on':''}" type="button" data-collect="${x.id}" data-collect-type="item" data-collect-ids="${(x.collections||[]).join(',')}" title="${(x.collections||[]).length?'已在收藏夹中':'收藏到收藏夹'}">★</button><strong>${Math.round(x.importance)}</strong><small>重要度</small><small>相关度 ${Math.round(x.relevance)}</small>${x.excluded_at?`<button class="item-del restore" type="button" data-restore="${x.id}" title="放回资料库">恢复</button>`:`<button class="item-del" type="button" data-delete="${x.id}" title="从资料库删除（可恢复）">删除</button>`}</div></article>`;
  }).join('')||`<div class="panel muted">没有匹配的资料。${$('#searchExcluded').checked?'':'如果确定它被过滤或手动删过，可勾选「含已删除」再看一次。'}</div>`;
  renderPager();
  const searched=$('#searchQ').value.trim();
  const hidden=Number(data.hidden_low_relevance||0);
  $('#itemsSummary').innerHTML=`共 ${itemTotal} 条${searched?`，搜索「${esc(searched)}」`:''}`+
    (hidden?` · <span class="low-hidden">另有 ${hidden} 条低相关已隐藏 <button type="button" class="link" id="showLow">看看</button></span>`:'');
  if($('#showLow'))$('#showLow').onclick=()=>{$('#hideLow').checked=false;$('#searchStatus').value='filtered';reloadItems()};
  syncBulkBar();
  if(rows.some(x=>x.active_job))scheduleListRefresh();
}
function renderPager(){
  const pages=Math.max(1,Math.ceil(itemTotal/itemPageSize));
  itemPage=Math.min(Math.max(1,itemPage),pages);
  const nums=[];
  const push=n=>nums.push(`<button type="button" class="${n===itemPage?'current':''}" data-page="${n}">${n}</button>`);
  if(pages<=9){for(let n=1;n<=pages;n++)push(n)}
  else{
    push(1);
    if(itemPage>3)nums.push('<span class="muted">…</span>');
    for(let n=Math.max(2,itemPage-1);n<=Math.min(pages-1,itemPage+1);n++)push(n);
    if(itemPage<pages-2)nums.push('<span class="muted">…</span>');
    push(pages);
  }
  $('#pager').innerHTML=`<div class="pager-info">共 ${itemTotal} 条 · 第 ${itemPage}/${pages} 页</div>
    <div class="pager-btns"><button type="button" data-page="${itemPage-1}" ${itemPage<=1?'disabled':''}>上一页</button>${nums.join('')}<button type="button" data-page="${itemPage+1}" ${itemPage>=pages?'disabled':''}>下一页</button></div>
    <label class="pager-size">每页 <select id="pageSizeSel">${[20,50,100,200].map(n=>`<option value="${n}" ${n===itemPageSize?'selected':''}>${n}</option>`).join('')}</select> 条</label>`;
  const select=$('#pageSizeSel');
  if(select)select.onchange=()=>{itemPageSize=Number(select.value)||50;itemPage=1;loadItems().catch(e=>toast(e.message))};
}
/* ---------- 收藏夹 ---------- */
let collectTarget=null;
function collectScope(type){return type==='item'?'library':'ideas'}
async function openCollectPop(entryType,entryId,ids,anchor,refresh){
  collectTarget={entryType,entryId,ids:[...(ids||[])],refresh};
  // 每次打开都从空白开始：上次搜的关键字留在框里，会让人以为收藏夹"变少了"。
  const search=$('#collectionPopSearch'),fresh=$('#collectionNewName');
  if(search)search.value='';
  if(fresh)fresh.value='';
  await loadCollections(collectScope(entryType));
  renderCollectPop();
  const pop=$('#collectionPop');
  // 必须 showModal()：精读页也是 modal，普通 div 的 z-index 压不过 top layer，
  // 从精读页点「收藏到…」时弹层会长在对话框背后，看起来就是点了没反应。
  if(pop.open)pop.close();
  pop.showModal();
  const rect=anchor.getBoundingClientRect();
  const width=pop.offsetWidth||300,height=pop.offsetHeight||220;
  const left=Math.min(window.innerWidth-width-12,Math.max(12,rect.right-width));
  let top=rect.bottom+8;
  if(top+height>window.innerHeight-12)top=Math.max(12,rect.top-height-8);
  pop.style.left=left+'px';
  pop.style.top=top+'px';
}
function collectPopRows(){
  const scope=collectScope(collectTarget.entryType);
  const list=scope==='library'?itemCollections:ideaCollections;
  const word=($('#collectionPopSearch')?.value||'').trim().toLowerCase();
  const mode=$('#collectionPopSort')?.value||'recent';
  const rows=(word?list.filter(c=>(c.name||'').toLowerCase().includes(word)):list).slice();
  if(mode==='count')rows.sort((a,b)=>(b.count||0)-(a.count||0)||String(a.name).localeCompare(String(b.name),'zh'));
  else if(mode==='name')rows.sort((a,b)=>String(a.name).localeCompare(String(b.name),'zh'));
  else rows.sort((a,b)=>String(b.last_used||b.created_at||'').localeCompare(String(a.last_used||a.created_at||'')));
  return {list,rows,word};
}
function renderCollectPop(){
  if(!collectTarget)return;
  const {list,rows,word}=collectPopRows();
  $('#collectionPopList').innerHTML=rows.length?rows.map(c=>`<label><input type="checkbox" data-collect-toggle="${esc(c.id)}" ${collectTarget.ids.includes(c.id)?'checked':''}><span>${esc(c.name)}</span><small class="muted">${c.count||0}</small></label>`).join(''):`<span class="muted">${word?`没有匹配「${esc(word)}」的收藏夹`:'还没有收藏夹，先在下面新建一个。'}</span>`;
  // 边打名字边提示「已经有很像的了」，这是阻止重复创建的关键一步：
  // 等用户按下「新建并加入」才发现重复，那时已经晚了。
  const hint=$('#collectionPopHint');
  const typed=($('#collectionNewName').value||'').trim();
  const similar=typed?findSimilarCollections(typed,list):[];
  hint.innerHTML=similar.length
    ?`<span>已有相近的收藏夹：</span>${similar.map(c=>`<button type="button" data-use-collection="${esc(c.id)}">${esc(c.name)}</button>`).join('')}`
    :'';
}
function closeCollectPop(){const pop=$('#collectionPop');if(pop&&pop.open)pop.close();collectTarget=null}
async function toggleCollect(collectionId,member){
  if(!collectTarget)return;
  const target=collectTarget;
  const r=await api('/api/collections/toggle',{method:'POST',body:{collection_id:collectionId,entry_type:target.entryType,entry_id:target.entryId,member}});
  target.ids=r.member?[...new Set([...target.ids,collectionId])]:target.ids.filter(x=>x!==collectionId);
  await loadCollections(collectScope(target.entryType));
  renderCollectPop();
  syncCollectionButton(collectScope(target.entryType));
  if(target.refresh)target.refresh().catch(()=>{});
  toast(r.member?'已加入收藏夹':'已移出收藏夹');
}
/* ---------- 智能收藏建议 ----------
   只在用户点「智能收藏」时跑一次，不再自动弹：自动弹窗在连着看十几条时很烦，
   而且那时用户往往还没想好要不要收藏。评分在本地做（分类/标签/标题对收藏夹名
   做子串匹配），最多两个候选，勾选并点确认才写入。 */
let smartCollectItemId=null,smartCollectRows=[];
async function suggestCollections(x,opts){
  const manual=!!(opts&&opts.manual);
  if(!x||!x.id)return;
  const dlg=$('#smartCollect');
  if(dlg.open)return;
  // 按钮按下去就要有动静：本地库大时这一步也要几百毫秒，什么都不显示就成了「没反应」。
  // 但正常只要几十毫秒，提示一闪而过反而像闪烁，所以慢过 250ms 才冒出来。
  const slow=manual?setTimeout(()=>toast('正在看哪几个收藏夹对得上…'),250):null;
  let data=null;
  try{data=await api('/api/items/'+encodeURIComponent(x.id)+'/collection-suggestions')}
  catch(e){if(slow)clearTimeout(slow);toast(`取收藏建议失败：${e.message||e}`);return}
  if(slow)clearTimeout(slow);
  const rows=(data&&data.suggestions)||[];
  // 请求回来时用户可能已经切到别的条目了，别把建议弹到新页面上。
  const stillHere=(wbIsOpen()&&wb.itemId===x.id)||(classicItemId===x.id&&$('#detailDialog').open);
  if(!stillHere){if(manual)toast('这条已经关掉了，重新打开再点一次');return}
  smartCollectItemId=x.id;smartCollectRows=rows;
  const title=$('#smartCollectTitle'),lead=$('#smartCollectLead'),list=$('#smartCollectList');
  const ok=$('#smartCollectOk'),pick=$('#smartCollectPick');
  if(rows.length){
    title.textContent=rows.length>1?'要不要收藏到这几个收藏夹？':'要不要收藏到这个收藏夹？';
    lead.textContent=`这篇的分类与关键词刚好对上了下面${rows.length>1?'这几个':'这个'}收藏夹的名字。建议在本地算，不花点数；勾选后点确认才会收藏。`;
    list.innerHTML=rows.map((s,i)=>`<label class="sc-row"><input type="checkbox" data-sc-pick="${i}" checked><span class="sc-name">${esc(s.name)}</span><span class="sc-score">${(s.score||0)>=4.5?'很相关':'相关'}</span><small>${esc(s.reason||'主题接近')}</small></label>`).join('');
    list.hidden=false;ok.hidden=false;
    if(pick)pick.hidden=true;
  }else{
    // 没匹配到也不能当成「什么都没发生」：说清是「已经收藏过了」还是「对不上」，并给出口。
    const already=(data&&data.already)||[];
    title.textContent='这次没有对得上的收藏夹';
    if(already.length){
      lead.textContent=`这篇已经在「${already.join('」「')}」里了，没有别的更对得上的收藏夹。要挪到别处就自己挑一个。`;
    }else if((x.collections||[]).length){
      lead.textContent='这篇的分类与关键词没有对上任何一个「还没收藏」的收藏夹。可以自己挑一个放进去。';
    }else{
      lead.textContent='这篇还缺可用来匹配的分类或关键词（精读完成后会准得多）。先自己挑一个收藏夹吧。';
    }
    list.innerHTML='';list.hidden=true;ok.hidden=true;
    if(pick)pick.hidden=false;
  }
  dlg.showModal();
  // showModal 默认把焦点给第一个可聚焦元素（右上角的 ✕），焦点环看着像个黑框。
  dlg.focus();
}
function closeSmartCollect(){
  const d=$('#smartCollect');if(d.open)d.close();
  smartCollectItemId=null;smartCollectRows=[];
}
async function applySmartCollect(){
  const itemId=smartCollectItemId;
  const picked=[...document.querySelectorAll('[data-sc-pick]:checked')]
    .map(box=>smartCollectRows[Number(box.dataset.scPick)]).filter(Boolean);
  closeSmartCollect();
  if(!picked.length||!itemId)return;
  try{
    for(const row of picked){
      await api('/api/collections/toggle',{method:'POST',
        body:{collection_id:row.id,entry_type:'item',entry_id:itemId,member:true}});
    }
    toast(`已收藏到 ${picked.map(row=>row.name).join('、')}`);
    await loadCollections('library').catch(()=>{});
    if($('#detailDialog').open)await showItemClassic(itemId,true,true).catch(()=>{});
    else await refreshOpenReader(itemId).catch(()=>{});
    loadItems().catch(()=>{});
  }catch(err){toast(err.message)}
}
$('#smartCollectClose').onclick=()=>closeSmartCollect();
$('#smartCollectSkip').onclick=()=>closeSmartCollect();
// 没匹配到收藏夹时的出口：直接开「收藏到…」那个挑夹子的弹层，省得用户再去找按钮。
$('#smartCollectPick').onclick=async()=>{
  const itemId=smartCollectItemId;
  const ids=(window.__currentItem&&window.__currentItem.collections)||[];
  closeSmartCollect();
  if(!itemId)return;
  const anchor=$('#wbItemActions [data-collect]')||$('#detailDialog [data-collect]')||document.body;
  await openCollectPop('item',itemId,ids,anchor,()=>currentView==='library'?loadItems():loadIdeas());
};
$('#smartCollectOk').onclick=()=>applySmartCollect().catch(err=>toast(err.message));
$('#smartCollect').addEventListener('click',e=>{if(e.target===e.currentTarget)closeSmartCollect()});
async function createCollectionFor(name,scope,entry){
  if(!name.trim())return toast('先写一个收藏夹名称');
  const created=await api('/api/collections',{method:'POST',body:{name:name.trim(),scope}});
  await loadCollections(scope);
  if(entry&&collectTarget){
    await api('/api/collections/toggle',{method:'POST',body:{collection_id:created.id,entry_type:collectTarget.entryType,entry_id:collectTarget.entryId,member:true}});
    collectTarget.ids=[...new Set([...collectTarget.ids,created.id])];
    await loadCollections(scope);
    renderCollectPop();
    if(collectTarget.refresh)collectTarget.refresh().catch(()=>{});
  }
  syncCollectionButton(scope);
  toast(`已创建收藏夹「${created.name}」`);
  return created;
}
async function openCollectionManager(scope){
  window.__manageScope=scope;
  $('#collectionDialogTitle').textContent=scope==='library'?'管理资料库收藏夹':'管理 Ideas 与笔记收藏夹';
  if($('#collectionDialogSearch'))$('#collectionDialogSearch').value='';
  if($('#collectionDialogSort'))$('#collectionDialogSort').value='recent';
  await loadCollections(scope);
  renderCollectionManage();
  $('#collectionDialog').showModal();
}
function renderCollectionManage(){
  const scope=window.__manageScope||'library';
  const list=scope==='library'?itemCollections:ideaCollections;
  const rows=collectionManageRows(scope);
  const word=($('#collectionDialogSearch')?.value||'').trim();
  $('#collectionDialogList').innerHTML=rows.length?rows.map(c=>`<div class="row" data-collection-row="${esc(c.id)}"><input value="${esc(c.name)}" data-collection-name="${esc(c.id)}"><span class="count">${c.count||0} 项${c.last_used?` · ${time(c.last_used).slice(0,10)}`:''}</span><button type="button" data-collection-save="${esc(c.id)}">改名</button><button type="button" class="danger" data-collection-delete="${esc(c.id)}">删除</button></div>`).join('')
    :(word?`<div class="muted">没有匹配「${esc(word)}」的收藏夹。共 ${list.length} 个，换个词试试。</div>`:'<div class="muted">还没有收藏夹。</div>');
}
const listBlock=(title,items)=>Array.isArray(items)&&items.length?`<div class="detail-section"><h3>${title}</h3><ol>${items.map(x=>`<li>${esc(typeof x==='string'?x:JSON.stringify(x))}</li>`).join('')}</ol></div>`:'';
let detailWatchTimer=null,detailWatchId=null,detailSeq=0;
function stopItemWatch(){if(detailWatchTimer)clearInterval(detailWatchTimer);detailWatchTimer=null;detailWatchId=null}
function watchItem(id){if(detailWatchId===id&&detailWatchTimer)return;stopItemWatch();detailWatchId=id;let ticks=0;
  detailWatchTimer=setInterval(async()=>{ticks++;
    // The user closed the reader or moved to another item: this timer must die, or it
    // keeps re-rendering and re-opening the panel over whatever they are doing.
    // 两套界面都要认：经典弹层看 dialog 开没开，工作台看 pane 里还是不是这篇。
    const alive=readingMode==='classic'?$('#detailDialog').open:(wbIsOpen()&&wb.itemId===id);
    if(!alive||detailWatchId!==id){stopItemWatch();return}
    try{const x=await api('/api/items/'+id);const hasAnalysis=x.analysis&&Object.keys(x.analysis).length>0;
    if(detailWatchId!==id)return;
    // 刚点完「重新精读」时任务还没进库，前两次看不到 active_job 是正常的，别急着收摊。
    const job=x.last_job||{};
    const queued=x.active_job||['READY','LEASED','RETRY_WAIT'].includes(String(job.state||''));
    if(!queued&&ticks>2){stopItemWatch();toast(hasAnalysis?'精读已完成，结果已更新':'处理已结束，可查看最新状态')}
    wbRenderJob(x);
    await refreshOpenReader(id)}catch(e){stopItemWatch()}},3000)}
const READ_STEP_FIELDS=[['why','为什么做这一步'],['did','具体做了什么'],['saw','看到了什么'],['means','说明什么']];
function blobUrl(path){const parts=String(path||'').split(/[\\/]/);return parts.length>=2?`/api/blobs/${parts[parts.length-2]}/${parts[parts.length-1]}`:''}
function fmtMs(ms){const t=Number(ms||0);if(!t)return '';const s=Math.round(t/1000);return s<60?`${s} 秒`:`${Math.floor(s/60)} 分 ${s%60} 秒`}
function tools(path){return `<div class="block-tools"><button type="button" data-copy>复制</button>${path?`<button type="button" data-note="${path}">存笔记</button><button type="button" data-edit="${path}">编辑</button>`:''}</div>`}
function readingBody(a,itemId){
  const steps=Array.isArray(a.steps)?a.steps:[];
  const numbers=(a.numbers||[]).filter(n=>n&&(n.value||n.meaning));
  const terms=(a.terms||[]).filter(t=>t&&(t.term||t.plain));
  if(!a.hook&&!a.plain_problem&&!steps.length)return '';
  const stepHtml=steps.map((s,i)=>{
    const path='steps.'+i;
    return `<li class="read-step" data-block="${path}"><div class="step-head"><span class="step-n">${i+1}</span><b>${esc(s.title||'第 '+(i+1)+' 步')}</b></div>${READ_STEP_FIELDS.map(([k,label])=>s[k]?`<p data-field="${path}.${k}"><span class="step-label">${label}</span><span class="field-text">${esc(s[k])}</span></p>`:'').join('')}${tools(path+'.did')}</li>`;
  }).join('');
  const block=(path,title,text,cls='')=>text?`<div class="read-block ${cls}" data-block="${path}"><h2>${title}</h2><p data-field="${path}"><span class="field-text">${esc(text)}</span></p>${tools(path)}</div>`:'';
  return `<section class="reading" data-item="${esc(itemId||'')}">
    ${a.hook?`<div class="read-hook-box" data-block="hook"><p class="read-hook" data-field="hook"><span class="field-text">${esc(a.hook)}</span></p>${tools('hook')}</div>`:''}
    ${block('plain_problem','这项研究在解决什么问题',a.plain_problem)}
    ${block('obstacle','以前为什么做不到',a.obstacle)}
    ${block('core_idea','他们的思路',a.core_idea)}
    ${stepHtml?`<div class="read-block" data-block="steps"><h2>研究是怎么一步步做的</h2><ol class="read-steps">${stepHtml}</ol></div>`:''}
    ${block('answer','所以结论是',a.answer,'answer')}
    ${numbers.length?`<div class="read-block" data-block="numbers"><h2>关键数字</h2><table class="numbers"><tbody>${numbers.map((n,i)=>`<tr><td>${esc(n.value)}</td><td data-field="numbers.${i}.meaning"><span class="field-text">${esc(n.meaning)}</span></td></tr>`).join('')}</tbody></table>${tools('numbers')}</div>`:''}
    ${terms.length?`<div class="read-block" data-block="terms"><h2>名词小抄</h2><dl class="terms">${terms.map((t,i)=>`<dt>${esc(t.term)}</dt><dd data-field="terms.${i}.plain"><span class="field-text">${esc(t.plain)}</span></dd>`).join('')}</dl>${tools('terms')}</div>`:''}
  </section>`;
}
function legacyBody(a){
  const findings=(a.key_findings||[]).map(f=>{
    if(typeof f==='string')return `<li>${esc(f)}</li>`;
    const head=f.finding||f.title||'';
    const detail=f.detail||'';
    return (head||detail)?`<li>${esc(head)}${detail?`<br><span class="muted">${esc(detail)}</span>`:''}</li>`:'';
  }).filter(Boolean).join('');
  return `${a.summary?`<p class="read-hook">${esc(a.summary)}</p>`:''}
    ${findings?`<div class="read-block"><h2>关键结果</h2><ol class="plain-list">${findings}</ol></div>`:''}`;
}
function figureBlock(x){
  const figs=(x.figures||[]).filter(f=>f&&f.local_path);
  if(!figs.length)return '';
  return `<details class="figures"><summary>原文图片（${figs.length} 张，点开对照；双击图片可放大、滚轮缩放）</summary><div class="figure-grid">${figs.map(f=>`<figure><img loading="lazy" src="${blobUrl(f.local_path)}" alt="${esc(f.caption||('图'+f.number))}" title="双击放大查看"><figcaption>图${f.number}${f.caption?`　${esc(f.caption)}`:''}</figcaption></figure>`).join('')}</div></details>`;
}
function afterBlock(a){
  const blocks=[['值得借鉴的优点',a.strengths],['局限',a.limits],['值得追问',a.next_questions],['证据缺口',a.evidence_gaps],['可继续做的选题',a.potential_topics],['与我的 ideas 的连接',a.idea_connections]];
  const rows=blocks.map(([title,items])=>{
    if(!Array.isArray(items)||!items.length)return '';
    const lis=items.map(x=>{
      if(typeof x==='string')return `<li>${esc(x)}</li>`;
      if(x&&x.title)return `<li><b>${esc(x.title)}</b>${x.rationale?`　${esc(x.rationale)}`:''}${x.first_experiment?`<br><small>首个实验：${esc(x.first_experiment)}</small>`:''}</li>`;
      if(x&&x.connection)return `<li>${esc(x.connection)}${x.hypothesis?`<br><small>假设：${esc(x.hypothesis)}</small>`:''}</li>`;
      if(x&&x.finding)return `<li>${esc(x.finding)}</li>`;
      return `<li>${esc(JSON.stringify(x))}</li>`;
    }).join('');
    return `<div class="after-block"><h3>${title}</h3><ul>${lis}</ul></div>`;
  }).join('');
  return rows?`<details class="after-reading"><summary>再往深一层：局限、追问与可做的选题</summary>${rows}</details>`:'';
}
/* ---------- 阅读工作台 ----------
   把用户的原话翻译成五条规则：
   1. 点资料库条目 → 先看离线阅读页，单栏居中，不再跳浏览器。
   2. 点「精读」→ 原文让到左边，精读从右侧进来，两栏铺满；再点一次收起。
   3. 「笔记」「Ideas」抢的是同一块第二栏位置，所以能边读边记，不用退出重进。
   4. 只有右上角 ✕ 关整页；点暗处、按 Esc 都不关（读长文手滑不该丢进度）。
   5. 关掉一栏，剩下那栏自动回到居中。切 tab 只改 display，不重建 DOM，iframe 保住滚动位置。
*/
const WB_VIEWS={read:'原文',reading:'精读',paper:'论文',notes:'笔记',ideas:'Ideas'};
const wb={open:false,itemId:null,item:null,panes:[],paneSeq:0,noteSeq:0,paperIndex:0,actPage:0,actItems:[],actSplit:4,actSig:'',sig:''};
let wbIdeaTimer=null;
function wbEl(){return $('#workbench')}
function wbIsOpen(){const el=wbEl();return !!(el&&el.open)}
function wbPaneEl(key){return document.querySelector(`.wb-pane[data-pane="${key}"]`)}
function wbViewEl(pane,view){const el=wbPaneEl(pane.key);return el?el.querySelector(`.wb-view[data-view="${view}"]`):null}

function wbPaneHTML(key){
  return `<section class="wb-pane" data-pane="${key}">
    <div class="wb-pane-head">
      <div class="wb-tabs" role="tablist" aria-label="这一栏显示什么">${Object.entries(WB_VIEWS).map(([k,label])=>`<button type="button" role="tab" data-wb-view="${k}" aria-selected="false">${label}</button>`).join('')}</div>
      <button type="button" class="wb-pane-close" data-wb-pane-close title="关闭这一栏" aria-label="关闭这一栏">✕</button>
    </div>
    <div class="wb-pane-body">${Object.keys(WB_VIEWS).map(k=>`<div class="wb-view" data-view="${k}"></div>`).join('')}</div>
  </section>`;
}
function wbAddPane(view){
  const key='p'+(++wb.paneSeq);
  const pane={key,view};
  const host=document.createElement('div');
  host.innerHTML=wbPaneHTML(key);
  const el=host.firstElementChild;
  // 入场动画只挂在新加的这一栏上：用 data-count 驱动会让左栏在 1→2 时跟着闪一下。
  el.classList.add('wb-pane-enter');
  el.addEventListener('animationend',()=>el.classList.remove('wb-pane-enter'),{once:true});
  wb.panes.push(pane);
  $('#wbBody').appendChild(el);
  wbSyncLayout();
  wbSetView(pane,view,{force:true});
  return pane;
}
function wbRemovePane(key){
  const at=wb.panes.findIndex(p=>p.key===key);
  if(at<0)return;
  wb.panes.splice(at,1);
  const el=wbPaneEl(key);
  if(el)el.remove();
  if(!wb.panes.length){closeWorkbench();return}
  wbSyncLayout();
}
function wbSyncLayout(){
  const n=wb.panes.length;
  $('#wbBody').dataset.count=String(n);
  // 顶栏开关代表「现在看什么」：精读/论文/笔记/Ideas 对应第二栏，原文表示「没开并排、只看原文」。
  const side=n>1?wb.panes[1].view:null;
  const onlyRead=n===1&&wb.panes[0].view==='read';
  $$('#wbSideSwitch button').forEach(b=>{
    const on=b.dataset.wbSide==='read'?onlyRead:(!!side&&b.dataset.wbSide===side);
    b.setAttribute('aria-pressed',String(on));
  });
  wb.panes.forEach(p=>{
    const el=wbPaneEl(p.key);if(!el)return;
    el.querySelectorAll('[data-wb-view]').forEach(b=>b.setAttribute('aria-selected',String(b.dataset.wbView===p.view)));
  });
}
async function wbSetView(pane,view,opts={}){
  const el=wbPaneEl(pane.key);if(!el)return;
  pane.view=view;
  el.querySelectorAll('.wb-view').forEach(v=>v.classList.toggle('is-active',v.dataset.view===view));
  el.querySelectorAll('[data-wb-view]').forEach(b=>b.setAttribute('aria-selected',String(b.dataset.wbView===view)));
  wbSyncLayout();
  const box=el.querySelector(`.wb-view[data-view="${view}"]`);
  if(!box||!wb.item)return;
  if(view==='read'){
    // 同一个条目的 iframe 一旦建好就不再重建：来回切 tab 必须保住阅读位置。
    if(!box.querySelector('iframe')||box.dataset.item!==wb.item.id)wbMountReader(box,wb.item);
    return;
  }
  if(view==='reading'){
    box.innerHTML=wbReadingHTML(wb.item);
    return;
  }
  if(view==='paper'){
    box.innerHTML=paperPaneHTML(wb.item,{index:wb.paperIndex||0});
    return;
  }
  if(view==='notes')return wbRenderNotes(pane);
  if(view==='ideas')return wbRenderIdeas(pane);
}
function wbReaderHTML(x){
  if(!x.html_path)return `<div class="wb-empty"><b>这篇还没有离线阅读页</b><p class="muted">正文与配图还没归档。完成后这里会自动出现原文；也可以先看精读。</p>${x.status==='discovered'?`<button type="button" class="primary" data-process="${x.id}">让软件去找原文</button>`:'<span class="muted">正在处理，完成后自动刷新。</span>'}</div>`;
  const src=`/api/items/${encodeURIComponent(x.id)}/archive`;
  return `<iframe data-src="${src}" src="${src}" title="离线阅读页" loading="eager"></iframe>`;
}
function wbMountReader(box,x){
  box.dataset.item=x.id;
  box.innerHTML=wbReaderHTML(x);
  guardArchiveFrame(box.querySelector('iframe'));
}
function wbBadges(x){
  const stage=itemStage(x),a=x.analysis||{};
  const elapsed=fmtMs(a.llm_meta&&a.llm_meta.elapsed_ms);
  const triage=(x.metadata||{}).image_triage||{};
  return `<p class="wb-badges"><span class="badge ${x.status==='ready'?'ok':x.status==='error'?'error':x.status==='discovered'?'wait':''}">${esc(stage.badge)}</span><span class="badge">重要度 ${Math.round(x.importance)}</span><span class="badge">相关度 ${Math.round(x.relevance)}</span><span class="badge">证据 ${esc((x.metadata||{}).archive_completeness||'未知')}</span>${elapsed?`<span class="badge">精读用时 ${elapsed}</span>`:''}${triage.dropped?`<span class="badge">已过滤 ${triage.dropped} 张无用配图</span>`:''}</p>`;
}
function wbNotices(x){
  const progress=x.progress||null;
  const jobLine=x.active_job
    ? `<div class="notice progress-note"><span class="spin"></span>${x.active_job==='analyze'?'正在调用云模型精读，完成后本页自动刷新…':x.active_job==='verify'?'微信要求人机验证，正文暂时抓不到；30 分钟后自动重试，链接和标题都已保留…':'正在抓取正文、图片与原始快照…'}</div>`
    : '';
  const liveLine=progress&&x.active_job?`<div class="notice progress-note"><span class="spin"></span>云模型正在工作：已推理 <b>${progress.reasoning||0}</b> 字、已写出 <b>${progress.content||0}</b> 字，用时 ${progress.elapsed||0} 秒。数字还在涨就说明没有卡住。</div>`:'';
  const deadLine=(!x.active_job&&x.last_job&&x.last_job.state==='DEAD')
    ? `<div class="notice error-text">上次「${x.last_job.kind==='analyze_item'?'精读':'归档'}」失败：${esc((x.last_job.error||'未知原因').slice(0,300))}（${time(x.last_job.updated_at)}）</div>`:'';
  const manual=x.excluded_at&&(x.exclusion_reason||'').startsWith('手动删除');
  const excludedLine=x.excluded_at?`<div class="notice"><b>${manual?'你已经把这条从资料库删除了':'这条已经被本地过滤规则移出主资料库'}</b>：${esc(x.exclusion_reason||'未记录原因')}。<br><small>正文和图片仍然保留；如果判断有误，可以恢复收录。</small> <button type="button" class="primary" data-restore="${x.id}">恢复收录</button></div>`:'';
  const gone=(x.metadata||{}).source_status||null;
  const goneLine=gone&&gone.state==='deleted'?`<div class="notice"><b>原文已被发布者删除</b>：${esc(gone.reason||'原页面无法访问')}。<br><small>本地已保存离线页，所以这条继续留在资料库；内容以离线页为准。</small></div>`:'';
  return `${excludedLine}${goneLine}${jobLine}${liveLine}${deadLine}${x.error?`<div class="notice error-text">${esc(x.error)}</div>`:''}`;
}
function paperReady(x){return ((x&&x.papers)||[]).filter(p=>p&&p.local_path)}
function paperLabel(p){
  const doi=String((p&&p.doi)||'');
  if(!doi||doi.startsWith('upload/'))return '手动上传的 PDF';
  return doi;
}
/* 论文栏：有 PDF 就直接在栏里看，没有就给「再找一次 / 手动上传」两条路。
   以前这里只有居中的网页预览弹层，想和精读并排看根本做不到。 */
function paperPaneHTML(x,opts={}){
  const ready=paperReady(x);
  const all=(x&&x.papers)||[];
  if(!ready.length){
    const tries=all.length?`<ul class="wb-paper-tries">${all.map(p=>`<li><code>${esc(paperLabel(p))}</code> · ${esc(p.status||'未下载')}${p.error?` · ${esc(String(p.error).slice(0,140))}`:''}</li>`).join('')}</ul>`:'';
    return `<div class="wb-paper-pane"><div class="wb-empty wb-paper-empty">
      <div class="wb-paper-mark">PDF</div>
      <b>尚未下载到原论文</b>
      <p class="muted">这条推文没有能直接下载的开放论文——可能是付费墙、出版社限制，或者正文里没识别到 DOI。可以让软件再找一次，也可以把手动下载好的 PDF 传上来。</p>
      ${tries}
      <div class="wb-paper-actions">
        <button type="button" class="primary" data-paper-fetch="${x.id}">让软件去找论文</button>
        <button type="button" data-paper-upload="${x.id}">上传 PDF</button>
      </div>
      <p class="muted wb-paper-tip">上传后按这篇推文归档，并自动提取文字供精读引用。文件落在 data/papers/ 下这篇的文件夹里。</p>
    </div></div>`;
  }
  const index=Math.max(0,Math.min(opts.index||0,ready.length-1));
  const current=ready[index];
  const picker=ready.length>1?`<select class="wb-paper-pick" data-paper-switch="${x.id}" aria-label="切换这篇推文的论文">${ready.map((p,i)=>`<option value="${i}"${i===index?' selected':''}>${esc(String(p.title||paperLabel(p)).slice(0,70))}</option>`).join('')}</select>`:'';
  return `<div class="wb-paper-pane">
    <div class="wb-paper-bar">
      <div class="wb-paper-meta"><b>${esc(current.title||'原论文')}</b><span>${esc(paperLabel(current))}${current.year?` · ${esc(current.year)}`:''}</span></div>
      ${picker}
      <div class="wb-paper-buttons">
        <button type="button" data-paper-upload="${x.id}" data-paper-upload-target="${current.id}">换一份</button>
        <button type="button" data-paper-wps="${current.id}">WPS 打开</button>
        <button type="button" data-paper-reveal="${current.id}">所在目录</button>
      </div>
    </div>
    <iframe class="wb-paper-frame" src="/api/papers/${encodeURIComponent(current.id)}/file#view=FitH&navpanes=0" title="原论文 PDF"></iframe>
  </div>`;
}
function wbPapers(x,target='workbench'){
  const ready=paperReady(x);
  const open=target==='classic'?'data-classic-view="paper"':'data-wb-paper-open';
  if(!ready.length){
    const count=((x&&x.papers)||[]).length;
    return `<div class="detail-section"><h2>关联论文</h2><p class="muted">尚未下载到原论文${count?`：已识别到 ${count} 篇，但都没能下载下来`:''}。到「论文」栏可以再找一次，或手动上传 PDF。</p><div class="actions"><button type="button" ${open}>打开论文栏</button></div></div>`;
  }
  return `<div class="detail-section"><h2>关联论文</h2>${ready.map(p=>`<p><b>${esc(p.title||'原论文')}</b><br><small>${esc(paperLabel(p))}${p.year?` · ${esc(p.year)}`:''}</small></p>`).join('')}<div class="actions"><button type="button" ${open}>在论文栏阅读</button></div></div>`;
}
function wbReadingHTML(x){
  const a=x.analysis||{};
  const insufficient=a.analysis_mode==='insufficient_source';
  const recordOnly=a.analysis_mode==='record_only';
  const recordNote=recordOnly?`<div class="notice"><b>图文短记</b>：这条没有可精读的论文，内容主要是正文与配图本身，已按原样保存，没有调用云模型精读。<br><small>${esc((a.limits||[])[0]||'')}</small></div>`:'';
  const main=readingBody(a,x.id);
  const body=insufficient
    ? `<div class="notice insufficient"><b>无法详细归档</b><br>${esc(a.summary||'没有取得足以讲清这篇研究的材料。')}<br><small>为避免用只言片语编造解释，这里不做猜测性精读。</small></div>`
    : (recordNote+(main?`${main}${figureBlock(x)}${afterBlock(a)}`:`<section class="reading">${legacyBody(a)}${figureBlock(x)}${afterBlock(a)}</section>`));
  return `${wbBadges(x)}${wbNotices(x)}${body}
    <details class="raw-text"><summary>归档全文 / 转录</summary><div class="detail-text">${esc(x.text||'尚无正文')}</div></details>
    ${wbPapers(x)}`;
}
function wbRenderHeader(x){
  $('#wbTitle').textContent=x.title||x.url||'未命名条目';
  const when=time(x.published_at||x.discovered_at);
  $('#wbMeta').textContent=[x.author,when,x.excluded_at?'已移出主资料库':''].filter(Boolean).join(' · ');
  // 按钮一行排开，放不下的用两侧小箭头翻页。这个位置只有一行的宽度，
  // 之前用 flex-wrap 换行会把标题栏顶成两层，所以改成「一条可翻页的轨道」。
  // 分两组：常驻的（问 AI、删除/恢复）不进轨道，剩下的按「两页刚好装下」分页。
  // 每一页是独立容器，翻页是把整条 reel 平移过去——第二页里不可能混进第一页的按钮。
  // 第一页固定四个：看原文 / 查原文 / 收藏。第二页放「重做」和「屏蔽」。
  // 不再按宽度动态找切点——切点一变，用户记住的「第几页有什么」就乱了，翻页还会跟着抖。
  const first=[
    {k:'打开原文',h:`<a class="button" href="${esc(x.url)}" target="_blank" rel="noopener">打开原文</a>`},
    {k:'检查原文',h:`<button type="button" data-verify-source="${x.id}">检查原文</button>`},
    {k:'收藏到',h:`<button type="button" data-collect="${x.id}" data-collect-type="item" data-collect-ids="${(x.collections||[]).join(',')}">收藏到…</button>`},
    {k:'智能收藏',h:`<button type="button" data-smart-collect="${x.id}">智能收藏</button>`}
  ].filter(a=>a.h);
  const more=[
    // 原始短链是「换一条路看原文」，不占第一页那四个名额。
    {k:'原始短链',h:x.metadata&&x.metadata.original_share_url&&x.metadata.original_share_url!==x.url?`<a class="button" href="${esc(x.metadata.original_share_url)}" target="_blank" rel="noopener">原始短链</a>`:''},
    {k:'重新精读',h:x.excluded_at?'':`<button type="button" data-reanalyze="${x.id}">重新精读</button>`},
    {k:'重新检查配图',h:((x.metadata||{}).images||[]).length?`<button type="button" data-retriage="${x.id}">重新检查配图</button>`:''},
    {k:'屏蔽此类',h:`<button type="button" data-rule-from="${esc(x.author||'')}">屏蔽此类</button>`}
  ].filter(a=>a.h);
  const acts=first.concat(more);
  const pinned=[
    `<button type="button" class="primary" data-wb-ask>问 AI</button>`,
    x.excluded_at?`<button type="button" class="primary" data-restore="${x.id}">恢复收录</button>`
                 :`<button type="button" class="danger" data-delete="${x.id}">删除</button>`
  ];
  // 轮询每 3 秒刷一次顶栏。按钮集合没变就别重建——重建一次光标下的按钮就换人了，
  // 用户会觉得「点了 A 却触发了 B」，看上去也像抽了一下。
  const sig=[x.id,x.url,x.excluded_at,(x.metadata||{}).original_share_url,
    ((x.metadata||{}).images||[]).length,(x.collections||[]).join(','),x.author].join('|');
  if(wb.actSig!==sig){
    wb.actSig=sig;
    wb.actItems=acts.map(a=>a.h);
    wb.actSplit=first.length;          // 第一页永远是这四个，剩下的都在第二页
    $('#wbItemActions').innerHTML=`<button type="button" class="wb-act-nav" data-wb-act="-1" aria-label="上一组操作" title="上一组操作" disabled>‹</button>`
      +`<div class="wb-act-track" id="wbActTrack" role="group" aria-label="条目操作"><div class="wb-act-reel" id="wbActReel">`
      +`<div class="wb-act-page">${wb.actItems.join('')}</div>`
      +`</div></div>`
      +`<button type="button" class="wb-act-nav" data-wb-act="1" aria-label="下一组操作" title="下一组操作" disabled>›</button>`
      +`<div class="wb-act-pinned" id="wbActPinned">${pinned.join('')}</div>`;
    // 重建后回到用户刚才那一页：翻页位置记在 wb.actPage 里，不会自己弹回第一页。
    syncWbActions();
    watchWbActions();
  }
  wbRenderJob(x);
  // 工作台不在 #detailContent 的 MutationObserver 范围内，得主动通知对话面板换上下文。
  if(window.syncCurrentItem)window.syncCurrentItem();
}
/* 顶栏任务状态：只回答「点的按钮现在到哪了」。
   active_job 有值就转圈；精读还带已读字数与耗时（worker 每秒写一次 progress:*）。
   没有任务但上次失败，就把失败留在原地，别让人以为还在跑。 */
function wbJobText(x){
  const job=x&&x.active_job, p=x&&x.progress;
  if(job==='analyze'){
    if(p&&p.phase==='analyzing'){
      const chars=Number(p.reasoning||0)+Number(p.content||0);
      const spent=Number(p.elapsed||0);
      return `正在精读 · 已读 ${chars>=1000?Math.round(chars/1000)+'k 字':(chars||'…')+' 字'}`
        +(spent?` · ${spent<60?spent+' 秒':Math.floor(spent/60)+' 分 '+spent%60+' 秒'}`:'');
    }
    return '正在精读这篇…';
  }
  if(job==='archive')return '正在归档原文与配图…';
  if(job==='verify')return '原文要人机验证，等它放开…';
  return '';
}
function wbRenderJob(x){
  const el=$('#wbJob');if(!el)return;
  const text=wbJobText(x);
  const failed=!text&&x&&x.last_job&&x.last_job.error;
  if(text){
    el.hidden=false;el.classList.remove('failed');
    el.innerHTML=`<i class="spin"></i><span>${esc(text)}</span>`;
  }else if(failed){
    el.hidden=false;el.classList.add('failed');
    el.textContent=`上次处理失败：${String(x.last_job.error).slice(0,60)}`;
  }else{
    el.hidden=true;el.textContent='';
  }
}
/* 分页规则：第一页固定那四个（打开原文 / 检查原文 / 收藏到… / 智能收藏），第二页放重做与屏蔽。
   切点不随窗口大小变——「第几页有哪些按钮」必须稳定，窗口只决定条子多宽、要不要收字号。 */
function wbPlanPages(widths){
  const n=widths.length,gap=5;
  const span=(a,b)=>widths.slice(a,b).reduce((s,w)=>s+w,0)+Math.max(0,b-a-1)*gap;
  const k=Math.max(1,Math.min(wb.actSplit||4,n));
  return {k,w1:span(0,k),w2:span(k,n)};
}
function wbLayoutPages(k){
  const reel=$('#wbActReel'),track=$('#wbActTrack');
  const items=wb.actItems||[];
  if(!reel||!items.length)return;
  const cut=Math.max(1,Math.min(items.length,k||items.length));
  reel.innerHTML=`<div class="wb-act-page">${items.slice(0,cut).join('')}</div>`
    +(cut<items.length?`<div class="wb-act-page">${items.slice(cut).join('')}</div>`:'');
  if(track)track.dataset.split=String(cut);
}
/* 量一次：所有滚动按钮的宽度，以及轨道最多能用多宽（行宽扣掉箭头与常驻按钮）。
   注意 compact 会改变按钮宽度，所以先按「不紧凑」量，装不下再按紧凑量一次——顺序固定，
   不会出现「切紧凑→装得下→切回去→又装不下」的来回跳。 */
function wbActMetrics(){
  const row=$('#wbItemActions'),track=$('#wbActTrack'),reel=$('#wbActReel');
  if(!row||!track||!reel)return null;
  const widths=[...reel.querySelectorAll('.wb-act-page > *')].map(c=>c.offsetWidth);
  const all=[...row.children];
  const others=all.filter(c=>c!==track).reduce((s,c)=>s+c.offsetWidth,0);
  const gaps=Math.max(0,all.length-1)*4;   // .wb-item-actions 的 gap:4px
  const avail=Math.max(0,row.clientWidth-others-gaps);
  return {row,track,reel,widths,avail};
}
/* dialog 打开前量到的是 0 宽，此时先跳过；showModal 后补一帧，之后交给 ResizeObserver。
   ResizeObserver 自己已经按帧合并，不再套 requestAnimationFrame：窗口最小化或后台时
   rAF 会被挂起，恢复以后就停在旧宽度上，第二页被裁掉一半，看起来像「窗口变了它没变」。 */
let wbActRO=null,wbActBusy=false;
function watchWbActions(){
  const row=$('#wbItemActions');
  if(!row||!window.ResizeObserver)return;
  if(!wbActRO)wbActRO=new ResizeObserver(()=>{
    if(wbActBusy)return;                 // 改宽度会再触发一次，别套娃
    wbActBusy=true;
    try{syncWbActions()}finally{wbActBusy=false}
  });
  wbActRO.disconnect();
  wbActRO.observe(row);
}
function syncWbActions(){
  const row=$('#wbItemActions');
  if(!row||!wbIsOpen())return;           // 关着的时候不量：0 宽会把紧凑样式摘掉，下次打开就错位
  const probe=(compact)=>{
    row.classList.toggle('compact',compact);
    const m=wbActMetrics();
    if(!m||!m.widths.length||!m.widths[0])return null;
    const plan=wbPlanPages(m.widths);
    return {m,plan,fits:plan.w1<=m.avail&&plan.w2<=m.avail};
  };
  const full=probe(false);
  if(!full){row.classList.remove('compact');return}   // 还没布局：等 showModal 后的那一帧
  let pick=full,compact=false;
  if(!full.fits){                          // 正常字号装不下 → 收一号再试
    const tight=probe(true);
    if(tight){pick=tight;compact=true}
  }
  row.classList.toggle('compact',compact);
  const {track}=pick.m;
  if(Number(track.dataset.split||0)!==pick.plan.k)wbLayoutPages(pick.plan.k);
  // 条子宽度 = 最宽那页，受可用空间约束：窗口一动，这里就跟着动
  track.style.width=Math.max(120,Math.min(Math.max(pick.plan.w1,pick.plan.w2),pick.m.avail||pick.plan.w1))+'px';
  wbSetActPage(wb.actPage||0);
}
/* 翻页 = 把整条 reel 平移一页的距离（每页都是轨道满宽，所以正好对齐、绝不重叠）。
   平移不参与布局，不会像 scroll + smooth 那样被重排打断、「滑一半弹回去」。 */
function wbSetActPage(index,animate){
  const track=$('#wbActTrack'),reel=$('#wbActReel');
  if(!track||!reel)return;
  const pages=[...reel.children];
  if(!pages.length)return;
  const i=Math.max(0,Math.min(pages.length-1,index));
  wb.actPage=i;
  const dx=pages[i].offsetLeft;          // offsetLeft 以轨道为参照：正好把这一页左边缘对齐
  // 只有用户点箭头时才做滑行动画；刷新与还原必须瞬时，否则会看到条子自己「抽」一下。
  if(!animate)reel.style.transition='none';
  reel.style.transform=dx?`translateX(${-dx}px)`:'';
  if(!animate){void reel.offsetWidth;reel.style.transition=''}
  track.dataset.page=String(i);
  const prev=$('[data-wb-act="-1"]'),next=$('[data-wb-act="1"]');
  if(prev)prev.disabled=i<=0;
  if(next)next.disabled=i>=pages.length-1;
}
function wbStepActPage(dir){
  wbSetActPage((wb.actPage||0)+dir,true);
}
let wbActResizeTimer=0;
window.addEventListener('resize',()=>{
  if(wbActResizeTimer)clearTimeout(wbActResizeTimer);
  wbActResizeTimer=setTimeout(()=>{wbActResizeTimer=0;syncWbActions()},120);
});
/* 手动上传原论文：用原生文件选择器，选完直接 POST 二进制，不经过表单。
   成功后 refreshOpenReader 会把论文栏和精读底部的「关联论文」一起刷新。 */
function uploadPaper(itemId,paperId){
  const input=document.createElement('input');
  input.type='file';input.accept='application/pdf,.pdf';input.style.display='none';
  document.body.appendChild(input);
  input.onchange=async()=>{
    const file=input.files&&input.files[0];
    input.remove();
    if(!file)return;
    if(file.size>100*1024*1024){toast('PDF 超过 100 MB，先压缩或拆分再传');return}
    toast('正在上传并解析 PDF…');
    try{
      const url=`/api/items/${encodeURIComponent(itemId)}/paper?filename=${encodeURIComponent(file.name)}`
        +(paperId?`&paper_id=${encodeURIComponent(paperId)}`:'');
      const r=await fetch(url,{method:'POST',body:file,headers:{'Content-Type':'application/pdf'}});
      const data=await r.json().catch(()=>({}));
      if(!r.ok)throw new Error(data.detail||'上传失败');
      toast(`已归档${data.pages?`，共 ${data.pages} 页`:''}${data.chars?`，提取 ${data.chars} 字`:''}`);
      if($('#detailDialog').open&&classicItemId===itemId)await showItemClassic(itemId,true,true);
      else await refreshOpenReader(itemId);
    }catch(err){toast(err.message)}
  };
  input.click();
}
async function fetchItemPapers(itemId){
  try{
    const r=await api(`/api/items/${itemId}/papers/fetch`,{method:'POST'});
    toast(r.already_running?'已经在找了，完成后论文栏会自动刷新':'已开始查找开放论文，完成后论文栏会出现 PDF');
    watchItem(itemId);
  }catch(e){toast(e.message)}
}
function askCurrentItem(){
  const item=window.__currentItem||wb.item;
  if(window.openChat)window.openChat({itemId:item?item.id:undefined,selection:''}).catch(err=>toast(err.message));
  else toast('对话面板还没加载好，稍等一秒再点');
}
async function openWorkbench(id,opts={}){
  const x=await api('/api/items/'+id);
  window.__currentItem=x;
  const sameItem=wb.itemId===x.id;
  wb.item=x;wb.itemId=x.id;wb.open=true;
  if(!sameItem)wb.paperIndex=0;
  if(!sameItem){wb.actPage=0;wb.actSig='';}   // 换条目：按钮集合可能变了，翻页位置从头算
  wb.sig=wbItemSig(x);
  wbRenderHeader(x);
  const el=wbEl();
  if(!el.open){
    el.showModal();
    lockPageScroll();
    // 刚 showModal，布局还没算出来；补一帧再判箭头启停。
    requestAnimationFrame(()=>syncWbActions());
    wb.panes=[];$('#wbBody').innerHTML='';
    wbAddPane('read');
  }else if(!wb.panes.length){
    wbAddPane('read');
  }else if(!sameItem){
    // 换了条目：原文 iframe 换地址，精读与笔记按当前栏重建；Ideas 是全局的，不必动。
    wb.panes.forEach(p=>{
      if(p.view==='read'){const box=wbViewEl(p,'read');if(box)wbMountReader(box,x)}
      else if(p.view==='reading')wbSetView(p,'reading',{force:true});
      else if(p.view==='notes')wbRenderNotes(p);
    });
  }else{
    wb.panes.forEach(p=>{if(p.view==='reading')wbSetView(p,'reading',{force:true})});
  }
  wbSyncLayout();
  requestAnimationFrame(()=>syncWbActions());
  if(x.active_job)watchItem(id);else if(detailWatchId===id)stopItemWatch();
  return x;
}
/* 这一篇「真正的内容」变没变。轮询每 3 秒回来一次，进度数字在变但正文没变；
   按整篇重画会把正在读的那一栏滚回顶部、把 PDF 栏的 iframe 重新加载一次，
   看起来就是「隔几秒抽一下」。所以只比会影响渲染结果的字段。 */
function wbItemSig(x){
  const a=(x&&x.analysis)||{},m=(x&&x.metadata)||{},j=(x&&x.last_job)||null;
  return JSON.stringify([x&&x.status,x&&x.excluded_at,x&&x.importance,x&&x.relevance,x&&x.error,
    m.archive_completeness,(m.image_triage||{}).dropped,m.source_status,
    ((x&&x.papers)||[]).map(p=>[p.id,p.status,p.local_path,p.title,p.error]),
    j?[j.state,j.error,j.updated_at]:null,a]);
}
async function refreshWorkbench(id){
  if(!wbIsOpen()||wb.itemId!==id)return null;
  const x=await api('/api/items/'+id);
  window.__currentItem=x;wb.item=x;
  wbRenderHeader(x);                 // 标题、任务状态每次都要新；按钮行自己有签名，不会重建
  const sig=wbItemSig(x);
  if(sig===wb.sig)return x;          // 只是进度在涨：正文不动，别打断阅读
  wb.sig=sig;
  wb.panes.forEach(p=>{
    if(p.view==='read'){
      const box=wbViewEl(p,'read');
      // 归档刚完成时把占位换成真正的离线页；已经加载好的 iframe 不重建，保住滚动位置。
      if(box&&!box.querySelector('iframe')&&x.html_path)wbMountReader(box,x);
    }else if(p.view==='reading'){wbSetView(p,'reading',{force:true})}
    else if(p.view==='paper'){wbSetView(p,'paper',{force:true})}
    else if(p.view==='notes'&&!($('#wbNoteDraft')&&document.activeElement===$('#wbNoteDraft'))){wbRenderNotes(p)}
  });
  return x;
}
function closeWorkbench(){
  const el=wbEl();if(!el||!el.open)return;
  stopItemWatch();
  wb.open=false;wb.itemId=null;wb.item=null;wb.panes=[];
  wb.sig='';wb.actSig='';
  $('#wbBody').innerHTML='';$('#wbBody').dataset.count='0';
  el.close();
}
function wbToggleSide(view){
  if(!wb.panes.length)return;
  // 「原文」= 收起并排、回到只看原文：已经开了第二栏就收掉；只剩别的视图就切回原文。
  if(view==='read'){
    if(wb.panes.length>1)wbRemovePane(wb.panes[1].key);
    else wbSetView(wb.panes[0],'read');
    return;
  }
  if(wb.panes.length>1){
    const right=wb.panes[1];
    if(right.view===view)wbRemovePane(right.key);else wbSetView(right,view);
    return;
  }
  const left=wb.panes[0];
  // 唯一栏已经是它：把原文放回左栏、目标视图放到右栏，形成并排；直接收掉会把用户想看的内容弄丢。
  if(left.view===view){wbSetView(left,'read');wbAddPane(view);return}
  wbAddPane(view);
}
async function wbRenderNotes(pane){
  const box=wbViewEl(pane,'notes');if(!box)return;
  const seq=++wb.noteSeq;
  box.innerHTML=`<div class="wb-compose">
      <textarea id="wbNoteDraft" rows="3" placeholder="边读边记：这一段说明了什么？和我的课题有什么关系？"></textarea>
      <div class="wb-compose-actions"><button type="button" class="primary" data-wb-note-save>存下这段想法</button><span class="muted" id="wbNoteHint"></span></div>
    </div>
    <div id="wbNoteList" class="wb-note-list"><div class="muted wb-loading">正在读取这篇的笔记…</div></div>`;
  try{
    const rows=await api('/api/notes?limit=200&item_id='+encodeURIComponent(wb.itemId));
    if(seq!==wb.noteSeq)return;
    renderWbNotes(rows);
  }catch(e){const list=$('#wbNoteList');if(list)list.innerHTML=`<div class="error-text">${esc(e.message)}</div>`}
}
function renderWbNotes(rows){
  const list=$('#wbNoteList');if(!list)return;
  list.innerHTML=rows.length?rows.map(n=>`<article class="wb-note">
    ${n.quote?`<blockquote>${esc(n.quote)}</blockquote>`:''}
    <p>${esc(n.body).replace(/\n/g,'<br>')}</p>
    <div class="wb-note-foot"><span>${time(n.created_at)}</span><span>${esc(n.section||'')}</span><button type="button" class="link" data-wb-note-del="${esc(n.id)}">删除</button></div>
  </article>`).join(''):'<div class="wb-empty muted">这篇还没有笔记。读到关键处，在上面写两句，或者回精读页点段落右上角的「存笔记」。</div>';
}
async function saveWbNote(){
  const box=$('#wbNoteDraft');if(!box)return;
  const body=(box.value||'').trim();
  if(!body)return toast('先写点什么再存');
  const hint=$('#wbNoteHint');if(hint)hint.textContent='正在保存…';
  try{
    await api('/api/notes',{method:'POST',body:{item_id:wb.itemId,section:'工作台随手记',body,tags:[]}});
    box.value='';
    renderWbNotes(await api('/api/notes?limit=200&item_id='+encodeURIComponent(wb.itemId)));
    if(hint)hint.textContent='已保存';
    toast('笔记已保存到这篇');
  }catch(e){if(hint)hint.textContent='';toast(e.message)}
}
async function deleteWbNote(id){
  if(!confirm('删除这条笔记？'))return;
  await api('/api/notes/'+id,{method:'DELETE'});
  renderWbNotes(await api('/api/notes?limit=200&item_id='+encodeURIComponent(wb.itemId)));
  toast('笔记已删除');
}
async function wbRenderIdeas(pane){
  const box=wbViewEl(pane,'ideas');if(!box)return;
  box.innerHTML=`<div class="wb-compose">
      <input id="wbIdeaTitle" placeholder="一句话写下这个想法（假设 / 方向 / 长期问题）">
      <textarea id="wbIdeaBody" rows="3" placeholder="背景、已知证据、怎么验证……（可不填）"></textarea>
      <div class="wb-compose-actions"><button type="button" class="primary" data-wb-idea-save>存为 idea</button><span class="muted" id="wbIdeaHint"></span></div>
    </div>
    <div class="wb-idea-search"><input id="wbIdeaQ" placeholder="搜索已有 idea"></div>
    <div id="wbIdeaList" class="wb-idea-list"><div class="muted wb-loading">正在读取 ideas…</div></div>`;
  await wbLoadIdeas();
}
async function wbLoadIdeas(){
  const list=$('#wbIdeaList');if(!list)return;
  const word=($('#wbIdeaQ')&&$('#wbIdeaQ').value||'').trim().toLowerCase();
  try{
    const rows=await api('/api/ideas');
    const hit=word?rows.filter(x=>`${x.title} ${x.content} ${(x.tags||[]).join(' ')}`.toLowerCase().includes(word)):rows;
    list.innerHTML=hit.length?hit.map(x=>`<article class="wb-idea"><h3>${esc(x.title)}</h3>${x.content?`<p>${esc(x.content).replace(/\n/g,'<br>')}</p>`:''}<div class="wb-idea-foot"><span>${time(x.updated_at||x.created_at)}</span>${(x.tags||[]).map(t=>`<span class="badge">${esc(t)}</span>`).join('')}</div></article>`).join(''):`<div class="wb-empty muted">${word?'没有匹配的 idea':'还没有 idea。上面写一句就能存下第一条。'}</div>`;
  }catch(e){list.innerHTML=`<div class="error-text">${esc(e.message)}</div>`}
}
async function saveWbIdea(){
  const title=($('#wbIdeaTitle')&&$('#wbIdeaTitle').value||'').trim();
  if(!title)return toast('先给这个想法起个标题');
  const hint=$('#wbIdeaHint');if(hint)hint.textContent='正在保存…';
  try{
    await api('/api/ideas',{method:'POST',body:{title,content:($('#wbIdeaBody')&&$('#wbIdeaBody').value||'').trim(),tags:[],enabled:true}});
    $('#wbIdeaTitle').value='';$('#wbIdeaBody').value='';
    await wbLoadIdeas();
    if(hint)hint.textContent='已存为 idea';
    toast('idea 已保存，会参与后续文章关联');
  }catch(e){if(hint)hint.textContent='';toast(e.message)}
}
$('#wbClose').onclick=()=>closeWorkbench();
// 用户明确要求「只有点关闭按钮才关」。
// 只拦 cancel 不够：浏览器合成的 cancel 事件是 cancelable:false，preventDefault 无效，
// 必须在 keydown 的默认动作之前截住。有更上层的浮层（对话抽屉/划词/笔记/收藏）时放行，
// 让 Esc 先关它们——否则会出现「按 Esc 抽屉没关，工作台也没关」。
$('#workbench').addEventListener('keydown',e=>{
  if(e.key!=='Escape')return;
  if(document.querySelector('#chatDrawer.open,#notePop:popover-open,#selectionBar:popover-open,#collectionPop[open]'))return;
  e.preventDefault();e.stopPropagation();
},true);
$('#workbench').addEventListener('cancel',e=>e.preventDefault());
$('#wbSideSwitch').addEventListener('click',e=>{
  const b=e.target.closest('[data-wb-side]');if(!b)return;
  wbToggleSide(b.dataset.wbSide);
});
$('#wbItemActions').addEventListener('click',e=>{
  // 顶栏不在 #wbBody 里，事件冒泡不到下面那个监听；其余按钮走全局委托。
  if(e.target.closest('[data-wb-ask]'))askCurrentItem();
});
$('#wbBody').addEventListener('click',async e=>{
  const close=e.target.closest('[data-wb-pane-close]');
  if(close){const pane=close.closest('.wb-pane');if(pane)wbRemovePane(pane.dataset.pane);return}
  const tab=e.target.closest('[data-wb-view]');
  if(tab){const host=tab.closest('.wb-pane');const pane=wb.panes.find(p=>p.key===(host&&host.dataset.pane));if(pane)wbSetView(pane,tab.dataset.wbView);return}
  if(e.target.closest('[data-wb-ask]'))return askCurrentItem();
  if(e.target.closest('[data-wb-note-save]'))return saveWbNote().catch(err=>toast(err.message));
  if(e.target.closest('[data-wb-idea-save]'))return saveWbIdea().catch(err=>toast(err.message));
  const del=e.target.closest('[data-wb-note-del]');
  if(del)return deleteWbNote(del.dataset.wbNoteDel).catch(err=>toast(err.message));
});
$('#wbBody').addEventListener('input',e=>{
  if(e.target.id!=='wbIdeaQ')return;
  clearTimeout(wbIdeaTimer);
  wbIdeaTimer=setTimeout(()=>wbLoadIdeas().catch(err=>toast(err.message)),220);
});
/* ---------- 两套阅读排布，设置里可切 ----------
   workbench = 上文的阅读工作台（默认）；classic = 这一版之前的老界面。
   老界面不能删：有人就习惯「先看精读、要配图时再去浏览器」这一路。
   两套共用 watchItem / __currentItem / 收藏与删除的全局委托，所以切换只是换渲染入口。 */
let readingMode=(()=>{try{return localStorage.getItem('radar.readingMode')==='classic'?'classic':'workbench'}catch(e){return 'workbench'}})();
function setReadingMode(mode){
  readingMode=mode==='classic'?'classic':'workbench';
  try{localStorage.setItem('radar.readingMode',readingMode)}catch(e){}
  const sel=$('#uiReadingMode');if(sel)sel.value=readingMode;
  const hint=$('#uiReadingHint');
  if(hint)hint.textContent=readingMode==='classic'
    ?'经典弹层：点条目先看原文，用弹层顶部的「原文 / 精读 / 论文」切换；离线页仍可另开浏览器。适合精读为主、原文只作核对。'
    :'工作台：点条目直接在软件里读原文（配图按原文位置显示）；点「精读」原文让到左边、精读从右侧进来，两栏并排。论文、笔记与 Ideas 抢同一块右栏，边读边记不用退出。';
}
// 旧排布：精读弹层 + 离线页走浏览器。逻辑与工作台之前完全一致，只是入口多了个分支。
let classicView='read',classicItemId=null,classicPaperIndex=0,classicPaperCount=0;
async function showItemClassic(id,silent,forceRender){
  const seq=++detailSeq;
  const x=await api('/api/items/'+id),a=x.analysis||{},stage=itemStage(x);
  // A slower response for the previous item must not overwrite the one just opened.
  if(seq!==detailSeq)return x;
  if(detailWatchId&&detailWatchId!==id)stopItemWatch();
  window.__currentItem=x;
  const same=classicItemId===id;
  // 点开推文先看原文：换条目或重新打开时都回到 read；只有本来就没归档正文时才落到精读。
  if(!same||!$('#detailDialog').open){classicView=x.html_path?'read':'reading';classicPaperIndex=0}
  classicItemId=id;
  // 后台刷新别重建原文/论文的 iframe——每隔几秒把阅读位置顶回顶部比不刷新更烦。
  // 例外：论文栏里「刚下载好」或「刚上传」导致 PDF 数量变了，必须重画一次。
  if(same&&silent&&!forceRender&&classicView!=='reading'){
    if(classicView!=='paper'||paperReady(x).length===classicPaperCount)return x;
  }
  const paperCount=paperReady(x).length;
  if(classicView==='paper')classicPaperCount=paperCount;
  const progress=x.progress||null;
  const jobLine=x.active_job
    ? `<div class="notice progress-note"><span class="spin"></span>${x.active_job==='analyze'?'正在调用云模型精读，完成后本页自动刷新…':x.active_job==='verify'?'微信要求人机验证，正文暂时抓不到；30 分钟后自动重试，链接和标题都已保留…':'正在抓取正文、图片与原始快照…'}</div>`
    : '';
  const liveLine=progress&&x.active_job?`<div class="notice progress-note"><span class="spin"></span>云模型正在工作：已推理 <b>${progress.reasoning||0}</b> 字、已写出 <b>${progress.content||0}</b> 字，用时 ${progress.elapsed||0} 秒。数字还在涨就说明没有卡住。</div>`:'';
  const deadLine=(!x.active_job&&x.last_job&&x.last_job.state==='DEAD')
    ? `<div class="notice error-text">上次「${x.last_job.kind==='analyze_item'?'精读':'归档'}」失败：${esc((x.last_job.error||'未知原因').slice(0,300))}（${time(x.last_job.updated_at)}）</div>`:'';
  const elapsed=fmtMs(a.llm_meta&&a.llm_meta.elapsed_ms);
  const insufficient=a.analysis_mode==='insufficient_source';
  const recordOnly=a.analysis_mode==='record_only';
  const recordNote=recordOnly?`<div class="notice"><b>图文短记</b>：这条没有可精读的论文，内容主要是正文与配图本身，已按原样保存，没有调用云模型精读。<br><small>${esc((a.limits||[])[0]||'')}</small></div>`:'';
  const main=readingBody(a,x.id);
  const manual=x.excluded_at&&(x.exclusion_reason||'').startsWith('手动删除');
  const excludedLine=x.excluded_at?`<div class="notice"><b>${manual?'你已经把这条从资料库删除了':'这条已经被本地过滤规则移出主资料库'}</b>：${esc(x.exclusion_reason||'未记录原因')}。<br><small>正文和图片仍然保留；如果判断有误，可以恢复收录。</small> <button type="button" class="primary" data-restore="${x.id}">恢复收录</button></div>`:'';
  const goneStatus=(x.metadata||{}).source_status||null;
  const goneLine=goneStatus&&goneStatus.state==='deleted'?`<div class="notice"><b>原文已被发布者删除</b>：${esc(goneStatus.reason||'原页面无法访问')}。<br><small>本地已保存离线页，所以这条继续留在资料库；内容以离线页为准。</small></div>`:'';
  const body=insufficient
    ? `<div class="notice insufficient"><b>无法详细归档</b><br>${esc(a.summary||'没有取得足以讲清这篇研究的材料。')}<br><small>为避免用只言片语编造解释，这里不做猜测性精读。</small></div>`
    : (recordNote+(main?`${main}${figureBlock(x)}${afterBlock(a)}`:`<section class="reading">${legacyBody(a)}${figureBlock(x)}${afterBlock(a)}</section>`));
  const badges=`<p><span class="badge ${x.status==='ready'?'ok':x.status==='error'?'error':x.status==='discovered'?'wait':''}">${esc(stage.badge)}</span> <span class="badge">重要度 ${Math.round(x.importance)}</span> <span class="badge">相关度 ${Math.round(x.relevance)}</span> <span class="badge">证据 ${esc((x.metadata||{}).archive_completeness||'未知')}</span>${elapsed?` <span class="badge">精读用时 ${elapsed}</span>`:''}${((x.metadata||{}).image_triage||{}).dropped?` <span class="badge">已过滤 ${x.metadata.image_triage.dropped} 张无用配图</span>`:''}</p>`;
  const acts=[x.html_path?`<a class="button" href="/api/items/${x.id}/archive" target="_blank">离线阅读页</a>`:'',
    `<a class="button" href="${esc(x.url)}" target="_blank" rel="noopener">打开原文</a>`,
    x.metadata&&x.metadata.original_share_url&&x.metadata.original_share_url!==x.url?`<a class="button" href="${esc(x.metadata.original_share_url)}" target="_blank" rel="noopener">原始分享短链</a>`:'',
    x.excluded_at?'':(x.status==='discovered'?`<button class="primary" data-process="${x.id}">让软件去找原文</button>`:`<button data-reanalyze="${x.id}">重新精读</button>`),
    `<button type="button" data-verify-source="${x.id}">检查原文</button>`,
    ((x.metadata||{}).images||[]).length?`<button type="button" data-retriage="${x.id}">重新检查配图</button>`:'',
    `<button type="button" data-collect="${x.id}" data-collect-type="item" data-collect-ids="${(x.collections||[]).join(',')}">收藏到…</button>`,
    `<button type="button" data-smart-collect="${x.id}">智能收藏</button>`,
    x.excluded_at?`<button type="button" class="primary" data-restore="${x.id}">恢复收录</button>`:`<button type="button" class="danger" data-delete="${x.id}">删除</button>`,
    `<button type="button" data-rule-from="${esc(x.author||'')}">以后屏蔽这类</button>`].filter(Boolean).join('');
  const tabs=[['read','原文'],['reading','精读'],['paper','论文']].map(([key,label])=>
    `<button type="button" role="tab" data-classic-view="${key}" aria-selected="${classicView===key}">${label}</button>`).join('');
  const readBody=x.html_path
    ? `<div class="classic-read"><iframe data-src="/api/items/${encodeURIComponent(x.id)}/archive" src="/api/items/${encodeURIComponent(x.id)}/archive" title="离线阅读页"></iframe></div>`
    : `<div class="notice">这篇还没有离线阅读页，先看精读。</div>`;
  const readingBodyHtml=`${excludedLine}${goneLine}${jobLine}${liveLine}${deadLine}${x.error?`<div class="notice error-text">${esc(x.error)}</div>`:''}${badges}${body}<details class="raw-text"><summary>归档全文 / 转录</summary><div class="detail-text">${esc(x.text||'尚无正文')}</div></details>${wbPapers(x,'classic')}`;
  const shown=classicView==='read'?readBody:classicView==='paper'?`<div class="classic-paper">${paperPaneHTML(x,{index:classicPaperIndex})}</div>`:readingBodyHtml;
  $('#detailContent').innerHTML=`<h1 class="detail-title">${esc(x.title||x.url)}</h1><p>${esc(x.author)} · ${time(x.published_at||x.discovered_at)}</p><div class="actions">${acts}${(x.collections||[]).map(cid=>{const n=collectionName('library',cid);return n?`<span class="collection-chip">${esc(n)}</span>`:''}).join('')}</div><div class="detail-tabs" role="tablist" aria-label="这篇显示哪一部分">${tabs}</div>${shown}`;
  guardArchiveFrame($('#detailContent .classic-read iframe'));
  if(!silent||!$('#detailDialog').open)openDetail();
  if(x.active_job)watchItem(id);else if(detailWatchId===id)stopItemWatch();
  return x;
}
async function switchClassicView(view){
  if(!['read','reading','paper'].includes(view)||!classicItemId)return;
  classicView=view;
  await showItemClassic(classicItemId,true,true);
}
// 打开条目：按设置分派。调用方（列表点击、笔记库、任务完成刷新）不用关心当前是哪套。
async function showItem(id,silent){
  if(readingMode==='classic')return showItemClassic(id,silent);
  if(wbIsOpen()&&wb.itemId===id)return refreshWorkbench(id);
  return openWorkbench(id,{silent:!!silent});
}
// 当前打开着这个条目就就地刷新，没开就什么都不做。归档/精读完成、恢复收录都走这里。
function refreshOpenReader(id){
  if(readingMode==='classic')return $('#detailDialog').open?showItemClassic(id,true):Promise.resolve(null);
  return wbIsOpen()?refreshWorkbench(id):Promise.resolve(null);
}
async function previewPaper(id){const p=currentPapers.find(x=>x.id===id)||{title:'PDF 预览'};$('#detailContent').innerHTML=`<h1>${esc(p.title||'PDF 预览')}</h1><p class="muted">文件保存在：<code>${esc(p.local_path||'论文目录')}</code></p><iframe src="/api/papers/${id}/file#view=FitH" style="width:100%;height:78vh;border:1px solid #dde3e9;border-radius:8px"></iframe>`;openDetail()}
let paperPage=1, paperPageSize=20, paperTotal=0;
async function loadPapers(){
  const status=$('#paperStatus').value;
  const data=await api(`/api/papers?status=${encodeURIComponent(status)}&limit=${paperPageSize}&offset=${(paperPage-1)*paperPageSize}`);
  const rows=data.items||[];paperTotal=Number(data.total||0);currentPapers=rows;
  const pages=Math.max(1,Math.ceil(paperTotal/paperPageSize));
  if(paperPage>pages&&paperTotal){paperPage=pages;return loadPapers()}
  $('#papersList').innerHTML=rows.map(p=>{const md=JSON.parse(p.metadata_json||'{}');const via=md.matched_by?`<br><small class="muted" title="这篇论文是怎么找到的">🔎 ${esc(md.matched_by)}</small>`:'';return `<tr><td><b>${esc(p.title||'题名待解析')}</b><br><small>来源：${esc(p.source_title||'')}</small>${via}</td><td>${esc(p.year)}<br>${esc(p.journal)}</td><td><a href="https://doi.org/${esc(p.doi)}" target="_blank">${esc(p.doi)}</a><br><small class="error-text" title="${esc(p.error||'')}">${esc(p.error||'')}</small></td><td><span class="badge ${p.status==='downloaded'?'ok':'wait'}">${esc(p.status)}</span></td><td>${p.local_path?`<div class="actions"><button data-pdf-preview="${p.id}">网页预览</button><button data-paper-wps="${p.id}">WPS 打开</button><button data-paper-reveal="${p.id}">所在目录</button></div><small title="${esc(p.local_path)}">${esc(p.local_path)}</small>`:''}<button data-paper-retry="${p.id}">重试</button></td></tr>`}).join('')||'<tr><td colspan="5" class="muted">当前清单为空</td></tr>';
  renderPaperPager();
}
function renderPaperPager(){
  const pages=Math.max(1,Math.ceil(paperTotal/paperPageSize));
  const box=$('#paperPager'); if(!box)return;
  if(!paperTotal){box.innerHTML='';box.hidden=true;return}
  box.hidden=false;
  const nums=[];
  const push=n=>nums.push(`<button type="button" class="${n===paperPage?'current':''}" data-ppage="${n}">${n}</button>`);
  if(pages<=9){for(let n=1;n<=pages;n++)push(n)}
  else{
    push(1);
    if(paperPage>3)nums.push('<span class="muted">…</span>');
    for(let n=Math.max(2,paperPage-1);n<=Math.min(pages-1,paperPage+1);n++)push(n);
    if(paperPage<pages-2)nums.push('<span class="muted">…</span>');
    push(pages);
  }
  box.innerHTML=`<div class="pager-info">共 ${paperTotal} 篇 · 第 ${paperPage}/${pages} 页</div>
    <div class="pager-btns"><button type="button" data-ppage="${paperPage-1}" ${paperPage<=1?'disabled':''}>上一页</button>${nums.join('')}<button type="button" data-ppage="${paperPage+1}" ${paperPage>=pages?'disabled':''}>下一页</button></div>
    <label class="pager-size">每页 <select id="paperSizeSel">${[20,50,100,200].map(n=>`<option value="${n}" ${n===paperPageSize?'selected':''}>${n}</option>`).join('')}</select> 篇</label>`;
  const select=$('#paperSizeSel');
  if(select)select.onchange=()=>{paperPageSize=Number(select.value)||20;paperPage=1;loadPapers().catch(e=>toast(e.message))};
}
async function loadIdeas(){
  const params=new URLSearchParams();
  const since=daysAgoIso($('#ideaRange').value);
  if(since)params.set('since',since);
  const collection=ideaCollectionFilter;
  if(collection)params.set('collection',collection);
  const keyword=($('#ideaQ').value||'').trim().toLowerCase();
  const rows=(await api('/api/ideas?'+params.toString())).filter(x=>!keyword||`${x.title} ${x.content} ${(x.tags||[]).join(' ')}`.toLowerCase().includes(keyword));
  $('#ideasList').innerHTML=rows.map(x=>{
    const chips=(x.collections||[]).map(id=>{const name=collectionName('ideas',id);return name?`<span class="collection-chip">${esc(name)}</span>`:''}).join('');
    return `<div class="card idea-card"><button class="star ${(x.collections||[]).length?'on':''}" type="button" data-collect="${x.id}" data-collect-type="idea" data-collect-ids="${(x.collections||[]).join(',')}" title="收藏到…">★</button><div class="panel-head"><h3>${esc(x.title)}</h3><span class="badge ${x.enabled?'ok':''}">${x.enabled?'参与关联':'已停用'}</span></div><p class="idea-content">${esc(x.content)}</p><small>修订 ${x.revision} · ${time(x.updated_at)} · ${(x.tags||[]).map(esc).join(' / ')} ${chips}</small><div class="actions"><button data-idea-edit="${x.id}">编辑</button><button data-idea-toggle="${x.id}" data-enabled="${x.enabled?1:0}">${x.enabled?'停用':'启用'}</button><button class="danger" data-idea-delete="${x.id}">删除</button></div><script type="application/json" id="idea-${x.id}">${esc(JSON.stringify(x))}</script></div>`;
  }).join('')||'<div class="panel muted">还没有匹配的 idea。新分析会如实标记“没有可参考的 idea”。</div>';
  window.ideaRows=rows;
}
let notifyShowAll=false;
async function loadNotifications(){
  const data=await api('/api/notifications?limit=12'+(notifyShowAll?'&include_all=true':''));
  const rows=data.items||[];
  const labels={SENT:'已发送',MERGED:'已合并',CLOSED:'等待发送',OPEN:'收集中',FAILED:'发送失败'};
  $('#notificationsList').innerHTML=rows.map(x=>`<div class="card"><div class="panel-head"><h3>${x.priority==='critical'?'重要发现':'合并汇报'} · ${x.item_count} 条</h3><span class="badge ${x.state==='SENT'?'ok':x.state==='MERGED'?'':x.state==='CLOSED'?'wait':'wait'}">${esc(labels[x.state]||x.state)}</span></div><small>渠道 ${esc(x.channel)} · 创建 ${time(x.created_at)} · 发送 ${time(x.sent_at)}</small>${x.last_error?`<p class="error-text">${esc(x.last_error)}</p>`:''}<div class="actions"><button data-batch="${x.id}">查看内容</button>${x.state==='MERGED'?'':`<button data-batch-retry="${x.id}">重新发送</button>`}</div></div>`).join('')||'<div class="panel muted">还没有汇报批次。</div>';
  const more=$('#notificationsMore');
  if(!more)return;
  const total=Number(data.total||rows.length);
  if(total<=rows.length){more.hidden=true;more.innerHTML='';return}
  more.hidden=false;
  more.innerHTML=notifyShowAll
    ? `<button type="button" id="notifyShowLess">只看最近 ${12} 次</button>`
    : `共 ${total} 次汇报，这里只显示最近几次和所有还没发出去的 · <button type="button" id="notifyShowAll">查看全部</button>`;
  const moreBtn=$('#notifyShowAll'), lessBtn=$('#notifyShowLess');
  if(moreBtn)moreBtn.onclick=()=>{notifyShowAll=true;loadNotifications().catch(e=>toast(e.message))};
  if(lessBtn)lessBtn.onclick=()=>{notifyShowAll=false;loadNotifications().catch(e=>toast(e.message))};
}
async function refreshProxyStatus(force){const box=$('#proxyStatus');if(!box)return;box.textContent='正在检测本机代理…';try{const d=await api('/api/net_status'+(force?'?refresh=1':''));if(d.overseas){box.innerHTML=d.proxy?`海外站点：可用，走 <code>${esc(d.proxy)}</code>（${esc(d.source||'自动检测')}）`:'海外站点：可直连，这台电脑不需要代理'}else{box.innerHTML='海外站点：连不上。维基百科和 DuckDuckGo 会自动跳过，360 / Bing / PubMed / Crossref / arXiv 仍照常用。<br>如果这台电脑上有 Clash、v2rayN 之类，确认它在运行并开了「系统代理」；也可以在下面手填端口。'}}catch(err){box.textContent='检测失败：'+err.message}}
const proxyProbeBtn=$('#proxyProbe');if(proxyProbeBtn)proxyProbeBtn.onclick=()=>refreshProxyStatus(true);
async function refreshEmbedStatus(){const box=$('#embedStatus');if(!box)return;try{const d=await api('/api/embedding/status');if(d.available){box.innerHTML=`已启用（模型 ${(d.size/1048576).toFixed(1)} MB，纯 CPU 推理）`;$('#embedFetch').textContent='重新下载';}else{box.innerHTML=`未启用：${esc(d.reason||'模型未下载')}。检索仍然可用，只是同义改写会漏。`;}}catch(err){box.textContent='检测失败：'+err.message}}
const embedBtn=$('#embedFetch');if(embedBtn)embedBtn.onclick=async()=>{const out=$('#embedResult');try{embedBtn.disabled=true;out.textContent='正在下载（约 24 MB）…';const d=await api('/api/embedding/fetch',{method:'POST'});out.textContent=d.ok?`完成：${d.detail}`:`失败：${d.detail}`;await refreshEmbedStatus()}catch(err){out.textContent='下载失败：'+err.message}finally{embedBtn.disabled=false}};
async function loadSettings(){const data=await api('/api/settings'),s=data.settings;setReadingMode((s.ui&&s.ui.reading_mode)||readingMode);$('#llmEnabled').value=bool(s.llm.enabled);$('#llmProvider').value=s.llm.provider||'openai_compatible';$('#llmBase').value=s.llm.base_url;$('#llmModel').value=s.llm.model;$('#llmAutoCheapest').value=bool(s.llm.auto_cheapest??true);$('#visionEnabled').value=bool(s.llm.vision_enabled);$('#llmDaily').value=s.llm.daily_call_limit;$('#llmPrefilter').value=s.llm.min_local_relevance??40;$('#llmLongMaterial').value=s.llm.long_material_strategy||'head_tail';$('#autoAnalyze').value=bool(s.analysis.auto??true);$('#autoDownloadPapers').value=bool(s.papers.auto_download??false);$('#maxImages').value=s.max_images_per_article??8;$('#compressRaw').value=bool((s.storage||{}).compress_raw??true);$('#backupKeep').value=(s.storage||{}).backup_keep??3;$('#readingPrompt').value=s.analysis.custom_reading_prompt;$('#llmConcurrency').value=s.analysis.concurrency??3;$('#notifyChannel').value=s.notifications.channel;$('#instantThreshold').value=s.notifications.instant_threshold;$('#batchSize').value=s.notifications.batch_size;$('#maxWait').value=s.notifications.max_wait_minutes;$('#minRelevance').value=s.notifications.min_relevance;$('#quietStart').value=s.notifications.quiet_start;$('#quietEnd').value=s.notifications.quiet_end;$('#quietCritical').value=bool(s.notifications.quiet_allow_critical);$('#paperEmail').value=s.papers.email;window.__secretProviders=data.secrets.providers||[];$('#maxPerDay').value=s.notifications.max_per_day??3;$('#sendWindows').value=(s.notifications.send_windows||[]).join(',');$('#highlightThreshold').value=s.notifications.highlight_threshold??74;$('#digestLimit').value=s.notifications.digest_limit??10;if(window.renderAccounts)renderAccounts(s.llm.accounts||[]);$('#asrEnabled').value=bool(s.video.asr_enabled);$('#asrModel').value=s.video.asr_model;$('#asrDuration').value=s.video.max_duration_minutes;$('#llmKey').disabled=$('#llmProvider').value!=='openai_compatible';$('#llmBase').disabled=$('#llmProvider').value!=='openai_compatible';$('#llmKeyStatus').textContent=data.secrets.llm_api_key?'已安全保存':'未配置';$('#dataxKeyStatus').textContent=data.secrets.datax_api_key?'已安全保存':'未配置';$('#webhookStatus').textContent=data.secrets.notification_webhook?'已安全保存':'未配置';$('#wecomKfSecretStatus').textContent=data.secrets.wecom_kf_secret?'已安全保存':'未配置';$('#proxyUrl').value=s.http_proxy||'';refreshProxyStatus();refreshEmbedStatus();loadShellPanel()}

/* ---------- 桌面程序（托盘外壳） ---------- */
async function loadShellPanel(){
  try{
    const d=await api('/api/system/shell');
    $('#shellCloseAction').value=d.close_action||'ask';
    if($('#shellWindowMode'))$('#shellWindowMode').value=d.window_mode||'app';
    const boot=d.autostart||{};
    if($('#shellAutostart'))$('#shellAutostart').value=boot.enabled?'on':'off';
    if($('#shellOpenOnStart'))$('#shellOpenOnStart').value=d.open_on_start?'1':'0';
    const bootNote=boot.supported===false?'（本机不支持）':boot.mode==='legacy_shortcut'?'（旧启动文件夹方式）':'';
    $('#shellInfo').textContent=(d.tray_running?'托盘程序在运行':'托盘程序没在运行（当前是浏览器窗口）')
      +` · 开机自启${boot.enabled?'已开':'已关'}${bootNote} · ${d.url}`;
  }catch(e){$('#shellInfo').textContent=e.message}
}
$('#shellWindowMode')&&($('#shellWindowMode').onchange=async e=>{
  try{
    await api('/api/system/shell',{method:'POST',body:{window_mode:e.target.value}});
    toast(e.target.value==='app'?'以后双击图标开软件窗口':'以后双击图标开浏览器标签页');
    loadShellPanel();
  }catch(err){toast(err.message)}
});
$('#shellCloseAction').onchange=async e=>{
  const msg={ask:'以后关窗口会先问一句',tray:'以后关窗口只关窗口，服务留在托盘',quit:'以后关窗口会连服务一起退出'};
  try{
    await api('/api/system/shell',{method:'POST',body:{close_action:e.target.value}});
    toast(msg[e.target.value]||'已保存');
    loadShellPanel();
  }catch(err){toast(err.message)}
};
$('#shellAutostart')&&($('#shellAutostart').onchange=async e=>{
  try{
    await api('/api/system/shell',{method:'POST',body:{autostart:e.target.value==='on'}});
    toast(e.target.value==='on'?'已设为开机自启':'已取消开机自启');
    loadShellPanel();
  }catch(err){toast(err.message)}
});
$('#shellOpenOnStart')&&($('#shellOpenOnStart').onchange=async e=>{
  try{
    await api('/api/system/shell',{method:'POST',body:{open_on_start:e.target.value==='1'}});
    toast(e.target.value==='1'?'开机启动时会打开界面':'开机启动时只把图标放进托盘');
    loadShellPanel();
  }catch(err){toast(err.message)}
});
$('#shellOpen').onclick=async()=>{
  try{
    const r=await api('/api/system/shell',{method:'POST',body:{action:'open'}});
    if(!r.tray_running)window.open(r.url||'','_blank');
    else toast(r.window_mode==='browser'?'已用浏览器打开':'已在软件窗口打开');
  }catch(e){toast(e.message)}
};
$('#shellRestart').onclick=async()=>{
  if(!confirm('重启后台服务？正在抓取的条目会重新排队，已完成的不会丢。'))return;
  try{await api('/api/system/shell',{method:'POST',body:{action:'restart'}});toast('已通知托盘重启服务，约 10 秒后自动刷新');setTimeout(()=>location.reload(),12000)}catch(e){toast(e.message)}
};
$('#shellQuit').onclick=async()=>{
  if(!confirm('彻底退出？会停止后台服务，正在抓取的任务下次启动后继续。'))return;
  try{await api('/api/system/shell',{method:'POST',body:{action:'quit'}});toast('正在退出…');document.body.innerHTML='<div style="padding:60px;font:15px/1.8 system-ui;color:#556">Hermes 科研雷达已退出。要重新开始，双击桌面图标。</div>'}catch(e){toast(e.message)}
};

function fmtDuration(seconds){if(seconds===null||seconds===undefined)return '—';const s=Math.max(0,Math.round(seconds));if(s<60)return s+' 秒';if(s<3600)return Math.round(s/60)+' 分钟';return (s/3600).toFixed(1)+' 小时'}
async function loadProgress(){
  let p;try{p=await api('/api/progress')}catch(e){return}
  const done=(p.archived||0)+(p.ready||0)+(p.filtered||0);
  const pct=p.total?Math.round(done/p.total*100):0;
  const running=(p.running||[]).filter(j=>j.kind!=='papers_item');
  const activeHtml=running.length?running.map(j=>`<div class="line"><span><span class="spin"></span>${j.kind==='analyze_item'?'精读':j.kind==='archive_history_batch'?'批量归档':'归档'}：${esc(j.title||'（批量任务）')}</span><span>已运行 ${j.elapsed_seconds!=null?fmtDuration(j.elapsed_seconds):'—'}</span></div>`).join(''):'<div class="line"><span>当前没有正在执行的归档或精读任务</span><span>队列空闲</span></div>';
  const html=`<div class="panel-head"><h2>流水线进度</h2><span class="muted">每 2.5 秒自动刷新</span></div>
    <div class="progress-track"><div class="progress-fill" style="width:${pct}%"></div></div>
    <div class="progress-meta"><span>已完成 <b>${done}</b> / ${p.total}（${pct}%）</span><span>已归档 <b>${p.archived}</b></span><span>已精读 <b>${p.ready}</b></span><span>仅索引 <b>${p.index_only}</b></span><span>待归档 <b>${p.archive_queued}</b></span><span>执行中归档 <b>${p.archive_running}</b></span><span>待精读 <b>${p.analyze_queued}</b></span><span>近10分钟归档 <b>${p.archives_last_10min}</b></span><span>预计剩余 <b>${fmtDuration(p.eta_seconds)}</b></span><span>今日云调用 <b>${p.llm_calls_today}${p.llm_call_limit?' / '+p.llm_call_limit:''}</b></span></div>
    <div class="progress-active">${activeHtml}</div>
    <div class="actions" style="margin-top:12px"><button class="primary" data-archive-index>归档全部仅索引条目</button>${(!p.archive_paused&&(p.archive_batch_jobs||0)>0)?'<button data-archive-stop>停止批量归档</button>':''}<span class="muted">${p.archive_paused?'已暂停：正在跑的那一批做完手上这条就停；点「归档全部仅索引条目」可继续':'限速后台执行，不调用云模型，可随时查看上面的进度'}</span></div>`;
  ['#libraryProgress','#dashboardProgress'].forEach(sel=>{const el=$(sel);if(el)el.innerHTML=html});
}
async function loadCredits(){
  let c;try{c=await api('/api/credits?days=30')}catch(e){return}
  const money=v=>Number(v||0).toFixed(v>=100?1:2);
  // token 和点数分开显示：两万以下给精确值，再往上给「万 / 亿」，
  // 免得一位数一直涨到七位数把卡片撑破。
  const tokens=v=>{const n=Number(v||0);if(n<10000)return n.toLocaleString('zh-CN');if(n<1e8)return (n/1e4).toFixed(n<1e6?1:0)+' 万';return (n/1e8).toFixed(2)+' 亿'};
  const hostOf=url=>{try{return new URL(url).host}catch(e){return url||''}};
  const cards=[['今日实扣',money(c.today_points)+' 点'],['30 天实扣',money(c.window_points)+' 点'],['累计实扣',money(c.total_points)+' 点'],['平均每次精读',money(c.reading_avg)+' 点'],['精读次数',c.reading_calls],['每篇中位数',money(c.reading_median)+' 点']];
  const models=(c.by_model||[]).slice(0,4).map(m=>`<span class="badge">${esc(m.model)} ${m.calls} 次 · 实扣 ${money(m.points)} 点${m.reference&&m.reference!==m.points?`（原价 ${money(m.reference)}）`:''} · WorkBuddy 侧 ${tokens(m.tokens)} token · 均 ${m.avg_seconds}s</span>`).join(' ');
  // 不能叫 api：这个函数里已经用 api() 发请求，同名 const 会把整个函数体
  // 拖进 TDZ，第一行 await api(...) 直接抛 ReferenceError，又被 catch 静默
  // 吃掉——表现就是「总览里的点数与 token 整块空白」。
  const apiStats=c.api||{};
  const apiCards=[['今日 API token',tokens(apiStats.today_tokens)],['30 天 API token',tokens(apiStats.window_tokens)],['累计 API token',tokens(apiStats.total_tokens)],['今日调用',apiStats.today_calls||0],['输入 token',tokens(apiStats.prompt_tokens)],['输出 token',tokens(apiStats.completion_tokens)]];
  const apiModels=(apiStats.by_model||[]).slice(0,6).map(m=>`<span class="badge">${esc(m.label||m.model)} ${m.calls} 次 · ${tokens(m.tokens)} token${m.base_url?` · ${esc(hostOf(m.base_url))}`:''} · 均 ${m.avg_seconds}s</span>`).join(' ');
  const daily=(c.daily||[]).slice(-14);
  const peak=Math.max(1,...daily.map(d=>d.points));
  const bars=daily.map(d=>`<div class="bar" title="${d.day} · ${money(d.points)} 点 / ${d.calls} 次"><i style="height:${Math.max(3,Math.round(d.points/peak*46))}px"></i><span>${d.day.slice(5)}</span></div>`).join('');
  const rows=(c.recent||[]).slice(0,8).map(r=>`<tr><td>${time(r.time)}</td><td>${esc(r.model)}</td><td>${r.task==='analyze'?'精读':'对话'}</td><td>${r.free?'<span class="badge ok">免费</span>':money(r.points)}</td><td>${r.prompt_tokens}+${r.completion_tokens}</td><td>${Math.round((r.elapsed_ms||0)/1000)}s</td></tr>`).join('');
  const freeNote=c.today_free_calls?`；今日 ${c.today_free_calls} 次走免费档（${tokens(c.today_free_tokens)} token）不计点`:'';
  $('#creditsPanel').innerHTML=`<div class="panel-head"><h2>WorkBuddy 点数</h2><span class="muted">按每次调用的实扣 credit 记账，免费模型的 0 就是 0，不按 token 折算${freeNote}</span></div>
    <div class="credit-grid">${cards.map(([k,v])=>`<div class="credit-cell"><b>${v}</b><span>${k}</span></div>`).join('')}</div>
    <div class="credit-bars">${bars||'<span class="muted">最近没有调用</span>'}</div>
    <div class="credit-models">${models}</div>
    <div class="credit-sub">
      <div class="panel-head"><h3>API Key 模型 token</h3><span class="muted">设置里自带 Key 接入的 OpenAI 兼容模型单独统计；WorkBuddy 点数与它无关</span></div>
      <div class="credit-grid">${apiCards.map(([k,v])=>`<div class="credit-cell api"><b>${v}</b><span>${k}</span></div>`).join('')}</div>
      <div class="credit-models">${apiModels||'<span class="muted">还没有 API Key 模型的调用记录。用 WorkBuddy 点数精读、或本机 Ollama，都不计入这里。</span>'}</div>
    </div>
    ${rows?`<details><summary>最近 WorkBuddy 调用明细</summary><div class="table-wrap"><table><thead><tr><th>时间</th><th>模型</th><th>用途</th><th>点数</th><th>输入+输出</th><th>用时</th></tr></thead><tbody>${rows}</tbody></table></div></details>`:''}`;
}
const loaders={dashboard:()=>Promise.all([loadHealth(),loadEvents(),loadProgress(),loadCredits()]),sources:loadSources,wechat:loadWechatAccounts,library:()=>Promise.all([loadCollections('library'),loadItems(),loadProgress()]),papers:loadPapers,ideas:()=>Promise.all([loadCollections('ideas'),loadIdeas()]),notifications:loadNotifications,settings:()=>Promise.all([loadSettings(),window.wizEnsureBadge?window.wizEnsureBadge():null,window.loadStorageUsage?window.loadStorageUsage():null])};
function switchView(view){currentView=view;$$('.view').forEach(x=>x.classList.toggle('active',x.id===view));$$('.nav').forEach(x=>x.classList.toggle('active',x.dataset.view===view));loaders[view]().catch(e=>toast(e.message))}
$$('.nav').forEach(x=>x.onclick=()=>switchView(x.dataset.view));
$('#detailAsk').onclick=askCurrentItem;
// 设置页不一定会被打开：首屏就把阅读排布同步过来（localStorage 只当首屏兜底）。
setReadingMode(readingMode);
api('/api/settings').then(d=>{const m=d.settings&&d.settings.ui&&d.settings.ui.reading_mode;if(m)setReadingMode(m)}).catch(()=>{});
// 排布改完即时生效：一边开着就顺手把另一边收掉，免得两套界面同时挂在 top layer。
$('#uiReadingMode').onchange=async e=>{
  const next=e.target.value==='classic'?'classic':'workbench',changed=next!==readingMode;
  setReadingMode(next);
  try{await api('/api/settings',{method:'PUT',body:{ui:{reading_mode:next}}})}
  catch(err){toast('保存失败：'+err.message);return}
  if(!changed)return;
  if(next==='classic'){if(wbIsOpen())closeWorkbench();toast('已切到经典弹层：点条目先在弹层里看原文，精读与论文用顶部按钮切换')}
  else{if($('#detailDialog').open)closeDetail();toast('已切到阅读工作台：下次点条目直接在软件里看原文')}
};
$('#ingestBtn').onclick=async()=>{try{const result=await api('/api/ingest',{method:'POST',body:{text:$('#quickText').value}});$('#ingestResult').textContent=`已接收 ${result.items.length} 条，其中新增 ${result.items.filter(x=>x.created).length} 条`;$('#quickText').value='';loadHealth()}catch(e){toast(e.message)}};
$('#importWechat').onclick=async()=>{try{const body={names_text:$('#wechatNames').value,initial_backfill_days:Number($('#wechatBackfillDays').value),poll_seconds:Number($('#wechatPollSeconds').value)};const r=await api('/api/sources/wechat/import',{method:'POST',body});$('#wechatImportResult').textContent=`新增 ${r.added} 个；`+(r.needs_key?'清单已保存，填写 DataXAPI Key 后自动启用':'已开始自动监测与历史追补');loadSources()}catch(e){toast(e.message)}};
function reloadItems(){itemPage=1;loadItems().catch(e=>toast(e.message))}
$('#searchBtn').onclick=reloadItems;
$('#searchQ').onkeydown=e=>{if(e.key==='Enter')reloadItems()};
['#searchKind','#searchStatus','#searchRange','#searchExcluded','#hideLow'].forEach(sel=>{const el=$(sel);if(el)el.onchange=reloadItems});
$('#manageCollections').onclick=()=>openCollectionManager('library').catch(e=>toast(e.message));
$('#manageIdeaCollections').onclick=()=>openCollectionManager('ideas').catch(e=>toast(e.message));
$('#paperStatus').onchange=()=>{paperPage=1;loadPapers().catch(e=>toast(e.message))};
$('#saveIdea').onclick=async()=>{try{const body={title:$('#ideaTitle').value,content:$('#ideaContent').value,tags:$('#ideaTags').value.split(/[,，]/).map(x=>x.trim()).filter(Boolean),enabled:true};const id=$('#ideaId').value;await api(id?'/api/ideas/'+id:'/api/ideas',{method:id?'PATCH':'POST',body});$('#ideaId').value=$('#ideaTitle').value=$('#ideaContent').value=$('#ideaTags').value='';loadIdeas();toast('idea 已保存，将参与后续关联')}catch(e){toast(e.message)}};
$('#cancelIdea').onclick=()=>{$('#ideaId').value=$('#ideaTitle').value=$('#ideaContent').value=$('#ideaTags').value=''};
$('#flushNotifications').onclick=async()=>{const r=await api('/api/notifications/flush',{method:'POST'});toast(r.merged_into?`已封闭 ${r.batches} 个批次，积压合并为 1 条上报后再发送`:`已封闭 ${r.batches} 个批次并进入发送队列`);loadNotifications()};
$('#testNotification').onclick=async()=>{try{await api('/api/notifications/test',{method:'POST'});toast('测试成功；站内渠道无需外部发送')}catch(e){toast(e.message)}};
$('#copyPapers').onclick=async()=>{let all=[];try{all=await api('/api/papers?status=needed')}catch(e){toast(e.message);return}const text=all.map(p=>`${p.title||'题名待解析'}\n作者：${JSON.parse(p.authors_json||'[]').join('; ')}\n期刊/年份：${p.journal} ${p.year}\nDOI：https://doi.org/${p.doi}\n来源：${p.source_url||''}`).join('\n\n---\n\n');await navigator.clipboard.writeText(text||'当前没有待下载论文');toast(`已复制 ${all.length} 篇为便于人工或代下的文本`)};
$('#llmProvider').onchange=()=>{const provider=$('#llmProvider').value,local=provider==='ollama',cloud=provider==='workbuddy_cloud',base=$('#llmBase'),model=$('#llmModel');base.disabled=provider!=='openai_compatible';$('#llmKey').disabled=provider!=='openai_compatible';if(cloud||local){base.dataset.remote=base.value.includes('127.0.0.1')?'https://api.deepseek.com/v1':base.value;model.dataset.remote=model.value}if(cloud){model.value='hy4free';$('#llmKey').value=''}else if(local){base.value='http://127.0.0.1:11434/v1';model.value='qwen3:8b';$('#llmKey').value=''}else{base.value=base.dataset.remote||'https://api.deepseek.com/v1';model.value=model.dataset.remote||'deepseek-chat'}};
$('#loadWorkbuddyModels').onclick=async()=>{try{$('#workbuddyModelInfo').textContent='正在读取官方目录……';const r=await api('/api/llm/workbuddy/models');$('#workbuddyModelInfo').textContent=`共 ${r.models.length} 个可用模型`;$('#detailContent').innerHTML=`<h1>WorkBuddy 可用模型</h1><p class="muted">倍率仅为目录展示信息，实际扣减以每次调用记录为准。</p><div class="table-wrap"><table><thead><tr><th>模型</th><th>ID</th><th>积分倍率</th><th>图片</th><th>选择</th></tr></thead><tbody>${r.models.map(m=>`<tr><td>${esc(m.name)}</td><td><code>${esc(m.id)}</code></td><td>${esc(m.credits||'未标注')}</td><td>${m.supportsImages===true?'支持':m.supportsImages===false?'不支持':'未知'}</td><td><button data-cloud-model="${esc(m.id)}">使用</button></td></tr>`).join('')}</tbody></table></div>`;openDetail()}catch(e){$('#workbuddyModelInfo').textContent='读取失败';toast(e.message)}};
$('#settingsForm').onsubmit=async e=>{e.preventDefault();try{const patch={llm:{enabled:$('#llmEnabled').value==='true',provider:$('#llmProvider').value,base_url:$('#llmBase').value,model:$('#llmModel').value,auto_cheapest:$('#llmAutoCheapest').value==='true',vision_enabled:$('#visionEnabled').value==='true',daily_call_limit:Number($('#llmDaily').value),min_local_relevance:Number($('#llmPrefilter').value),long_material_strategy:$('#llmLongMaterial').value,accounts:window.collectAccounts?collectAccounts():undefined},analysis:{custom_reading_prompt:$('#readingPrompt').value,auto:$('#autoAnalyze').value==='true',concurrency:Number($('#llmConcurrency').value)},notifications:{channel:$('#notifyChannel').value,instant_threshold:Number($('#instantThreshold').value),batch_size:Number($('#batchSize').value),max_wait_minutes:Number($('#maxWait').value),min_relevance:Number($('#minRelevance').value),quiet_start:$('#quietStart').value,quiet_end:$('#quietEnd').value,quiet_allow_critical:$('#quietCritical').value==='true',max_per_day:Number($('#maxPerDay').value),send_windows:$('#sendWindows').value.split(',').map(x=>x.trim()).filter(Boolean),highlight_threshold:Number($('#highlightThreshold').value),digest_limit:Number($('#digestLimit').value)},papers:{email:$('#paperEmail').value,auto_download:$('#autoDownloadPapers').value==='true'},storage:{compress_raw:$('#compressRaw').value==='true',backup_keep:Number($('#backupKeep').value)},max_images_per_article:Number($('#maxImages').value),video:{asr_enabled:$('#asrEnabled').value==='true',asr_model:$('#asrModel').value,max_duration_minutes:Number($('#asrDuration').value)},http_proxy:$('#proxyUrl').value.trim(),ui:{reading_mode:$('#uiReadingMode').value}};await api('/api/settings',{method:'PUT',body:patch});for(const [id,name] of [['llmKey','llm_api_key'],['dataxKey','datax_api_key'],['notifyWebhook','notification_webhook'],['wecomKfSecret','wecom_kf_secret']]){if($('#'+id).value){await api('/api/settings/secrets',{method:'POST',body:{name,value:$('#'+id).value}});$('#'+id).value=''}}$('#settingsResult').textContent='已保存';toast('设置已安全保存')}catch(err){toast(err.message)}};
/* ---------- 模态栈 ---------- */
// showModal() 会让 dialog 子树外的一切变成 inert：浮层（对话抽屉、笔记、划词条）光待在
// top layer 上层没用，鼠标事件会穿透到遮罩上——点「对话的关闭」先把精读页关了就是这么来的。
// 所以浮层要挂在「最上面那层 modal」的子树里。这里记下入栈顺序，assistant.js 的
// overlayHome() 会来问最上面是谁。
const modalStack=[];
const _showModal=HTMLDialogElement.prototype.showModal;
HTMLDialogElement.prototype.showModal=function(){
  _showModal.call(this);
  const at=modalStack.indexOf(this);if(at>=0)modalStack.splice(at,1);
  modalStack.push(this);
  document.dispatchEvent(new CustomEvent('modal:open',{detail:this}));
};
document.addEventListener('close',e=>{
  const at=modalStack.indexOf(e.target);
  if(at<0)return;
  modalStack.splice(at,1);
  document.dispatchEvent(new CustomEvent('modal:close',{detail:e.target}));
},true);
window.topModal=()=>modalStack.length?modalStack[modalStack.length-1]:null;

function lockPageScroll(){
  if(!document.body.classList.contains('modal-open')){
    // 锁滚动会让页面滚动条消失，right:0 的抽屉会跟着横移 15px；先把槽位宽度记下来顶住，
    // 关闭时再撤掉，抽屉就始终停在同一处（见 styles.css 的 --sbw）。
    document.documentElement.style.setProperty('--sbw',(window.innerWidth-document.documentElement.clientWidth)+'px')}
  document.body.classList.add('modal-open');
}
function openDetail(){
  // 精读页也必须走 showModal：公众号推文面板是 showModal 打开的，待在 top layer 里。
  // 只有 showModal 才能叠到它上面，关闭后又正好回到下面那层——列表不用重新加载。
  // show() 是非模态，z-index 再高也压不过 top layer，所以以前只能先把面板关掉。
  const d=$('#detailDialog');
  if(!d.open)d.showModal();
  lockPageScroll();}
function closeDetail(){const d=$('#detailDialog');if(d.open)d.close()}
$('#closeDialog').onclick=closeDetail;
$('#detailDialog').addEventListener('close',()=>{stopItemWatch();closeCollectPop()});
// 任何一层模态关掉后，只要底下没有别的模态了才解锁滚动；否则关掉精读页会把
// 还开着的推文面板一起解锁。
document.addEventListener('close',e=>{if(e.target.tagName==='DIALOG'&&!$$('dialog[open]').length){document.body.classList.remove('modal-open');document.documentElement.style.removeProperty('--sbw')}},true);
// 点对话框外面那圈遮罩才关；点在面板内边距上不算。
$('#detailDialog').addEventListener('click',e=>{
  if(e.target!==e.currentTarget)return;
  const r=e.currentTarget.getBoundingClientRect();
  if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom){
    // 对话抽屉开着时点暗处：先关抽屉（它才是最上面那层），别一下把精读页也关了。
    if(window.chatIsOpen&&window.chatIsOpen()){window.closeChat();return}
    closeDetail();
  }
});
document.addEventListener('keydown',e=>{if(e.key!=='Escape')return;// 收藏弹层的 Esc 交给浏览器：它会先关最上层，手动 preventDefault 会连带把下一层一起关掉
if($('#collectionPop').open)return;if($('#chatDrawer').classList.contains('open')){e.preventDefault();$('#chatClose').click()}});
// Esc 不在这里手动关精读页：模态层自带「先关最上面那层」的顺序，
// 手动 close 会让浏览器接着把下面那层公众号面板一起关掉。
$('#collectionPopClose').onclick=closeCollectPop;
$('#searchCollectionBtn').onclick=e=>openCollectionPick('library',e.currentTarget).catch(err=>toast(err.message));
$('#ideaCollectionBtn').onclick=e=>openCollectionPick('ideas',e.currentTarget).catch(err=>toast(err.message));
$('#collectionPickClose').onclick=closeCollectionPick;
$('#collectionPickSearch').oninput=renderCollectionPick;
$('#collectionPickSort').onchange=renderCollectionPick;
$('#collectionPickManage').onclick=()=>{const scope=collectionPickScope;closeCollectionPick();openCollectionManager(scope).catch(err=>toast(err.message))};
$('#collectionDialogSearch').oninput=renderCollectionManage;
$('#collectionDialogSort').onchange=renderCollectionManage;
$('#collectionPick').addEventListener('click',e=>{if(e.target!==e.currentTarget)return;const r=e.currentTarget.getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)closeCollectionPick()});
// 点弹层外面（含遮罩）关掉；dialog 的遮罩点击在事件里表现为 target===dialog。
$('#collectionPop').addEventListener('click',e=>{if(e.target!==e.currentTarget)return;const r=e.currentTarget.getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)closeCollectPop()});
$('#collectionCreate').onclick=()=>{const scope=collectTarget?collectScope(collectTarget.entryType):'library';const input=$('#collectionNewName');createCollectionFor(input.value,scope,true).then(()=>{input.value=''}).catch(e=>toast(e.message))};
$('#collectionPopSearch').oninput=renderCollectPop;
$('#collectionPopSort').onchange=renderCollectPop;
$('#collectionNewName').oninput=renderCollectPop;
$('#collectionDialogClose').onclick=()=>$('#collectionDialog').close();
$('#collectionManageCreate').onclick=async()=>{const input=$('#collectionManageName');const scope=window.__manageScope||'library';try{await createCollectionFor(input.value,scope,false);input.value='';renderCollectionManage()}catch(e){toast(e.message)}};
document.addEventListener('change',e=>{
  const pick=e.target.closest('[data-paper-switch]');
  if(pick){
    const index=Number(pick.value)||0;
    if($('#detailDialog').open){classicPaperIndex=index;switchClassicView('paper').catch(err=>toast(err.message))}
    else{wb.paperIndex=index;const pane=wb.panes.find(p=>p.view==='paper');if(pane)wbSetView(pane,'paper',{force:true})}
    return;
  }
  const box=e.target.closest('[data-collect-toggle]');if(box)toggleCollect(box.dataset.collectToggle,box.checked).catch(err=>toast(err.message))});
document.addEventListener('click',async e=>{if(e.target.closest('.pick'))return;const t=e.target.closest('button,[data-item]');if(!t)return;try{
if(t.dataset.wbAct){wbStepActPage(Number(t.dataset.wbAct));return}
if(t.dataset.classicView){await switchClassicView(t.dataset.classicView);return}
if(t.dataset.wbPaperOpen!==undefined){wbToggleSide('paper');return}
if(t.dataset.smartCollect){const x=await api('/api/items/'+encodeURIComponent(t.dataset.smartCollect));await suggestCollections(x,{manual:true});return}
if(t.dataset.paperUpload){await uploadPaper(t.dataset.paperUpload,t.dataset.paperUploadTarget||'');return}
if(t.dataset.paperFetch){await fetchItemPapers(t.dataset.paperFetch);return}
if(t.dataset.delete){await deleteItem(t.dataset.delete);return}
if(t.dataset.ruleFrom){await openRuleDialog({account:t.dataset.ruleFrom,scope:'account',field:'title',patterns:''});return}
if(t.dataset.ruleToggle!==undefined){await api(`/api/filter-rules/${t.dataset.ruleToggle}`,{method:'PATCH',body:{enabled:t.dataset.enabled!=='1'}});await loadRuleList();reloadItems();return}
if(t.dataset.ruleRemove!==undefined){if(!confirm('删除这条规则？被它挡住的条目会一起放回资料库。'))return;const r=await api(`/api/filter-rules/${t.dataset.ruleRemove}`,{method:'DELETE'});toast(`规则已删除${r.restored?`，恢复 ${r.restored} 条`:''}`);await loadRuleList();reloadItems();return}
// 只认列表卡片。精读区本身也带 data-item（给「问 AI」定位用），点正文不能当成「重新打开这条」。
if(t.dataset.item&&t.classList.contains('item')){await showItem(t.dataset.item);return}
if(t.dataset.page!==undefined&&t.dataset.page!==''){const n=Number(t.dataset.page);if(n>=1){itemPage=n;await loadItems()}return}
if(t.dataset.ppage!==undefined&&t.dataset.ppage!==''){const n=Number(t.dataset.ppage);if(n>=1&&n!==paperPage){paperPage=n;await loadPapers()}return}
if(t.dataset.collect){await openCollectPop(t.dataset.collectType||'item',t.dataset.collect,(t.dataset.collectIds||'').split(',').filter(Boolean),t,()=>currentView==='library'?loadItems():loadIdeas());return}
if(t.dataset.pickCollection!==undefined&&t.dataset.pickCollection!==null&&t.closest('#collectionPick')){applyCollectionFilter(collectionPickScope,t.dataset.pickCollection);return}
if(t.dataset.restore){await api(`/api/items/${t.dataset.restore}/restore`,{method:'POST'});toast('已恢复收录');loadItems().catch(()=>{});await refreshOpenReader(t.dataset.restore);return}
if(t.dataset.useCollection){await toggleCollect(t.dataset.useCollection,true);$('#collectionNewName').value='';renderCollectPop();return}
if(t.dataset.collectionSave){const input=document.querySelector(`[data-collection-name="${t.dataset.collectionSave}"]`);await api(`/api/collections/${t.dataset.collectionSave}`,{method:'PATCH',body:{name:input.value}});toast('已改名');await loadCollections(window.__manageScope||'library');renderCollectionManage();return}
if(t.dataset.collectionDelete){if(!confirm('删除这个收藏夹？条目本身不会被删除。'))return;const scope=window.__manageScope||'library';await api(`/api/collections/${t.dataset.collectionDelete}`,{method:'DELETE'});toast('已删除收藏夹');if(collectionFilterValue(scope)===t.dataset.collectionDelete){if(scope==='library')libraryCollectionFilter='';else ideaCollectionFilter=''}await loadCollections(scope);renderCollectionManage();syncCollectionButton(scope);if(scope==='library')loadItems().catch(()=>{});else loadIdeas().catch(()=>{});return}
if(t.dataset.run){await api(`/api/sources/${t.dataset.run}/run`,{method:'POST'});toast('已加入立即检查队列');return}if(t.dataset.archiveHistory){if(!confirm('将控速归档历史全文、图片和可获取论文，不自动调用云精读。确定开始吗？'))return;const r=await api(`/api/sources/${t.dataset.archiveHistory}/archive-history`,{method:'POST'});toast(r.queued?`已开始历史归档，剩余 ${r.remaining} 条；云精读调用 0`:'没有待归档的历史索引');return}if(t.dataset.toggle){await api(`/api/sources/${t.dataset.toggle}`,{method:'PATCH',body:{enabled:t.dataset.enabled!=='1'}});loadSources();return}if(t.dataset.process){await api(`/api/items/${t.dataset.process}/process`,{method:'POST'});toast('已开始抓取原文与配图，完成后本页自动刷新（不调用云精读）');watchItem(t.dataset.process);refreshOpenReader(t.dataset.process).catch(()=>{});loadItems().catch(()=>{});return}if(t.dataset.reanalyze){await api(`/api/items/${t.dataset.reanalyze}/reanalyze`,{method:'POST'});toast('已排队，正在精读，完成后自动刷新');watchItem(t.dataset.reanalyze);refreshOpenReader(t.dataset.reanalyze).catch(()=>{});loadItems().catch(()=>{});return}if(t.dataset.retriage){toast('正在让模型重新判断配图…');const r=await api(`/api/items/${t.dataset.retriage}/retriage-images`,{method:'POST'});toast(r.dropped?`已删除 ${r.dropped} 张无关配图`:'没有发现需要删除的配图');await refreshOpenReader(t.dataset.retriage);loadItems().catch(()=>{});return}if(t.dataset.verifySource){toast('正在检查原文是否还能打开…');const r=await api(`/api/items/${t.dataset.verifySource}/verify-source`,{method:'POST'});toast(r.exists?'原文仍可访问':(r.kept?'原文已删除，但离线页已保存，继续保留':'原文已删除且没有离线页，已移出资料库'));await refreshOpenReader(t.dataset.verifySource);loadItems().catch(()=>{});return}if(t.dataset.archiveIndex!==undefined){if(!confirm('将限速归档所有仅索引条目（正文、图片、原始快照），不调用云精读。条目较多时会持续一段时间，可随时查看进度。确定开始吗？'))return;const r=await api('/api/archive-index-only',{method:'POST'});toast(r.queued?`已开始归档，剩余 ${r.remaining} 条；云精读调用 0`:'没有待归档的索引条目');loadProgress().catch(()=>{});return}if(t.dataset.archiveStop!==undefined){if(!confirm('停止批量归档？正在下载的这一条会先做完，之后不再继续排队。已归档的条目不受影响。'))return;const r=await api('/api/archive-index-only/stop',{method:'POST'});toast(`已暂停：撤销 ${r.cancelled} 个排队批次${r.running_batches?`，正在跑的那批收尾后停下`:''}；还剩 ${r.remaining} 条未归档`);loadProgress().catch(()=>{});return}if(t.dataset.cloudModel){$('#llmProvider').value='workbuddy_cloud';$('#llmProvider').dispatchEvent(new Event('change'));$('#llmModel').value=t.dataset.cloudModel;$('#detailDialog').close();toast('已选择模型，保存设置后生效');return}if(t.dataset.paperRetry){await api(`/api/papers/${t.dataset.paperRetry}/retry`,{method:'POST'});toast('已重试论文解析与下载');return}if(t.dataset.jobRetry){await api(`/api/jobs/${t.dataset.jobRetry}/retry`,{method:'POST'});toast('任务已重新排队');await loadTaskQueue();return}if(t.dataset.jobDiagnostic){const d=await api(`/api/jobs/${t.dataset.jobDiagnostic}/diagnostics`);window.currentDiagnostic=d.markdown;$('#detailContent').innerHTML=`<h1>任务失败诊断</h1>${d.hint?'<div class="notice"><b>已知模式：'+esc(d.hint)+'</b><br>'+esc(d.hint_advice)+'</div>':''}<p>${d.retryable?'<span class="badge wait">疑似临时错误，可重试</span>':'<span class="badge error">不是临时错误，盲目重试会再次失败</span>'}</p><p class="muted">复制下面内容，粘贴到 WorkBuddy 对话即可定位和修复。</p><div class="actions"><button class="primary" data-copy-diagnostic="1">复制诊断给 WorkBuddy</button></div><div class="detail-text"><pre style="white-space:pre-wrap">${esc(d.markdown)}</pre></div>`;openDetail();return}if(t.dataset.copyDiagnostic){try{await navigator.clipboard.writeText(window.currentDiagnostic||'')}catch(err){const ta=document.createElement('textarea');ta.value=window.currentDiagnostic||'';document.body.appendChild(ta);ta.select();document.execCommand('copy');ta.remove()}toast('诊断已复制，可直接粘贴到 WorkBuddy');return}if(t.dataset.pdfPreview){await previewPaper(t.dataset.pdfPreview);return}if(t.dataset.paperWps){const r=await api(`/api/papers/${t.dataset.paperWps}/open-wps`,{method:'POST'});toast(`${r.application} 已打开：${r.path}`);return}if(t.dataset.paperReveal){const r=await api(`/api/papers/${t.dataset.paperReveal}/reveal`,{method:'POST'});toast(`已打开目录：${r.directory}`);return}if(t.dataset.ideaEdit){const x=window.ideaRows.find(i=>i.id===t.dataset.ideaEdit);$('#ideaId').value=x.id;$('#ideaTitle').value=x.title;$('#ideaContent').value=x.content;$('#ideaTags').value=(x.tags||[]).join(', ');scrollTo({top:0,behavior:'smooth'});return}if(t.dataset.ideaToggle){const x=window.ideaRows.find(i=>i.id===t.dataset.ideaToggle);await api('/api/ideas/'+x.id,{method:'PATCH',body:{title:x.title,content:x.content,tags:x.tags,enabled:t.dataset.enabled!=='1'}});loadIdeas();return}if(t.dataset.batch){const x=await api('/api/notifications/'+t.dataset.batch);$('#detailContent').innerHTML=`<h1>汇报内容</h1><div class="detail-text">${esc(x.text)}</div>`;openDetail();return}if(t.dataset.batchRetry){const r=await api(`/api/notifications/${t.dataset.batchRetry}/retry`,{method:'POST'});toast(r.queued===false?(r.reason||'该批次无需重试'):'已重新进入发送队列');loadNotifications().catch(()=>{})}}catch(err){toast(err.message)}});
/* ---------- 图片查看器 ---------- */
const viewerState={scale:1,x:0,y:0,fit:1,dragging:false,moved:false,startX:0,startY:0};
function viewerParts(){return {box:$('#imageViewer'),stage:$('#imageViewerStage'),img:$('#imageViewerImg'),info:$('#imageViewerInfo')}}
function viewerApply(){const {img,info}=viewerParts();if(!img)return;img.style.transform=`translate(${viewerState.x}px,${viewerState.y}px) scale(${viewerState.scale})`;if(info)info.textContent=`${Math.round(viewerState.scale*100)}% · 滚轮缩放 · 拖动平移 · 双击复位`}
function viewerFit(){const {stage,img}=viewerParts();if(!stage||!img)return;const sw=stage.clientWidth||window.innerWidth,sh=stage.clientHeight||window.innerHeight;const iw=img.naturalWidth||sw,ih=img.naturalHeight||sh;viewerState.fit=Math.min(1,sw/iw,sh/ih);viewerState.scale=viewerState.fit||1;viewerState.x=(sw-iw*viewerState.scale)/2;viewerState.y=(sh-ih*viewerState.scale)/2;viewerApply()}
function openImageViewer(src,alt){const {box,img}=viewerParts();if(!box||!img)return;img.alt=alt||'';img.src=src;if(!box.open)box.showModal();document.body.classList.add('viewer-open');if(img.complete&&img.naturalWidth)viewerFit();else img.onload=()=>viewerFit()}
function closeImageViewer(){const {box,img}=viewerParts();if(!box)return;if(box.open)box.close();document.body.classList.remove('viewer-open');if(img)img.removeAttribute('src')}
function viewerZoom(factor,cx,cy){const {stage}=viewerParts();if(!stage)return;const sw=stage.clientWidth,sh=stage.clientHeight;const px=cx??sw/2,py=cy??sh/2;const next=Math.min(8,Math.max(0.05,viewerState.scale*factor));const ratio=next/viewerState.scale;viewerState.x=px-(px-viewerState.x)*ratio;viewerState.y=py-(py-viewerState.y)*ratio;viewerState.scale=next;viewerApply()}
(function bindImageViewer(){
  const stage=$('#imageViewerStage'),box=$('#imageViewer'),img=$('#imageViewerImg');
  if(!stage||!box||!img)return;
  // Never start with the overlay up: a stale `open` attribute would cover the whole
  // page and the close button only toggles that attribute.
  if(box.open)box.close();
  document.body.classList.remove('viewer-open');
  document.addEventListener('dblclick',e=>{const target=e.target.closest('.figure-grid img');if(!target||!target.getAttribute('src'))return;e.preventDefault();e.stopPropagation();openImageViewer(target.getAttribute('src'),target.getAttribute('alt'))},true);
  stage.addEventListener('dblclick',e=>{e.preventDefault();const rect=stage.getBoundingClientRect();const cx=e.clientX-rect.left,cy=e.clientY-rect.top;if(viewerState.scale<=viewerState.fit*1.02){viewerZoom(Math.max(2,viewerState.fit>0?1/viewerState.fit:2),cx,cy)}else{viewerFit()}});
  stage.addEventListener('wheel',e=>{e.preventDefault();const rect=stage.getBoundingClientRect();viewerZoom(Math.exp(-e.deltaY*0.0015),e.clientX-rect.left,e.clientY-rect.top)},{passive:false});
  stage.addEventListener('mousedown',e=>{if(e.button!==0)return;viewerState.dragging=true;viewerState.moved=false;viewerState.startX=e.clientX-viewerState.x;viewerState.startY=e.clientY-viewerState.y;stage.classList.add('grabbing');e.preventDefault()});
  window.addEventListener('mousemove',e=>{if(!viewerState.dragging)return;viewerState.moved=true;viewerState.x=e.clientX-viewerState.startX;viewerState.y=e.clientY-viewerState.startY;viewerApply()});
  window.addEventListener('mouseup',()=>{if(!viewerState.dragging)return;viewerState.dragging=false;stage.classList.remove('grabbing')});
  stage.addEventListener('click',e=>{if(e.target===stage&&!viewerState.moved)closeImageViewer()});
  $('#imageViewerClose').onclick=closeImageViewer;
  box.addEventListener('close',()=>{document.body.classList.remove('viewer-open');img.removeAttribute('src')});
  $('#imageViewerIn').onclick=()=>viewerZoom(1.25);
  $('#imageViewerOut').onclick=()=>viewerZoom(0.8);
  $('#imageViewerReset').onclick=viewerFit;
  document.addEventListener('keydown',e=>{if(e.key==='Escape'&&box.open){e.stopPropagation();closeImageViewer()}},true);
})();
/* ---------- 原文删除识别自检 ---------- */
$('#goneSelfCheck').onclick=async()=>{
  const info=$('#goneSelfCheckInfo'),box=$('#goneSelfCheckResult');
  info.textContent='正在探测三个样本…';box.innerHTML='';
  try{
    const r=await api('/api/diagnostics/gone-detection',{method:'POST'});
    info.textContent=r.all_pass?'三项全部通过：删除能识别、正常能放行':'有样本未按预期判定，见下表';
    box.innerHTML=`<div class="table-wrap"><table><thead><tr><th>样本</th><th>期望判定</th><th>结论</th><th>依据</th></tr></thead><tbody>${r.samples.map(s=>`<tr><td>${esc(s.name)}${s.title?`<br><small class="muted">${esc(s.title)}</small>`:''}</td><td>${s.expect==='should_not_exist'?'应判为已删除':'应判为可访问'}</td><td>${s.pass?'通过':'不符'}</td><td><small>${esc(s.error||s.reason||'')}</small></td></tr>`).join('')}</tbody></table></div>`;
  }catch(e){info.textContent='自检失败：'+e.message}
};
// ---------------- 删除与过滤规则 ----------------
function syncBulkBar(){
  const bar=$('#bulkBar');if(!bar)return;
  bar.hidden=itemPicked.size===0;
  if(itemPicked.size)$('#bulkInfo').textContent=`已选 ${itemPicked.size} 条`;
}
async function deleteItem(id){
  if(!confirm('从资料库删除这一条？\n\n正文与图片会保留，之后可以在「含已删除」里找回。'))return;
  const r=await api(`/api/items/${id}/delete`,{method:'POST',body:{}});
  itemPicked.delete(id);
  toast(r.deleted?'已从资料库删除，可在「含已删除」里找回':(r.reason||'没有改动'));
  loadItems().catch(()=>{});
  refreshOpenReader(id).catch(()=>{});
}
async function deletePicked(){
  if(!itemPicked.size)return toast('先勾选要删的条目');
  if(!confirm(`把选中的 ${itemPicked.size} 条从资料库删除？\n\n正文与图片会保留，之后可以在「含已删除」里找回。`))return;
  const r=await api('/api/items/bulk-delete',{method:'POST',body:{ids:[...itemPicked]}});
  itemPicked.clear();toast(`已删除 ${r.deleted} 条`);reloadItems();
}
let ruleRows=[];
function rulePayload(){
  return {scope:$('#ruleScope').value,account:$('#ruleAccount').value.trim(),field:$('#ruleField').value,
          patterns:$('#rulePatterns').value,match_mode:'any',note:$('#ruleNote').value.trim(),apply_now:true};
}
async function openRuleDialog(prefill){
  if(prefill){
    $('#ruleScope').value=prefill.scope||'account';
    $('#ruleAccount').value=prefill.account||'';
    $('#ruleField').value=prefill.field||'title';
    $('#rulePatterns').value=prefill.patterns||'';
    $('#ruleNote').value=prefill.note||'';
    $('#rulePreviewResult').textContent='';
  }
  await loadRuleList();
  const d=$('#ruleDialog');if(!d.open)d.showModal();
}
async function loadRuleList(){
  const r=await api('/api/filter-rules');ruleRows=r.items||[];
  $('#ruleList').innerHTML=ruleRows.map(x=>`<div class="rule-row"><div><b>${esc(x.description)}</b>${x.note?`<br><small class="muted">${esc(x.note)}</small>`:''}<br><small class="muted">${x.enabled?'启用中':'已停用'} · 累计挡住 ${x.hits||0} 条</small></div><div class="actions"><button type="button" data-rule-toggle="${x.id}" data-enabled="${x.enabled?1:0}">${x.enabled?'停用':'启用'}</button><button type="button" data-rule-remove="${x.id}">删除</button></div></div>`).join('')||'<p class="muted">还没有规则。规则只看标题，比拿全文做屏蔽安全得多。</p>';
}
async function previewRule(){
  const body=rulePayload();
  if(!body.patterns.trim())return toast('先写关键词');
  try{
    const r=await api('/api/filter-rules/preview',{method:'POST',body});
    $('#rulePreviewResult').innerHTML=r.matched
      ? `会挡住 <b>${r.matched}</b> 条已收录的：<ul>${r.samples.map(s=>`<li>${esc((s.title||'').slice(0,64))} <span class="muted">· ${esc(s.author)} · 命中 ${esc(s.hits)}</span></li>`).join('')}</ul>`
      : '按现在的关键词，一条都挡不到（以后新收录的会照规则拦）。';
  }catch(err){$('#rulePreviewResult').textContent='试跑失败：'+err.message}
}
async function saveRule(){
  const body=rulePayload();
  if(!body.patterns.trim())return toast('至少写一个关键词');
  try{
    const r=await api('/api/filter-rules',{method:'POST',body});
    toast(`规则已保存${r.applied?`，并从资料库移出 ${r.applied} 条`:'，之后新收录的会照规则拦'}`);
    $('#rulePatterns').value='';$('#ruleNote').value='';$('#rulePreviewResult').textContent='';
    await loadRuleList();reloadItems();
  }catch(err){toast(err.message)}
}
$('#filterRulesBtn').onclick=()=>openRuleDialog().catch(e=>toast(e.message));
$('#ruleClose').onclick=()=>$('#ruleDialog').close();
$('#rulePreview').onclick=previewRule;
$('#ruleSave').onclick=saveRule;
$('#bulkDelete').onclick=()=>deletePicked().catch(e=>toast(e.message));
$('#bulkClear').onclick=()=>{itemPicked.clear();loadItems().catch(()=>{})};
document.addEventListener('change',e=>{const box=e.target.closest('[data-pick]');if(!box)return;if(box.checked)itemPicked.add(box.dataset.pick);else itemPicked.delete(box.dataset.pick);const card=box.closest('.item');if(card)card.classList.toggle('picked',box.checked);syncBulkBar()});
const deepView=new URLSearchParams(location.search).get('view');if(deepView)setTimeout(()=>{if(loaders[deepView])switchView(deepView)},0);
// 深链：?item=<id>&pane=paper 直接打开某条与某一栏。通知、书签和自动化截图都用得上。
const deepItem=new URLSearchParams(location.search).get('item');
const deepPane=new URLSearchParams(location.search).get('pane');
if(deepItem)setTimeout(async()=>{try{await showItem(deepItem);if(deepPane&&wbIsOpen())wbToggleSide(deepPane)}catch(err){toast(err.message)}},200);
loadHealth();loadEvents();loadProgress().catch(()=>{});loadCredits().catch(()=>{});setInterval(loadHealth,5000);setInterval(()=>{if(currentView==='dashboard')loadCredits().catch(()=>{})},60000);setInterval(()=>{if(['dashboard','library'].includes(currentView))loadProgress().catch(()=>{})},2500);setInterval(()=>{if(['dashboard','sources','notifications'].includes(currentView))loaders[currentView]().catch(()=>{})},20000);

/* ---------- 自动更新 ----------
   启动 20 秒后后端会自己查一次，这里只负责把结果画出来；也可以手动点检查。
   装更新＝先下载 120 MB 的安装包（校验 sha256），再让安装器静默覆盖当前目录，
   所以点"重启并更新"之后页面会断十几秒，属于正常。 */
(function(){
  const badge=$('#updateBadge'),badgeVer=$('#updateBadgeVer'),current=$('#updateCurrent'),
        info=$('#updateInfo'),checkedAt=$('#updateCheckedAt'),notes=$('#updateNotes'),
        applyBtn=$('#updateApply'),checkBtn=$('#updateCheck'),
        progressWrap=$('#updateProgress'),bar=$('#updateBar'),progressText=$('#updateProgressText');
  if(!badge||!checkBtn)return;
  let timer=null;
  const mb=bytes=>bytes?`${(bytes/1024/1024).toFixed(0)} MB`:'';
  function paint(state){
    current.textContent=state.current||'–';
    checkedAt.textContent=state.checked_at?`上次检查 ${state.checked_at}`:'';
    badge.hidden=!state.has_update;
    badgeVer.textContent=state.latest||'';
    applyBtn.hidden=!state.has_update;
    applyBtn.disabled=false;
    if(state.has_update){
      info.textContent=`有新版本 ${state.latest}${state.size?`（${mb(state.size)}）`:''}${state.downloaded?'，安装包已下好':''}`;
      notes.hidden=!state.notes;notes.textContent=state.notes||'';
      applyBtn.textContent=state.downloaded?'重启并更新':'下载并更新';
    }else{
      notes.hidden=true;
      info.textContent=state.error?`没查到：${state.error}`:(state.latest?'已经是最新的':'还没查过');
    }
    const d=state.download||{},downloading=d.state==='downloading';
    progressWrap.hidden=!downloading&&d.state!=='done';
    if(downloading){
      bar.style.width=(d.percent||0)+'%';
      progressText.textContent=`下载中 ${d.percent||0}% · ${mb(d.bytes)} / ${mb(d.total)}`;
    }else if(d.state==='done'){
      bar.style.width='100%';progressText.textContent='安装包已下好，点「重启并更新」';
    }else if(d.state==='error'){
      progressWrap.hidden=false;bar.style.width='0%';progressText.textContent='下载失败：'+(d.error||'');
    }
    return downloading;
  }
  async function refresh(){
    try{
      const state=await api('/api/update');
      const downloading=paint(state);
      if(downloading&&!timer)timer=setInterval(refresh,1200);
      if(!downloading&&timer){clearInterval(timer);timer=null}
    }catch(e){info.textContent='读不到更新状态：'+e.message}
  }
  checkBtn.onclick=async()=>{
    checkBtn.disabled=true;info.textContent='正在查…';
    try{
      const state=await api('/api/update/check',{method:'POST',body:{}});
      paint(state);
      toast(state.has_update?`发现新版本 ${state.latest}`:'已经是最新版本');
    }catch(e){info.textContent='检查失败：'+e.message}
    finally{checkBtn.disabled=false}
  };
  applyBtn.onclick=async()=>{
    const state=await api('/api/update').catch(()=>null);
    if(!state)return;
    if(!state.downloaded){
      applyBtn.disabled=true;
      try{
        await api('/api/update/download',{method:'POST',body:{version:state.latest,url:state.url}});
        toast('开始下载安装包');
        if(!timer)timer=setInterval(refresh,1200);
      }catch(e){toast(e.message);applyBtn.disabled=false}
      return;
    }
    if(!confirm(`现在会退出程序，把 ${state.latest} 装到当前目录，装完自动重新打开。继续吗？`))return;
    try{
      await api('/api/update/apply',{method:'POST',body:{version:state.latest}});
      toast('正在退出并安装…十几秒后刷新页面即可');
    }catch(e){toast('启动更新失败：'+e.message)}
  };
  badge.onclick=()=>{
    switchView('settings');
    const tab=document.querySelector('.subnav-btn[data-subtab="params"]');
    if(tab)tab.click();
    setTimeout(()=>$('#updatePanel')?.scrollIntoView({behavior:'smooth',block:'center'}),180);
  };
  refresh();
  setTimeout(refresh,9000);
  window.refreshUpdateState=refresh;
})();

/* ---------- 微信文件与 C 盘 ----------
   微信 4.x 的存储位置在 %APPDATA%\Tencent\xwechat\config\*.ini 里，升级后可能被
   重置成默认值，一重置就往 C 盘的「文档」下灌。这里只做三件事：看、补一条兜底
   软链接、回收安装包缓存。不碰聊天记录。 */
(function(){
  const body=$('#wechatSpaceBody'),result=$('#wechatSpaceResult');
  if(!body)return;
  const gb=n=>(n?`${(n/1024/1024/1024).toFixed(1)} GB`:'0 B');
  function paint(s){
    if(!s||s.ok===false){body.textContent='检查失败：'+((s&&s.error)||'拿不到状态');return}
    const f=s.four_x||{},t=s.three_x||{},sz=s.sizes||{};
    const rows=[
      ['微信 4.x 存储', f.root||'（没找到）', f.root?(f.on_c?'在 C 盘 ⚠':'在外置盘 ✓'):''],
      ['4.x 配置', f.ini||'（没有 ini，按扫描结果推断）', ''],
      ['文档兜底链接', f.link_target?`${f.link} → ${f.link_target}`:`${f.link}（还没建）`, f.link_target?'✓':'⚠'],
      ['旧版 3.x', t.link_target?`${t.link} → ${t.link_target}`:t.link, t.on_c?'在 C 盘 ⚠':'✓'],
      ['C 盘上的微信占用', `4.x ${gb(sz.xwechat)} · 3.x ${gb(sz.wechat3)} · 其它腾讯 ${gb(sz.local_tencent)}`, `剩余 ${gb(s.free_c)}`],
      ['可回收', gb(s.cleanable), '安装包缓存 + 7 天前日志'],
    ];
    body.innerHTML=rows.map(function(r){
      return `<div class="space-row"><span>${r[0]}</span><b>${r[1]}</b><i>${r[2]||''}</i></div>`;
    }).join('')+(s.warnings&&s.warnings.length?`<p class="warn-text">${s.warnings.join('；')}</p>`:'');
  }
  async function load(){
    try{paint(await api('/api/wechat/space'))}catch(e){body.textContent='检查失败：'+e.message}
  }
  const refreshBtn=$('#wechatSpaceRefresh'),fixBtn=$('#wechatSpaceFix'),cleanBtn=$('#wechatSpaceClean');
  if(refreshBtn)refreshBtn.onclick=load;
  if(fixBtn)fixBtn.onclick=async()=>{
    result.textContent='正在补…';
    try{
      const r=await api('/api/wechat/space/fix',{method:'POST',body:{}});
      const parts=[].concat(r.actions||[],r.errors||[]);
      result.textContent=parts.length?parts.join('；'):'已经是对的，不用改';
      if(r.snapshot)paint(r.snapshot);
    }catch(e){result.textContent='失败：'+e.message}
  };
  if(cleanBtn)cleanBtn.onclick=async()=>{
    result.textContent='正在统计…';
    try{
      const dry=await api('/api/wechat/space/clean',{method:'POST',body:{}});
      const mb=(dry.freed/1024/1024).toFixed(0);
      if(!dry.freed){result.textContent='没有可回收的东西';return}
      if(!confirm(`可以回收 ${mb} MB（微信的安装包缓存 + 7 天前的日志）。现在删掉吗？`)){result.textContent='';return}
      const done=await api('/api/wechat/space/clean',{method:'POST',body:{apply:true}});
      result.textContent=`已回收 ${(done.freed/1024/1024).toFixed(0)} MB`;
      load();
    }catch(e){result.textContent='失败：'+e.message}
  };
  load();
})();

/* ---------- 磁盘占用与备份 ---------- */
(function(){
  const info=$('#backupInfo'),button=$('#backupRun');
  if(!info)return;
  const human=n=>{const units=['B','KB','MB','GB','TB'];let v=Math.abs(n||0),i=0;while(v>=1024&&i<units.length-1){v/=1024;i++}return `${v>=10?Math.round(v):v.toFixed(1)} ${units[i]}`};
  function paint(s){
    if(!s||s.error){info.textContent='读不到备份状态：'+((s&&s.error)||'');return}
    info.textContent=s.count
      ? `${s.count} 份每日快照（保留 ${s.keep||7} 份），最新 ${s.latest_at}（${human(s.latest_size)}）`+(s.manual?`；另有 ${s.manual} 份手动留底`:'')
      : '还没有每日快照，点左边按钮补一份';
  }
  async function load(){try{paint(await api('/api/backup'))}catch(e){info.textContent='读不到备份状态：'+e.message}}
  async function loadUsage(refresh){
    const box=$('#storageUsageInfo'),dir=$('#storageDirText');
    if(!box)return;
    box.textContent='正在统计（第一次要一两秒）…';
    try{
      const u=await api('/api/storage/usage'+(refresh?'?refresh=1':''));
      if(dir)dir.textContent=u.data_dir;
      const top=(u.parts||[]).filter(p=>p.bytes>0).slice(0,5)
        .map(p=>`${p.name} ${human(p.bytes)}`).join(' · ');
      box.textContent=`共 ${human(u.total_bytes)}（${u.total_files} 个文件）：${top}`;
    }catch(e){box.textContent='统计失败：'+e.message}
  }
  window.loadStorageUsage=loadUsage;
  if(button)button.onclick=async()=>{
    button.disabled=true;info.textContent='正在备份（库大的话要一两秒）…';
    try{const r=await api('/api/backup/run',{method:'POST',body:{}});
      info.textContent=r.ok?`已生成 ${human(r.size)} 的快照`:'备份失败：'+(r.error||'');
      if(r.ok)load();
    }catch(e){info.textContent='备份失败：'+e.message}
    finally{button.disabled=false}
  };
  const refreshBtn=$('#storageRefresh');
  if(refreshBtn)refreshBtn.onclick=()=>loadUsage(true);
  load();
})();
