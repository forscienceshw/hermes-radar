/* 笔记库、就地提问与对话抽屉。依赖 app.js 暴露的 api / esc / toast / time / showItem。 */
(function () {
  const q = (s) => document.querySelector(s);
  const money = (v) => Number(v || 0).toFixed(Number(v) >= 100 ? 1 : 2);

  /* ---------------- 浮层挂在谁身上 ----------------
     精读页是 showModal() 的 <dialog>：只有它自己的子树能收到事件，外面的元素全部 inert。
     浮层留在 body 里照样画在最上层（top layer），但点哪儿都点不动——鼠标事件直接穿透到
     遮罩上，结果就是「点对话的关闭，先把精读页关了」。所以精读页开着时，要把可点的浮层
     挂进 dialog 子树；精读页一关，它们又会跟着 display:none，需要搬回 body 重新上顶层。 */
  function overlayHome() {
    // app.js 的模态栈：最上面那层 modal 才是唯一能收事件的容器。
    const top = window.topModal ? window.topModal() : null;
    if (top && top.open) return top;
    const detail = q('#detailDialog');
    return detail && detail.open ? detail : document.body;
  }
  function remountOverlay(el) {
    if (!el) return;
    const target = overlayHome();
    if (el.parentElement === target) return;
    const shown = el.matches(':popover-open');
    if (shown) { try { el.hidePopover(); } catch (e) {} }
    target.appendChild(el);
    if (shown) { try { el.showPopover(); } catch (e) {} }
  }
  function returnOverlays() {
    remountOverlay(q('#chatDrawer'));
    remountOverlay(q('#notePop'));
    remountOverlay(q('#selectionBar'));
  }
  let ideaTab = 'ideas';
  let notesAll = [];
  let currentItemId = null;
  let currentItemTitle = '';
  let selectionPeek = false;

  /* ---------------- 当前文章跟踪 ---------------- */
  function syncCurrentItem() {
    const node = document.querySelector('.reading[data-item]');
    if (node) {
      currentItemId = (node && node.dataset.item) || (window.__currentItem && window.__currentItem.id) || null;
      const title = document.querySelector('.detail-title');
      currentItemTitle = title ? title.textContent.trim() : '';
    }
    updateChatContext();
  }
  const detailObserver = new MutationObserver(() => setTimeout(syncCurrentItem, 30));
  const detailContent = document.querySelector('#detailContent');
  if (detailContent) detailObserver.observe(detailContent, { childList: true, subtree: true });

  /* ---------------- 笔记库 ---------------- */
  async function loadNotes() {
    const params = new URLSearchParams({ limit: '300' });
    const since = daysAgoIso(q('#ideaRange') ? q('#ideaRange').value : '');
    if (since) params.set('since', since);
    const collection = q('#ideaCollection') ? q('#ideaCollection').value : '';
    if (collection) params.set('collection', collection);
    try {
      notesAll = (await api('/api/notes?' + params.toString())) || [];
    } catch (e) {
      notesAll = [];
    }
    renderNotes();
    fillNoteFilter();
  }

  function fillNoteFilter() {
    const select = q('#noteItemFilter');
    if (!select) return;
    const seen = new Map();
    notesAll.forEach((n) => {
      if (n.item_id && n.item_title) seen.set(n.item_id, n.item_title);
    });
    const keep = select.value;
    select.innerHTML='<option value="">全部文章</option>' +
      [...seen.entries()].map(([id, title]) => `<option value="${esc(id)}">${esc(title.slice(0, 40))}</option>`).join('');
    select.value = keep;
  }

  function renderNotes() {
    const host = q('#notesList');
    if (!host) return;
    const keyword = (q('#ideaQ').value || '').trim().toLowerCase();
    const itemFilter = q('#noteItemFilter').value;
    const rows = notesAll.filter((n) => {
      if (itemFilter && n.item_id !== itemFilter) return false;
      if (!keyword) return true;
      return `${n.quote} ${n.body} ${n.item_title || ''}`.toLowerCase().includes(keyword);
    });
    host.innerHTML = rows.map((n) => {
      const chips = (n.collections || []).map((id) => {
        const item = (window.__ideaCollections || []).find((c) => c.id === id);
        return item ? `<span class="collection-chip">${esc(item.name)}</span>` : '';
      }).join('');
      return `
      <div class="card note-card" data-note-card="${n.id}">
        <button class="star ${(n.collections || []).length ? 'on' : ''}" type="button" data-collect="${n.id}" data-collect-type="note" data-collect-ids="${(n.collections || []).join(',')}" title="收藏到…">★</button>
        <div class="panel-head">
          <h3>${n.pinned ? '<span class="pin">置顶</span> ' : ''}${esc((n.item_title || '随手笔记').slice(0, 60))}</h3>
          <small>${time(n.created_at)}</small>
        </div>
        ${n.quote ? `<blockquote class="note-quote">${esc(n.quote)}</blockquote>` : ''}
        <textarea class="note-body" rows="${Math.min(6, Math.max(2, Math.ceil((n.body || '').length / 60)))}" data-note-body="${n.id}" placeholder="补一句你的想法…">${esc(n.body)}</textarea>
        <div class="actions">
          <button data-note-save="${n.id}" class="primary">保存</button>
          <button data-note-pin="${n.id}" data-pinned="${n.pinned ? 1 : 0}">${n.pinned ? '取消置顶' : '置顶'}</button>
          <button data-note-copy="${n.id}">复制</button>
          ${n.item_id ? `<button data-note-open="${esc(n.item_id)}">打开原文</button>` : ''}
          <button class="danger" data-note-delete="${n.id}">删除</button>
        </div>
        ${chips ? `<div style="margin-top:6px">${chips}</div>` : ''}
      </div>`}).join('') || '<div class="panel muted">还没有匹配的笔记。精读页每一段右上角都有「存笔记」。</div>';
  }

  function switchIdeaTab(tab) {
    ideaTab = tab;
    document.querySelectorAll('[data-idea-tab]').forEach((b) => b.classList.toggle('primary', b.dataset.ideaTab === tab));
    const ideas = q('#ideasList'), notes = q('#notesList');
    if (ideas) ideas.style.display = tab === 'ideas' ? '' : 'none';
    if (notes) notes.style.display = tab === 'notes' ? '' : 'none';
    q('#noteItemFilter').style.display = tab === 'notes' ? '' : 'none';
    if (tab === 'notes') loadNotes(); else loadIdeas();
  }

  function refreshIdeasView() {
    if (ideaTab === 'notes') loadNotes(); else loadIdeas();
  }

  /* ---------------- 笔记弹层 ---------------- */
  let pendingNote = null;
  function closeNotePop() {
    const pop = q('#notePop');
    pop.hidden = true;
    if (pop.matches(':popover-open')) pop.hidePopover();
    pendingNote = null;
  }
  function openNotePop(section, quote, itemId) {
    pendingNote = { section, quote, itemId };
    q('#notePopQuote').textContent = quote.length > 600 ? quote.slice(0, 600) + '…' : quote;
    q('#notePopBody').value = '';
    q('#notePopFrom').textContent = section || '';
    const pop = q('#notePop');
    pop.hidden = false;
    // popover 才能压在精读页上面：精读页是 modal，普通 div 的 z-index 再高也没用。
    // 但 popover 只有在 dialog 子树里才点得动，见 overlayHome()。
    remountOverlay(pop);
    if (pop.showPopover && !pop.matches(':popover-open')) pop.showPopover();
    q('#notePopBody').focus();
  }
  async function saveNotePop() {
    if (!pendingNote) return;
    try {
      await api('/api/notes', {
        method: 'POST',
        body: { item_id: pendingNote.itemId, section: pendingNote.section,
                quote: pendingNote.quote, body: q('#notePopBody').value },
      });
      closeNotePop();
      toast('已存入笔记库');
      if (ideaTab === 'notes') loadNotes();
    } catch (e) {
      toast(e.message);
    }
  }

  /* ---------------- 块工具条 ---------------- */
  function blockText(node) {
    const clone = node.cloneNode(true);
    clone.querySelectorAll('.block-tools').forEach((x) => x.remove());
    return (clone.innerText || clone.textContent || '').trim();
  }

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      toast('已复制');
    } catch (e) {
      toast('复制失败，请手动选中');
    }
  }

  function startEdit(path) {
    const host = document.querySelector(`[data-field="${path}"]`);
    if (!host || host.querySelector('textarea')) return;
    const span = host.querySelector('.field-text') || host;
    const value = span.textContent || '';
    const area = document.createElement('textarea');
    area.className = 'field-edit';
    area.rows = Math.min(10, Math.max(3, Math.ceil(value.length / 42)));
    area.value = value;
    span.replaceWith(area);
    const actions = document.createElement('div');
    actions.className = 'edit-actions';
    actions.innerHTML = `<button type="button" data-save-edit="${path}" class="primary">保存</button><button type="button" data-cancel-edit>取消</button><span class="muted">只改本机显示，不重新调用云模型</span>`;
    area.after(actions);
    area.focus();
  }

  async function saveEdit(path, button) {
    const host = document.querySelector(`[data-field="${path}"]`);
    const area = host && host.querySelector('textarea');
    if (!area) return;
    try {
      await api(`/api/items/${currentItemId}/analysis`, { method: 'PATCH', body: { path, value: area.value } });
      toast('已保存修改');
      await showItem(currentItemId, true);
    } catch (e) {
      toast(e.message);
    }
  }

  /* ---------------- 对话抽屉 ---------------- */
  const chat = {
    open: false,
    chatId: null,
    itemId: null,
    selection: '',
    selectionBlock: '',
    selectionLocked: false,
    account: localStorage.getItem('hermes.chat.account') || '',
    webSearch: localStorage.getItem('hermes.chat.websearch') === '1',
    messages: [],
    chats: [],
    accounts: [],
    busy: false,
  };

  function paintWebToggle() {
    const button = q('#chatWebSearch');
    if (!button) return;
    button.classList.toggle('on', chat.webSearch);
    button.setAttribute('aria-pressed', chat.webSearch ? 'true' : 'false');
    button.title = chat.webSearch
      ? '已开启：每次提问先检索 Europe PMC / arXiv / 网页，再交给模型'
      : '开启后先联网检索，再回答';
  }

  function updateChatContext() {
    const box = q('#chatContext');
    if (!box) return;
    const parts = [];
    if (chat.itemId) {
      const x = window.__currentItem || {};
      parts.push(`<span class="ctx-badge">文章：${esc((x.title || currentItemTitle || '当前文章').slice(0, 34))}</span>`);
      if (chat.selection) {
        const where = chat.selectionBlock ? ` · ${chat.selectionBlock}` : '';
        parts.push(`<span class="ctx-badge sel" title="只会把这一段一起发给模型">选中 ${chat.selection.length} 字${esc(where)}</span>`);
        parts.push('<button type="button" id="peekSelection">查看原文</button>');
        parts.push('<button type="button" id="clearSelection">移除</button>');
      }
    } else {
      parts.push('<span class="muted">未绑定文章：先打开一篇资料，问题会自动带上它的全文与精读。</span>');
    }
    box.innerHTML = parts.join(' ') + (chat.selection && selectionPeek
      ? `<pre class="ctx-selection">${esc(chat.selection)}</pre>` : '');
    const clear = q('#clearSelection');
    if (clear) clear.onclick = () => {
      chat.selection = '';
      chat.selectionBlock = '';
      chat.selectionLocked = false;
      window.getSelection().removeAllRanges();
      hideSelectionBar();
      updateChatContext();
      updateSelectionUI();
    };
    const peek = q('#peekSelection');
    if (peek) peek.onclick = () => { selectionPeek = !selectionPeek; updateChatContext(); };
  }

  async function loadAccounts() {
    try {
      const data = await api('/api/llm/accounts');
      chat.accounts = data.accounts || [];
    } catch (e) {
      chat.accounts = [];
    }
    const select = q('#chatModel');
    if (!select) return;
    if (!chat.account || !chat.accounts.some((a) => a.id === chat.account)) {
      chat.account = chat.accounts[0] ? chat.accounts[0].id : '';
    }
    select.innerHTML = chat.accounts.map((a) =>
      `<option value="${esc(a.id)}">${esc(a.label)}${a.supports_images ? ' · 图片' : ''}</option>`).join('') ||
      '<option value="">未配置模型</option>';
    select.value = chat.account;
  }

  function fmtTokens(n) {
    const value = Number(n || 0);
    return value >= 1000 ? `${(value / 1000).toFixed(value >= 10000 ? 0 : 1)}k` : String(value);
  }

  function hostOf(url) {
    try { return new URL(url).hostname.replace(/^www\./, ''); } catch (e) { return ''; }
  }

  // 把回答里的 [2] 变成能点开的来源；没对上号的数字保持原样，避免误链。
  function citeHtml(text, sources) {
    let html = esc(text).replace(/\n/g, '<br>');
    if (!Array.isArray(sources) || !sources.length) return html;
    const map = new Map(sources.map((s) => [String(s.n), s]));
    return html.replace(/\[(\d{1,2})\]/g, (raw, n) => {
      const src = map.get(n);
      if (!src || !src.url) return raw;
      return `<a class="cite" href="${esc(src.url)}" target="_blank" rel="noopener" title="${esc(src.title || '')}">[${n}]</a>`;
    });
  }

  function sourcesHtml(sources) {
    if (!Array.isArray(sources) || !sources.length) return '';
    return `<div class="chat-sources">${sources.map((s) =>
      `<a href="${esc(s.url || '')}" target="_blank" rel="noopener"><b>[${s.n}]</b><span>${esc(s.title || '')}</span><em>${esc(hostOf(s.url || ''))}</em></a>`).join('')}</div>`;
  }

  function contextBadge(meta) {
    const ctx = meta.context;
    if (!ctx || !ctx.tokens) return '';
    const parts = [`上下文 ${fmtTokens(ctx.tokens)}/${fmtTokens(ctx.window || ctx.budget)}`];
    if (ctx.folded) parts.push(`折叠 ${ctx.folded} 条旧对话`);
    if (ctx.dropped) parts.push(`丢弃 ${ctx.dropped} 条`);
    if (ctx.retried) parts.push('已自动压缩重试');
    return `<span class="chat-usage" title="估算的输入 token / 该模型的上下文窗口">${esc(parts.join(' · '))}</span>`;
  }

  function renderChat() {
    const host = q('#chatMessages');
    if (!host) return;
    if (!chat.messages.length) {
      host.innerHTML = `<div class="chat-empty"><p>问细节不用再复制背景。</p><p class="muted">打开一篇文章后，这里会自动带上标题、精读结构、关键数字和归档全文节选；选中一段再点「问 AI」，连那段话一起带上。开「联网」还会先检索 Europe PMC、arXiv 和网页。</p></div>`;
      return;
    }
    host.innerHTML = chat.messages.map((m) => {
      const meta = m.meta || {};
      if (m.role === 'assistant') {
        const badge = meta.error ? '<span class="badge error">失败</span>' :
          `<span class="muted">${esc(meta.label || meta.model || '')}${meta.points != null && meta.points !== '' ? ` · ${money(meta.points)} 点` : ''}${meta.elapsed_ms ? ` · ${Math.round(meta.elapsed_ms / 1000)}s` : ''}</span>`;
        return `<div class="chat-msg assistant"><div class="chat-bubble">${citeHtml(m.content, meta.sources)}</div>${sourcesHtml(meta.sources)}<div class="chat-meta">${badge}${contextBadge(meta)}<button type="button" data-chat-copy="${esc(m.content).slice(0, 0)}">复制</button><button type="button" data-to-note="${esc(m.content).slice(0, 0)}">存笔记</button></div></div>`;
      }
      const searched = meta.web_search ? `<div class="chat-note">联网检索 ${(meta.sources || []).length} 条来源</div>` : '';
      return `<div class="chat-msg user"><div class="chat-bubble">${esc(m.content).replace(/\n/g, '<br>')}</div>${searched}</div>`;
    }).join('');
    host.querySelectorAll('[data-chat-copy]').forEach((b, i) => {
      b.onclick = () => copyText(chat.messages.filter((m) => m.role === 'assistant')[i].content);
    });
    host.querySelectorAll('[data-to-note]').forEach((b, i) => {
      b.onclick = () => openNotePop('对话', chat.messages.filter((m) => m.role === 'assistant')[i].content, chat.itemId);
    });
    host.scrollTop = host.scrollHeight;
  }

  async function openChat(options = {}) {
    chat.open = true;
    if (options.itemId !== undefined) chat.itemId = options.itemId;
    if (options.selection) {
      // 锁定这一段：之后输入框获得焦点导致的 selectionchange 不会把它清掉。
      chat.selection = options.selection;
      chat.selectionLocked = true;
    } else if (options.selection === '') {
      chat.selection = '';
      chat.selectionLocked = false;
    }
    const drawer = q('#chatDrawer');
    // 必须是 popover：精读页用 showModal() 进了 top layer，抽屉留在普通层里会被压在
    // 对话框背后，「看着能点、其实点不到」。popover 只占 top layer、不锁死底下那层，
    // 所以还能一边滚文章一边问。
    // 还要挂进精读页的子树：modal 会让子树外的元素全部 inert，光在 top layer 上层没用，
    // 点击会穿透到遮罩，把精读页关掉。
    remountOverlay(drawer);
    if (drawer.showPopover && !drawer.matches(':popover-open')) drawer.showPopover();
    requestAnimationFrame(() => drawer.classList.add('open'));
    q('#chatBackdrop').classList.add('open');
    hideSelectionBar();
    drawer.setAttribute('aria-hidden', 'false');
    if (!chat.accounts.length) await loadAccounts();
    updateChatContext();
    renderChat();
    if (options.focus !== false) setTimeout(() => q('#chatInput').focus(), 120);
  }

  function closeChat() {
    chat.open = false;
    const drawer = q('#chatDrawer');
    drawer.classList.remove('open');
    q('#chatBackdrop').classList.remove('open');
    drawer.setAttribute('aria-hidden', 'true');
    // 等滑出动画走完再退出 top layer；这期间又点开就别关。
    setTimeout(() => {
      if (chat.open) return;
      if (drawer.matches(':popover-open')) drawer.hidePopover();
      // 关掉后从精读页子树里搬回 body：否则下次精读页一关，它会跟着一起消失。
      if (drawer.parentElement !== document.body) document.body.appendChild(drawer);
    }, 220);
    q('#chatSessionsPanel').hidden = true;
    // 关闭后释放锁定，下一次重新选中段落时按钮才会更新。
    chat.selection = '';
    chat.selectionBlock = '';
    chat.selectionLocked = false;
    updateChatContext();
    updateSelectionUI();
  }

  async function ensureChat() {
    if (chat.chatId) return chat.chatId;
    const created = await api('/api/chats', { method: 'POST', body: { model: chat.account, item_id: chat.itemId } });
    chat.chatId = created.id;
    return chat.chatId;
  }

  async function sendChat() {
    if (chat.busy) return;
    const input = q('#chatInput');
    const text = input.value.trim();
    if (!text) return;
    if (!chat.accounts.length) { toast('先在设置里配置一个模型'); return; }
    chat.busy = true;
    q('#chatSend').disabled = true;
    q('#chatStatus').textContent = chat.webSearch ? '联网检索中…' : '正在请求…';
    const id = await ensureChat();
    const selection = chat.selection;
    input.value = '';
    chat.messages.push({ role: 'user', content: text, meta: { web_search: chat.webSearch } });
    chat.selection = '';
    chat.selectionLocked = false;
    try { window.getSelection().removeAllRanges(); } catch (e) {}
    hideSelectionBar();
    updateChatContext();
    renderChat();
    try {
      const result = await api(`/api/chats/${id}/messages`, {
        method: 'POST',
        body: {
          content: text, item_id: chat.itemId, selection, account: chat.account,
          web_search: chat.webSearch,
        },
      });
      chat.messages = result.messages || [];
      renderChat();
      if (!result.ok) toast(result.error || '调用失败');
      else if (result.search && result.search.errors && result.search.errors.length) {
        toast(`联网检索部分失败：${result.search.errors[0]}`);
      }
    } catch (e) {
      toast(e.message);
    } finally {
      chat.busy = false;
      q('#chatSend').disabled = false;
      q('#chatStatus').textContent = 'Enter 发送 · Shift+Enter 换行';
    }
  }

  async function loadSessions() {
    const panel = q('#chatSessionsPanel');
    panel.hidden = false;
    try {
      chat.chats = await api('/api/chats');
    } catch (e) {
      panel.innerHTML = `<div class="muted">${esc(e.message)}</div>`;
      return;
    }
    panel.innerHTML = `<div class="panel-head"><b>历史对话</b><button type="button" data-session-close>收起</button></div>` +
      (chat.chats.map((c) => `<div class="session-row ${c.id === chat.chatId ? 'active' : ''}">
        <div><b>${c.pinned ? '置顶 · ' : ''}${esc(c.title)}</b><small>${c.message_count} 条 · ${time(c.updated_at)}</small></div>
        <div class="actions"><button type="button" data-session-open="${c.id}">打开</button>
        <button type="button" data-session-pin="${c.id}" data-pinned="${c.pinned ? 1 : 0}">${c.pinned ? '取消置顶' : '置顶'}</button>
        <a class="button" href="/api/chats/${c.id}/export">导出</a>
        <button type="button" class="danger" data-session-delete="${c.id}">删除</button></div></div>`).join('') ||
        '<div class="muted">还没有对话记录</div>');
  }

  async function openSession(id) {
    const data = await api(`/api/chats/${id}`);
    chat.chatId = id;
    chat.messages = data.messages || [];
    chat.itemId = data.chat.item_id || null;
    chat.account = data.chat.model || chat.account;
    q('#chatModel').value = chat.account;
    q('#chatSessionsPanel').hidden = true;
    updateChatContext();
    renderChat();
  }

  /* ---------------- 设置：模型账户 ---------------- */
  function accountRow(account = {}) {
    const id = account.id || `acc-${Math.random().toString(36).slice(2, 8)}`;
    return `<div class="account-row" data-account-row>
      <label>名称<input data-account="label" value="${esc(account.label || '')}" placeholder="DeepSeek"></label>
      <label>类型<select data-account="provider">
        <option value="openai_compatible" ${account.provider !== 'workbuddy_cloud' ? 'selected' : ''}>OpenAI 兼容</option>
        <option value="workbuddy_cloud" ${account.provider === 'workbuddy_cloud' ? 'selected' : ''}>WorkBuddy 点数</option>
      </select></label>
      <label>模型<input data-account="model" value="${esc(account.model || '')}" placeholder="deepseek-chat"></label>
      <label>Base URL<input data-account="base_url" value="${esc(account.base_url || '')}" placeholder="https://api.deepseek.com/v1"></label>
      <label>API Key（留空保持不变）<input data-account="key" type="password" autocomplete="off" placeholder="${account.key_set ? '已保存' : '未配置'}"></label>
      <input type="hidden" data-account="id" value="${esc(id)}">
      <button type="button" class="danger" data-account-remove>移除</button>
    </div>`;
  }

  window.renderAccounts = function (accounts) {
    const host = q('#accountList');
    if (!host) return;
    const saved = new Set((window.__secretProviders || []));
    host.innerHTML = (accounts || []).map((a) => accountRow({ ...a, key_set: saved.has(a.id) })).join('') ||
      '<div class="muted">当前只有默认账户，也就是上面「大模型精读」用的那个模型。点「添加账户」可接入 DeepSeek、Kimi、通义等 OpenAI 兼容端点。</div>';
  };

  window.collectAccounts = function () {
    return [...document.querySelectorAll('[data-account-row]')].map((row) => {
      const get = (key) => row.querySelector(`[data-account="${key}"]`).value.trim();
      return { id: get('id'), label: get('label') || get('model'), provider: get('provider'),
               model: get('model'), base_url: get('base_url') };
    }).filter((a) => a.model);
  };

  async function saveAccountKeys() {
    const rows = [...document.querySelectorAll('[data-account-row]')];
    for (const row of rows) {
      const key = row.querySelector('[data-account="key"]').value.trim();
      const id = row.querySelector('[data-account="id"]').value.trim();
      if (key && id) {
        await api('/api/settings/secrets', { method: 'POST', body: { name: `provider:${id}`, value: key } });
        row.querySelector('[data-account="key"]').value = '';
      }
    }
  }

  /* ---------------- 事件绑定 ---------------- */
  document.addEventListener('click', async (e) => {
    const target = e.target.closest('button,[data-idea-tab]');
    if (!target) return;
    try {
      if (target.dataset.ideaTab) return switchIdeaTab(target.dataset.ideaTab);
      if (target.dataset.copy !== undefined) {
        const block = target.closest('[data-block]');
        if (block) return copyText(blockText(block));
      }
      if (target.dataset.note) {
        const block = target.closest('[data-block]');
        if (block) return openNotePop(target.dataset.note, blockText(block), currentItemId);
      }
      if (target.dataset.edit) return startEdit(target.dataset.edit);
      if (target.dataset.saveEdit) return saveEdit(target.dataset.saveEdit, target);
      if (target.dataset.cancelEdit !== undefined) return showItem(currentItemId, true);
      if (target.dataset.ideaDelete) {
        if (!confirm('删除这条 idea？')) return;
        await api(`/api/ideas/${target.dataset.ideaDelete}`, { method: 'DELETE' });
        toast('已删除');
        return loadIdeas();
      }
      if (target.dataset.noteSave) {
        const id = target.dataset.noteSave;
        const body = document.querySelector(`[data-note-body="${id}"]`).value;
        await api(`/api/notes/${id}`, { method: 'PATCH', body: { body } });
        toast('笔记已保存');
        return loadNotes();
      }
      if (target.dataset.notePin) {
        await api(`/api/notes/${target.dataset.notePin}`, { method: 'PATCH',
          body: { pinned: target.dataset.pinned !== '1' } });
        return loadNotes();
      }
      if (target.dataset.noteDelete) {
        if (!confirm('删除这条笔记？')) return;
        await api(`/api/notes/${target.dataset.noteDelete}`, { method: 'DELETE' });
        toast('已删除');
        return loadNotes();
      }
      if (target.dataset.noteCopy) {
        const note = notesAll.find((n) => n.id === target.dataset.noteCopy);
        if (note) return copyText(`${note.quote}\n\n${note.body}`);
      }
      if (target.dataset.noteOpen) return showItem(target.dataset.noteOpen);
      if (target.dataset.sessionOpen) return openSession(target.dataset.sessionOpen);
      if (target.dataset.sessionPin) {
        await api(`/api/chats/${target.dataset.sessionPin}`, { method: 'PATCH',
          body: { pinned: target.dataset.pinned !== '1' } });
        return loadSessions();
      }
      if (target.dataset.sessionDelete) {
        if (!confirm('删除这段对话？')) return;
        await api(`/api/chats/${target.dataset.sessionDelete}`, { method: 'DELETE' });
        if (chat.chatId === target.dataset.sessionDelete) { chat.chatId = null; chat.messages = []; renderChat(); }
        return loadSessions();
      }
      if (target.dataset.sessionClose !== undefined) { q('#chatSessionsPanel').hidden = true; return; }
      if (target.dataset.accountRemove !== undefined) { target.closest('[data-account-row]').remove(); return; }
    } catch (err) {
      toast(err.message);
    }
  });

  q('#notePopSave').onclick = saveNotePop;
  q('#notePopClose').onclick = closeNotePop;
  const ideaSearch = q('#ideaQ');
  if (ideaSearch) ideaSearch.oninput = () => refreshIdeasView();
  q('#noteItemFilter').onchange = renderNotes;
  ['#ideaRange', '#ideaCollection'].forEach((sel) => {
    const el = q(sel);
    if (el) el.onchange = () => refreshIdeasView();
  });

  q('#chatClose').onclick = closeChat;
  q('#chatBackdrop').onclick = closeChat;
  q('#chatSend').onclick = sendChat;
  q('#chatNew').onclick = () => {
    chat.chatId = null; chat.messages = []; chat.selection = ''; chat.selectionBlock = ''; chat.selectionLocked = false;
    q('#chatSessionsPanel').hidden = true;
    renderChat(); updateChatContext(); q('#chatInput').focus();
  };
  q('#chatSessions').onclick = () => { loadSessions(); };
  const webToggle = q('#chatWebSearch');
  if (webToggle) {
    webToggle.onclick = () => {
      chat.webSearch = !chat.webSearch;
      localStorage.setItem('hermes.chat.websearch', chat.webSearch ? '1' : '0');
      paintWebToggle();
      toast(chat.webSearch ? '已开启联网检索' : '已关闭联网检索');
    };
    paintWebToggle();
  }
  q('#chatModel').onchange = () => {
    chat.account = q('#chatModel').value;
    localStorage.setItem('hermes.chat.account', chat.account);
    if (chat.chatId) api(`/api/chats/${chat.chatId}`, { method: 'PATCH', body: { model: chat.account } }).catch(() => {});
  };
  q('#chatInput').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendChat(); }
  });
  q('#addAccount').onclick = () => {
    const host = q('#accountList');
    host.insertAdjacentHTML('beforeend', accountRow({}));
  };
  const settingsForm = q('#settingsForm');
  if (settingsForm) settingsForm.addEventListener('submit', () => { saveAccountKeys().catch((e) => toast(e.message)); });

  /* ---------------- 选中文字：精确控制问哪一段 ---------------- */
  let selTimer = null;

  function hideSelectionBar() {
    const bar = q('#selectionBar');
    if (!bar) return;
    bar.hidden = true;
    if (bar.matches(':popover-open')) bar.hidePopover();
  }

  function currentSelection() {
    const selection = window.getSelection();
    const text = selection ? selection.toString().trim() : '';
    if (!selection || selection.rangeCount === 0 || text.length <= 2) return null;
    const anchor = selection.anchorNode;
    const element = anchor && (anchor.nodeType === 1 ? anchor : anchor.parentElement);
    if (!element || !element.closest('#detailDialog')) return null;
    const block = element.closest('[data-block]');
    return {
      text,
      block: block ? block.dataset.block : '',
      range: selection.getRangeAt(0),
    };
  }

  function updateSelectionUI() {
    if (chat.selectionLocked) return;
    const picked = currentSelection();
    const fab = q('#chatFab');
    if (!picked) {
      chat.selection = '';
      chat.selectionBlock = '';
      selectionPeek = false;
      fab.classList.remove('ask-selection');
      fab.textContent = '问 AI';
      hideSelectionBar();
      updateChatContext();
      return;
    }
    chat.selection = picked.text.slice(0, 4000);
    chat.selectionBlock = picked.block;
    fab.classList.add('ask-selection');
    fab.textContent = `问这段（${chat.selection.length} 字）`;
    const bar = q('#selectionBar');
    bar.hidden = false;
    // 划词工具条要能点，同样得在精读页子树里。
    remountOverlay(bar);
    if (bar.showPopover && !bar.matches(':popover-open')) bar.showPopover();
    q('#selectionInfo').textContent = `${picked.block ? picked.block + ' · ' : ''}${picked.text.length} 字：${picked.text.slice(0, 26)}${picked.text.length > 26 ? '…' : ''}`;
    const rect = picked.range.getBoundingClientRect();
    const width = bar.offsetWidth || 340;
    const height = bar.offsetHeight || 38;
    const left = Math.min(window.innerWidth - width - 10, Math.max(10, rect.left));
    let top = rect.bottom + 8;
    if (top + height > window.innerHeight - 10) top = Math.max(10, rect.top - height - 8);
    bar.style.left = left + 'px';
    bar.style.top = top + 'px';
    updateChatContext();
  }

  document.addEventListener('selectionchange', () => {
    clearTimeout(selTimer);
    selTimer = setTimeout(updateSelectionUI, 180);
  });
  // 点击按钮时不要清掉选区，否则“问这段”会丢掉刚刚选中的文字。
  ['#chatFab', '#selectionBar'].forEach((sel) => {
    const node = q(sel);
    if (node) node.addEventListener('mousedown', (e) => e.preventDefault());
  });
  document.querySelectorAll('[data-sel-act]').forEach((button) => {
    button.addEventListener('click', () => {
      const action = button.dataset.selAct;
      if (action === 'clear') {
        window.getSelection().removeAllRanges();
        hideSelectionBar();
        return;
      }
      if (!chat.selection) return toast('先选中一段文字');
      if (action === 'copy') return copyText(chat.selection);
      if (action === 'note') return openNotePop(chat.selectionBlock || '选中段落', chat.selection, currentItemId);
      if (action === 'ask') return openChat({ itemId: currentItemId, selection: chat.selection });
    });
  });
  q('#chatFab').addEventListener('click', () => {
    openChat({ itemId: currentItemId, selection: chat.selection || '' });
  });
  window.addEventListener('scroll', () => hideSelectionBar(), true);

  window.loadNotes = loadNotes;
  window.openChat = openChat;
  window.closeChat = closeChat;
  window.chatIsOpen = () => chat.open;
  window.__chatState = chat;

  // 模态一开一关都要重新安置浮层：开着时得挂进它的子树（否则整块 inert，点不动），
  // 关掉后要搬回 body（否则跟着 dialog 的 display:none 一起消失）。
  document.addEventListener('modal:open', () => returnOverlays());
  document.addEventListener('modal:close', () => {
    returnOverlays();
    if (!q('#detailDialog').open) hideSelectionBar();
  });

  // Seed the model selector as soon as the page is interactive.
  loadAccounts().catch(() => {});
  setInterval(syncCurrentItem, 4000);
})();
