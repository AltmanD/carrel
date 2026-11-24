/* One processing selector shared by import, settings and later upgrades. */
(() => {
  const e = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const scopes = ['none', 'key', 'core', 'all'];
  const flags = ['translate', 'classify', 'assess', 'motivation', 'relations'];
  const descriptions = {indexed: '保存标题与来源', skim: '关键笔记 · 总结 · 进图', notes: '全部核心章节笔记', full: '核心章节笔记 + 翻译'};
  const money = v => '$' + Number(v || 0).toFixed(v > 1 ? 2 : 3);
  const costText = v => `约 ${Number(v.tokens || 0).toLocaleString()} tokens / ${money(v.usd)}`;
  async function get(url) {
    const response = await fetch(url);
    const data = await response.json();
    if (!response.ok) throw Error(data.message || '读取失败');
    return data;
  }
  function union(a, b) {
    const profile = {...b, extract: scopes[Math.max(scopes.indexOf(a.extract), scopes.indexOf(b.extract))]};
    flags.forEach(key => profile[key] = a[key] || b[key]);
    return profile;
  }
  const equal = (a,b) => ['extract', ...flags].every(k => a[k] === b[k]);

  class Selector {
    constructor(root, data, initial) {
      this.root = root; this.data = data; this.value = {...(initial || data.default)};
      root.classList.add('processing-selector');
      root.innerHTML = `<div class="processing-heading"><strong>处理深度</strong><span class="badge" data-depth-label></span></div>
        <div class="processing-presets" role="group" aria-label="处理深度">${data.presets.map(p => `<button type="button" data-depth="${p.id}"><strong>${p.label}</strong><span>${descriptions[p.id]}</span><small>${costText(p.estimate)}</small></button>`).join('')}</div>
        <details class="processing-advanced"><summary>调整处理内容</summary><label class="processing-scope">笔记范围 <select data-extract><option value="none">仅收录，不解析</option><option value="key">摘要、引言、结论</option><option value="core">核心章节（含方法与实验）</option><option value="all">所有章节（含附录）</option></select><small data-option-cost="extract"></small></label>
        <div class="processing-options">${[
          ['translate','中文翻译','翻译所选范围，含术语统一与复核'],['classify','分类与标签','用于分类图及论文关系图'],
          ['assess','阅读评估','帮助判断阅读优先级'],['motivation','研究动机','用于研究脉络，需要关键章节笔记'],
          ['relations','引用与相似论文','不消耗模型 token；查询文献服务需要时间']
        ].map(([key,label,hint])=>`<label><input type="checkbox" data-option="${key}"><span><strong>${label}</strong><small>${hint}</small><small data-option-cost="${key}"></small></span></label>`).join('')}</div></details>
        <p class="processing-impact" role="status" aria-live="polite"></p><p class="dim small-text">费用按本库历史调用估算，无记录时使用参考值；实际随篇幅、模型与重试变化。升级复用已有内容。</p>`;
      root.querySelectorAll('[data-depth]').forEach(b => b.addEventListener('click', () => {this.value = union(data.current, data.presets.find(p=>p.id === b.dataset.depth).profile); this.render();}));
      root.querySelector('[data-extract]').addEventListener('change', event => {
        this.value.extract = event.target.value;
        if (this.value.extract === 'none') flags.forEach(k => this.value[k] = false);
        this.render();
      });
      root.querySelectorAll('[data-option]').forEach(box=>box.addEventListener('change',()=>{
        this.value[box.dataset.option] = box.checked;
        if (box.checked && box.dataset.option !== 'relations' && this.value.extract === 'none') this.value.extract = 'key';
        this.render();
      }));
      this.render();
    }
    estimate() {
      const p = union(this.data.current, this.value), covered = new Set(this.data.coverage), fraction = {none:0,key:.22,core:1,all:1.5};
      const weights = {}, expanding = p.extract !== 'none' && !covered.has('extract_' + p.extract);
      if (p.extract !== 'none') {
        if (!covered.size) { weights.preview = 1; weights.title = 1; }
        if (expanding) weights.extract = Math.max(0, fraction[p.extract] - Math.max(0, ...scopes.filter(s=>covered.has('extract_'+s)).map(s=>fraction[s])));
        if (expanding || !covered.has('summary')) weights.summarize = 1;
      }
      [['classify','classified'],['assess','assessed'],['motivation','motivation']].forEach(([key,cap])=>{ if (p[key] && (!covered.has(cap) || key === 'motivation' && expanding)) weights[key] = 1; });
      if (p.translate && !covered.has('translated_'+p.extract)) {
        const existing = Math.max(0, ...scopes.filter(s=>covered.has('translated_'+s)).map(s=>fraction[s]));
        weights.translate = Math.max(0, fraction[p.extract] - existing);
        if (!existing) weights.glossary = 1;
        weights.term_review = fraction[p.extract];
      }
      return Object.entries(weights).reduce((sum,[task,w])=>({tokens: Math.round(sum.tokens + this.data.tasks[task].tokens*w),usd:sum.usd+this.data.tasks[task].usd*w}),{tokens:0,usd:0});
    }
    render() {
      this.value = union(this.data.current, this.value);
      const match = this.data.presets.find(p=>equal(p.profile,this.value));
      this.root.querySelector('[data-depth-label]').textContent = match?.label || '自定义';
      this.root.querySelectorAll('[data-depth]').forEach(b=>{const on = b.dataset.depth === match?.id; b.classList.toggle('on',on); b.setAttribute('aria-pressed',String(on));});
      this.root.querySelector('[data-extract]').value = this.value.extract;
      this.root.querySelectorAll('[data-option]').forEach(b=>{b.checked = this.value[b.dataset.option]; b.disabled = this.data.current[b.dataset.option];});
      this.root.querySelectorAll('[data-extract] option').forEach(o=>o.disabled = scopes.indexOf(o.value) < scopes.indexOf(this.data.current.extract));
      this.root.querySelectorAll('[data-option-cost]').forEach(node=>{
        const key = node.dataset.optionCost, task = this.data.tasks[key];
        node.textContent = task ? costText(task) + (key === 'extract' ? '（核心章节参考）' : '') : '0 模型 tokens';
      });
      const p = union(this.data.current, this.value);
      const impact = !p.classify ? '未启用分类：不会进入分类图与动机脉络。' : !p.motivation ? '未启用动机：不会进入动机脉络。' : '可进入分类图与动机脉络，实际以已完成内容为准。';
      this.root.querySelector('.processing-impact').textContent = `${costText(this.estimate())} · ${impact}`;
      this.root.dispatchEvent(new CustomEvent('profilechange', {bubbles:true}));
    }
  }
  async function mount(root, paper='', initial=null) { const data = await get('/api/processing-profiles' + (paper ? '?paper='+encodeURIComponent(paper) : '')); return new Selector(root,data,initial); }
  function dialog(title) {
    const d = document.createElement('dialog'); d.className = 'edit-dialog processing-dialog';
    d.innerHTML = `<div class="dialog-head"><h2>${e(title)}</h2><button class="icon-btn" type="button" data-dismiss aria-label="关闭">×</button></div><div data-body></div><p class="form-error" role="alert" hidden></p><div class="processing-dialog-actions"></div>`;
    document.body.append(d); d.querySelector('[data-dismiss]').onclick = () => d.close(); d.addEventListener('close',()=>d.remove());
    return d;
  }
  async function upgrade(paper, depth) {
    const d = dialog('选择处理内容'); const root = d.querySelector('[data-body]');
    try {
      const data = await get('/api/processing-profiles?paper='+encodeURIComponent(paper));
      const selected = depth === 'custom' ? data.current : data.presets.find(p=>p.id===depth)?.profile || data.default;
      const selector = new Selector(root,data,union(data.current,selected));
      const submit = document.createElement('button'); submit.className = 'btn primary'; submit.textContent = '开始处理';
      d.querySelector('.processing-dialog-actions').append(submit);
      submit.onclick = async () => {
        submit.disabled = true;
        try {
          await api(`/api/papers/${encodeURIComponent(paper)}/retry`, {profile: selector.value});
          d.close();
          location.reload();
        } catch (error) {
          const msg = d.querySelector('.form-error');
          msg.hidden = false;
          msg.textContent = error.message;
          submit.disabled = false;
        }
      };
      d.showModal();
    } catch(error) { d.remove(); toast(error.message,{error:true}); }
  }
  async function graphChoice(graph, rebuild=false) {
    const report = await get('/api/graph-readiness?graph='+graph);
    if (!report.missing.length) return {action:'ready'};
    return new Promise(resolve=>{
      const d = dialog('图谱准备情况'); let result = {action:'cancel'};
      d.querySelector('[data-body]').innerHTML = `<p>${report.eligible.length} 篇已就绪，${report.missing.length} 篇尚缺少下列内容。翻译不影响进图。</p><div class="readiness-list">${report.missing.map(p=>`<label><input type="checkbox" value="${e(p.paper_id)}"><span><strong>${e(p.title)}</strong><small>缺少：${e(p.reasons.join('、'))} · ${costText({tokens:p.est_tokens,usd:p.est_usd})}</small></span></label>`).join('')}</div>`;
      const actions = d.querySelector('.processing-dialog-actions');
      if (rebuild) {
        const use = document.createElement('button');
        use.className = 'btn primary';
        use.textContent = `仅用已就绪的 ${report.eligible.length} 篇整理`;
        use.onclick = () => { result = {action: 'ready'}; d.close(); };
        actions.append(use);
      }
      const fill = document.createElement('button');
      fill.className = 'btn';
      actions.append(fill);
      const selectedIds = () => [...d.querySelectorAll('.readiness-list input:checked')].map(node => node.value);
      function update() {
        const ids = new Set(selectedIds());
        const rows = report.missing.filter(paper => ids.has(paper.paper_id));
        const cost = {tokens: rows.reduce((sum, paper) => sum + paper.est_tokens, 0),
          usd: rows.reduce((sum, paper) => sum + paper.est_usd, 0)};
        fill.disabled = !rows.length;
        fill.textContent = `补齐 ${rows.length} 篇（${costText(cost)}）${rebuild ? '后整理' : ''}`;
      }
      d.querySelectorAll('input').forEach(box => box.onchange = update);
      update();
      fill.onclick = async () => {
        fill.disabled = true;
        try {
          const batch = await api('/api/graph-fill', {graph, paper_ids: selectedIds()});
          result = {action: 'fill', ...batch};
          d.close();
          toast('已提交补齐任务，完成后自动更新图谱');
        } catch (error) {
          const msg = d.querySelector('.form-error');
          msg.textContent = error.message;
          msg.hidden = false;
          update();
        }
      };
      const cancel = document.createElement('button');
      cancel.className = 'btn ghost';
      cancel.textContent = '取消';
      cancel.onclick = () => d.close();
      actions.append(cancel);
      d.addEventListener('close', () => resolve(result));
      d.showModal();
    });
  }
  async function refreshReadiness(root) {
    const report = await get('/api/graph-readiness?graph=' + root.dataset.graphScope);
    root.querySelector('.readiness-banner')?.remove();
    if (!report.missing.length) return;
    const bar = document.createElement('div');
    bar.className = 'readiness-banner';
    const text = document.createElement('span');
    const reasons = [...new Set(report.missing.flatMap(paper => paper.reasons))];
    text.textContent = `${report.missing.length} 篇未纳入 · 缺少${reasons.join(' / ')}`;
    const button = document.createElement('button');
    button.className = 'btn tiny';
    button.textContent = '查看 / 补齐';
    button.onclick = async () => {
      try {
        const result = await graphChoice(root.dataset.graphScope);
        if (result.action !== 'fill') return;
        const poll = async () => {
          if (!root.isConnected) return;
          try {
            const batch = await get('/api/batches/' + result.batch_id);
            if (!batch.finalized) { setTimeout(poll, 3000); return; }
            await refreshReadiness(root);
            const graph = window.CarrelGraph?.last;
            if (graph?.root === root) await graph.reload();
            if (batch.warning) toast(batch.warning, {error: true});
          } catch (error) { toast(error.message + '，请到任务页查看', {error: true}); }
        };
        setTimeout(poll, 3000);
      } catch (error) { toast(error.message, {error: true}); }
    };
    bar.append(text, button);
    root.prepend(bar);
  }
  window.CarrelProcessing = {mount, upgrade, graphChoice, refreshReadiness};
  document.querySelectorAll('[data-processing-picker]').forEach(async root => {
    try {
      root.selector = await mount(root);
      if (root.dataset.processingPicker === 'settings') {
        const form = root.closest('form');
        form.querySelector('[type=submit]').disabled = false;
        form.addEventListener('submit', async event => {
          event.preventDefault(); busy(form, true);
          try {
            await api('/api/preferences/processing', {profile: root.selector.value});
            toast('已保存默认处理深度');
          } catch (error) { toast(error.message, {error: true}); }
          finally { busy(form, false); root.selector.render(); }
        });
      }
    } catch (error) { root.textContent = error.message; }
  });
  document.addEventListener('click', event=>{const button=event.target.closest('[data-upgrade]');if(button)upgrade(button.dataset.paper,button.dataset.upgrade);});
  document.querySelectorAll('[data-graph-scope]').forEach(root => {
    // Explorer statistics already come from the background projection. Running
    // full-library coverage here would undo its lazy loading. Explicit filling
    // still uses graphChoice when the user asks to rebuild.
    if (root.closest('[data-graph-explorer]')) return;
    refreshReadiness(root).catch(error => console.warn('Graph readiness:', error.message));
  });
})();
