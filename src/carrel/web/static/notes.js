(() => {
  const e = escapeHtml;
  const markdown = window.markdownit?.({ html: false, breaks: true });
  markdown?.disable('image');
  if (markdown) {
    markdown.renderer.rules.link_open = (tokens, index, options, env) => {
      const href = tokens[index].attrGet('href') || '';
      const external = /^https?:\/\//i.test(href);
      (env.noteLinkTags ||= []).push(external ? 'a' : 'span');
      return external ? `<a href="${e(href)}" target="_blank" rel="noreferrer">` : '<span>';
    };
    markdown.renderer.rules.link_close = (tokens, index, options, env) => `</${env.noteLinkTags.pop()}>`;
  }
  const noteBody = text => markdown ? markdown.render(text) : e(text);
  async function remove(url) {
    const response = await fetch(url, { method: 'DELETE', headers: { 'X-Carrel': '1' } });
    const data = await response.json();
    if (!response.ok) throw new Error(data.message || '删除失败');
    return data;
  }
  const list = document.getElementById('collection-list');
  if (list) {
    let collections = JSON.parse(document.getElementById('collections-data').textContent);
    const dialog = document.getElementById('collection-dialog');
    const form = document.getElementById('collection-form');
    function render() {
      list.innerHTML = collections.length ? collections.map(c => `<div class="collection-row" data-id="${e(c.id)}">${icon('folder')}<div class="collection-row-body"><h2><a href="/?collection=${encodeURIComponent(c.id)}&sort=rank">${e(c.name)}</a></h2>${c.note ? `<p>${e(c.note)}</p>` : ''}</div><span class="count">${c.count} 篇</span><button class="icon-btn" data-edit title="编辑集合" aria-label="编辑 ${e(c.name)}">${icon('pencil')}</button><button class="icon-btn" data-delete title="删除集合" aria-label="删除 ${e(c.name)}">${icon('trash-2')}</button></div>`).join('') : '<div class="empty"><h2>暂无阅读集合</h2><a class="btn" href="/sync">同步本地目录</a></div>';
      refreshIcons();
    }
    function edit(c = {}) {
      form.reset(); form.querySelector('.form-error').hidden = true;
      form.elements.id.value = c.id || ''; form.elements.name.value = c.name || ''; form.elements.note.value = c.note || '';
      dialog.showModal(); form.elements.name.focus();
    }
    document.getElementById('collection-add').onclick = () => edit();
    list.addEventListener('click', async event => {
      const row = event.target.closest('[data-id]');
      if (!row) return;
      const c = collections.find(c => c.id === row.dataset.id);
      if (event.target.closest('[data-edit]')) edit(c);
      if (event.target.closest('[data-delete]') && window.confirm(`删除集合“${c.name}”？论文会保留，集合中的笔记将转为论文通用笔记。`)) {
        try { collections = (await remove(`/api/collections/${encodeURIComponent(c.id)}`)).collections; render(); toast('集合已删除，论文与笔记已保留'); }
        catch (error) { toast(error.message, { error: true }); }
      }
    });
    form.addEventListener('submit', async event => {
      event.preventDefault(); busy(form, true);
      try {
        const saved = await api('/api/collections', Object.fromEntries(new FormData(form)));
        collections = saved.collections; render(); dialog.close(); toast('集合已保存');
      } catch (error) { showError(form, error); }
      finally { busy(form, false); }
    });
    render();
  }

  const section = document.getElementById('human-notes');
  if (section) {
    const paperId = section.dataset.paper;
    const endpoint = `/api/papers/${encodeURIComponent(paperId)}/notes`;
    const container = document.getElementById('notes-list');
    const dialog = document.getElementById('note-dialog');
    const form = document.getElementById('note-add-form');
    let notes = JSON.parse(document.getElementById('notes-data').textContent);
    function render() {
      container.innerHTML = notes.length ? notes.map((n, i) => `<article class="human-note" data-note="${i}"><div class="human-note-head"><span class="small-text">${e(n.collection_name || '通用笔记')}</span>${n.priority ? `<span class="human-priority">${e(n.priority)}</span>` : ''}<div class="note-actions"><button class="icon-btn small" data-note-edit title="编辑笔记" aria-label="编辑笔记">${icon('pencil')}</button><button class="icon-btn small" data-note-delete title="删除笔记" aria-label="删除笔记">${icon('trash-2')}</button></div></div><div class="human-note-body">${noteBody(n.body)}</div>${n.source_ref ? `<details class="human-note-source"><summary>${n.source === 'human' ? '已人工修改 · 查看导入来源' : '导入来源'}</summary><pre>${e(n.source_ref)}</pre></details>` : ''}</article>`).join('') : '<p class="dim small-text">暂无笔记</p>';
      refreshIcons();
    }
    function editNote(article, n) {
      article.innerHTML = `<form class="human-note-form"><label for="note-priority-edit">阅读优先级</label><input id="note-priority-edit" name="priority" value="${e(n.priority)}" list="note-priorities" maxlength="100"><label for="note-body-edit">笔记 · ${e(n.collection_name || '通用笔记')}</label><textarea id="note-body-edit" name="body" rows="7" maxlength="100000">${e(n.body)}</textarea><p class="form-error" role="alert" hidden></p><div class="dialog-actions"><button type="button" class="btn" data-note-cancel>取消</button><button type="submit" class="btn primary">${icon('check')}保存</button></div></form>`;
      const editForm = article.querySelector('form');
      editForm.querySelector('[data-note-cancel]').onclick = render;
      editForm.addEventListener('submit', async event => {
        event.preventDefault(); busy(editForm, true);
        try { notes = (await api(endpoint, { ...Object.fromEntries(new FormData(editForm)), collection_id: n.collection_id })).notes; render(); toast('笔记已保存'); }
        catch (error) { showError(editForm, error); }
        finally { busy(editForm, false); }
      });
      refreshIcons(); editForm.elements.body.focus();
    }
    container.addEventListener('click', async event => {
      const article = event.target.closest('[data-note]');
      if (!article) return;
      const n = notes[Number(article.dataset.note)];
      if (event.target.closest('[data-note-edit]')) { render(); editNote(container.children[Number(article.dataset.note)], n); }
      if (event.target.closest('[data-note-delete]') && window.confirm('删除这条笔记？')) {
        try { notes = (await remove(`${endpoint}?collection_id=${encodeURIComponent(n.collection_id)}`)).notes; render(); }
        catch (error) { toast(error.message, { error: true }); }
      }
    });
    document.getElementById('note-add').addEventListener('click', async () => {
      try {
        const data = await api(endpoint);
        notes = data.notes; form.reset(); form.querySelector('.form-error').hidden = true;
        const options = [{ id: '', name: '通用笔记' }, ...data.collections].filter(c => !notes.some(n => n.collection_id === c.id));
        if (!options.length) { toast('每个集合已有笔记，可在对应笔记中继续编辑'); return; }
        form.elements.collection_id.innerHTML = options.map(c => `<option value="${e(c.id)}">${e(c.name)}</option>`).join('');
        dialog.showModal(); form.elements.body.focus();
      } catch (error) { toast(error.message, { error: true }); }
    });
    form.addEventListener('submit', async event => {
      event.preventDefault(); busy(form, true);
      try { notes = (await api(endpoint, Object.fromEntries(new FormData(form)))).notes; render(); dialog.close(); toast('笔记已添加'); }
      catch (error) { showError(form, error); }
      finally { busy(form, false); }
    });
    render();
  }
  document.querySelector('[data-process-indexed]')?.addEventListener('click', async event => {
    await CarrelProcessing.upgrade(event.currentTarget.dataset.processIndexed);
  });
})();
