function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
function icon(name) { return `<i data-lucide="${name}" class="icon" aria-hidden="true"></i>`; }
function refreshIcons() { window.lucide?.createIcons(); }
function preference(key, value) {
  try { if (value !== undefined) localStorage.setItem(key, value); return localStorage.getItem(key); } catch { return null; }
}
function normalizeTex(source) {
  // MinerU occasionally attaches a delimiter size command to an operator
  // (for example `\\Bigg\\sqcap`). KaTeX rejects that combination,
  // while the operator itself is valid and should remain visible.
  return String(source ?? '')
    .replace(/\\Bigg\s*\\sqcap\b/g, '\\sqcap')
    .replace(/\\Bigg\s*\\sqcup\b/g, '\\sqcup');
}
function renderMath(root = document) {
  if (!window.renderMathInElement || !window.katex || !window.markdownit || !window.texmath) return;
  const options = { throwOnError: false, trust: false, strict: 'ignore', maxSize: 20, maxExpand: 1000 };
  const parser = window.markdownit('zero').enable('escape').use(window.texmath, {
    engine: window.katex, delimiters: ['dollars', 'brackets'], katexOptions: options,
  });
  // MinerU leaves whitespace inside dollar delimiters. Stop at the first
  // unescaped closing dollar so adjacent formulas cannot consume their prose.
  const dollarRule = window.texmath.rules.dollars.inline.find((rule) => rule.tag === '$');
  parser.inline.ruler.at('math_inline', window.texmath.inline({
    ...dollarRule, rex: /\$((?:\\[\s\S]|[^\\$])+?)\$/gy,
  }));
  for (const rule of ['math_inline', 'math_inline_double']) {
    parser.renderer.rules[rule] = (tokens, index) => window.texmath.render(normalizeTex(tokens[index].content.trim()), rule === 'math_inline_double', options);
  }
  root.querySelectorAll('[data-math-display]').forEach((element) => {
    if (element.dataset.mathRendered) return;
    let source = normalizeTex(element.textContent.trim());
    for (const [left, right] of [['$$', '$$'], ['\\[', '\\]'], ['\\(', '\\)'], ['$', '$']]) {
      if (source.startsWith(left) && source.endsWith(right)) {
        source = source.slice(left.length, -right.length).trim();
        break;
      }
    }
    window.katex.render(source, element, { ...options, displayMode: true });
    element.dataset.mathRendered = 'true';
  });
  root.querySelectorAll('[data-math]').forEach((element) => {
    if (element.dataset.mathRendered) return;
    window.renderMathInElement(element, {
      ...options,
      delimiters: [
        { left: '\\[', right: '\\]', display: true },
        ...['equation', 'equation*', 'align', 'align*', 'gather', 'gather*'].map((name) => ({
          left: `\\begin{${name}}`, right: `\\end{${name}}`, display: true,
        })),
      ],
      ignoredClasses: ['katex', 'katex-error'],
    });
    // Parse each source block separately; escaped currency must not open math.
    const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
    const nodes = [];
    while (walker.nextNode()) {
      const node = walker.currentNode;
      if (/[$\\]/.test(node.textContent) && !node.parentElement.closest('.katex, .katex-error, [data-math-rendered], code, pre, script, style, textarea')) nodes.push(node);
    }
    for (const node of nodes) {
      const span = document.createElement('span');
      span.dataset.mathRendered = 'true';
      // Captions use ($) and its full-width variant as a currency unit.
      const source = normalizeTex(node.textContent.replace(/([\uFF08(])\$(?=[\uFF09)])/g, '$1\\$'));
      span.innerHTML = parser.renderInline(source);
      node.replaceWith(span);
    }
    element.dataset.mathRendered = 'true';
  });
}
renderMath();
async function api(url, body) {
  const response = await fetch(url, body === undefined ? {} : {
    method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Carrel': '1' }, body: JSON.stringify(body),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error([data.message || data.detail?.message || `操作失败（${response.status}），请重试`, data.hint].filter(Boolean).join('。'));
  return data;
}
function showError(form, error) {
  const el = form.querySelector('.form-error') || document.getElementById(`${form.id.replace('-form', '')}-error`);
  if (el) { el.textContent = error?.message || '保存失败，请检查连接后重试'; el.hidden = false; }
  else toast(error?.message || '操作失败，请重试', { error: true });
}
document.querySelectorAll('[data-title-editor]').forEach(form => {
  async function updateTitle(action) {
    form.querySelectorAll('button').forEach(button => { button.disabled = true; });
    form.setAttribute('aria-busy', 'true');
    const status = form.querySelector('.form-error');
    status.hidden = false; status.textContent = action === 'retry' ? '正在生成中文标题…' : '正在保存…';
    try {
      const data = await api('/api/papers/' + encodeURIComponent(form.dataset.titleEditor) + '/title', {
        action, title_zh: form.elements.title_zh.value,
      });
      if (data.status === 'accepted') location.reload();
      else status.textContent = data.issues?.join('；') || '暂未生成合格标题，可手动填写后保存。';
    } catch (error) { status.textContent = error.message; }
    finally {
      form.querySelectorAll('button').forEach(button => { button.disabled = false; });
      form.setAttribute('aria-busy', 'false');
    }
  }
  form.addEventListener('submit', event => { event.preventDefault(); updateTitle('save'); });
  form.querySelector('[data-title-retry]')?.addEventListener('click', () => updateTitle('retry'));
});
function busy(form, value) {
  form.dataset.busy = String(value);
  form.setAttribute('aria-busy', String(value));
  form.querySelectorAll('button[type="submit"]').forEach((button) => { button.disabled = value; });
  if (value) form.querySelectorAll('.form-error').forEach((el) => { el.hidden = true; });
}
function toast(message, options = {}) {
  const el = document.createElement('div');
  el.className = `toast${options.error ? ' error' : ''}`;
  el.innerHTML = `${icon(options.error ? 'circle-alert' : 'check')}<span>${escapeHtml(message)}</span>`;
  if (options.action) {
    const action = document.createElement('button');
    action.className = 'toast-action'; action.textContent = options.label || '撤销';
    action.addEventListener('click', async () => {
      action.disabled = true;
      try { await options.action(); el.remove(); } catch (error) { action.disabled = false; toast(error.message, { error: true }); }
    });
    el.append(action);
  }
  const close = document.createElement('button');
  close.className = 'icon-btn small'; close.setAttribute('aria-label', '关闭提示'); close.title = '关闭提示'; close.innerHTML = icon('x');
  close.onclick = () => el.remove(); el.append(close);
  document.getElementById('toast-region').append(el); refreshIcons();
  let timer;
  const schedule = () => { timer = setTimeout(() => el.remove(), options.action ? 15000 : 6000); };
  el.addEventListener('mouseenter', () => clearTimeout(timer));
  el.addEventListener('focusin', () => clearTimeout(timer));
  el.addEventListener('mouseleave', schedule); el.addEventListener('focusout', schedule); schedule();
}

const themeToggle = document.getElementById('theme-toggle');
function renderTheme() {
  const dark = document.documentElement.dataset.theme === 'dark';
  themeToggle.innerHTML = icon(dark ? 'sun' : 'moon');
  themeToggle.title = themeToggle.ariaLabel = dark ? '切换浅色主题' : '切换深色主题';
  refreshIcons();
}
themeToggle?.addEventListener('click', () => {
  const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  document.documentElement.dataset.theme = next; preference('carrel-theme', next); renderTheme();
});
if (themeToggle) renderTheme();
document.querySelectorAll('.nav-link').forEach((link) => { link.title = link.textContent.trim(); link.setAttribute('aria-label', link.textContent.trim()); });
document.querySelectorAll('[data-autosubmit]').forEach((input) => input.addEventListener('change', () => input.form.requestSubmit()));
const collection = document.getElementById('paper-collection');
const facetTabs = [...document.querySelectorAll('[data-facet]')];
function setFacet(name) {
  facetTabs.forEach(tab => {
    const selected = tab.dataset.facet === name;
    tab.setAttribute('aria-selected', String(selected));
    tab.tabIndex = selected ? 0 : -1;
    const panel = document.getElementById(tab.getAttribute('aria-controls'));
    if (panel) panel.hidden = !selected;
  });
  const input = document.querySelector('#library-search input[name="facet"]');
  if (input) input.value = name;
  const url = new URL(location.href);
  url.searchParams.set('facet', name);
  history.replaceState(null, '', url);
  document.querySelectorAll('.active-filters .chip, .clear-library-filters').forEach(link => {
    const target = new URL(link.href);
    target.searchParams.set('facet', name); link.href = target;
  });
}
facetTabs.forEach((tab, index) => {
  tab.addEventListener('click', () => setFacet(tab.dataset.facet));
  tab.addEventListener('keydown', event => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? facetTabs.length - 1 : (index + (event.key === 'ArrowRight' ? 1 : -1) + facetTabs.length) % facetTabs.length;
    setFacet(facetTabs[next].dataset.facet); facetTabs[next].focus();
  });
  tab.tabIndex = tab.getAttribute('aria-selected') === 'true' ? 0 : -1;
});
function setView(view) {
  if (collection) collection.dataset.view = view;
  document.querySelectorAll('button[data-view]').forEach((button) => {
    const selected = button.dataset.view === view;
    button.classList.toggle('on', selected); button.setAttribute('aria-pressed', String(selected));
  });
}
setView(preference('carrel-view') === 'grid' ? 'grid' : 'list');
document.querySelectorAll('button[data-view]').forEach((button) => button.addEventListener('click', () => { setView(button.dataset.view); preference('carrel-view', button.dataset.view); }));

function activateTab(name, updateHistory = true) {
  if (name === 'trans') name = 'pdf';
  if (!document.querySelector(`[data-tab="${name}"]`)) return;
  document.querySelectorAll('[role="tab"]').forEach((tab) => {
    const active = tab.dataset.tab === name;
    tab.classList.toggle('on', active); tab.setAttribute('aria-selected', String(active)); tab.tabIndex = active ? 0 : -1;
  });
  document.querySelectorAll('.pane').forEach((pane) => pane.classList.toggle('on', pane.dataset.pane === name));
  if (updateHistory) history.replaceState(null, '', `#${name}`);
  if (name === 'pdf') {
    ensurePdfViewer();
    // The reader needs the pane visible before measuring and attaching.
    window.CarrelReader?.activate?.();
  }
  // The paper graph lays out only once its pane has a real width.
  if (name === 'rel') window.CarrelGraph?.activate?.();
}
document.querySelector('[role="tablist"]')?.addEventListener('keydown', (event) => {
  const tabs = [...document.querySelectorAll('[role="tab"]')];
  const index = tabs.indexOf(document.activeElement);
  if (index < 0 || !['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
  event.preventDefault();
  const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : (index + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length;
  tabs[next].focus(); activateTab(tabs[next].dataset.tab);
});
document.addEventListener('click', (event) => {
  const tab = event.target.closest('[data-tab]');
  if (tab) { activateTab(tab.dataset.tab); return; }
  const page = event.target.closest('a[data-page]');
  if (page) { event.preventDefault(); activateTab('pdf'); ensurePdfViewer().then((viewer) => viewer?.goTo(Number(page.dataset.page))); return; }
  const section = event.target.closest('[data-section]');
  if (section) {
    event.preventDefault(); window.CarrelReader?.navigateTo(document.getElementById(section.dataset.section)); return;
  }
  const jump = event.target.closest('[data-jump]');
  if (jump) { event.preventDefault(); activateTab('pdf'); window.CarrelReader?.navigateTo(document.getElementById(jump.dataset.jump)); return; }
  if (event.target.closest('[data-organize]')) { activateTab('summary'); document.querySelector('.side-col')?.scrollIntoView({ behavior: 'smooth', block: 'start' }); }
  if (event.target.closest('[data-refresh]')) window.location.reload();
  const close = event.target.closest('[data-close-dialog]');
  if (close && close.closest('form')?.dataset.busy !== 'true') close.closest('dialog').close();
});
document.querySelectorAll('[data-relation-toggle]').forEach((button) => {
  const list = document.getElementById(button.getAttribute('aria-controls'));
  button.addEventListener('click', () => {
    const expanded = button.getAttribute('aria-expanded') !== 'true';
    list.querySelectorAll('[data-relation-extra]').forEach((item) => { item.hidden = !expanded; });
    button.setAttribute('aria-expanded', String(expanded));
    button.setAttribute('aria-label', expanded ? button.dataset.collapseLabel : button.dataset.expandLabel);
    button.querySelector('[data-relation-toggle-label]').textContent = expanded ? 'less' : 'more';
    if (!expanded) button.scrollIntoView({ block: 'nearest' });
  });
});

document.querySelectorAll('dialog').forEach((dialog) => {
  dialog.addEventListener('cancel', (event) => { if (dialog.querySelector('form')?.dataset.busy === 'true') event.preventDefault(); });
  dialog.addEventListener('click', (event) => {
    const rect = dialog.getBoundingClientRect();
    if (event.target === dialog && (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) && dialog.querySelector('form')?.dataset.busy !== 'true') dialog.close();
  });
});

// The database response is authoritative for both labels and provenance.
const tagEditor = document.getElementById('tag-editor');
if (tagEditor) {
  const endpoint = `/api/papers/${encodeURIComponent(tagEditor.dataset.paper)}/tags`;
  const addForm = document.getElementById('tag-add-form');
  const editForm = document.getElementById('tag-edit-form');
  const dialog = document.getElementById('tag-dialog');
  let editing = null;
  const existing = (label) => [...tagEditor.querySelectorAll('[data-tag]')].some((el) => el.dataset.tag === label);
  const tagData = (chip) => ({ tag: chip.dataset.tag, evidence: chip.dataset.evidence || '', source_block: chip.dataset.sourceBlock || '' });
  function renderTags(tags) {
    tagEditor.innerHTML = tags.map((tag) => `<span class="tag editable-tag" data-tag="${escapeHtml(tag.tag)}" data-origin="${escapeHtml(tag.origin)}" data-evidence="${escapeHtml(tag.evidence)}" data-source-block="${escapeHtml(tag.source_block)}"><a href="/?tag=${encodeURIComponent(tag.tag)}" title="${escapeHtml(tag.origin === 'human' ? '手工关键词' : '模型关键词')}${tag.evidence ? ' · ' + escapeHtml(tag.evidence) : ''}">${escapeHtml(tag.tag)}</a><button type="button" class="tag-action" data-edit-tag aria-label="编辑关键词 ${escapeHtml(tag.tag)}" title="编辑关键词">${icon('pencil')}</button><button type="button" class="tag-action" data-remove="${escapeHtml(tag.tag)}" aria-label="移除关键词 ${escapeHtml(tag.tag)}" title="移除关键词">${icon('x')}</button></span>`).join('');
    document.getElementById('tag-count').textContent = tags.length;
    document.getElementById('tags-empty').hidden = tags.length > 0;
    refreshIcons();
    document.dispatchEvent(new Event('carrel:organization-changed'));
  }
  let pendingMutation = Promise.resolve();
  function mutate(body) {
    const operation = pendingMutation.then(async () => {
      const data = await api(endpoint, body); renderTags(data.tags);
    });
    pendingMutation = operation.catch(() => {});
    return operation;
  }
  tagEditor.addEventListener('click', async (event) => {
    const chip = event.target.closest('[data-tag]'); if (!chip) return;
    if (event.target.closest('[data-edit-tag]')) {
      editing = tagData(chip); editForm.elements.tag.value = editing.tag;
      editForm.querySelector('.form-error').hidden = true; dialog.showModal(); editForm.elements.tag.select();
    }
    const remove = event.target.closest('[data-remove]');
    if (remove) {
      const removed = tagData(chip); remove.disabled = true;
      try { await mutate({ remove: [removed.tag] }); toast(`已移除「${removed.tag}」`, { action: async () => { await mutate({ add: [removed] }); toast('关键词已恢复'); } }); addForm.elements.tag.focus(); }
      catch (error) { remove.disabled = false; toast(error.message, { error: true }); }
    }
  });
  addForm.addEventListener('submit', async (event) => {
    event.preventDefault(); if (addForm.dataset.busy === 'true') return;
    const label = addForm.elements.tag.value.trim();
    if (!label) return;
    if (existing(label)) { showError(addForm, new Error('这篇论文已有此关键词')); return; }
    busy(addForm, true); document.getElementById('tag-add-error').hidden = true;
    try { await mutate({ add: [label] }); addForm.reset(); toast(`已添加「${label}」`); }
    catch (error) { showError(addForm, error); }
    finally { busy(addForm, false); addForm.elements.tag.focus(); }
  });
  editForm.addEventListener('submit', async (event) => {
    event.preventDefault(); if (!editing || editForm.dataset.busy === 'true') return;
    const label = editForm.elements.tag.value.trim();
    if (label === editing.tag) { dialog.close(); return; }
    if (!label) { showError(editForm, new Error('请填写关键词名称')); return; }
    if (existing(label)) { showError(editForm, new Error('这篇论文已有此关键词，请使用其他名称')); return; }
    busy(editForm, true);
    try { await mutate({ remove: [editing.tag], add: [{ ...editing, tag: label }] }); dialog.close(); toast('关键词已更新'); }
    catch (error) { showError(editForm, error); }
    finally { busy(editForm, false); }
  });
}

const taxonomyData = document.getElementById('taxonomy-data');
const taxonomyPaths = taxonomyData ? JSON.parse(taxonomyData.textContent) : [];
function bindCategorySuggestions(form) {
  if (!form || !taxonomyData) return;
  const fill = (id, values) => {
    const list = document.getElementById(id); if (!list) return;
    list.replaceChildren(...[...new Set(values.filter(Boolean))].map((value) => { const option = document.createElement('option'); option.value = value; return option; }));
  };
  const update = () => {
    fill('discipline-options', taxonomyPaths.map((path) => path.discipline));
    fill('field-options', taxonomyPaths.filter((path) => path.discipline === form.elements.discipline.value).map((path) => path.field));
  };
  form.addEventListener('input', update); form.addEventListener('categoryopen', update); update();
}
const catsEditor = document.getElementById('cats-edit');
if (catsEditor) {
  const form = document.getElementById('category-edit-form');
  const dialog = document.getElementById('category-dialog');
  let editingIndex = 0;
  function renderCategories(categories) {
    catsEditor.innerHTML = categories.map((cat, index) => `<div class="cat-line" data-index="${index}" data-discipline="${escapeHtml(cat.discipline)}" data-field="${escapeHtml(cat.field)}"><a href="/?${new URLSearchParams({ discipline: cat.discipline, field: cat.field })}" title="${escapeHtml([cat.discipline, cat.field].filter(Boolean).join(' / '))}">${icon('folder')}<span>${escapeHtml(cat.field || cat.discipline)}<small>${escapeHtml([cat.discipline].filter(Boolean).join(' / '))}</small></span></a><button class="icon-btn small" data-edit-category title="编辑分类" aria-label="编辑分类 ${escapeHtml(cat.field || cat.discipline)}">${icon('pencil')}</button></div>`).join('') || '<p class="dim small-text">未分类</p>';
    catsEditor.dataset.count = categories.length; refreshIcons();
    document.dispatchEvent(new Event('carrel:organization-changed'));
  }
  bindCategorySuggestions(form);
  document.addEventListener('click', (event) => {
    const edit = event.target.closest('[data-edit-category]');
    const add = event.target.closest('[data-add-category]'); if (!edit && !add) return;
    const line = edit?.closest('.cat-line');
    editingIndex = line ? Number(line.dataset.index) : Number(catsEditor.dataset.count);
    for (const key of ['discipline', 'field']) form.elements[key].value = line?.dataset[key] || '';
    form.querySelector('.form-error').hidden = true;
    document.getElementById('category-dialog-title').textContent = add ? '添加分类' : '编辑分类';
    document.getElementById('category-remove').hidden = !edit || editingIndex >= Number(catsEditor.dataset.count);
    form.dispatchEvent(new Event('categoryopen')); dialog.showModal();
  });
  form.addEventListener('submit', async (event) => {
    event.preventDefault(); if (form.dataset.busy === 'true') return;
    busy(form, true);
    const body = Object.fromEntries(new FormData(form)); body.index = editingIndex;
    try {
      const data = await api(`/api/papers/${encodeURIComponent(catsEditor.dataset.paper)}/category`, body);
      renderCategories(data.categories); dialog.close(); toast('分类已保存');
    } catch (error) { showError(form, error); }
    finally { busy(form, false); }
  });
  document.getElementById('category-remove').addEventListener('click', async () => {
    if (form.dataset.busy === 'true') return;
    busy(form, true);
    try {
      const data = await api(`/api/papers/${encodeURIComponent(catsEditor.dataset.paper)}/category`, { index: editingIndex, remove: true });
      renderCategories(data.categories); dialog.close(); toast('分类已移除');
    } catch (error) { showError(form, error); }
    finally { busy(form, false); }
  });
}
const categoryAddForm = document.getElementById('cat-add-form');
if (categoryAddForm) {
  bindCategorySuggestions(categoryAddForm);
  categoryAddForm.addEventListener('submit', async (event) => {
    event.preventDefault(); if (categoryAddForm.dataset.busy === 'true') return;
    busy(categoryAddForm, true);
    try { await api('/api/taxonomy', Object.fromEntries(new FormData(categoryAddForm))); window.location.reload(); }
    catch (error) { showError(categoryAddForm, error); busy(categoryAddForm, false); }
  });
}
const taxonomyEditForm = document.getElementById('taxonomy-edit-form');
if (taxonomyEditForm) {
  const dialog = document.getElementById('taxonomy-dialog');
  const input = document.getElementById('taxonomy-name');
  let source;
  document.addEventListener('click', (event) => {
    const edit = event.target.closest('[data-edit-taxonomy]');
    if (!edit || taxonomyEditForm.dataset.busy === 'true') return;
    source = Object.fromEntries(['discipline', 'field'].map((key) => [key, edit.dataset[key] || '']));
    const label = source.field ? '具体主题' : '研究方向';
    document.getElementById('taxonomy-dialog-title').textContent = `修改${label}`;
    document.getElementById('taxonomy-name-label').textContent = `${label}名称`;
    document.getElementById('taxonomy-context').textContent = Object.values(source).filter(Boolean).join(' / ');
    input.value = source.field || source.discipline;
    taxonomyEditForm.querySelector('.form-error').hidden = true;
    dialog.showModal(); input.focus(); input.select();
  });
  taxonomyEditForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (taxonomyEditForm.dataset.busy === 'true') return;
    const name = input.value.trim();
    if (!name) { showError(taxonomyEditForm, new Error('请填写分类名称')); return; }
    busy(taxonomyEditForm, true);
    try {
      await api('/api/taxonomy/rename', { source, name });
      window.location.reload();
    } catch (error) { showError(taxonomyEditForm, error); busy(taxonomyEditForm, false); }
  });
}

const progressEl = document.getElementById('pipe-progress');
if (progressEl) {
  let lastCounts = '';
  const tick = async () => {
    try {
      const data = await api(`/api/papers/${encodeURIComponent(progressEl.dataset.paper)}/progress`);
      for (const [stage, state] of Object.entries(data.stages)) {
        const el = progressEl.querySelector(`[data-stage="${stage}"]`); if (el) el.className = `pipe-stage ${state}`;
      }
      const tr = data.translations, mod = data.modules;
      document.getElementById('pipe-count').textContent = data.stages.translating === 'running' ? `翻译 ${tr.done}/${tr.total} 节` : data.stages.structuring === 'running' ? `整理 ${mod.done}/${mod.total} 节` : '';
      const signature = `${mod.done}/${mod.total}|${tr.done}/${tr.total}|${data.artifacts.preview}|${data.artifacts.summary}`;
      if (lastCounts && signature !== lastCounts) document.getElementById('pipe-refresh').hidden = false;
      lastCounts = signature;
      if (data.terminal) {
        // Let the reader refresh explicitly so in-progress edits and ink survive.
        progressEl.querySelector('.pipe-title').textContent = data.status === 'failed' ? '处理失败' : '处理已完成';
        document.getElementById('pipe-refresh').hidden = false; return;
      }
    } catch { /* The next poll retries a transient connection failure. */ }
    setTimeout(tick, 3000);
  }; tick();
}

let pdfLibraryPromise;
function loadPdfLibrary() {
  if (!pdfLibraryPromise) pdfLibraryPromise = new Promise((resolve, reject) => {
    const script = document.createElement('script'); script.src = '/static/vendor/pdf.min.js';
    script.onload = () => { window.pdfjsLib.GlobalWorkerOptions.workerSrc = '/static/vendor/pdf.worker.min.js'; resolve(window.pdfjsLib); };
    script.onerror = () => { pdfLibraryPromise = null; reject(new Error('PDF 阅读器加载失败')); };
    document.head.append(script);
  });
  return pdfLibraryPromise;
}
const covers = document.querySelectorAll('[data-cover]');
if (covers.length) {
  const queue = []; let active = 0;
  const work = async () => {
    if (active >= 2 || !queue.length) return;
    const cover = queue.shift(); active += 1;
    let pdf;
    try {
      const lib = await loadPdfLibrary();
      pdf = await lib.getDocument({ url: `/api/papers/${encodeURIComponent(cover.dataset.cover)}/pdf`, disableAutoFetch: true, disableStream: true, isEvalSupported: false }).promise;
      const page = await pdf.getPage(1), first = page.getViewport({ scale: 1 });
      const viewport = page.getViewport({ scale: 280 / first.width });
      const canvas = document.createElement('canvas'); canvas.width = viewport.width; canvas.height = viewport.height;
      canvas.setAttribute('aria-hidden', 'true');
      await page.render({ canvasContext: canvas.getContext('2d'), viewport }).promise;
      cover.append(canvas); cover.style.aspectRatio = `${first.width} / ${first.height}`;
    } catch { /* The document icon remains the cover when no preview is available. */ }
    finally { if (pdf) await pdf.destroy(); active -= 1; work(); }
  };
  const observer = new IntersectionObserver((entries) => {
    for (const entry of entries) if (entry.isIntersecting) { observer.unobserve(entry.target); queue.push(entry.target); work(); }
  }, { rootMargin: '150px' });
  covers.forEach((cover) => observer.observe(cover));
}

let pdfViewerPromise;
function ensurePdfViewer() {
  const root = document.getElementById('pdf-viewer');
  if (!root) return Promise.resolve(null);
  if (!pdfViewerPromise) pdfViewerPromise = initPdfViewer(root).catch((error) => {
    root.innerHTML = `<p>原文暂时无法预览：${escapeHtml(error.message)}<br><a href="/api/papers/${encodeURIComponent(root.dataset.paper)}/pdf" target="_blank" rel="noreferrer">直接打开 PDF</a></p>`;
    document.getElementById('anno-status').textContent = '预览未就绪'; return null;
  });
  return pdfViewerPromise;
}
// The reading pane (reader.js) drives the PDF from outside this file.
window.CarrelPdf = { ensure: ensurePdfViewer };
async function initPdfViewer(root) {
  const endpoint = `/api/papers/${encodeURIComponent(root.dataset.paper)}`;
  const lib = await loadPdfLibrary();
  const pdf = await lib.getDocument({ url: `${endpoint}/pdf`, isEvalSupported: false }).promise;
  const annotations = await api(`${endpoint}/annotations`);
  const strokes = annotations.pages || {};
  const pens = { pen: { color: '#cf3e4e', alpha: .9, width: 2.5 }, highlight: { color: '#ffe45c', alpha: .4, width: 11 } };
  let tool = 'pointer', currentPage = 1, version = 0, savedVersion = 0, saving = false, saveTimer, drawing = false;
  const status = document.getElementById('anno-status');
  const pageInput = document.getElementById('pdf-page-input');
  pageInput.max = pdf.numPages; document.getElementById('pdf-total').textContent = `/ ${pdf.numPages}`;
  async function save() {
    if (saving || version === savedVersion) return;
    saving = true; const sentVersion = version; status.textContent = '正在保存批注…';
    try {
      await api(`${endpoint}/annotations`, { pages: strokes }); savedVersion = sentVersion;
      status.textContent = '批注已保存';
    } catch (error) {
      status.textContent = '批注未保存'; toast('批注保存失败', { error: true, label: '重试', action: save });
    } finally { saving = false; }
    if (savedVersion === sentVersion && version !== savedVersion) save();
  }
  function scheduleSave() { version += 1; status.textContent = '正在保存批注…'; clearTimeout(saveTimer); saveTimer = setTimeout(save, 500); }
  window.addEventListener('beforeunload', (event) => { if (version !== savedVersion) { event.preventDefault(); event.returnValue = ''; } });
  for (const name of ['pointer', 'pen', 'highlight', 'eraser']) document.getElementById(`tool-${name}`)?.addEventListener('click', () => {
    tool = name; root.dataset.tool = name;
    for (const key of ['pointer', 'pen', 'highlight', 'eraser']) {
      const button = document.getElementById(`tool-${key}`); button.classList.toggle('on', key === name); button.setAttribute('aria-pressed', String(key === name));
    }
  });
  document.getElementById('tool-clear-page').addEventListener('click', () => {
    const page = currentPage, old = strokes[page];
    if (!old?.length) { toast('当前页没有批注'); return; }
    delete strokes[page]; redraw(page); scheduleSave();
    toast(`已清除第 ${page} 页批注`, { action: () => { strokes[page] = [...old, ...(strokes[page] || [])]; redraw(page); scheduleSave(); } });
  });
  let syncWindow = null;   // ignore the intermediate pages of a programmatic scroll
  function updatePage(num, origin = 'scroll') {
    const changed = num !== currentPage;
    currentPage = num;
    // Background scroll/render events must not replace a page number being typed.
    if (document.activeElement !== pageInput || origin === 'user') pageInput.value = num;
    document.getElementById('pdf-prev').disabled = num <= 1;
    document.getElementById('pdf-next').disabled = num >= pdf.numPages;
    if (changed || origin !== 'scroll') {
      root.dispatchEvent(new CustomEvent('carrel:pdf-page', { detail: { page: num, origin } }));
    }
  }
  function goTo(num, options = {}) {
    num = Math.max(1, Math.min(pdf.numPages, Number.isFinite(num) ? Math.round(num) : 1));
    syncWindow = { page: num, until: performance.now() + 1200 };
    updatePage(num, options.sync ? 'sync' : 'user');
    const page = root.querySelector(`.pdf-page[data-page="${num}"]`);
    root.scrollTo({ top: root.scrollTop + page.getBoundingClientRect().top - root.getBoundingClientRect().top - 12,
      behavior: options.instant || window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth' });
  }
  document.getElementById('pdf-prev').onclick = () => goTo(currentPage - 1);
  document.getElementById('pdf-next').onclick = () => goTo(currentPage + 1);
  pageInput.addEventListener('change', () => goTo(Number(pageInput.value)));
  pageInput.addEventListener('keydown', (event) => { if (event.key === 'Enter') { event.preventDefault(); goTo(Number(pageInput.value)); } });
  root.replaceChildren();
  const firstPage = await pdf.getPage(1), size = firstPage.getViewport({ scale: 1 });
  for (let num = 1; num <= pdf.numPages; num++) {
    const page = document.createElement('div'); page.className = 'pdf-page'; page.dataset.page = num;
    page.style.aspectRatio = `${size.width} / ${size.height}`; page.innerHTML = `<span class="pdf-page-num">${num}</span>`; root.append(page);
  }
  function redraw(num) {
    const ink = root.querySelector(`.ink-canvas[data-page="${num}"]`); if (!ink) return;
    const ctx = ink.getContext('2d'); ctx.clearRect(0, 0, ink.width, ink.height);
    for (const stroke of strokes[num] || []) {
      ctx.strokeStyle = stroke.color; ctx.globalAlpha = stroke.alpha; ctx.lineWidth = stroke.width * Number(ink.dataset.scale);
      ctx.lineCap = 'round'; ctx.lineJoin = 'round'; ctx.beginPath();
      stroke.points.forEach(([x, y], index) => { if (index) ctx.lineTo(x * ink.width, y * ink.height); else ctx.moveTo(x * ink.width, y * ink.height); });
      ctx.stroke();
    }
    ctx.globalAlpha = 1;
  }
  const position = (event, ink) => { const rect = ink.getBoundingClientRect(); return [(event.clientX - rect.left) / rect.width, (event.clientY - rect.top) / rect.height]; };
  function startStroke(event, ink, num) {
    if (tool === 'pointer' || event.button !== 0) return;
    drawing = true;
    event.preventDefault(); ink.setPointerCapture(event.pointerId); updatePage(num);
    let changed = false, stroke;
    const erase = (e) => {
      const [x, y] = position(e, ink), before = (strokes[num] || []).length;
      strokes[num] = (strokes[num] || []).filter((s) => !s.points.some(([px, py]) => Math.hypot(px - x, py - y) < .016));
      if (before !== strokes[num].length) { changed = true; redraw(num); }
    };
    if (tool === 'eraser') erase(event);
    else { stroke = { ...pens[tool], points: [position(event, ink)] }; (strokes[num] ||= []).push(stroke); changed = true; }
    const move = (e) => { if (stroke) { stroke.points.push(position(e, ink)); redraw(num); } else erase(e); };
    const end = () => {
      drawing = false;
      ink.removeEventListener('pointermove', move); ink.removeEventListener('pointerup', end); ink.removeEventListener('pointercancel', end);
      if (changed) scheduleSave();
      refreshVisiblePages();
    };
    ink.addEventListener('pointermove', move); ink.addEventListener('pointerup', end); ink.addEventListener('pointercancel', end);
  }
  async function renderPage(container) {
    const width = container.clientWidth;
    if (!width || drawing || container.dataset.rendering || Number(container.dataset.renderedWidth) === width) return;
    container.dataset.rendering = 'true';
    try {
      const num = Number(container.dataset.page), page = await pdf.getPage(num), base = page.getViewport({ scale: 1 });
      const scale = width / base.width * Math.min(window.devicePixelRatio || 1, 2);
      const viewport = page.getViewport({ scale }); container.style.aspectRatio = `${base.width} / ${base.height}`;
      const canvas = document.createElement('canvas'); canvas.width = viewport.width; canvas.height = viewport.height;
      const ink = document.createElement('canvas'); ink.width = viewport.width; ink.height = viewport.height; ink.className = 'ink-canvas'; ink.dataset.page = num; ink.dataset.scale = scale;
      await page.render({ canvasContext: canvas.getContext('2d'), viewport }).promise;
      if (drawing) return;
      container.querySelectorAll('canvas').forEach(old => old.remove());
      container.append(canvas, ink); redraw(num); ink.addEventListener('pointerdown', (event) => startStroke(event, ink, num));
      container.querySelector('.pdf-render-error')?.remove();
      container.dataset.renderedWidth = String(width);
    } catch {
      if (!container.querySelector('canvas, .pdf-render-error')) container.insertAdjacentHTML('beforeend', '<p class="pdf-render-error">此页预览失败，请直接打开 PDF。</p>');
    } finally {
      delete container.dataset.rendering;
      if (container.clientWidth && width !== container.clientWidth) renderPage(container);
    }
  }
  const observer = new IntersectionObserver((entries) => entries.forEach((entry) => { if (entry.isIntersecting) renderPage(entry.target); }), { root, rootMargin: '300px' });
  root.querySelectorAll('.pdf-page').forEach((page) => observer.observe(page));
  for (const name of ['wheel', 'touchstart', 'pointerdown']) {
    root.addEventListener(name, () => { syncWindow = null; }, { passive: true });
  }
  function refreshVisiblePages() {
    if (!root.clientWidth || drawing) return;
    const view = root.getBoundingClientRect();
    root.querySelectorAll('.pdf-page').forEach(page => {
      const rect = page.getBoundingClientRect();
      if (rect.bottom >= view.top - 300 && rect.top <= view.bottom + 300) renderPage(page);
    });
  }
  let resizeTimer;
  const resizeObserver = new ResizeObserver(() => {
    clearTimeout(resizeTimer); resizeTimer = setTimeout(refreshVisiblePages, 160);
  });
  resizeObserver.observe(root);
  let scrolling = false;
  root.addEventListener('scroll', () => {
    if (scrolling) return; scrolling = true;
    requestAnimationFrame(() => {
      const center = root.getBoundingClientRect().top + root.clientHeight * .4;
      for (const page of root.querySelectorAll('.pdf-page')) {
        const rect = page.getBoundingClientRect();
        if (rect.top <= center && rect.bottom >= center) {
          const num = Number(page.dataset.page);
          // While we are smooth-scrolling to a known page, the pages we pass
          // on the way are not a state change worth reporting.
          if (syncWindow) {
            if (num === syncWindow.page || performance.now() > syncWindow.until) syncWindow = null;
            else { scrolling = false; return; }
          }
          updatePage(num);
          break;
        }
      }
      scrolling = false;
    });
  });
  // In pointer mode the ink layer ignores clicks, so a page click reaches here.
  root.addEventListener('click', (event) => {
    if (root.dataset.tool !== 'pointer') return;
    const page = event.target.closest('.pdf-page');
    if (page) updatePage(Number(page.dataset.page), 'click');
  });
  updatePage(1, 'init'); status.textContent = '批注已就绪';
  return {
    goTo: (num, options) => goTo(num, options),
    get page() { return currentPage; },
    numPages: pdf.numPages,
    onPage(callback) {
      const handler = (event) => callback(event.detail.page, event.detail.origin);
      root.addEventListener('carrel:pdf-page', handler);
      return () => root.removeEventListener('carrel:pdf-page', handler);
    },
  };
}
// The translation tab was merged into the reading tab; old bookmarks follow.
const TAB_ALIAS = { trans: 'pdf' };
function openTab(name) {
  const tab = TAB_ALIAS[name] || name;
  if (['summary', 'pdf', 'rel'].includes(tab)) activateTab(tab, false);
}
openTab(location.hash.slice(1));
window.addEventListener('hashchange', () => openTab(location.hash.slice(1)));
refreshIcons();
