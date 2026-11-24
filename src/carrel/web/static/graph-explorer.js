/* Lightweight overview navigation. Only the active scope reaches the SVG renderer. */
(() => {
  'use strict';
  const root = document.querySelector('[data-graph-explorer]');
  if (!root) return;
  const $ = s => root.querySelector(s);
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const preferenceKey = `carrel:explorer:v2:${root.dataset.preferenceKey}`;
  const readable = value => {
    if (value == null) return '';
    if (Array.isArray(value)) return value.map(readable).filter(Boolean).join('；');
    if (typeof value !== 'object') return String(value);
    const labels = {text:'内容',quote:'原文',evidence:'证据',reason:'原因',source:'来源',motivation:'动机',problem:'研究问题',limitation:'局限',approach:'方法',summary:'摘要',description:'说明',title:'标题',confidence:'置信度'};
    return Object.entries(value).filter(([k,v]) => v != null && !/(^|_)(id|ids|key|keys|path|hash|revision)$/.test(k)).map(([k,v]) => labels[k] ? `${labels[k]}: ${readable(v)}` : readable(v)).filter(Boolean).join('；');
  };
  let detailTimer;
  const viewStorageKey = preferenceKey + ':viewports';
  let storedViews = [];
  try { const saved = JSON.parse(sessionStorage.getItem(viewStorageKey) || '[]'); if (Array.isArray(saved)) storedViews = saved.slice(-30).filter(x => Array.isArray(x) && typeof x[0] === 'string' && ['x','y','k'].every(k => Number.isFinite(x[1]?.[k])) && x[1].k > 0); } catch {}
  const views = new Map(storedViews);
  let activeURL = location.search;
  function rememberView() {
    if (!renderer?.transform) return;
    views.delete(activeURL); views.set(activeURL, {...renderer.transform});
    while (views.size > 30) views.delete(views.keys().next().value);
    try { sessionStorage.setItem(viewStorageKey, JSON.stringify([...views])); } catch {}
  }
  let params, request, detailRequest, serial = 0, timer, renderer, payload, facets = {}, returnState;
  const defaultLimit = () => params.get('mode') === 'neighbors' ? 5 : params.get('mode') === 'branch' ? 12 : 24;
  function restore() {
    params = new URLSearchParams(location.search);
    if (!['graph','tree'].includes(params.get('view'))) params.set('view','graph');
    if (!params.has('group_by')) { try { params.set('group_by', localStorage.getItem(preferenceKey + ':' + params.get('view')) || 'category'); } catch { params.set('group_by','category'); } }
    if (!['overview','group','neighbors','branch'].includes(params.get('mode'))) params.set('mode','overview');
    if (!params.has('limit')) params.set('limit',defaultLimit());
    syncControls();
  }
  function syncControls() {
    $('[data-cites]').checked = params.get('include_cites') === '1';
    $('[data-edge-limit]').value = params.get('edge_limit') || '40';
    for (const name of ['group_by','q','sort','keywords_match']) $(`[name=${name}]`).value = params.get(name) || {sort:'recent',keywords_match:'any',q:'',group_by:'category'}[name];
    root.querySelectorAll('[data-view]').forEach(b => { b.classList.toggle('on', b.dataset.view === params.get('view')); b.setAttribute('aria-pressed', b.dataset.view === params.get('view')); });
    $('[data-similar]').checked = params.get('include_similar') === '1';
    $('[data-context]').checked = params.get('include_context') === '1';
    $('[data-neighbor-limit]').value = params.get('mode') === 'neighbors' ? params.get('limit') : '5';
  }
  function navigate(changes, reset = false) {
    rememberView();
    if (reset) { ['group_id','focus','offset','include_context'].forEach(k => params.delete(k)); params.set('mode','overview'); params.set('limit','24'); returnState = null; }
    Object.entries(changes).forEach(([k,v]) => { params.delete(k); (Array.isArray(v) ? v : [v]).forEach(x => { if (x !== '' && x != null) params.append(k,x); }); });
    history.pushState({},'',`${location.pathname}?${params}`); activeURL = location.search; syncControls(); load();
  }
  function feedback(message, failed = false) { $('[data-feedback]').textContent = message; $('[data-retry]').hidden = !failed; }
  async function json(url, options = {}) {
    const response = await fetch(url, {credentials:'same-origin', ...options});
    const data = await response.json();
    if (!response.ok || data.status === 'error') throw new Error(data.message || data.error?.message || data.detail || '请求失败，请重试');
    return data;
  }
  async function load() {
    clearTimeout(timer); clearTimeout(detailTimer); request?.abort(); detailRequest?.abort(); $('[data-detail]').hidden = true;
    request = new AbortController(); const version = ++serial;
    root.setAttribute('aria-busy','true'); feedback('正在加载当前范围…');
    try {
      const data = await json('/api/graph/explore?' + params, {signal:request.signal});
      if (version !== serial) return;
      if (data.status === 'building') { feedback('正在建立资料库快照…'); timer = setTimeout(load,Math.max(300,data.retry_after_ms || 700)); return; }
      payload = data; render(data); feedback(data.stale || data.rebuilding ? '正在更新；当前显示上次有效快照。' : '');
      if (data.stale || data.rebuilding) timer = setTimeout(load,1500);
    } catch (error) { if (error.name !== 'AbortError' && version === serial) feedback(error.message,true); }
    finally { if (version === serial) root.setAttribute('aria-busy','false'); }
  }
  function renderFacets(data) {
    if (data.facets) facets = data.facets;
    $('[data-facets]').innerHTML = [['category','categories','分类'],['collection','collections','阅读集合'],['keyword','keywords','关键词']].map(([key,list,title]) => {
      const selected = params.getAll(key); const items = [...(facets[list] || [])];
      if (key === 'category') items.sort((a,b) => String(a.parent || a.label).localeCompare(String(b.parent || b.label),'zh') || (a.level || 2) - (b.level || 2));
      selected.forEach(id => { if (!items.some(x => String(x.id) === id)) items.push({id,label:id}); });
      return `<fieldset><legend>${title}</legend>${items.map(x => `<label class="${key === 'category' ? 'facet-level-' + (x.level || 2) : ''}"><input type="checkbox" data-facet="${key}" value="${esc(x.id)}" ${selected.includes(String(x.id)) ? 'checked' : ''}>${key === 'category' ? (x.level === 1 ? '一级 ? ' : '二级 ? ') : ''}${esc(x.parent ? x.parent + ' / ' : '')}${esc(x.label)}${x.count == null ? '' : ' (' + esc(x.count) + ')'}</label>`).join('') || '<p>暂无选项</p>'}</fieldset>`;
    }).join('');
    $('[data-chips]').innerHTML = ['category','collection','keyword'].flatMap(k => params.getAll(k).map(v => { const item = (facets[{category:'categories',collection:'collections',keyword:'keywords'}[k]] || []).find(x => String(x.id) === v); return `<button class="btn tiny" data-remove="${k}" data-value="${esc(v)}" aria-label="移除 ${esc(item?.label || v)}">${esc(item?.label || v)} ×</button>`; })).join('');
  }
  function render(data) {
    const overview = params.get('mode') === 'overview', tree = params.get('view') === 'tree';
    $('[data-branch-label-form]').hidden = params.get('mode') !== 'branch';
    $('[data-branch-label]').value = data.scope_label || '';
    $('[data-branch-label-feedback]').textContent = '';
    $('[data-similar]').parentElement.hidden = tree;
    renderFacets(data); $('[data-cards]').hidden = !overview; $('[data-local]').hidden = overview; $('[data-back]').hidden = overview;
    $('[data-group-edges]').hidden = tree || params.get('mode') !== 'group';
    $('[data-print]').hidden = !overview; $('[data-empty-toggle]').hidden = !overview || params.get('group_by') !== 'collection';
    $('[data-neighbor-control]').hidden = params.get('mode') !== 'neighbors'; $('[data-context-control]').hidden = !tree;
    $('[data-summary]').textContent = params.get('mode') === 'neighbors' ? `展示 ${Math.max(0,(data.shown || 0) - 1)} / ${data.neighbor_total ?? data.total ?? 0} 篇相关论文` : `当前 ${data.shown ?? 0} / ${data.total ?? 0} 篇论文${data.omitted ? ` · 尚未展开 ${data.omitted} 篇` : ''}`;
    $('[data-snapshot]').textContent = data.generated_at ? `快照更新：${new Date(data.generated_at).toLocaleString('zh-CN', {hour12:false})}` : '';
    if (Number(params.get('offset')) > 0 && params.get('mode') !== 'neighbors') {
      const start = Number(params.get('offset'));
      $('[data-summary]').textContent = `当前第 ${start + 1}–${start + (data.shown || 0)} / ${data.total || 0} 篇论文`;
    }
    if (data.context_shown) $('[data-summary]').textContent += ' · 另显示 ' + data.context_shown + ' 篇上下文';
    $('[data-notes]').replaceChildren(...(data.notes || []).map(note => { const p = document.createElement('p'); p.textContent = typeof note === 'string' ? note : JSON.stringify(note); return p; }));
    if (overview) {
      $('[data-notes]').insertAdjacentHTML('beforeend','<details class="explorer-help"><summary>如何理解处理进度</summary><p>每篇论文只计入最高已完成的档位：收录、速览、笔记或全文。使用自定义处理选项时，部分内容可能已完成，但尚未满足下一档的全部要求。这些数字表示处理进度，与是否读过无关。</p></details>');
      if (data.stats?.tasks) $('[data-notes]').insertAdjacentHTML('beforeend', `<p>任务状态：待处理 ${data.stats.tasks.pending || 0} · 失败 ${data.stats.tasks.failed || 0}</p>`);
      if (tree && !data.shown) $('[data-notes]').insertAdjacentHTML('beforeend', `<p>${esc(data.empty_reason || '当前范围尚无已确认的动机主线，请检查分析完成状态及筛选条件。')}</p>`);
      renderer?.destroy?.(); renderer = null;
      const candidates = [...(data.groups || [])];
      if (!tree && params.get('group_by') === 'collection') for (const c of facets.collections || []) { if (!candidates.some(g => String(g.id) === String(c.id))) candidates.push({...c,count:0}); }
      const groups = candidates.filter(g => $('[data-empty]').checked || g.count || params.get('group_by') !== 'collection');
      if (params.get('sort') === 'name') groups.sort((a,b) => String(a.label).localeCompare(String(b.label),'zh'));
      if (params.get('sort') === 'depth') { const depth = g => (g.coverage?.full || 0)*3+(g.coverage?.notes || 0)*2+(g.coverage?.skim || 0); groups.sort((a,b)=>depth(b)-depth(a)); }
      $('[data-cards]').innerHTML = groups.map(g => `<article class="explorer-card"><p class="eyebrow">${esc(g.parent || (tree ? '研究主线' : '研究方向'))}</p><h2>${esc(g.label)}</h2><p class="explorer-number">${esc(g.count)} <small>篇论文</small></p>${g.coverage ? `<p class="explorer-coverage">收录 ${g.coverage.indexed || 0} · 速览 ${g.coverage.skim || 0} · 笔记 ${g.coverage.notes || 0} · 全文 ${g.coverage.full || 0}</p>` : ''}<ul>${(g.representatives || []).slice(0,3).map(p => `<li>${esc(p.label || p.title)}</li>`).join('')}</ul><button class="btn" data-group="${esc(g.id)}">${tree ? '展开主线' : '查看全部'} ${esc(g.count)} 篇</button></article>`).join('') || '<p>当前范围没有可展示内容。可调整筛选或查看论文关系。</p>';
      $('[data-cards]').querySelectorAll('.explorer-card').forEach((card,i) => { const hue = [...String(groups[i].category_id || groups[i].id)].reduce((n,c)=>(n*31+c.charCodeAt(0))%360,0); card.style.borderTop = `4px solid hsl(${hue} 32% 48%)`; });
      if (tree) $('[data-notes]').insertAdjacentHTML('beforeend','<p>主线由已有动机证据连接；阅读集合仅限定范围，不改变前驱关系。</p>');
      return;
    }
    const graphRoot = $('[data-renderer]'); graphRoot.dataset.graphScope = tree ? 'motivation_tree' : 'library';
    graphRoot.dataset.exportCaption = $('[data-summary]').textContent;
    if (!window.CarrelGraph?.mount) throw new Error('图谱渲染器尚未就绪，请重试');
    const scopeCaption = [params.get('view') === 'tree' ? '动机脉络' : '论文关系', ...root.querySelector('[data-chips]').textContent.split('×').map(x=>x.trim()).filter(Boolean)].join(' · ');
    const graphData = {...(data.graph || {nodes:[],edges:[],scope:graphRoot.dataset.graphScope}), boundary:data.boundary || [], omitted:data.omitted || 0, scope_label:data.scope_label || scopeCaption, mode:params.get('mode')};
    renderer = window.CarrelGraph.mount(graphRoot,graphData,{managed:true,onSelect:select,boundary:data.boundary || [],include_cites:params.get('mode') === 'neighbors' || params.get('include_cites') === '1',include_similar:params.get('include_similar') === '1',edge_limit:Number(params.get('edge_limit') || 40)});
    const mounted = renderer, savedView = views.get(activeURL);
    Promise.resolve(renderer.ready).then(() => { if (mounted === renderer && savedView) { renderer.transform = {...savedView}; renderer.applyTransform?.(); } });
    const remaining = Math.max(0, (data.total || 0) - Number(params.get('offset') || 0) - (data.shown || 0));
    $('[data-more]').hidden = !remaining || params.get('mode') === 'neighbors';
    $('[data-more]').textContent = `展开更多（剩余 ${remaining} 篇）`;
    $('[data-boundary]').innerHTML = (data.boundary || []).map(b => `<p>${esc(b.label || '前驱论文')} · ${b.reason === 'pagination' ? '前驱在其他分页' : '前驱在筛选范围外'} <button class="btn tiny" data-predecessor="${esc(b.predecessor_id)}">查看前驱详情</button><button class="btn tiny" data-show-context>显示前驱上下文</button></p>`).join('');
    if (params.get('focus')) showDetail(params.get('focus'));
  }
  function select(id) {
    if (!id) return;
    if (params.get('view') === 'tree') { rememberView(); params.set('focus',id); history.replaceState({},'',`${location.pathname}?${params}`); activeURL = location.search; showDetail(id); return; }
    if (params.get('mode') !== 'neighbors') returnState = new URLSearchParams(params);
    navigate({mode:'neighbors',focus:id,limit:5});
  }
  async function showDetail(id, attempt = 0) {
    clearTimeout(detailTimer);
    detailRequest?.abort(); detailRequest = new AbortController(); const signal = detailRequest.signal;
    $('[data-detail]').hidden = false; if (!attempt) $('[data-detail-body]').textContent = '正在加载论文详情…';
    try {
      const data = await json('/api/graph/node?paper_id=' + encodeURIComponent(id), {signal});
      if (signal.aborted) return;
      const retryPending = () => { if (attempt < 8) detailTimer = setTimeout(() => { if (!signal.aborted) showDetail(id, attempt + 1); }, Math.min(5000,Math.max(700,data.retry_after_ms || 1200))); };
      if (data.status === 'building') { if (attempt >= 8) throw new Error('详情仍在准备，请稍后重试'); retryPending(); return; }
      const node = data.node || data.paper || data, meta = node.meta || node;
      const collections = meta.collections || data.collections || [], tags = meta.tags || data.keywords || [];
      const relations = data.relations || meta.relations || {};
      const relationHTML = Object.entries({cites:'引用',cited_by:'被引用',similar:'相似论文'}).map(([key,label]) => `<h4>${label} · ${data.counts?.[key] ?? relations[key]?.length ?? 0}</h4><ul>${(relations[key] || []).map(r => `<li><a href="/papers/${encodeURIComponent(r.paper_id)}">${esc(r.label || '相关论文')}</a>${r.evidence?.length ? `<details><summary>查看证据</summary><pre>${esc(readable(r.evidence))}</pre></details>` : ''}</li>`).join('')}</ul>`).join('');
      $('[data-detail-body]').innerHTML = `<h2>${esc(node.title || meta.title || node.label || '论文详情')}</h2><a class="btn" href="/papers/${encodeURIComponent(id)}">打开论文</a><form data-label-form><label>图中展示名<input name="label" maxlength="60" value="${esc(node.label || meta.label || '')}"></label><button class="btn">保存展示名</button><p data-label-feedback role="status"></p></form><h3>阅读集合</h3>${collections.map(c => `<button class="btn tiny" data-collection="${esc(c.id)}">${esc(c.name || c.label || '阅读集合')}</button>`).join('') || '<p>未加入集合</p>'}<h3>关键词</h3><p>${tags.map(t => esc(typeof t === 'string' ? t : t.label || t.tag)).join(' · ') || '暂无关键词'}</p><h3>关系与证据</h3>${relationHTML}${data.motivation || meta.motivation ? `<h3>动机</h3><pre>${esc(readable(data.motivation || meta.motivation))}</pre>` : ''}`;
      if (data.motivation_edges?.length) {
        const section = document.createElement('section');
        section.innerHTML = '<h3>动机递进关系</h3>';
        for (const edge of data.motivation_edges) {
          const row = document.createElement('p'), source = document.createElement('a'), target = document.createElement('a');
          source.href = '/papers/' + encodeURIComponent(edge.source_paper_id);
          target.href = '/papers/' + encodeURIComponent(edge.target_paper_id);
          source.textContent = edge.source_label || '前驱论文'; target.textContent = edge.target_label || '后继论文';
          row.append(source, document.createTextNode(' → '), target,
            document.createTextNode(' · ' + (edge.primary ? '主要关系' : '补充关系') + (edge.label ? ' · ' + edge.label : '')));
          section.append(row);
          if (edge.evidence || edge.basis) { const note = document.createElement('p'); note.textContent = readable(edge.evidence || edge.basis); section.append(note); }
        }
        $('[data-detail-body]').append(section);
      }
      if (data.motivation_pending) {
        $('[data-detail-body]').insertAdjacentHTML('beforeend', `<p role="status">${attempt < 8 ? '动机信息正在准备…' : '动机信息仍在准备，请稍后重试详情。'}</p>`);
        retryPending();
      }
      $('[data-label-form]').onsubmit = async event => {
        event.preventDefault(); const button = event.target.querySelector('button'); button.disabled = true;
        try { await json('/api/graph/node-label',{method:'POST',headers:{'Content-Type':'application/json','X-Carrel':'1'},body:JSON.stringify({paper_id:id,label:new FormData(event.target).get('label')})}); load(); }
        catch(error) { $('[data-label-feedback]').textContent = error.message; } finally { button.disabled = false; }
      };
    } catch(error) { if (!signal.aborted) { if (!attempt) $('[data-detail-body]').textContent = error.message; else { const status = document.createElement('p'); status.textContent = error.message; $('[data-detail-body]').append(status); } const retry = document.createElement('button'); retry.className='btn'; retry.textContent='重试详情'; retry.onclick=()=>showDetail(id); $('[data-detail-body]').append(retry); } }
  }
  $('[data-branch-label-form]').onsubmit = async event => {
    event.preventDefault(); const button = event.target.querySelector('button');
    const branchId = params.get('group_id'), label = $('[data-branch-label]').value.trim();
    if (!branchId) return;
    button.disabled = true;
    try {
      await json('/api/graph/branch-label',{method:'POST',headers:{'Content-Type':'application/json','X-Carrel':'1'},body:JSON.stringify({branch_id:branchId,label})});
      if (params.get('group_id') === branchId) { rememberView(); await load(); $('[data-branch-label-feedback]').textContent = '主线名称已保存'; }
    } catch(error) { $('[data-branch-label-feedback]').textContent = error.message; }
    finally { button.disabled = false; }
  };
  $('[data-explorer-form]').onsubmit = e => { e.preventDefault(); navigate({q:$('[name=q]').value.trim()},true); };
  $('[data-explorer-form]').onchange = e => {
    const input = e.target;
    if (input.dataset.facet) navigate({[input.dataset.facet]:Array.from(root.querySelectorAll(`[data-facet=${input.dataset.facet}]:checked`),x=>x.value)},true);
    else if (['group_by','sort','keywords_match'].includes(input.name)) { if (input.name === 'group_by') try { localStorage.setItem(preferenceKey + ':' + params.get('view'),input.value); } catch {} navigate({[input.name]:input.value},true); }
  };
  root.addEventListener('click', e => {
    const b = e.target.closest('button'); if (!b) return;
    if (b.dataset.predecessor) showDetail(b.dataset.predecessor);
    if (b.hasAttribute('data-show-context')) navigate({include_context:'1'});
    if (b.dataset.view) navigate({view:b.dataset.view},true);
    if (b.dataset.group) navigate({group_id:b.dataset.group,mode:params.get('view') === 'tree' ? 'branch' : 'group',limit:params.get('view') === 'tree' ? 12 : 24});
    if (b.dataset.remove) navigate({[b.dataset.remove]:params.getAll(b.dataset.remove).filter(v=>v!==b.dataset.value)},true);
    if (b.dataset.collection) navigate({collection:[b.dataset.collection]},true);
  });
  $('[data-back]').onclick = () => { if (params.get('mode') === 'neighbors') { if (returnState) { params = returnState; returnState=null; navigate({}); } else navigate({mode:'group',focus:'',limit:24}); } else navigate({},true); };
  $('[data-more]').onclick = () => { const limit = Number(params.get('limit') || defaultLimit()); navigate(limit >= 200 ? {offset:Number(params.get('offset') || 0) + 200,limit:defaultLimit()} : {limit:Math.min(200,limit + (params.get('view') === 'tree' ? 12 : 24))}); };
  $('[data-neighbor-limit]').onchange = e => navigate({limit:e.target.value});
  $('[data-similar]').onchange = e => navigate({include_similar:e.target.checked ? '1' : '0'});
  $('[data-cites]').onchange = e => navigate({include_cites:e.target.checked ? '1' : '0'});
  $('[data-edge-limit]').onchange = e => navigate({edge_limit:e.target.value});
  $('[data-context]').onchange = e => navigate({include_context:e.target.checked ? '1' : '0'});
  $('[data-detail-close]').onclick = () => { clearTimeout(detailTimer); detailRequest?.abort(); $('[data-detail]').hidden = true; };
  $('[data-empty]').onchange = () => payload && render(payload);
  $('[data-retry]').onclick = load; $('[data-print]').onclick = () => window.print();
  $('[data-refresh]').onclick = async () => { try { await json('/api/graph/refresh',{method:'POST',headers:{'X-Carrel':'1'}}); load(); } catch(error) { feedback(error.message,true); } };
  $('[data-reset]').onclick = () => { try { ['graph','tree'].forEach(v=>localStorage.removeItem(preferenceKey + ':' + v)); } catch {} params = new URLSearchParams({view:params.get('view'),group_by:'category',mode:'overview',limit:'24'}); navigate({}); };
  window.addEventListener('popstate',()=>{rememberView();activeURL=location.search;restore();load();});
  window.addEventListener('pagehide',()=>{rememberView();clearTimeout(timer);clearTimeout(detailTimer);request?.abort();detailRequest?.abort();renderer?.destroy?.();});
  document.addEventListener('carrel:organization-changed',load);
  restore(); load();
})();
