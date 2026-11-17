const $=s=>document.querySelector(s), $$=s=>[...document.querySelectorAll(s)];
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const api=async(path,options={})=>{if(options.body&&typeof options.body!=='string'){options.body=JSON.stringify(options.body);options.headers={'Content-Type':'application/json',...(options.headers||{})}}const r=await fetch(path,options);const type=r.headers.get('content-type')||'';const data=type.includes('json')?await r.json():await r.text();if(!r.ok)throw new Error(data.detail||data.message||JSON.stringify(data));return data};
const toast=message=>{const el=$('#toast');el.textContent=message;el.classList.add('show');setTimeout(()=>el.classList.remove('show'),3200)};
const time=value=>value?new Date(value).toLocaleString('zh-CN',{hour12:false}):'—';
const bool=value=>value?'true':'false';
let currentView='dashboard', currentPapers=[];
let taskFilter='active',taskRefreshTimer=null;
const TASK_LABELS={poll_source:'采集检查',archive_item:'归档全文',papers_item:'论文下载与解析',analyze_item:'WorkBuddy 精读',flush_batch:'整理待发送汇报',deliver_batch:'发送汇报'};
const TASK_STATES={READY:'排队中',LEASED:'执行中',RETRY_WAIT:'等待重试',SUCCEEDED:'已完成',DEAD:'失败',CANCELLED:'已取消'};
const TASK_PROGRESS={READY:12,LEASED:62,RETRY_WAIT:32,SUCCEEDED:100,DEAD:100,CANCELLED:0};
async function loadHealth(){try{const h=await api('/api/health');$('#healthDot').className='dot '+(h.worker_alive?'online':'offline');$('#healthText').textContent=h.worker_alive?`后台运行中 · ${h.jobs_pending} 个任务待处理`:'后台 Worker 未连接';$('#taskPendingBadge').textContent=h.jobs_pending;$('#taskQueueBtn').classList.toggle('alert',h.jobs_dead>0);const cards=[['全部资料',h.items],['文章',h.articles],['视频',h.videos],['已下载论文',h.papers_downloaded],['待人工论文',h.papers_needed],['异常 / 死信',`${h.errors+h.source_errors} / ${h.jobs_dead}`]];$('#stats').innerHTML=cards.map(([k,v])=>`<div class="stat"><strong>${v}</strong><span>${k}</span></div>`).join('');if($('#taskDrawer').classList.contains('open'))loadTaskQueue().catch(()=>{})}catch(e){$('#healthDot').className='dot offline';$('#healthText').textContent='管理进程未连接'}}
function openTaskDrawer(){taskFilter='active';$('#taskDrawer').classList.add('open');$('#taskBackdrop').classList.add('open');$('#taskDrawer').setAttribute('aria-hidden','false');loadTaskQueue().catch(e=>{$('#taskList').innerHTML=`<div class="task-empty">${esc(e.message)}</div>`})}
function closeTaskDrawer(){$('#taskDrawer').classList.remove('open');$('#taskBackdrop').classList.remove('open');$('#taskDrawer').setAttribute('aria-hidden','true')}
async function loadTaskQueue(){const data=await api('/api/jobs/summary?limit=180');const c=data.counts;const cards=[['进行中',data.active],['排队',c.ready],['执行中',c.running],['待重试',c.retry],['失败',c.failed]];$('#taskSummary').innerHTML=cards.map(([k,v])=>`<div class="task-mini"><b>${v}</b><span>${k}</span></div>`).join('');const states=taskFilter==='active'?['READY','LEASED','RETRY_WAIT']:taskFilter==='failed'?['DEAD']:taskFilter==='done'?['SUCCEEDED','CANCELLED']:null;const rows=data.jobs.filter(j=>!states||states.includes(j.state));const list=$('#taskList'),scrollTop=list.scrollTop,openIds=new Set([...list.querySelectorAll('.task-item')].filter(x=>x.querySelector('details[open]')).map(x=>x.dataset.jobId));list.innerHTML=rows.map(j=>{const cls=j.state==='DEAD'?'failed':j.state==='RETRY_WAIT'?'retry':j.state==='READY'?'ready':'';const progress=TASK_PROGRESS[j.state]??0;const detail=JSON.stringify(j.payload||{});return `<article class="task-item ${cls}" data-job-id="${j.id}"><div class="task-item-head"><div><div class="task-kind">${esc(TASK_LABELS[j.kind]||j.kind)}</div><div class="task-target">${esc(j.target_title||'系统任务')}</div></div><span class="badge ${j.state==='SUCCEEDED'?'ok':j.state==='DEAD'?'error':j.state==='LEASED'?'wait':''}">${esc(TASK_STATES[j.state]||j.state)}</span></div><div class="task-progress"><i style="width:${progress}%"></i></div><div class="task-meta"><span>尝试 ${j.attempts}/${j.max_attempts}</span><span>优先级 ${j.priority}</span><span>更新 ${time(j.updated_at)}</span></div>${j.last_error?`<div class="task-error">${esc(j.last_error)}</div>`:''}${j.state==='DEAD'?`<div class="actions" style="margin-top:7px"><button class="primary" data-job-diagnostic="${j.id}">查看诊断</button>${j.retryable?`<button data-job-retry="${j.id}">临时错误重试</button>`:''}</div>`:''}<details><summary>任务详情</summary><pre>${esc('任务ID：'+j.id+'\n类型：'+(TASK_LABELS[j.kind]||j.kind)+'\n计划：'+time(j.run_at)+'\n参数：'+detail)}</pre></details></article>`}).join('')||`<div class="task-empty">当前筛选下没有任务。可切换「失败」或「最近完成」查看历史。</div>`;list.scrollTop=scrollTop;for(const item of list.querySelectorAll('.task-item')){if(openIds.has(item.dataset.jobId)){const d=item.querySelector('details');if(d)d.open=true}}$('#taskUpdated').textContent='更新于 '+new Date().toLocaleTimeString('zh-CN',{hour12:false})}
$('#taskQueueBtn').onclick=openTaskDrawer;$('#taskClose').onclick=closeTaskDrawer;$('#taskBackdrop').onclick=closeTaskDrawer;$('#taskRefresh').onclick=()=>loadTaskQueue().catch(e=>toast(e.message));document.addEventListener('keydown',e=>{if(e.key==='Escape')closeTaskDrawer()});document.querySelectorAll('[data-task-filter]').forEach(b=>b.onclick=()=>{taskFilter=b.dataset.taskFilter;document.querySelectorAll('[data-task-filter]').forEach(x=>x.classList.toggle('primary',x===b));loadTaskQueue().catch(e=>toast(e.message))});
async function loadEvents(){try{const rows=await api('/api/events?limit=60');$('#events').innerHTML=rows.map(x=>`<div class="event"><time>${time(x.created_at)}</time><span class="badge ${x.type.includes('error')?'error':''}">${esc(x.type)}</span><span>${esc(x.message)}</span></div>`).join('')||'<p class="muted">还没有运行事件</p>'}catch(e){toast(e.message)}}
async function loadSources(){const rows=await api('/api/sources');$('#sourcesList').innerHTML=rows.map(x=>`<div class="card source-card"><div><h3>${esc(x.name)}</h3><span class="badge ${x.enabled?'ok':''}">${x.enabled?'持续运行':'已停用'}</span> <span class="badge">${esc(x.type)}</span></div><div><div>${esc(x.capability)}</div><small>每 ${x.poll_seconds} 秒 · 上次成功 ${time(x.last_success_at)}</small>${x.catalog_total!==undefined?`<div><span class="badge ok">公众号库已启用 ${x.catalog_enabled} / ${x.catalog_total}</span>${x.catalog_linked?` <span class="badge ok">本地监测已联动${x.local_catalog&&x.local_catalog.matched?` · 已匹配 ${x.local_catalog.matched}`:''}</span>`:''}</div>`:''}${x.last_error?`<div class="error-text">${esc(x.last_error)}</div>`:''}<details><summary>来源配置</summary><pre>${esc(JSON.stringify(x.config,null,2))}</pre></details></div><div class="actions"><button data-run="${x.id}">立即检查</button>${x.type==='wechat_local'?`<button data-archive-history="${x.id}">历史全文归档（零精读点数）</button>`:''}<button data-toggle="${x.id}" data-enabled="${x.enabled?1:0}">${x.enabled?'停用':'启用'}</button></div></div>`).join('')||'<div class="panel muted">还没有来源。可先从 RSS 或分享桥 JSONL 开始。</div>'}
const WA_LABELS={LIFE_MOLECULAR:'生命科学基础',STRUCT_BIO:'结构生物学',CHEM_BIOL:'化学生物学',SYNTH_BIO:'合成生物学',OMICS_BIOINFO:'生信与组学',NEURO:'神经科学',BIOMED:'生物医学临床',CHEM_MAT:'化学与材料',PHYS_MATH:'物理数学天文',CS_AI:'计算机与AI',ENG_TECH:'工程与微流控',INTERDISCIPLINARY:'跨学科',SCI_COMM:'期刊与科学媒体',ACADEMIC_TOOL:'科研工具服务',CAMPUS_EDU:'校园教育',SOCIAL_HUMANITIES:'人文社科',LIFE_ENTERTAINMENT:'生活娱乐动漫',OTHER:'待判断'};
let waAll=[],waFiltered=[],waPage=0,waLoaded=false;const waSelected=new Set(),WA_PAGE_SIZE=60;
async function loadWechatAccounts(){const data=await api('/api/wechat/accounts?limit=2000');waAll=data.items;const s=data.stats||{};const cards=[['收录总数',s.total||0],['已启用',s.enabled||0],['CORE',s.core||0],['OPTIONAL',s.optional||0],['EXCLUDE',s.excluded||0],['已停用',(s.total||0)-(s.enabled||0)]];$('#wechatStats').innerHTML=cards.map(([k,v])=>`<div class="stat"><strong>${v}</strong><span>${k}</span></div>`).join('');if(!waLoaded){$('#waCategory').innerHTML='<option value="">全部分类</option>'+Object.entries(WA_LABELS).map(([k,v])=>`<option value="${k}">${v}</option>`).join('');waLoaded=true}applyWaFilter()}
function applyWaFilter(){const q=$('#waQ').value.trim().toLowerCase(),cat=$('#waCategory').value,fit=$('#waFit').value,en=$('#waEnabled').value;waFiltered=waAll.filter(x=>(!q||(x.name+' '+x.description+' '+x.reason+' '+(x.recent_article?x.recent_article.title:'')+' '+WA_LABELS[x.category_code]).toLowerCase().includes(q))&&(!cat||x.category_code===cat)&&(!fit||x.fit===fit)&&(!en||String(x.enabled)===en));waPage=0;renderWechat()}
function renderWechat(){const pages=Math.max(1,Math.ceil(waFiltered.length/WA_PAGE_SIZE));waPage=Math.min(waPage,pages-1);const rows=waFiltered.slice(waPage*WA_PAGE_SIZE,(waPage+1)*WA_PAGE_SIZE);$('#waRows').innerHTML=rows.map(x=>`<tr><td><input type="checkbox" data-wa-check="${x.id}" ${waSelected.has(x.id)?'checked':''}></td><td><b class="wa-open" data-wa-open="${x.id}" title="点开看它最近的推文">${esc(x.name)}</b>${x.account_id?`<div class="update">${esc(x.account_id)}</div>`:''}</td><td><span class="badge">${esc(WA_LABELS[x.category_code]||x.category_code)}</span></td><td><span class="badge ${x.fit.toLowerCase()}">${x.fit}</span></td><td><span class="badge ${x.enabled?'ok':'wait'}">${x.enabled?'已启用':'已停用'}</span></td><td><span class="reason">${esc(x.reason||'—')}</span><br><span class="recent">${x.recent_article?`最近：${esc((x.recent_article.title||'').slice(0,70))} · ${time(x.recent_article.discovered_at)}`:'<span class="muted">本站还没收录它的文章</span>'}</span>${x.description?`<br><small class="muted">简介：${esc(x.description.slice(0,56))}</small>`:''}</td></tr>`).join('')||'<tr><td colspan="6" class="muted">没有匹配的公众号</td></tr>';$('#waPageInfo').textContent=`第 ${waPage+1}/${pages} 页 · 筛选结果 ${waFiltered.length} 个`;$('#waSelectedInfo').textContent=`已勾选 ${waSelected.size} 个`;$('#waHeaderCheck').checked=rows.length>0&&rows.every(x=>waSelected.has(x.id));$('#waPrev').disabled=waPage<=0;$('#waNext').disabled=waPage>=pages-1}
async function setWaEnabled(enabled){if(!waSelected.size)return toast('请先勾选公众号');const r=await api('/api/wechat/accounts/bulk',{method:'POST',body:{ids:[...waSelected],enabled}});toast(`已${enabled?'启用':'停用'} ${r.updated} 个公众号`);await loadWechatAccounts()}
$('#waQ').oninput=applyWaFilter;$('#waSearch').onclick=applyWaFilter;$('#waCategory').onchange=applyWaFilter;$('#waFit').onchange=applyWaFilter;$('#waEnabled').onchange=applyWaFilter;$('#waPrev').onclick=()=>{waPage--;renderWechat()};$('#waNext').onclick=()=>{waPage++;renderWechat()};$('#waEnable').onclick=()=>setWaEnabled(true).catch(e=>toast(e.message));$('#waDisable').onclick=()=>setWaEnabled(false).catch(e=>toast(e.message));$('#waCheckPage').onclick=()=>{waFiltered.slice(waPage*WA_PAGE_SIZE,(waPage+1)*WA_PAGE_SIZE).forEach(x=>waSelected.add(x.id));renderWechat()};$('#waUncheckPage').onclick=()=>{waSelected.clear();renderWechat()};$('#waCheckAll').onclick=()=>{waFiltered.forEach(x=>waSelected.add(x.id));renderWechat()};$('#waHeaderCheck').onchange=e=>{waFiltered.slice(waPage*WA_PAGE_SIZE,(waPage+1)*WA_PAGE_SIZE).forEach(x=>e.target.checked?waSelected.add(x.id):waSelected.delete(x.id));renderWechat()};
$('#waRows').addEventListener('change',e=>{if(e.target.matches('[data-wa-check]')){const id=+e.target.dataset.waCheck;e.target.checked?waSelected.add(id):waSelected.delete(id);$('#waSelectedInfo').textContent=`已勾选 ${waSelected.size} 个`}});

/* ---------- 公众号近期推文面板 ----------
   数据来自本机微信的公众号消息库（PublicMsg.db），所以能看到很多还没进雷达的推文，
   那正是「补录」的来源。补录＝建条目＋排队抓正文，排除＝记住原因并可选地建一条
   该号专属的过滤规则（以后同类标题在入库那一刻就被挡住）。 */
let waPanelAcc=null,waPanelItems=[],waPanelRules=[],waPanelOffset=0,waPanelDays=365,waPanelHasMore=false;
const waPanelSelected=new Set();
let waPresets=[],waExcludeTargets=[];
const WA_PANEL_PAGE=24;

const waStateTag={archived:'<span class="wa-tag ok">已收录</span>',index_only:'<span class="wa-tag info">仅索引</span>',
  excluded:'<span class="wa-tag warn">已排除</span>',new:''};

function waCard(item){
  const state=item.state||'new';
  // 封面走本机代理：微信图床有防盗链，浏览器直连只会拿到"不可引用"的占位图。
  const cover=item.cover?`<img class="wa-cover-img" src="/api/wechat/cover?u=${encodeURIComponent(item.cover)}" alt="" loading="lazy" onerror="this.remove()">`:'';
  const initial=`<span class="wa-initial">${esc((item.title||'#').slice(0,1))}</span>`;
  const detail=item.detail||{};
  let note='';
  if(state==='excluded')note=`<div class="wa-note">${esc(detail.reason||'已排除')}</div>`;
  else if(item.rule_note)note=`<div class="wa-note">按现有规则不会收录：${esc(item.rule_note)}</div>`;
  const tools=[];
  if(state==='new')tools.push('<button type="button" data-wa-act="import">补录</button>');
  if(state==='index_only')tools.push('<button type="button" data-wa-act="import">补全文</button>');
  if(state==='archived'&&detail.item_id)tools.push('<button type="button" data-wa-act="go">在资料库</button>');
  if(state==='excluded')tools.push('<button type="button" data-wa-act="restore">恢复</button>');
  if(state!=='excluded')tools.push('<button type="button" class="danger" data-wa-act="exclude">排除</button>');
  const picked=waPanelSelected.has(item.canonical_url)?' picked':'';
  return `<article class="wa-card${picked}" data-state="${state}" data-wa-key="${esc(item.canonical_url)}">
    <div class="wa-cover ${item.cover?'':'noimg'}">${initial}${cover}<span class="wa-pick">✓</span>${waStateTag[state]||''}</div>
    <div class="wa-body">
      <a class="wa-title" href="${esc(item.url)}" target="_blank" rel="noreferrer" title="${esc(item.summary||'')}">${esc(item.title)}</a>
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
  const cat=$('#waPanelCategory');
  cat.innerHTML=Object.entries(WA_LABELS).map(([k,v])=>`<option value="${k}">${esc(v)}</option>`).join('');
  cat.value=acc.category_code||'OTHER';
  $('#waPanelFit').value=acc.fit||'OPTIONAL';
  $('#waPanelEnabled').checked=!!acc.enabled;
  $('#waPanelName').textContent=acc.name;
  $('#waPanelMeta').innerHTML=`${acc.account_id?`<span class="badge">${esc(acc.account_id)}</span>`:''}<span>${esc(acc.reason||'')}</span>`;
  $('#waPanelStatus').textContent='正在读取本机微信的公众号消息库…';
  $('#waArticlesGrid').innerHTML='';
  $('#waPanelMore').hidden=true;$('#waPanelCount').textContent='';
  syncWaPanelPicked();
  if(!$('#waArticles').open)$('#waArticles').showModal();
  await loadWaPanel(true);
}
function closeWaPanel(){const d=$('#waArticles');if(d.open)d.close()}

async function loadWaPanel(reset){
  if(!waPanelAcc)return;
  if(reset){waPanelOffset=0;waPanelItems=[];}
  const more=$('#waPanelMore');more.hidden=true;more.disabled=true;
  try{
    const data=await api(`/api/wechat/accounts/${waPanelAcc.id}/articles?limit=${WA_PANEL_PAGE}&offset=${waPanelOffset}&days=${waPanelDays}`);
    if(data.error){
      $('#waPanelStatus').textContent=data.error;
      if(reset)$('#waArticlesGrid').innerHTML='<div class="wa-empty">这台电脑上暂时看不到它的推文</div>';
      return;
    }
    waPanelItems=waPanelItems.concat(data.items||[]);
    waPanelRules=data.rules||[];
    waPanelHasMore=!!data.has_more;
    waPanelOffset+=WA_PANEL_PAGE;
    const bits=[`共 ${data.total} 篇`];
    bits.push(`资料库已收录 ${waPanelItems.filter(x=>x.state==='archived'||x.state==='index_only').length} 篇`);
    if(waPanelRules.length)bits.push(`本号排除规则 ${waPanelRules.length} 条`);
    if(data.note)bits.push(data.note);
    $('#waPanelStatus').textContent=bits.join(' · ');
    $('#waArticlesGrid').innerHTML=waPanelItems.map(waCard).join('')||'<div class="wa-empty">这段时间里它没有推文</div>';
    more.hidden=!waPanelHasMore;
    $('#waPanelCount').textContent=waPanelHasMore?`已显示 ${waPanelItems.length} / ${data.total} 篇`:`共 ${waPanelItems.length} 篇`;
  }catch(e){
    $('#waPanelStatus').textContent=e.message;
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
    await api(`/api/wechat/accounts/${waPanelAcc.id}`,{method:'PATCH',body:{enabled:e.target.checked}});
    waPanelAcc.enabled=e.target.checked?1:0;
    toast(e.target.checked?'已恢复监测':'已停止监测这个号');
    loadWechatAccounts().catch(()=>{});
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
    if(x.processing_policy==='history_index_only') return {badge:'仅索引',text:'仅保存了索引，尚未抓取正文；在详情里点「归档并精读」即可补全文'};
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
async function loadCollections(scope){try{const rows=await api('/api/collections?scope='+scope);if(scope==='library'){itemCollections=rows;window.__itemCollections=rows}else{ideaCollections=rows;window.__ideaCollections=rows}return rows}catch(e){return []}}
function collectionName(scope,id){const list=scope==='library'?itemCollections:ideaCollections;const hit=list.find(c=>c.id===id);return hit?hit.name:''}
function fillCollectionSelect(select,list){
  if(!select)return;
  const keep=select.value;
  const head=select.querySelector('option[value=""]');
  select.innerHTML=`<option value="">${head?head.textContent:'全部收藏夹'}</option><option value="__none__">未收藏</option>`+list.map(c=>`<option value="${esc(c.id)}">${esc(c.name)}${c.count?`（${c.count}）`:''}</option>`).join('');
  select.value=[...select.options].some(o=>o.value===keep)?keep:'';
}
function scheduleListRefresh(){if(listRefreshTimer)return;listRefreshTimer=setTimeout(()=>{listRefreshTimer=null;if(currentView==='library')loadItems().catch(()=>{})},4000)}
async function loadItems(){
  const params=new URLSearchParams({q:$('#searchQ').value.trim(),kind:$('#searchKind').value,status:$('#searchStatus').value,limit:itemPageSize,offset:(itemPage-1)*itemPageSize});
  const since=daysAgoIso($('#searchRange').value);
  if(since)params.set('since',since);
  const collection=$('#searchCollection').value;
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
  await loadCollections(collectScope(entryType));
  renderCollectPop();
  const pop=$('#collectionPop');
  pop.hidden=false;
  const rect=anchor.getBoundingClientRect();
  const width=pop.offsetWidth||300,height=pop.offsetHeight||220;
  const left=Math.min(window.innerWidth-width-12,Math.max(12,rect.right-width));
  let top=rect.bottom+8;
  if(top+height>window.innerHeight-12)top=Math.max(12,rect.top-height-8);
  pop.style.left=left+'px';
  pop.style.top=top+'px';
}
function renderCollectPop(){
  if(!collectTarget)return;
  const scope=collectScope(collectTarget.entryType);
  const list=scope==='library'?itemCollections:ideaCollections;
  $('#collectionPopList').innerHTML=list.length?list.map(c=>`<label><input type="checkbox" data-collect-toggle="${esc(c.id)}" ${collectTarget.ids.includes(c.id)?'checked':''}><span>${esc(c.name)}</span><small class="muted">${c.count||0}</small></label>`).join(''):'<span class="muted">还没有收藏夹，先在下面新建一个。</span>';
}
function closeCollectPop(){$('#collectionPop').hidden=true;collectTarget=null}
async function toggleCollect(collectionId,member){
  if(!collectTarget)return;
  const target=collectTarget;
  const r=await api('/api/collections/toggle',{method:'POST',body:{collection_id:collectionId,entry_type:target.entryType,entry_id:target.entryId,member}});
  target.ids=r.member?[...new Set([...target.ids,collectionId])]:target.ids.filter(x=>x!==collectionId);
  await loadCollections(collectScope(target.entryType));
  renderCollectPop();
  fillCollectionSelect($('#searchCollection'),itemCollections);
  if(target.refresh)target.refresh().catch(()=>{});
  toast(r.member?'已加入收藏夹':'已移出收藏夹');
}
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
  fillCollectionSelect($('#searchCollection'),itemCollections);
  toast(`已创建收藏夹「${created.name}」`);
  return created;
}
async function openCollectionManager(scope){
  window.__manageScope=scope;
  $('#collectionDialogTitle').textContent=scope==='library'?'管理资料库收藏夹':'管理 Ideas 与笔记收藏夹';
  await loadCollections(scope);
  renderCollectionManage();
  $('#collectionDialog').showModal();
}
function renderCollectionManage(){
  const scope=window.__manageScope||'library';
  const list=scope==='library'?itemCollections:ideaCollections;
  $('#collectionDialogList').innerHTML=list.length?list.map(c=>`<div class="row" data-collection-row="${esc(c.id)}"><input value="${esc(c.name)}" data-collection-name="${esc(c.id)}"><span class="count">${c.count||0} 项</span><button type="button" data-collection-save="${esc(c.id)}">改名</button><button type="button" class="danger" data-collection-delete="${esc(c.id)}">删除</button></div>`).join(''):'<div class="muted">还没有收藏夹。</div>';
}
const listBlock=(title,items)=>Array.isArray(items)&&items.length?`<div class="detail-section"><h3>${title}</h3><ol>${items.map(x=>`<li>${esc(typeof x==='string'?x:JSON.stringify(x))}</li>`).join('')}</ol></div>`:'';
let detailWatchTimer=null,detailWatchId=null,detailSeq=0;
function stopItemWatch(){if(detailWatchTimer)clearInterval(detailWatchTimer);detailWatchTimer=null;detailWatchId=null}
function watchItem(id){if(detailWatchId===id&&detailWatchTimer)return;stopItemWatch();detailWatchId=id;let ticks=0;
  detailWatchTimer=setInterval(async()=>{ticks++;
    // The user closed the panel or moved to another item: this timer must die, or it
    // keeps re-rendering and re-opening the dialog over whatever they are doing.
    if(!$('#detailDialog').open||detailWatchId!==id){stopItemWatch();return}
    try{const x=await api('/api/items/'+id);const hasAnalysis=x.analysis&&Object.keys(x.analysis).length>0;
    if(detailWatchId!==id)return;
    if(!x.active_job){stopItemWatch();toast(hasAnalysis?'精读已完成，结果已更新':'处理已结束，可查看最新状态')}
    await showItem(id,true)}catch(e){stopItemWatch()}},3000)}
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
async function showItem(id,silent){
  const seq=++detailSeq;
  const x=await api('/api/items/'+id),a=x.analysis||{},stage=itemStage(x);
  // A slower response for the previous item must not overwrite the one just opened.
  if(seq!==detailSeq)return x;
  if(detailWatchId&&detailWatchId!==id)stopItemWatch();
  window.__currentItem=x;
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
  $('#detailContent').innerHTML=`<h1 class="detail-title">${esc(x.title||x.url)}</h1><p>${esc(x.author)} · ${time(x.published_at||x.discovered_at)}</p><div class="actions">${x.html_path?`<a class="button primary" href="/api/items/${x.id}/archive" target="_blank">离线阅读页</a>`:''}<a class="button" href="${esc(x.url)}" target="_blank" rel="noopener">打开原文</a>${x.metadata.original_share_url&&x.metadata.original_share_url!==x.url?`<a class="button" href="${esc(x.metadata.original_share_url)}" target="_blank" rel="noopener">原始分享短链</a>`:''}${x.excluded_at?'':(x.status==='discovered'?`<button class="primary" data-process="${x.id}">归档并精读</button>`:`<button data-reanalyze="${x.id}">重新精读</button>`)}<button type="button" data-verify-source="${x.id}">检查原文</button>${(x.metadata.images||[]).length?`<button type="button" data-retriage="${x.id}">重新判断配图</button>`:''}<button type="button" data-collect="${x.id}" data-collect-type="item" data-collect-ids="${(x.collections||[]).join(',')}">收藏到…</button>${x.excluded_at?'':`<button type="button" class="danger" data-delete="${x.id}">删除</button>`}<button type="button" data-rule-from="${esc(x.author||'')}">以后屏蔽这类</button>${(x.collections||[]).map(id=>{const n=collectionName('library',id);return n?`<span class="collection-chip">${esc(n)}</span>`:''}).join('')}</div><p><span class="badge ${x.status==='ready'?'ok':x.status==='error'?'error':x.status==='discovered'?'wait':''}">${esc(stage.badge)}</span> <span class="badge">重要度 ${Math.round(x.importance)}</span> <span class="badge">相关度 ${Math.round(x.relevance)}</span> <span class="badge">证据 ${esc(x.metadata.archive_completeness||'未知')}</span>${elapsed?` <span class="badge">精读用时 ${elapsed}</span>`:''}${((x.metadata||{}).image_triage||{}).dropped?` <span class="badge">已过滤 ${x.metadata.image_triage.dropped} 张无用配图</span>`:''}</p>${excludedLine}${goneLine}${jobLine}${liveLine}${deadLine}${x.error?`<div class="notice error-text">${esc(x.error)}</div>`:''}${body}<details class="raw-text"><summary>归档全文 / 转录</summary><div class="detail-text">${esc(x.text||'尚无正文')}</div></details><div class="detail-section"><h2>关联论文</h2>${x.papers.map(p=>`<p><a href="https://doi.org/${esc(p.doi)}" target="_blank">${esc(p.doi)}</a> · ${esc(p.status)} · ${esc(p.title)}<br>${p.local_path?`<span class="actions"><button data-pdf-preview="${p.id}">网页预览</button><button data-paper-wps="${p.id}">WPS 打开</button><button data-paper-reveal="${p.id}">所在目录</button></span><br><small>${esc(p.local_path)}</small>`:''}<br><small>${esc(p.error||'')}</small></p>`).join('')||'<p class="muted">未识别到 DOI</p>'}</div>`;
  if(!silent||!$('#detailDialog').open)openDetail();
  if(x.active_job)watchItem(id);else if(detailWatchId===id)stopItemWatch();
  return x;
}
async function previewPaper(id){const p=currentPapers.find(x=>x.id===id)||{title:'PDF 预览'};$('#detailContent').innerHTML=`<h1>${esc(p.title||'PDF 预览')}</h1><p class="muted">文件保存在：<code>${esc(p.local_path||'E 盘论文目录')}</code></p><iframe src="/api/papers/${id}/file#view=FitH" style="width:100%;height:78vh;border:1px solid #dde3e9;border-radius:8px"></iframe>`;openDetail()}
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
  const collection=$('#ideaCollection').value;
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
async function loadSettings(){const data=await api('/api/settings'),s=data.settings;$('#llmEnabled').value=bool(s.llm.enabled);$('#llmProvider').value=s.llm.provider||'openai_compatible';$('#llmBase').value=s.llm.base_url;$('#llmModel').value=s.llm.model;$('#visionEnabled').value=bool(s.llm.vision_enabled);$('#llmDaily').value=s.llm.daily_call_limit;$('#llmPrefilter').value=s.llm.min_local_relevance??40;$('#llmLongMaterial').value=s.llm.long_material_strategy||'head_tail';$('#readingPrompt').value=s.analysis.custom_reading_prompt;$('#notifyChannel').value=s.notifications.channel;$('#instantThreshold').value=s.notifications.instant_threshold;$('#batchSize').value=s.notifications.batch_size;$('#maxWait').value=s.notifications.max_wait_minutes;$('#minRelevance').value=s.notifications.min_relevance;$('#quietStart').value=s.notifications.quiet_start;$('#quietEnd').value=s.notifications.quiet_end;$('#quietCritical').value=bool(s.notifications.quiet_allow_critical);$('#paperEmail').value=s.papers.email;window.__secretProviders=data.secrets.providers||[];$('#maxPerDay').value=s.notifications.max_per_day??3;$('#sendWindows').value=(s.notifications.send_windows||[]).join(',');$('#highlightThreshold').value=s.notifications.highlight_threshold??74;$('#digestLimit').value=s.notifications.digest_limit??10;if(window.renderAccounts)renderAccounts(s.llm.accounts||[]);$('#asrEnabled').value=bool(s.video.asr_enabled);$('#asrModel').value=s.video.asr_model;$('#asrDuration').value=s.video.max_duration_minutes;$('#llmKey').disabled=$('#llmProvider').value!=='openai_compatible';$('#llmBase').disabled=$('#llmProvider').value!=='openai_compatible';$('#llmKeyStatus').textContent=data.secrets.llm_api_key?'已安全保存':'未配置';$('#dataxKeyStatus').textContent=data.secrets.datax_api_key?'已安全保存':'未配置';$('#webhookStatus').textContent=data.secrets.notification_webhook?'已安全保存':'未配置';$('#wecomKfSecretStatus').textContent=data.secrets.wecom_kf_secret?'已安全保存':'未配置'}
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
    <div class="actions" style="margin-top:12px"><button class="primary" data-archive-index>归档全部仅索引条目</button><span class="muted">限速后台执行，不调用云模型，可随时查看上面的进度</span></div>`;
  ['#libraryProgress','#dashboardProgress'].forEach(sel=>{const el=$(sel);if(el)el.innerHTML=html});
}
async function loadCredits(){
  let c;try{c=await api('/api/credits?days=30')}catch(e){return}
  const money=v=>Number(v||0).toFixed(v>=100?1:2);
  const cards=[['今日实扣',money(c.today_points)+' 点'],['30 天实扣',money(c.window_points)+' 点'],['累计实扣',money(c.total_points)+' 点'],['平均每次精读',money(c.reading_avg)+' 点'],['精读次数',c.reading_calls],['每篇中位数',money(c.reading_median)+' 点']];
  const models=(c.by_model||[]).slice(0,4).map(m=>`<span class="badge">${esc(m.model)} ${m.calls} 次 · 实扣 ${money(m.points)} 点${m.reference?`（原价 ${money(m.reference)}）`:''} · 均 ${m.avg_seconds}s</span>`).join(' ');
  const daily=(c.daily||[]).slice(-14);
  const peak=Math.max(1,...daily.map(d=>d.points));
  const bars=daily.map(d=>`<div class="bar" title="${d.day} · ${money(d.points)} 点 / ${d.calls} 次"><i style="height:${Math.max(3,Math.round(d.points/peak*46))}px"></i><span>${d.day.slice(5)}</span></div>`).join('');
  const rows=(c.recent||[]).slice(0,8).map(r=>`<tr><td>${time(r.time)}</td><td>${esc(r.model)}</td><td>${r.task==='analyze'?'精读':'对话'}</td><td>${money(r.points)}</td><td>${r.prompt_tokens}+${r.completion_tokens}</td><td>${Math.round((r.elapsed_ms||0)/1000)}s</td></tr>`).join('');
  $('#creditsPanel').innerHTML=`<div class="panel-head"><h2>WorkBuddy 点数</h2><span class="muted">按每次调用的实扣 credit 记账；免费模型只显示折算原价 ${money(c.total_reference)} 点</span></div>
    <div class="credit-grid">${cards.map(([k,v])=>`<div class="credit-cell"><b>${v}</b><span>${k}</span></div>`).join('')}</div>
    <div class="credit-bars">${bars||'<span class="muted">最近没有调用</span>'}</div>
    <div class="credit-models">${models}</div>
    ${rows?`<details><summary>最近调用明细</summary><div class="table-wrap"><table><thead><tr><th>时间</th><th>模型</th><th>用途</th><th>点数</th><th>输入+输出</th><th>用时</th></tr></thead><tbody>${rows}</tbody></table></div></details>`:''}`;
}
const loaders={dashboard:()=>Promise.all([loadHealth(),loadEvents(),loadProgress(),loadCredits()]),sources:loadSources,wechat:loadWechatAccounts,library:()=>Promise.all([loadCollections('library').then(list=>fillCollectionSelect($('#searchCollection'),list)),loadItems(),loadProgress()]),papers:loadPapers,ideas:()=>Promise.all([loadCollections('ideas').then(list=>fillCollectionSelect($('#ideaCollection'),list)),loadIdeas()]),notifications:loadNotifications,settings:()=>Promise.all([loadSettings(),window.loadSetupPanel?loadSetupPanel():null])};
function switchView(view){currentView=view;$$('.view').forEach(x=>x.classList.toggle('active',x.id===view));$$('.nav').forEach(x=>x.classList.toggle('active',x.dataset.view===view));loaders[view]().catch(e=>toast(e.message))}
$$('.nav').forEach(x=>x.onclick=()=>switchView(x.dataset.view));
$('#ingestBtn').onclick=async()=>{try{const result=await api('/api/ingest',{method:'POST',body:{text:$('#quickText').value}});$('#ingestResult').textContent=`已接收 ${result.items.length} 条，其中新增 ${result.items.filter(x=>x.created).length} 条`;$('#quickText').value='';loadHealth()}catch(e){toast(e.message)}};
$('#sourceType').onchange=()=>{const examples={rss:{url:'https://example.com/feed.xml',kind:'article'},wecom_kf:{corp_id:'ww...',open_kf_id:'wk...',start_at_now:true},wechat_datax:{accounts:[{name:'公众号准确名称',id:'gh_原始编号'}],max_price_per_call:'0.05'},bilibili_datax:{queries:['基因编辑 研究进展','蛋白质结构预测'],up_ids:[],sort:'latest',min_views:100,exclude_keywords:['课程','广告'],max_price_per_call:'0.01'},jsonl:{path:'E:\\HermesResearchRadar\\data\\share_inbox.jsonl',start_at_end:false},wechat_local:{target_wxid:'对方的微信号',target_name:'聊天窗口名称',history_mode:'index_only'},wechat_public_local:{start_at_now:true,use_catalog:true,max_rows_per_poll:1000}};$('#sourceConfig').value=JSON.stringify(examples[$('#sourceType').value]||{},null,2);$('#sourcePoll').value=$('#sourceType').value==='jsonl'?2:($('#sourceType').value==='wecom_kf'||['wechat_local','wechat_public_local'].includes($('#sourceType').value)?60:300)};
$('#addSource').onclick=async()=>{try{const body={name:$('#sourceName').value,type:$('#sourceType').value,poll_seconds:Number($('#sourcePoll').value),config:JSON.parse($('#sourceConfig').value)};await api('/api/sources',{method:'POST',body});$('#sourceName').value='';$('#sourceResult').textContent='已创建，后台会自动检查';loadSources()}catch(e){toast(e.message)}};
$('#importWechat').onclick=async()=>{try{const body={names_text:$('#wechatNames').value,initial_backfill_days:Number($('#wechatBackfillDays').value),poll_seconds:Number($('#wechatPollSeconds').value)};const r=await api('/api/sources/wechat/import',{method:'POST',body});$('#wechatImportResult').textContent=`新增 ${r.added} 个；`+(r.needs_key?'清单已保存，填写 DataXAPI Key 后自动启用':'已开始自动监测与历史追补');loadSources()}catch(e){toast(e.message)}};
function reloadItems(){itemPage=1;loadItems().catch(e=>toast(e.message))}
$('#searchBtn').onclick=reloadItems;
$('#searchQ').onkeydown=e=>{if(e.key==='Enter')reloadItems()};
['#searchKind','#searchStatus','#searchRange','#searchCollection','#searchExcluded','#hideLow'].forEach(sel=>{const el=$(sel);if(el)el.onchange=reloadItems});
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
$('#settingsForm').onsubmit=async e=>{e.preventDefault();try{const patch={llm:{enabled:$('#llmEnabled').value==='true',provider:$('#llmProvider').value,base_url:$('#llmBase').value,model:$('#llmModel').value,vision_enabled:$('#visionEnabled').value==='true',daily_call_limit:Number($('#llmDaily').value),min_local_relevance:Number($('#llmPrefilter').value),long_material_strategy:$('#llmLongMaterial').value,accounts:window.collectAccounts?collectAccounts():undefined},analysis:{custom_reading_prompt:$('#readingPrompt').value},notifications:{channel:$('#notifyChannel').value,instant_threshold:Number($('#instantThreshold').value),batch_size:Number($('#batchSize').value),max_wait_minutes:Number($('#maxWait').value),min_relevance:Number($('#minRelevance').value),quiet_start:$('#quietStart').value,quiet_end:$('#quietEnd').value,quiet_allow_critical:$('#quietCritical').value==='true',max_per_day:Number($('#maxPerDay').value),send_windows:$('#sendWindows').value.split(',').map(x=>x.trim()).filter(Boolean),highlight_threshold:Number($('#highlightThreshold').value),digest_limit:Number($('#digestLimit').value)},papers:{email:$('#paperEmail').value},video:{asr_enabled:$('#asrEnabled').value==='true',asr_model:$('#asrModel').value,max_duration_minutes:Number($('#asrDuration').value)}};await api('/api/settings',{method:'PUT',body:patch});for(const [id,name] of [['llmKey','llm_api_key'],['dataxKey','datax_api_key'],['notifyWebhook','notification_webhook'],['wecomKfSecret','wecom_kf_secret']]){if($('#'+id).value){await api('/api/settings/secrets',{method:'POST',body:{name,value:$('#'+id).value}});$('#'+id).value=''}}$('#settingsResult').textContent='已保存';toast('设置已安全保存')}catch(err){toast(err.message)}};
function openDetail(){const d=$('#detailDialog');if(!d.open)d.show();$('#detailBackdrop').classList.add('open');document.body.classList.add('modal-open')}
function closeDetail(){const d=$('#detailDialog');stopItemWatch();if(d.open)d.close();$('#detailBackdrop').classList.remove('open');document.body.classList.remove('modal-open');closeCollectPop()}
$('#closeDialog').onclick=closeDetail;
$('#detailBackdrop').onclick=closeDetail;
$('#detailDialog').addEventListener('close',()=>{stopItemWatch();$('#detailBackdrop').classList.remove('open');document.body.classList.remove('modal-open')});
document.addEventListener('keydown',e=>{if(e.key!=='Escape')return;if(!$('#collectionPop').hidden){closeCollectPop();return}if($('#chatDrawer').classList.contains('open')){$('#chatClose').click();return}if($('#detailDialog').open)closeDetail()});
$('#collectionPopClose').onclick=closeCollectPop;
$('#collectionCreate').onclick=()=>{const scope=collectTarget?collectScope(collectTarget.entryType):'library';const input=$('#collectionNewName');createCollectionFor(input.value,scope,true).then(()=>{input.value=''}).catch(e=>toast(e.message))};
$('#collectionDialogClose').onclick=()=>$('#collectionDialog').close();
$('#collectionManageCreate').onclick=async()=>{const input=$('#collectionManageName');const scope=window.__manageScope||'library';try{await createCollectionFor(input.value,scope,false);input.value='';renderCollectionManage()}catch(e){toast(e.message)}};
document.addEventListener('change',e=>{const box=e.target.closest('[data-collect-toggle]');if(box)toggleCollect(box.dataset.collectToggle,box.checked).catch(err=>toast(err.message))});
document.addEventListener('click',async e=>{if(e.target.closest('.pick'))return;const t=e.target.closest('button,[data-item]');if(!t)return;try{
if(t.dataset.delete){await deleteItem(t.dataset.delete);return}
if(t.dataset.ruleFrom){await openRuleDialog({account:t.dataset.ruleFrom,scope:'account',field:'title',patterns:''});return}
if(t.dataset.ruleToggle!==undefined){await api(`/api/filter-rules/${t.dataset.ruleToggle}`,{method:'PATCH',body:{enabled:t.dataset.enabled!=='1'}});await loadRuleList();reloadItems();return}
if(t.dataset.ruleRemove!==undefined){if(!confirm('删除这条规则？被它挡住的条目会一起放回资料库。'))return;const r=await api(`/api/filter-rules/${t.dataset.ruleRemove}`,{method:'DELETE'});toast(`规则已删除${r.restored?`，恢复 ${r.restored} 条`:''}`);await loadRuleList();reloadItems();return}
if(t.dataset.item)await showItem(t.dataset.item);
if(t.dataset.page!==undefined&&t.dataset.page!==''){const n=Number(t.dataset.page);if(n>=1){itemPage=n;await loadItems()}return}
if(t.dataset.ppage!==undefined&&t.dataset.ppage!==''){const n=Number(t.dataset.ppage);if(n>=1&&n!==paperPage){paperPage=n;await loadPapers()}return}
if(t.dataset.collect){await openCollectPop(t.dataset.collectType||'item',t.dataset.collect,(t.dataset.collectIds||'').split(',').filter(Boolean),t,()=>currentView==='library'?loadItems():loadIdeas());return}
if(t.dataset.restore){await api(`/api/items/${t.dataset.restore}/restore`,{method:'POST'});toast('已恢复收录');loadItems().catch(()=>{});await showItem(t.dataset.restore,true);return}
if(t.dataset.collectionSave){const input=document.querySelector(`[data-collection-name="${t.dataset.collectionSave}"]`);await api(`/api/collections/${t.dataset.collectionSave}`,{method:'PATCH',body:{name:input.value}});toast('已改名');await loadCollections(window.__manageScope||'library');renderCollectionManage();fillCollectionSelect($('#searchCollection'),itemCollections);return}
if(t.dataset.collectionDelete){if(!confirm('删除这个收藏夹？条目本身不会被删除。'))return;await api(`/api/collections/${t.dataset.collectionDelete}`,{method:'DELETE'});toast('已删除收藏夹');await loadCollections(window.__manageScope||'library');renderCollectionManage();fillCollectionSelect($('#searchCollection'),itemCollections);loadItems().catch(()=>{});return}
if(t.dataset.run){await api(`/api/sources/${t.dataset.run}/run`,{method:'POST'});toast('已加入立即检查队列')}if(t.dataset.archiveHistory){if(!confirm('将控速归档历史全文、图片和可获取论文，不自动调用云精读。确定开始吗？'))return;const r=await api(`/api/sources/${t.dataset.archiveHistory}/archive-history`,{method:'POST'});toast(r.queued?`已开始历史归档，剩余 ${r.remaining} 条；云精读调用 0`:'没有待归档的历史索引')}if(t.dataset.toggle){await api(`/api/sources/${t.dataset.toggle}`,{method:'PATCH',body:{enabled:t.dataset.enabled!=='1'}});loadSources()}if(t.dataset.process){await api(`/api/items/${t.dataset.process}/process`,{method:'POST'});toast('已加入归档与精读队列，完成后本页自动刷新');if($('#detailDialog').open)showItem(t.dataset.process,true).catch(()=>{});loadItems().catch(()=>{})}if(t.dataset.reanalyze){await api(`/api/items/${t.dataset.reanalyze}/reanalyze`,{method:'POST'});toast('已排队，正在精读，完成后自动刷新');if($('#detailDialog').open)showItem(t.dataset.reanalyze,true).catch(()=>{})}if(t.dataset.retriage){toast('正在让模型重新判断配图…');const r=await api(`/api/items/${t.dataset.retriage}/retriage-images`,{method:'POST'});toast(r.dropped?`已删除 ${r.dropped} 张无关配图`:'没有发现需要删除的配图');await showItem(t.dataset.retriage,true);loadItems().catch(()=>{})}if(t.dataset.verifySource){const r=await api(`/api/items/${t.dataset.verifySource}/verify-source`,{method:'POST'});toast(r.exists?'原文仍可访问':(r.kept?'原文已删除，但离线页已保存，继续保留':'原文已删除且没有离线页，已移出资料库'));await showItem(t.dataset.verifySource,true);loadItems().catch(()=>{})}if(t.dataset.archiveIndex!==undefined){if(!confirm('将限速归档所有仅索引条目（正文、图片、原始快照），不调用云精读。条目较多时会持续一段时间，可随时查看进度。确定开始吗？'))return;const r=await api('/api/archive-index-only',{method:'POST'});toast(r.queued?`已开始归档，剩余 ${r.remaining} 条；云精读调用 0`:'没有待归档的索引条目');loadProgress().catch(()=>{})}if(t.dataset.cloudModel){$('#llmProvider').value='workbuddy_cloud';$('#llmProvider').dispatchEvent(new Event('change'));$('#llmModel').value=t.dataset.cloudModel;$('#detailDialog').close();toast('已选择模型，保存设置后生效')}if(t.dataset.paperRetry){await api(`/api/papers/${t.dataset.paperRetry}/retry`,{method:'POST'});toast('已重试论文解析与下载')}if(t.dataset.jobRetry){await api(`/api/jobs/${t.dataset.jobRetry}/retry`,{method:'POST'});toast('任务已重新排队');await loadTaskQueue()}if(t.dataset.jobDiagnostic){const d=await api(`/api/jobs/${t.dataset.jobDiagnostic}/diagnostics`);window.currentDiagnostic=d.markdown;$('#detailContent').innerHTML=`<h1>任务失败诊断</h1><p>${d.retryable?'<span class="badge wait">疑似临时错误，可重试</span>':'<span class="badge error">不是临时错误，盲目重试会再次失败</span>'}</p><p class="muted">复制下面内容，粘贴到 WorkBuddy 对话即可定位和修复。</p><div class="actions"><button class="primary" data-copy-diagnostic="1">复制诊断给 WorkBuddy</button></div><div class="detail-text"><pre style="white-space:pre-wrap">${esc(d.markdown)}</pre></div>`;openDetail()}if(t.dataset.copyDiagnostic){try{await navigator.clipboard.writeText(window.currentDiagnostic||'')}catch(err){const ta=document.createElement('textarea');ta.value=window.currentDiagnostic||'';document.body.appendChild(ta);ta.select();document.execCommand('copy');ta.remove()}toast('诊断已复制，可直接粘贴到 WorkBuddy')}if(t.dataset.pdfPreview){await previewPaper(t.dataset.pdfPreview)}if(t.dataset.paperWps){const r=await api(`/api/papers/${t.dataset.paperWps}/open-wps`,{method:'POST'});toast(`${r.application} 已打开：${r.path}`)}if(t.dataset.paperReveal){const r=await api(`/api/papers/${t.dataset.paperReveal}/reveal`,{method:'POST'});toast(`已打开目录：${r.directory}`)}if(t.dataset.ideaEdit){const x=window.ideaRows.find(i=>i.id===t.dataset.ideaEdit);$('#ideaId').value=x.id;$('#ideaTitle').value=x.title;$('#ideaContent').value=x.content;$('#ideaTags').value=(x.tags||[]).join(', ');scrollTo({top:0,behavior:'smooth'})}if(t.dataset.ideaToggle){const x=window.ideaRows.find(i=>i.id===t.dataset.ideaToggle);await api('/api/ideas/'+x.id,{method:'PATCH',body:{title:x.title,content:x.content,tags:x.tags,enabled:t.dataset.enabled!=='1'}});loadIdeas()}if(t.dataset.batch){const x=await api('/api/notifications/'+t.dataset.batch);$('#detailContent').innerHTML=`<h1>汇报内容</h1><div class="detail-text">${esc(x.text)}</div>`;openDetail()}if(t.dataset.batchRetry){const r=await api(`/api/notifications/${t.dataset.batchRetry}/retry`,{method:'POST'});toast(r.queued===false?(r.reason||'该批次无需重试'):'已重新进入发送队列');loadNotifications().catch(()=>{})}}catch(err){toast(err.message)}});
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
  if($('#detailDialog').open)showItem(id,true).catch(()=>{});
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
loadHealth();loadEvents();loadProgress().catch(()=>{});loadCredits().catch(()=>{});setInterval(loadHealth,5000);setInterval(()=>{if(currentView==='dashboard')loadCredits().catch(()=>{})},60000);setInterval(()=>{if(['dashboard','library'].includes(currentView))loadProgress().catch(()=>{})},2500);setInterval(()=>{if(['dashboard','sources','notifications'].includes(currentView))loaders[currentView]().catch(()=>{})},20000);
