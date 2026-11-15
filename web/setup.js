/* 配置向导：一个从上到下走完就能用的页面。
 *
 * 它只做三件事：把 /api/setup/status 的状态画出来、把每一步的按钮接到后端、
 * 保存后刷新一次「参数设置」里的表单（避免两处显示不一致）。
 * 这里刻意不复制 app.js 里的任何状态，所有事实都以后端返回为准。
 */
let setupData = null;
let setupProvider = 'workbuddy_cloud';
let setupBound = false;

const CHANNEL_NAMES = {
  inbox: '站内收件箱',
  wecom_webhook: '企业微信群机器人',
  serverchan: 'Server酱',
  generic_webhook: '通用 Webhook',
};
const PROVIDER_NAMES = {
  workbuddy_cloud: 'WorkBuddy 点数',
  openai_compatible: 'OpenAI 兼容 API',
  ollama: 'Ollama 本地模型',
};

function stateBadge(state, okText) {
  if (state === 'ok') return `<span class="wz-state ok">${okText || '已就绪'}</span>`;
  if (state === 'off') return `<span class="wz-state off">已关闭</span>`;
  if (state === 'error') return `<span class="wz-state error">有问题</span>`;
  return `<span class="wz-state warn">待配置</span>`;
}

function setStepState(name, state, text) {
  const node = document.querySelector(`.wz-state[data-state="${name}"]`);
  if (!node) return;
  node.className = 'wz-state ' + (state || 'warn');
  node.textContent = text;
}

function showResult(id, ok, message, detail) {
  const box = $(id);
  if (!box) return;
  box.hidden = false;
  box.className = 'inline-result ' + (ok ? 'ok' : 'bad');
  box.innerHTML = `<b>${esc(message)}</b>` + (detail ? `<div class="muted">${esc(detail)}</div>` : '');
}

function busy(button, on, label) {
  if (!button) return;
  if (on) {
    button.dataset.label = button.textContent;
    button.textContent = label || '处理中…';
    button.disabled = true;
  } else {
    button.textContent = button.dataset.label || button.textContent;
    button.disabled = false;
  }
}

async function loadSetupPanel() {
  try {
    setupData = await api('/api/setup/status');
  } catch (err) {
    const box = $('#setupChecks');
    if (box) box.innerHTML = `<div class="check-item"><span class="check-dot error"></span><div><b>读取状态失败</b><small>${esc(err.message)}</small></div></div>`;
    return;
  }
  renderSetup();
  bindSetup();
}

function renderSetup() {
  const data = setupData;
  if (!data) return;

  $('#setupDone').textContent = data.progress.done;
  $('#setupTotal').textContent = data.progress.total;

  // 环境缺件单独提出来，放在最上面，避免用户配到一半才发现跑不起来。
  const warn = $('#setupWarnings');
  if (data.env_warnings && data.env_warnings.length) {
    warn.hidden = false;
    warn.innerHTML = `<b>环境提示</b><ul>${data.env_warnings.map(x => `<li>${esc(x)}</li>`).join('')}</ul>`;
  } else {
    warn.hidden = true;
  }

  renderWechat(data.wechat);
  renderLlm(data.llm);
  renderNotify(data.notify);
  renderSetupAccounts(data.accounts);
  renderAutostart(data.autostart);

  $('#setupChecks').innerHTML = data.checks.map(c => `
    <div class="check-item">
      <span class="check-dot ${c.state}"></span>
      <div><b>${esc(c.label)}</b><small>${esc(c.detail)}</small></div>
    </div>`).join('');

  $('#setupEnvInfo').textContent =
    `版本 ${data.version} · 程序目录 ${data.install_dir} · 数据目录 ${data.data_dir} · Python ${data.python}`;
}

function renderWechat(wechat) {
  const map = {
    ready: ['ok', '已就绪'],
    offline: ['ok', '可读离线目录'],
    'need-login': ['warn', '待授权'],
    'version-unknown': ['warn', '版本待验证'],
    'unsupported-4x': ['error', '4.x 缺数据目录'],
    '4x-ready': ['warn', '4.x 可尝试'],
    missing: ['warn', '没找到微信'],
  };
  const [cls, text] = map[wechat.state] || ['warn', '待配置'];
  setStepState('wechat', cls, text);

  const bits = [];
  if (wechat.version) bits.push(`微信 ${wechat.version}`);
  bits.push(wechat.running ? '正在运行' : '未运行');
  bits.push(wechat.key_saved ? '密钥已保存' : '还没取过密钥');
  if (wechat.saved_account && wechat.saved_account.wxid) bits.push(`账号 ${wechat.saved_account.wxid}`);
  const probe4 = $('#wechatProbe4');
  if (probe4) probe4.hidden = !(wechat.family === '4.x' || wechat.four);
  $('#wechatStatus').innerHTML = bits.map(x => `<span>${esc(x)}</span>`).join('<i>·</i>') +
    (wechat.hint ? `<p class="muted">${esc(wechat.hint)}</p>` : '') +
    (wechat.saved_account && wechat.saved_account.db_root
      ? `<p class="muted path">${esc(wechat.saved_account.db_root)}</p>` : '');
}

function renderLlm(llm) {
  setupProvider = llm.provider || 'workbuddy_cloud';
  setStepState('llm', llm.configured ? 'ok' : 'warn', llm.configured ? '已就绪' : '待配置');
  document.querySelectorAll('#llmChoices .choice').forEach(btn => {
    btn.classList.toggle('on', btn.dataset.provider === setupProvider);
  });
  document.querySelectorAll('#llmFields .field-group').forEach(group => {
    group.hidden = group.dataset.for !== setupProvider;
  });
  if ($('#wizBase') && !$('#wizBase').value) $('#wizBase').value = llm.base_url || '';
  if ($('#wizModel') && !$('#wizModel').value) $('#wizModel').value = llm.model || '';
  if ($('#wizOllamaBase') && !$('#wizOllamaBase').value) $('#wizOllamaBase').value = 'http://127.0.0.1:11434/v1';
  if ($('#wizOllamaModel') && !$('#wizOllamaModel').value) $('#wizOllamaModel').value = 'qwen2.5:14b';
  $('#wizKeyHint').textContent = llm.key_saved ? '已保存过 Key，留空不会覆盖' : '还没有保存 Key';
}

function renderNotify(notify) {
  setStepState('notify', notify.configured ? 'ok' : 'warn',
    notify.configured ? (notify.channel === 'inbox' ? '站内收件箱' : '已配置') : '待配置');
  if ($('#wizChannel') && !$('#wizChannel').dataset.touched) $('#wizChannel').value = notify.channel;
  $('#wizWebhookHint').textContent = notify.webhook_saved ? '已保存发送地址，留空不会覆盖' : '还没保存发送地址';
  $('#wizWebhook').parentElement.hidden = !$('#wizChannel').value || $('#wizChannel').value === 'inbox';
}

function renderSetupAccounts(accounts) {
  const enabled = accounts.enabled || 0;
  setStepState('accounts', enabled ? 'ok' : 'warn', enabled ? `追更 ${enabled} 个` : '待挑选');
  $('#accountsStatus').innerHTML = enabled
    ? `<span>${enabled} 个号在追更</span><i>·</i><span>名单共 ${accounts.total} 个</span>`
    : `<span>还没有启用任何公众号</span>`;
}

function renderAutostart(auto) {
  setStepState('autostart', auto.enabled ? 'ok' : 'off', auto.enabled ? '已开启' : '已关闭');
  $('#wizAutostart').checked = !!auto.enabled;
  $('#wizAutostartText').textContent = auto.enabled ? '已开启' : '关闭';
  const mode = auto.enabled
    ? (auto.mode === 'registry' ? '注册表启动项' : '启动文件夹快捷方式')
    : '当前不开机启动';
  $('#autostartStatus').innerHTML = `<span>${esc(mode)}</span>` +
    (auto.legacy_shortcuts && auto.legacy_shortcuts.length
      ? `<p class="muted">还留着旧版快捷方式：${auto.legacy_shortcuts.map(esc).join('、')}，打开开关会自动清掉。</p>` : '') +
    `<p class="muted path">${esc(auto.command || '')}</p>`;
}

function currentLlmPayload() {
  const provider = setupProvider;
  if (provider === 'openai_compatible') {
    return { provider, base_url: $('#wizBase').value.trim(), model: $('#wizModel').value.trim(), api_key: $('#wizKey').value.trim() };
  }
  if (provider === 'ollama') {
    return { provider, base_url: $('#wizOllamaBase').value.trim(), model: $('#wizOllamaModel').value.trim(), api_key: '' };
  }
  return { provider, base_url: '', model: '', api_key: '' };
}

function bindSetup() {
  if (setupBound) return;
  setupBound = true;

  document.querySelectorAll('.subnav-btn').forEach(btn => {
    btn.onclick = () => {
      document.querySelectorAll('.subnav-btn').forEach(b => b.classList.toggle('active', b === btn));
      document.querySelectorAll('.subview').forEach(view => {
        view.hidden = view.id !== 'subtab-' + btn.dataset.subtab;
      });
    };
  });

  document.querySelectorAll('#llmChoices .choice').forEach(btn => {
    btn.onclick = () => {
      setupProvider = btn.dataset.provider;
      document.querySelectorAll('#llmChoices .choice').forEach(b => b.classList.toggle('on', b === btn));
      document.querySelectorAll('#llmFields .field-group').forEach(group => {
        group.hidden = group.dataset.for !== setupProvider;
      });
    };
  });

  $('#wechatProbe').onclick = async () => {
    const button = $('#wechatProbe');
    busy(button, true, '正在读取密钥…');
    $('#wechatResult').hidden = true;
    try {
      const result = await api('/api/setup/test/wechat', { method: 'POST' });
      showResult('#wechatResult', result.ok, result.message,
        result.ok ? `${result.wxid} · ${result.db_root}（密钥 …${result.key_tail}，${result.seconds}s）` : '');
      await loadSetupPanel();
    } catch (err) {
      showResult('#wechatResult', false, err.message);
    } finally {
      busy(button, false);
    }
  };

  $('#wechatProbe4').onclick = async () => {
    const button = $('#wechatProbe4');
    busy(button, true, '扫描中，最多 1 分钟…');
    $('#wechatResult').hidden = true;
    try {
      const result = await api('/api/setup/wechat4/probe', { method: 'POST' });
      showResult('#wechatResult', result.ok,
        result.ok ? `4.x 解锁成功：${result.opened.length} 个库可读` : (result.reason || '没解开'),
        result.ok ? `扫描 ${result.keys_found} 个候选密钥（${result.scanned_mb} MB 内存），${result.seconds}s；失败的库 ${result.failed.length} 个`
                  : (result.scanned_mb ? `已扫描 ${result.scanned_mb} MB 内存` : ''));
      await loadSetupPanel();
    } catch (err) {
      showResult('#wechatResult', false, err.message);
    } finally {
      busy(button, false);
    }
  };

  $('#wechatEnable').onclick = async () => {
    const button = $('#wechatEnable');
    busy(button, true, '正在开启…');
    try {
      const result = await api('/api/setup/wechat/enable', { method: 'POST' });
      showResult('#wechatResult', true, result.created ? '已创建「本机微信公众号」并开始追更' : '已重新打开「本机微信公众号」');
      await loadSetupPanel();
    } catch (err) {
      showResult('#wechatResult', false, err.message);
    } finally {
      busy(button, false);
    }
  };

  $('#wechatRefresh').onclick = () => loadSetupPanel();
  $('#setupRefresh').onclick = () => {
    $('#setupChecks').innerHTML = '<div class="check-item"><span class="check-dot"></span><div><b>正在体检…</b></div></div>';
    loadSetupPanel();
  };

  $('#llmTest').onclick = async () => {
    const button = $('#llmTest');
    busy(button, true, '正在连接…');
    try {
      const result = await api('/api/setup/test/llm', { method: 'POST', body: currentLlmPayload() });
      showResult('#llmResult', result.ok, result.message,
        (result.models && result.models.length ? `看到这些：${result.models.join('、')}　` : '') + `${result.seconds}s`);
    } catch (err) {
      showResult('#llmResult', false, err.message);
    } finally {
      busy(button, false);
    }
  };

  $('#llmSave').onclick = async () => {
    const button = $('#llmSave');
    const payload = currentLlmPayload();
    if (payload.provider === 'openai_compatible' && !payload.model) return toast('先填模型名');
    if (payload.provider === 'ollama' && !payload.model) return toast('先填本地模型名');
    busy(button, true, '保存中…');
    try {
      const patch = {
        llm: {
          enabled: true,
          provider: payload.provider,
          base_url: payload.provider === 'workbuddy_cloud' ? setupData.llm.base_url || '' : payload.base_url,
          model: payload.model || (payload.provider === 'workbuddy_cloud' ? (setupData.llm.model || 'hy4free') : ''),
        },
      };
      await api('/api/settings', { method: 'PUT', body: patch });
      if (payload.api_key) {
        await api('/api/settings/secrets', { method: 'POST', body: { name: 'llm_api_key', value: payload.api_key } });
        $('#wizKey').value = '';
      }
      showResult('#llmResult', true, '已保存并启用，新任务会用这个模型');
      await loadSetupPanel();
      if (window.loadSettings) await loadSettings();
    } catch (err) {
      showResult('#llmResult', false, err.message);
    } finally {
      busy(button, false);
    }
  };

  $('#wizChannel').onchange = () => {
    $('#wizChannel').dataset.touched = '1';
    const value = $('#wizChannel').value;
    $('#wizWebhook').parentElement.hidden = value === 'inbox';
  };

  $('#notifySave').onclick = async () => {
    const button = $('#notifySave');
    const channel = $('#wizChannel').value;
    const webhook = $('#wizWebhook').value.trim();
    busy(button, true, '保存中…');
    try {
      if (webhook) {
        await api('/api/settings/secrets', { method: 'POST', body: { name: 'notification_webhook', value: webhook } });
        $('#wizWebhook').value = '';
      }
      await api('/api/settings', { method: 'PUT', body: { notifications: { channel } } });
      showResult('#notifyResult', true, `已保存：${CHANNEL_NAMES[channel] || channel}`);
      await loadSetupPanel();
      if (window.loadSettings) await loadSettings();
    } catch (err) {
      showResult('#notifyResult', false, err.message);
    } finally {
      busy(button, false);
    }
  };

  $('#notifyTest').onclick = async () => {
    const button = $('#notifyTest');
    busy(button, true, '发送中…');
    try {
      await $('#notifySave').onclick();
      const result = await api('/api/notifications/test', { method: 'POST' });
      if (result && result.ok === false) {
        showResult('#notifyResult', false, result.message || '没发出去，检查一下上面的发送地址');
      } else {
        showResult('#notifyResult', true, '测试消息已发送', result && result.detail ? String(result.detail).slice(0, 200) : '');
      }
    } catch (err) {
      showResult('#notifyResult', false, err.message);
    } finally {
      busy(button, false);
    }
  };

  $('#accountsGo').onclick = () => {
    const nav = document.querySelector('.nav[data-view="sources"]');
    if (nav) nav.click();
  };

  $('#wizAutostart').onchange = async event => {
    const wanted = event.target.checked;
    $('#wizAutostartText').textContent = wanted ? '设置中…' : '设置中…';
    try {
      const result = await api('/api/setup/autostart', { method: 'POST', body: { enabled: wanted } });
      renderAutostart(result);
      toast(wanted ? '已设为开机自动运行' : '已取消开机自启');
      if (wanted && result.removed_legacy && result.removed_legacy.length) {
        toast('已清掉旧版启动快捷方式：' + result.removed_legacy.join('、'));
      }
      await loadSetupPanel();
    } catch (err) {
      event.target.checked = !wanted;
      $('#wizAutostartText').textContent = wanted ? '关闭' : '已开启';
      toast(err.message);
    }
  };

  $('#setupDoneBtn').onclick = () => {
    const nav = document.querySelector('.nav[data-view="library"]');
    if (nav) nav.click();
  };
}

function activateSubtab(name) {
  document.querySelectorAll('.subnav-btn').forEach(b => b.classList.toggle('active', b.dataset.subtab === name));
  document.querySelectorAll('.subview').forEach(v => { v.hidden = v.id !== 'subtab-' + name; });
}

window.loadSetupPanel = loadSetupPanel;
const deepTab = new URLSearchParams(location.search).get('tab');
if (deepTab) activateSubtab(deepTab);
// 直接用 ?view=settings 打开时，本文件可能比 app.js 的初始化晚到，
// 那时 loaders.settings 已经跑过一轮。这里补一次，保证深链也能看到状态。
if (document.querySelector('#settings') && document.querySelector('#settings').classList.contains('active')) {
  loadSetupPanel();
}
