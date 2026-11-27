/* Research-direction navigation, with bounded graph scopes and paper location. */
(() => {
  'use strict';
  const root = document.querySelector('[data-graph-explorer]');
  if (!root) return;
  const $ = s => root.querySelector(s);
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const readable = value => {
    if (value == null) return '';
    if (Array.isArray(value)) return value.map(readable).filter(Boolean).join('；');
    if (typeof value !== 'object') return String(value);
    return Object.entries(value).filter(([k,v]) => v != null && !/(^|_)(id|ids|key|keys|path|hash|revision)$/.test(k)).map(([,v])=>readable(v)).filter(Boolean).join('；');
  };
  const viewStorageKey = 'carrel:explorer:v3:' + root.dataset.preferenceKey + ':viewports';
  let storedViews = [];
  try { storedViews = JSON.parse(sessionStorage.getItem(viewStorageKey) || '[]').slice(-30).filter(x=>Array.isArray(x) && typeof x[0]==='string' && ['x','y','k'].every(k=>Number.isFinite(x[1]?.[k])) && x[1].k>0); } catch {}
  const views = new Map(storedViews), overviewCache = new Map();
  let params, renderer, request, detailRequest, detailTimer, timer, serial = 0, payload, overviewData, returnState;
  let activeURL = location.search, autoScope = !new URLSearchParams(location.search).has('mode');
  let locateRequest, locateTimer, locateSerial = 0, locateHits = [], locating = false, pendingLocate = '';
  let citationRequest, citationTimer, citationSerial = 0;
  function graphOptions(extra = {}) {
    return {managed:true,onSelect:id=>{if(!renderer?.nodes.get(id)?.meta.aggregate)select(id);},onActivate:select,onBackground:dismissSelection,...extra};
  }
  function closeDetail() { clearTimeout(detailTimer);detailRequest?.abort();$('[data-detail]').hidden=true; }
  function cancelCitations() { clearTimeout(citationTimer);citationRequest?.abort();citationSerial++; }
  function dismissSelection() {
    closeDetail();cancelCitations();renderer?.clearSelection();root.dataset.citationState='idle';
    if(renderer?.data?.nodes.length) renderer.setStatus('');
  }
  async function showCitations(id,attempt=0) {
    clearTimeout(citationTimer);citationRequest?.abort();citationRequest=new AbortController();
    const version=++citationSerial,signal=citationRequest.signal,controller=renderer;
    root.dataset.citationState='loading';
    const query=new URLSearchParams({paper_id:id,limit:'5'});
    for(const node of controller.nodes.values()) {
      if(!node.meta?.aggregate&&!controller.citationOverlay?.added.has(node.id)) query.append('visible',node.meta.paper_id||node.id);
    }
    try {
      const data=await json('/api/graph/citations?'+query,{signal});
      if(signal.aborted||version!==citationSerial||controller!==renderer) return;
      if(data.status==='building') {
        if(attempt>=8) throw new Error('引用关系仍在准备，请稍后重新选择论文');
        citationTimer=setTimeout(()=>{if(!signal.aborted)showCitations(id,attempt+1);},Math.max(500,data.retry_after_ms||700));return;
      }
      controller.setCitationOverlay(data.graph);root.dataset.citationState='ready';
      const total=data.graph.edges.length,outside=data.outside_shown||0;
      controller.setStatus(total?'引用 '+total+' 条'+(outside?' · 图外 '+outside+'/'+data.outside_total+' 篇':''):'暂无已收录的引用关系');
    } catch(error) {
      if(!signal.aborted&&version===citationSerial) {root.dataset.citationState='error';controller.setStatus(error.message,{error:true});}
    }
  }
  const defaultLimit = () => params.get('view') === 'tree' ? 12 : 24;
  function rememberView() {
    if (!renderer?.transform) return;
    views.delete(activeURL); views.set(activeURL,{...renderer.transform});
    if(views.size>30) views.delete(views.keys().next().value);
    try {sessionStorage.setItem(viewStorageKey,JSON.stringify([...views]));} catch {}
  }
  function restore() {
    params = new URLSearchParams(location.search);
    if (!['graph','tree'].includes(params.get('view'))) params.set('view','graph');
    if (!['overview','group','neighbors','branch'].includes(params.get('mode'))) params.set('mode','overview');
    // Scope is expressed by direction and optional reading collection. Search locates nodes only.
    ['q','keyword','keywords_match','sort','group_by'].forEach(k => params.delete(k));
    params.set('group_by','category');
    if (!params.has('limit')) params.set('limit',defaultLimit());
    syncControls();
  }
  function syncControls() {
    const tree = params.get('view') === 'tree'; root.dataset.view = tree ? 'tree' : 'graph';
    root.querySelectorAll('[data-view]').forEach(b => b.setAttribute('aria-pressed',String(b.dataset.view === params.get('view'))));
    $('[data-cites]').checked = params.get('include_cites') === '1';
    $('[data-similar]').checked = params.get('include_similar') === '1';
    $('[data-context]').checked = params.get('include_context') === '1';
    $('[data-edge-limit]').value = params.get('edge_limit') || '40';
    $('[data-neighbor-limit]').value = params.get('mode') === 'neighbors' ? params.get('limit') : '5';
    $('[data-collection-scope]').value = params.get('collection') || '';
  }
  function navigate(changes, reset = false, replace = false) {
    rememberView();
    if (reset) { ['group_id','focus','offset','include_context','category'].forEach(k=>params.delete(k)); params.set('mode','overview'); params.set('limit',defaultLimit()); returnState = null; }
    Object.entries(changes).forEach(([k,v]) => { params.delete(k); (Array.isArray(v)?v:[v]).forEach(x=>{if(x!=='' && x!=null) params.append(k,x);}); });
    history[replace?'replaceState':'pushState']({},'',location.pathname+'?'+params); activeURL = location.search;
    syncControls(); load();
  }
  function feedback(message, state = 'ready') { $('[data-feedback]').textContent = message; $('.explorer-state').dataset.state = state; $('[data-retry]').hidden = state !== 'error'; }
  async function json(url, options = {}) {
    const response = await fetch(url,{credentials:'same-origin',...options}); const data = await response.json();
    if (!response.ok || data.status === 'error') throw new Error(data.message || data.error?.message || data.detail || '加载失败，请重试');
    return data;
  }
  function overviewParams() {
    const scope = new URLSearchParams({view:params.get('view'),mode:'overview',group_by:'category'});
    params.getAll('collection').forEach(c=>scope.append('collection',c)); return scope;
  }
  async function load() {
    dismissSelection();
    clearTimeout(timer); clearTimeout(detailTimer); request?.abort(); detailRequest?.abort(); locateRequest?.abort();
    $('[data-detail]').hidden = true; closeLocate();
    request = new AbortController(); const version = ++serial; root.setAttribute('aria-busy','true'); feedback('加载中','loading');
    try {
      const overviewQuery = overviewParams().toString(), cached = overviewCache.get(overviewQuery);
      if (params.get('mode') === 'overview') {
        payload = await json('/api/graph/explore?'+params,{signal:request.signal});
        overviewData = params.get('category') ? (cached || await json('/api/graph/explore?'+overviewQuery,{signal:request.signal})) : payload;
      } else {
        const responses = await Promise.all([
          json('/api/graph/explore?'+params,{signal:request.signal}),
          cached ? Promise.resolve(cached) : json('/api/graph/explore?'+overviewQuery,{signal:request.signal})
        ]); [payload,overviewData] = responses;
      }
      if (version !== serial) return;
      if (payload.status === 'building' || overviewData.status === 'building') { feedback('准备图谱','loading'); timer=setTimeout(load,Math.max(300,payload.retry_after_ms||700)); return; }
      overviewCache.set(overviewQuery,overviewData);
      if (autoScope && params.get('mode') === 'overview') {
        autoScope = false;
        if (params.get('view') === 'tree') {
          const largest = [...(payload.groups || [])].sort((a,b)=>b.count-a.count)[0];
          if (largest) { navigate({mode:'branch',group_id:largest.id,limit:12},false,true); return; }
        } else {
          const largest = topDirections()[0];
          if (largest) { navigate({mode:'group',category:largest.id,limit:24},false,true); return; }
        }
      }
      renderNavigation(); await render(payload);
      if (version !== serial) return;
      const updating = payload.stale || payload.rebuilding || overviewData.stale || overviewData.rebuilding;
      feedback(updating?'更新中':'已就绪',updating?'updating':'ready');
      if (updating) { overviewCache.delete(overviewQuery); timer=setTimeout(load,1500); }
    } catch(error) { if (error.name !== 'AbortError' && version === serial) feedback(error.message,'error'); }
    finally { if(version === serial) root.setAttribute('aria-busy','false'); }
  }
  const sortedGroups = () => [...(overviewData?.groups || [])].filter(g=>g.count>0).sort((a,b)=>b.count-a.count || String(a.label).localeCompare(String(b.label),'zh'));
  const directionParent = c => c?.parent || c?.id || '';
  function topDirections() {
    const categories = overviewData?.facets?.categories || [];
    const result = categories.filter(c=>c.level === 1 || !c.parent).map(c=>({...c}));
    return result.filter(c=>c.count>0).sort((a,b)=>b.count-a.count || String(a.label).localeCompare(String(b.label),'zh'));
  }
  function currentDirection() {
    const category = params.get('category');
    if (category) return (overviewData?.facets?.categories || []).find(c=>String(c.id)===category) || {id:category,label:category};
    const group = sortedGroups().find(g=>String(g.id)===params.get('group_id'));
    if (!group) return null;
    return (overviewData?.facets?.categories || []).find(c=>c.label === (group.parent || group.label)) || {id:group.parent||group.id,label:group.parent||group.label,parent:''};
  }
  function scopeButton(id,label,count,attr,active=false) { return '<button type="button" class="explorer-scope-button'+(active?' active':'')+'" '+attr+'="'+esc(id)+'" aria-pressed="'+active+'">'+esc(label)+(count==null?'':'<span>'+count+'</span>')+'</button>'; }
  function renderNavigation() {
    const current = currentDirection(), parent = directionParent(current), overview = params.get('mode') === 'overview';
    $('[data-directions]').innerHTML = scopeButton('','总览',null,'data-overview',overview&&!params.get('category')) + topDirections().map(c=>scopeButton(c.id,c.label,c.count,'data-direction',parent===c.id || parent===c.label)).join('');
    const children = (overviewData?.facets?.categories || []).filter(c=>c.parent && (c.parent===parent || c.parent===current?.label) && c.id!==parent && c.count>0).sort((a,b)=>b.count-a.count);
    $('[data-subdirections]').hidden = !children.length;
    $('[data-subdirections]').innerHTML = children.map(c=>scopeButton(c.id,c.label,c.count,'data-direction',params.get('category')===String(c.id))).join('');
    const collections = (overviewData?.facets?.collections || []).filter(c=>c.id!=='uncollected' && c.count>0);
    $('[data-collection-control]').hidden = !collections.length;
    $('[data-collection-scope]').innerHTML = '<option value="">全部集合</option>'+collections.map(c=>'<option value="'+esc(c.id)+'">'+esc(c.label)+'</option>').join('');
    $('[data-collection-scope]').value = params.get('collection') || '';
  }
  function openDirection(id) {
    autoScope=false;
    returnState=null;
    navigate({category:id,mode:params.get('view')==='tree'?'overview':'group',group_id:'',focus:'',offset:'',limit:defaultLimit()});
  }
  function aggregateGraph() {
    const tree=params.get('view')==='tree', category=params.get('category');
    const groups=tree?[...(payload?.groups || [])].sort((a,b)=>b.count-a.count):topDirections();
    return {scope:'library',mode:'aggregate',revision:overviewData.revision,nodes:groups.map((g,i)=>({
      id:'aggregate:'+g.id,role:'paper',label:g.label,title:g.label,sort_key:String(i).padStart(6,'0'),
      meta:{aggregate:true,count:g.count,group_id:g.id,branch:{key:tree?(g.parent||g.id):g.id,label:tree?g.parent:g.label},scope_label:category||'总览'}
    })),edges:[],scope_label:tree?(currentDirection()?.label||'研究主线'):'研究方向总览'};
  }
  async function render(data) {
    const version = serial;
    const overview=params.get('mode')==='overview', tree=params.get('view')==='tree';
    const graphData=overview?aggregateGraph():{...data.graph,boundary:data.boundary||[],omitted:data.omitted||0,scope_label:data.scope_label,mode:params.get('mode')};
    $('[data-scope-title]').textContent=overview?(currentDirection()?.label || '研究方向总览'):(!tree && params.get('category') ? currentDirection()?.label : data.scope_label) || '全部论文';
    $('[data-summary]').textContent=overview?(tree?graphData.nodes.length+' 条主线 · '+(data.shown||0)+' 篇':(data.total||0)+' 篇'):((data.shown||0)+(data.omitted?' / '+data.total:'')+' 篇');
    $('[data-back]').hidden=!returnState;
    $('[data-branch-label-form]').hidden=params.get('mode')!=='branch'; $('[data-branch-label]').value=data.scope_label||''; $('[data-branch-label-feedback]').textContent='';
    $('[data-group-edges]').hidden=overview||tree||params.get('mode')==='neighbors'; $('[data-similar]').parentElement.hidden=overview||tree;
    $('[data-neighbor-control]').hidden=params.get('mode')!=='neighbors'; $('[data-context-control]').hidden=overview||!tree;
    $('[data-snapshot]').textContent=data.generated_at?'更新于 '+new Date(data.generated_at).toLocaleString('zh-CN',{hour12:false}):'';
    const graphRoot=$('[data-renderer]'); graphRoot.dataset.exportCaption=$('[data-scope-title]').textContent; graphRoot.dataset.aggregate=String(overview);
    if(!window.CarrelGraph?.mount) throw new Error('图谱渲染器尚未就绪');
    renderer=window.CarrelGraph.mount(graphRoot,graphData,graphOptions({include_cites:params.get('include_cites')==='1',include_similar:params.get('include_similar')==='1',edge_limit:Number(params.get('edge_limit')||40)}));
    const mounted=renderer; await renderer.ready; if(mounted!==renderer || version!==serial) return;
    renderer.layers.cites = params.get('mode') === 'neighbors' || params.get('include_cites') === '1';
    renderer.layers.similar = params.get('include_similar') === '1';
    renderer.applyEdgeVisibility();
    const saved=views.get(activeURL); if(saved) { renderer.transform={...saved}; renderer.applyTransform(); }
    const remaining=Math.max(0,(data.total||0)-Number(params.get('offset')||0)-(data.shown||0));
    $('[data-more]').hidden=overview||!remaining||params.get('mode')==='neighbors'; $('[data-more]').textContent='展开更多（'+remaining+' 篇）';
    $('[data-boundary]').innerHTML=(data.boundary?.length?'<button class="btn tiny" type="button" data-show-context>补充 '+data.boundary.length+' 篇前驱</button>':'');
    $('[data-legend]').innerHTML=overview?'<span>'+(tree?'点击主线展开':'点击方向展开')+'</span>':tree?'<span class="explorer-legend-line"></span><span>前驱 → 改进 / 扩展</span>':'<span>按研究方向分区</span>'+(params.get('include_cites')==='1'?'<span class="explorer-legend-line"></span><span>引用 → 被引用</span>':'');
    renderMessage();
    if(!graphData.nodes.length) renderer.setStatus(tree?'当前方向暂无可展示的语义脉络':'当前范围暂无论文');
    if(pendingLocate) { const id=pendingLocate; pendingLocate=''; centerPaper(id); }
  }
  function renderMessage() {
    const stats=overviewData?.stats||{}, tasks=stats.tasks||{}, tree=params.get('view')==='tree';
    const excluded=tree?(stats.excluded||0):0, notes=overviewData?.notes||[], issue=tasks.pending||tasks.failed||excluded||stats.tree_error||stats.tree_rebuilding||notes.length;
    $('[data-processing-message]').hidden=!tree&&!issue;
    $('[data-processing-summary]').textContent=tree?'脉络依据论文语义整理'+(issue?' · 部分内容暂未纳入':''):'部分论文尚未完成处理';
    const items=[];
    if(tree) items.push('按论文声明的动机与方法判断递进关系，每篇论文只保留主要前驱。没有可靠关系的论文暂不进入脉络图。');
    if(excluded) items.push(excluded+' 篇论文尚未形成可靠主线。');
    if(tasks.pending||tasks.failed) items.push('待处理 '+(tasks.pending||0)+' 项 · 失败 '+(tasks.failed||0)+' 项。');
    if(stats.tree_rebuilding) items.push('语义脉络正在更新，当前图仍可浏览。');
    if(stats.tree_error) items.push(stats.tree_error);
    items.push(...notes.map(readable));
    $('[data-processing-details]').replaceChildren(...[...new Set(items)].map(text=>{const p=document.createElement('p');p.textContent=text;return p;}));
  }
  function select(id) {
    const node=renderer?.nodes.get(id);
    if(node?.meta.aggregate) { autoScope=false; if(params.get('view')==='tree') navigate({mode:'branch',group_id:node.meta.group_id,focus:'',limit:12}); else openDirection(node.meta.group_id); return; }
    if(!id || locating) return;
    showDetail(node?.meta.paper_id||id);
    if(params.get('view')==='graph') showCitations(node?.meta.paper_id||id);
  }
  function centerPaper(id) {
    const node=[...(renderer?.nodes.values()||[])].find(n=>!n.meta.aggregate&&(n.id===id||n.meta.paper_id===id)); if(!node||node.hidden) return false;
    locating=true; renderer.select(node.id); locating=false;
    const rect=renderer.canvas.getBoundingClientRect(); const k=Math.min(1.15,Math.max(0.85,renderer.transform.k));
    renderer.transform={k,x:rect.width/2-node.x*k,y:rect.height/2-node.y*k}; renderer.userZoomed=true; renderer.applyTransform();
    $('[data-locate-feedback]').textContent='已定位 '+node.label; return true;
  }
  function closeLocate() { $('[data-locate-results]').hidden=true; $('[data-locate-input]').setAttribute('aria-expanded','false'); }
  async function findPapers() {
    locateRequest?.abort(); const version=++locateSerial, term=$('[data-locate-input]').value.trim();
    locateHits=[]; if(!term) {closeLocate();return;}
    locateRequest=new AbortController();
    $('[data-locate-results]').hidden=false; $('[data-locate-input]').setAttribute('aria-expanded','true'); $('[data-locate-results]').textContent='正在定位…';
    try {
      const query=new URLSearchParams({q:term,view:params.get('view'),limit:'8'});
      if(params.get('collection')) query.set('collection',params.get('collection'));
      const result=await json('/api/graph/locate?'+query,{signal:locateRequest.signal}); if(version!==locateSerial) return;
      locateHits=result.matches||[];
      $('[data-locate-results]').innerHTML=locateHits.map((p,i)=>'<button type="button" data-locate-hit="'+i+'"><strong>'+esc(p.label)+'</strong><small>'+esc(p.title)+'</small><span>'+esc(p.direction||'')+'</span></button>').join('')||'<p>'+(result.status==='building'?'图谱正在准备，请稍后定位':'没有匹配的论文')+'</p>';
    } catch(error) {if(error.name!=='AbortError'&&version===locateSerial) $('[data-locate-results]').textContent=error.message;}
  }
  async function locatePaper(hit) {
    if(!hit) return; closeLocate(); $('[data-locate-input]').value=hit.label;
    if(centerPaper(hit.paper_id)) return;
    returnState=new URLSearchParams(params); pendingLocate=hit.paper_id;
    if(params.get('view')==='tree') {
      if(!hit.branch_id) {pendingLocate='';$('[data-locate-feedback]').textContent='这篇论文尚未形成语义主线';return;}
      navigate({mode:'branch',group_id:hit.branch_id,focus:hit.paper_id,category:'',offset:'',limit:12});
    } else navigate({mode:'neighbors',group_id:'',focus:hit.paper_id,category:'',offset:'',limit:5});
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
      $('[data-detail-body]').innerHTML = `<h2>${esc(node.title || meta.title || node.label || '论文详情')}</h2><a class="btn" href="/papers/${encodeURIComponent(id)}">打开论文</a><button type="button" class="btn" data-related="${esc(id)}">相关论文</button><form data-label-form><label>图中展示名<input name="label" maxlength="60" value="${esc(node.label || meta.label || '')}"></label><button class="btn">保存展示名</button><p data-label-feedback role="status"></p></form><h3>阅读集合</h3>${collections.map(c => `<button class="btn tiny" data-collection="${esc(c.id)}">${esc(c.name || c.label || '阅读集合')}</button>`).join('') || '<p>未加入集合</p>'}<h3>关键词</h3><p>${tags.map(t => esc(typeof t === 'string' ? t : t.label || t.tag)).join(' · ') || '暂无关键词'}</p><h3>关系与证据</h3>${relationHTML}${data.motivation || meta.motivation ? `<h3>动机</h3><pre>${esc(readable(data.motivation || meta.motivation))}</pre>` : ''}`;
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
  $('[data-branch-label-form]').onsubmit=async event=>{
    event.preventDefault(); const button=event.target.querySelector('button');button.disabled=true;
    try {await json('/api/graph/branch-label',{method:'POST',headers:{'Content-Type':'application/json','X-Carrel':'1'},body:JSON.stringify({branch_id:params.get('group_id'),label:$('[data-branch-label]').value.trim()})});overviewCache.clear();await load();$('[data-branch-label-feedback]').textContent='已保存';}
    catch(error){$('[data-branch-label-feedback]').textContent=error.message;}finally{button.disabled=false;}
  };
  root.addEventListener('click',e=>{
    const b=e.target.closest('button');if(!b)return;
    if(b.hasAttribute('data-view')) {autoScope=true;returnState=null;const direction=params.get('category');navigate({view:b.dataset.view,category:direction},true);}
    if(b.hasAttribute('data-overview')) {autoScope=false;navigate({},true);}
    if(b.hasAttribute('data-direction')) openDirection(b.dataset.direction);
    if(b.hasAttribute('data-locate-hit')) locatePaper(locateHits[Number(b.dataset.locateHit)]);
    if(b.dataset.related) {returnState=new URLSearchParams(params);navigate({mode:'neighbors',focus:b.dataset.related,group_id:'',category:'',offset:'',limit:5});}
    if(b.hasAttribute('data-show-context')) navigate({include_context:'1'});
    if(b.dataset.collection) {autoScope=true;navigate({collection:b.dataset.collection},true);}
  });
  $('[data-collection-scope]').onchange=e=>{autoScope=true;navigate({collection:e.target.value},true);};
  $('[data-back]').onclick=()=>{if(returnState){const previous=returnState;returnState=null;params=previous;navigate({});}else navigate({},true);};
  $('[data-more]').onclick=()=>{const limit=Number(params.get('limit')||defaultLimit());navigate(limit>=200?{offset:Number(params.get('offset')||0)+200,limit:defaultLimit()}:{limit:Math.min(200,limit+defaultLimit())});};
  $('[data-neighbor-limit]').onchange=e=>navigate({limit:e.target.value});
  $('[data-cites]').onchange=e=>navigate({include_cites:e.target.checked?'1':'0'});
  $('[data-similar]').onchange=e=>navigate({include_similar:e.target.checked?'1':'0'});
  $('[data-edge-limit]').onchange=e=>navigate({edge_limit:e.target.value});
  $('[data-context]').onchange=e=>navigate({include_context:e.target.checked?'1':'0'});
  $('[data-detail-close]').onclick=closeDetail;
  $('[data-retry]').onclick=load;
  $('[data-refresh]').onclick=async()=>{try{await json('/api/graph/refresh',{method:'POST',headers:{'X-Carrel':'1'}});overviewCache.clear();load();}catch(error){feedback(error.message,'error');}};
  root.querySelectorAll('[data-graph-export]').forEach(b=>b.onclick=()=>renderer?.exportGraph(b.dataset.graphExport,b.dataset.exportScope==='viewport'));
  $('[data-locate-input]').oninput=()=>{clearTimeout(locateTimer);locateRequest?.abort();locateSerial++;locateHits=[];closeLocate();locateTimer=setTimeout(findPapers,180);};
  $('[data-locate-input]').onkeydown=e=>{if(e.key==='Escape')closeLocate();if(e.key==='ArrowDown'){$('[data-locate-hit]')?.focus();e.preventDefault();}};
  $('[data-locate-form]').onsubmit=async e=>{e.preventDefault();clearTimeout(locateTimer);if(!locateHits.length)await findPapers();if(locateHits.length)locatePaper(locateHits[0]);};
  document.addEventListener('click',e=>{if(!e.target.closest('.explorer-locate'))closeLocate();});
  window.addEventListener('popstate',()=>{rememberView();activeURL=location.search;autoScope=!new URLSearchParams(location.search).has('mode');restore();load();});
  window.addEventListener('pagehide',()=>{rememberView();cancelCitations();clearTimeout(timer);clearTimeout(detailTimer);clearTimeout(locateTimer);request?.abort();detailRequest?.abort();locateRequest?.abort();renderer?.destroy();});
  document.addEventListener('carrel:organization-changed',()=>{overviewCache.clear();load();});
  restore();load();
})();
