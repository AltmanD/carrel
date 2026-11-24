(() => {
  if (!document.getElementById('settings-page')) return;
  const $ = id => document.getElementById(id), esc = escapeHtml;
  const labels = { title: '中文标题', preview: '论文速览', extract: '章节抽取', summarize: '全文总结', glossary: '术语表', translate: '翻译', repair: '翻译修复', term_review: '术语复核', classify: '分类与标签', curate: '标签整理（预留）', assess: '模型评估', motivation: '研究动机', motivation_tree: '动机脉络' };
  const thinking = [['', '继承默认'], ['disabled', '关闭'], ['low', '低'], ['medium', '中'], ['high', '高']];
  const groups = [
    { id: 'reading', title: '理解与总结', icon: 'book-open', tasks: ['title', 'preview', 'extract', 'summarize'], description: '生成中文标题、速览、章节要点和核心贡献。理解能力更强的模型通常更适合复杂论文。' },
    { id: 'translation', title: '翻译与校对', icon: 'languages', tasks: ['glossary', 'translate', 'repair', 'term_review'], description: '生成中文译文，统一术语并修复数字、引用等错误。长论文主要在这里消耗时间与额度。' },
    { id: 'organizing', title: '分类与评估', icon: 'tags', tasks: ['classify', 'curate', 'assess'], description: '整理分类、标签和阅读价值评估。使用较快的模型可以减少整理时的等待。' },
    { id: 'connections', title: '动机与脉络', icon: 'git-branch', tasks: ['motivation', 'motivation_tree'], description: '提取论文动机、比较前人不足并梳理递进关系。更强的推理能力有助于区分真正相关的工作。' },
  ];
  let state, providers = [], dirty = false, timer, verificationRunning = false, verified = false;
  const option = (value, text, selected) => '<option value="' + esc(value) + '"' + (value === selected ? ' selected' : '') + '>' + esc(text) + '</option>';
  const choices = (selected, empty = '继承默认提供方') => option('', empty, selected) + providers.map(p => option(p.id, p.label, selected)).join('');
  const endpoint = id => '/api/providers/' + encodeURIComponent(id);
  const requestError = error => { $('settings-error').textContent = error.message; $('settings-error').hidden = false; };
  function setDirty(value) {
    dirty = value;
    $('route-dirty').textContent = value ? '有未保存的任务分配 · 保存后生效' : '任务分配已保存';
    $('route-dirty').closest('.route-savebar').classList.toggle('is-dirty', value);
    $('routes-reset').hidden = !value;
    updateRecommend();
  }
  const modelChoices = (selected, empty = '跟随默认模型') => option('', empty, selected) + providers.map(p => option(p.id, p.label + ' · ' + (p.model || '未填写模型'), selected)).join('');
  function collectRoutes() {
    const routes = {};
    document.querySelectorAll('#route-rows tr').forEach(row => {
      const get = key => row.querySelector('[data-route="' + key + '"]');
      if (get('enabled').checked) routes[row.dataset.task] = { provider: get('provider').value, name: get('name').value.trim(), thinking: get('thinking').value, max_tokens: Number(get('max_tokens').value || 0) };
    });
    return routes;
  }
  function renderGroups() {
    const focusedGroup = document.activeElement?.matches('[data-group-model]') ? document.activeElement.id : '';
    const routes = collectRoutes();
    const fallback = providers.find(p => p.id === $('default-provider').value);
    $('default-model-hint').textContent = '各功能组默认使用 ' + (fallback?.model || state.legacy.name) + '。切换组模型会统一该组任务，并恢复该组的默认思考强度与回复预算。';
    $('route-groups').innerHTML = groups.map(group => {
      const values = group.tasks.map(t => routes[t]);
      const untouched = values.every(v => !v);
      const common = !untouched && values.every(v => v && v.provider && !v.name && !v.thinking && !v.max_tokens && v.provider === values[0]?.provider);
      const selected = untouched ? '' : common ? values[0].provider : '__custom__';
      return '<article class="route-group" data-route-group="' + group.id + '"><div class="route-group-heading"><i data-lucide="' + group.icon + '" class="icon" aria-hidden="true"></i><h3>' + group.title + '</h3></div><p>' + group.description + '</p><label class="sr-only" for="group-' + group.id + '">' + group.title + '使用的模型</label><select id="group-' + group.id + '" data-group-model="' + group.id + '">' + modelChoices(selected, '跟随默认 · ' + (fallback?.model || state.legacy.name)) + (selected === '__custom__' ? option('__custom__', '已逐项配置 · 保留现有分配', selected) : '') + '</select><span class="route-group-detail">' + (selected === '__custom__' ? '该组含独立设置，可在高级选项中查看。' : group.tasks.filter(t => t !== 'curate').map(t => labels[t]).join('、')) + '</span></article>';
    }).join('');
    $('route-custom-count').textContent = Object.keys(routes).length ? '（' + Object.keys(routes).length + ' 项独立分配）' : '';
    refreshIcons();
    if (focusedGroup) $(focusedGroup)?.focus({ preventScroll: true });
  }
  function renderProviders() {
    $('provider-rows').innerHTML = providers.map(p => {
      const buttons = [['edit', '编辑'], ['test', '测试连接'], ['delete', '删除']].map(([action, text]) => '<button class="btn tiny" data-provider-action="' + action + '" data-id="' + esc(p.id) + '">' + text + '</button>').join('');
      return '<tr><td><strong>' + esc(p.label) + '</strong><br><code class="dim">' + esc(p.id) + '</code><p class="dim">' + esc(p.notes) + '</p></td><td>' + esc(p.protocol) + '<br><span class="dim">' + esc(p.base_url) + '</span></td><td>' + esc(p.model || '尚未填写') + '</td><td><span class="' + (p.configured ? 'ok' : 'bad') + '">' + (p.configured ? '已配置' : '待配置') + '</span><br><code class="dim">' + esc(p.api_key) + '</code></td><td><div class="settings-row-actions">' + buttons + '</div></td></tr>';
    }).join('') || '<tr><td colspan="5"><p>尚未添加提供方，当前沿用原有模型配置。</p><button class="btn" data-provider-action="add">添加第一个提供方</button></td></tr>';
    const target = $('verify-provider').value;
    $('verify-provider').innerHTML = choices(target, '选择提供方');
    if (!target && providers.length) $('verify-provider').value = providers[0].id;
    const baseline = $('verify-baseline').value;
    $('verify-baseline').innerHTML = option('', '原模型配置', baseline) + providers.filter(p => p.model.toLowerCase().startsWith('glm')).map(p => option(p.id, p.label, baseline)).join('');
    refreshIcons();
  }
  function routeRow(task) {
    const route = state.routes[task], value = route || {}, defaults = state.defaults[task], label = labels[task];
    return '<tr data-task="' + task + '"><th scope="row">' + label + '</th>' +
      '<td><input type="checkbox" data-route="enabled" ' + (route ? 'checked' : '') + ' aria-label="单独设置' + label + '"></td>' +
      '<td><select data-route="provider" aria-label="' + label + '提供方">' + choices(value.provider) + '</select></td>' +
      '<td><input data-route="name" value="' + esc(value.name || '') + '" placeholder="' + esc(defaults.name) + '" aria-label="' + label + '模型名"></td>' +
      '<td><select data-route="thinking" aria-label="' + label + '思考强度">' + thinking.map(([v, t]) => option(v, t, value.thinking || '')).join('') + '</select></td>' +
      '<td><input type="number" min="0" max="1000000" data-route="max_tokens" value="' + (value.max_tokens || '') + '" placeholder="' + defaults.max_tokens + '" aria-label="' + label + '输出上限"></td></tr>';
  }
  function syncRow(row) {
    const enabled = row.querySelector('[data-route="enabled"]').checked;
    row.querySelectorAll('[data-route]:not([data-route="enabled"])').forEach(e => { e.disabled = !enabled; });
    const pid = row.querySelector('[data-route="provider"]').value || $('default-provider').value;
    const provider = providers.find(p => p.id === pid);
    row.querySelector('[data-route="name"]').placeholder = provider?.model || state.defaults[row.dataset.task].name;
    row.querySelector('[data-route="max_tokens"]').placeholder = provider ? state.default_max_tokens[row.dataset.task] : state.defaults[row.dataset.task].max_tokens;
  }
  async function load({ routes = true } = {}) {
    const [p, r] = await Promise.all([api('/api/providers'), api('/api/task-routes')]);
    providers = p.providers; state = r;
    if (p.warning) requestError(new Error(p.warning));
    else $('settings-error').hidden = true;
    renderProviders();
    const selected = routes ? r.default_provider : $('default-provider').value;
    $('default-provider').innerHTML = modelChoices(selected, '原有配置 · ' + r.legacy.name);
    if (routes) { $('route-rows').innerHTML = r.tasks.map(routeRow).join(''); setDirty(false); }
    else document.querySelectorAll('#route-rows select[data-route="provider"]').forEach(e => { e.innerHTML = choices(e.value); });
    document.querySelectorAll('#route-rows tr').forEach(syncRow);
    renderGroups();
    await refreshVerification();
  }
  function openProvider(id) {
    const p = providers.find(p => p.id === id), form = $('provider-form');
    form.reset(); form.querySelector('.form-error').hidden = true;
    $('provider-title').textContent = p ? '编辑提供方' : '添加提供方';
    $('provider-id').readOnly = !!p;
    if (p) for (const [key, value] of Object.entries(p)) {
      const field = form.elements.namedItem(key);
      if (field && key !== 'api_key') field.value = ['extra_headers', 'pricing'].includes(key) ? JSON.stringify(value, null, 2) : value;
    }
    $('provider-key').value = '';
    $('provider-dialog').showModal();
  }
  $('provider-add').addEventListener('click', () => openProvider());
  $('provider-form').addEventListener('submit', async e => {
    e.preventDefault(); const form = e.currentTarget, data = Object.fromEntries(new FormData(form)); busy(form, true);
    try {
      for (const key of ['extra_headers', 'pricing']) data[key] = JSON.parse(data[key] || '{}');
      for (const key of ['timeout_s', 'max_concurrency']) data[key] = Number(data[key]);
      await api('/api/providers', data); $('provider-dialog').close();
      await load({ routes: false }); toast('提供方已保存，后续调用立即生效');
    } catch (error) { showError(form, error); } finally { busy(form, false); }
  });
  $('provider-rows').addEventListener('click', async e => {
    const button = e.target.closest('[data-provider-action]'); if (!button) return;
    const { providerAction: action, id } = button.dataset;
    if (action === 'edit' || action === 'add') return openProvider(id);
    button.disabled = true;
    try {
      if (action === 'test') {
        $('probe-result').hidden = false; $('probe-result').textContent = '正在测试连接…';
        const result = await api(endpoint(id) + '/test', {});
        $('probe-result').className = result.ok && !result.substituted ? 'ok' : 'bad';
        $('probe-result').textContent = result.ok ? id + ' · ' + result.model + ' · ' + result.latency_ms + ' ms' + (result.substituted ? ' · 注意：实际模型与请求模型不同' : ' · 连接成功') : '连接失败：' + result.error;
      } else {
        const response = await fetch(endpoint(id), { method: 'DELETE', headers: { 'X-Carrel': '1' } });
        const result = await response.json();
        if (!response.ok) throw new Error([result.message, result.hint].filter(Boolean).join('。'));
        await load({ routes: false }); toast('提供方已删除');
      }
    } catch (error) { toast(error.message, { error: true }); } finally { button.disabled = false; }
  });
  $('routes-form').addEventListener('input', e => {
    if (e.target.matches('[data-group-model]')) return;
    setDirty(true);
    if (e.target.matches('[data-route]')) renderGroups();
  });
  $('routes-form').addEventListener('change', e => {
    if (e.target.matches('[data-group-model]')) {
      const selected = e.target.value;
      if (selected === '__custom__') return;
      const group = groups.find(g => g.id === e.target.dataset.groupModel);
      for (const task of group.tasks) {
        const row = document.querySelector('#route-rows [data-task="' + task + '"]');
        row.querySelector('[data-route="enabled"]').checked = Boolean(selected);
        for (const key of ['provider', 'name', 'thinking', 'max_tokens']) row.querySelector('[data-route="' + key + '"]').value = key === 'provider' ? selected : '';
      }
    }
    setDirty(true); document.querySelectorAll('#route-rows tr').forEach(syncRow); renderGroups();
  });
  $('routes-reset').addEventListener('click', () => load().catch(requestError));
  $('routes-form').addEventListener('submit', async e => {
    e.preventDefault(); const form = e.currentTarget, routes = collectRoutes(); busy(form, true);
    try { await api('/api/task-routes', { routes, default_provider: $('default-provider').value }); setDirty(false); toast('任务分配已保存'); }
    catch (error) { showError(form, error); } finally { busy(form, false); }
  });
  function updateRecommend() {
    $('recommend-apply').disabled = !verified || verificationRunning || dirty;
    $('recommend-apply').title = dirty ? '请先保存任务分配，避免覆盖未保存修改' : verified ? '将全部任务切换到此提供方' : '须先通过全部任务验证';
  }
  async function refreshVerification() {
    clearTimeout(timer); const id = $('verify-provider').value;
    verified = false; verificationRunning = false; updateRecommend();
    $('verify-start').disabled = !id;
    if (!id) { $('verification-report').textContent = ''; $('verification-status').textContent = '添加并选择提供方后开始验证。'; return; }
    const result = await api(endpoint(id) + '/verification');
    if (id !== $('verify-provider').value) return;
    verified = result.passed; verificationRunning = result.job?.status === 'running';
    $('verify-start').disabled = verificationRunning;
    $('verification-status').textContent = verificationRunning ? '验证中 · ' + result.job.done + '/' + result.job.total + ' · ' + (labels[result.job.task] || '准备样本') : result.job?.error || (verified ? '全部任务验证通过，可以应用推荐路由。' : result.report?.created_at ? result.current ? '验证未通过，展开失败任务查看详情。' : '配置已改变，需重新验证。' : '尚未进行逐任务验证。');
    $('verification-report').innerHTML = (result.report?.tasks || []).map(row => '<details class="verification-task"><summary><span class="' + (row.passed ? 'ok' : 'bad') + '">' + (row.passed ? '通过' : '失败') + '</span> ' + (labels[row.task] || esc(row.task)) + '</summary><p>目标：' + (row.candidate?.passed ? '通过' : '失败') + ' · GLM 基线：' + (row.baseline?.passed ? '通过' : '失败') + '</p>' + ['candidate', 'baseline'].map(k => '<p class="bad">' + esc((row[k]?.errors || []).join('；')) + '</p><pre>' + esc(row[k]?.response || '') + '</pre>').join('') + '</details>').join('');
    updateRecommend();
    if (verificationRunning) timer = setTimeout(() => refreshVerification().catch(requestError), 3000);
  }
  $('verify-provider').addEventListener('change', () => refreshVerification().catch(requestError));
  $('verify-start').addEventListener('click', async () => {
    $('verify-start').disabled = true;
    try { await api(endpoint($('verify-provider').value) + '/verify', { baseline_provider: $('verify-baseline').value }); await refreshVerification(); }
    catch (error) { requestError(error); $('verify-start').disabled = false; }
  });
  $('recommend-apply').addEventListener('click', async () => {
    $('recommend-apply').disabled = true;
    try { await api('/api/task-routes', { recommend: $('verify-provider').value }); await load(); toast('全部任务已应用通过验证的推荐路由'); }
    catch (error) { requestError(error); updateRecommend(); }
  });
  window.addEventListener('beforeunload', e => { if (dirty) { e.preventDefault(); e.returnValue = ''; } });
  load().catch(requestError);
})();
