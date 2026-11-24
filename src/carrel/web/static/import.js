(() => {
  const sourceForm = document.getElementById('import-form');
  const confirmForm = document.getElementById('import-confirm-form');
  const area = sourceForm.elements.input;
  const layout = document.querySelector('.import-layout');
  const candidates = document.getElementById('import-candidates');
  const template = document.getElementById('import-candidate-template');
  const progress = document.getElementById('import-progress');
  const selectAll = document.getElementById('import-select-all');
  const back = confirmForm.querySelector('[data-import-back]');
  let items = [], pending = 0, submitting = false;

  function fitFields(root = candidates) {
    if (confirmForm.hidden) return;
    root.querySelectorAll('textarea').forEach((field) => {
      field.style.height = 'auto';
      field.style.height = `${field.scrollHeight + 2}px`;
    });
  }
  window.addEventListener('resize', () => fitFields());

  function message(text, error = false) {
    progress.hidden = false;
    progress.replaceChildren();
    const span = document.createElement('span');
    span.className = error ? 'bad' : '';
    span.textContent = text;
    progress.append(span);
  }

  function selected() {
    return items.filter((item) => !item.submitted && item.row.querySelector('[data-import-select]').checked);
  }

  function updateSelection() {
    const remaining = items.filter((item) => !item.submitted);
    const count = selected().length;
    selectAll.checked = count > 0 && count === remaining.length;
    selectAll.indeterminate = count > 0 && count < remaining.length;
    selectAll.disabled = !remaining.length;
    document.getElementById('import-selected').textContent = `已选 ${count} / ${remaining.length} 篇`;
    confirmForm.querySelector('[type="submit"]').disabled = !count || pending > 0 || submitting;
    confirmForm.querySelector('[data-import-confirm-label]').textContent = count ? `确认导入 ${count} 篇` : '确认导入';
    back.disabled = pending > 0 || submitting;
    for (const item of items) {
      const checked = item.row.querySelector('[data-import-select]').checked;
      item.row.classList.toggle('is-unselected', !checked && !item.submitted);
      item.row.querySelector('[data-import-link]').disabled = !checked || item.loading || item.submitted;
      item.row.querySelector('[data-import-title]').disabled = !checked || item.loading || item.submitted;
      item.row.querySelector('[data-import-refresh]').disabled = !checked || item.loading || item.submitted;
    }
  }

  function updateRow(item) {
    const row = item.row;
    row.querySelector('[data-import-source]').textContent = { arxiv: 'arXiv', hf: 'Hugging Face', pdf_url: 'PDF', local_path: '本地 PDF' }[item.kind];
    const state = row.querySelector('[data-import-state]');
    state.textContent = item.loading ? '正在获取标题…' : item.dirty ? '链接已修改，待核对' : item.submitted ? '已提交' :
      item.existing ? '已在资料库中' : item.title.trim() ? '' : item.title_status === 'pending_parse' ? '标题待 PDF 解析' : '标题未获取，可填写或重试';
    row.querySelector('[data-import-select]').setAttribute('aria-label', `导入 ${item.title || item.input}`);
    const link = row.querySelector('[data-import-open]');
    const value = row.querySelector('[data-import-link]').value.trim();
    link.hidden = !/^https?:\/\//i.test(value);
    if (!link.hidden) link.href = value;
    const error = row.querySelector('[data-import-error]');
    error.textContent = item.error || '';
    error.hidden = !item.error;
    fitFields(row);
  }

  async function refreshItem(item) {
    if (item.loading) return false;
    item.loading = true; item.error = ''; pending += 1;
    updateRow(item); updateSelection();
    try {
      const value = item.row.querySelector('[data-import-link]').value.trim();
      const data = await api('/api/import/preview', { input: value });
      if (data.papers.length !== 1) throw new Error('每一项只能填写一篇论文的链接或路径。');
      Object.assign(item, data.papers[0], { dirty: false });
      item.row.querySelector('[data-import-title]').value = item.title;
      item.row.querySelector('[data-import-link]').value = item.input;
      return true;
    } catch (error) {
      item.error = error.message;
      return false;
    } finally {
      item.loading = false; pending -= 1;
      updateRow(item); updateSelection();
    }
  }

  function render(data) {
    candidates.replaceChildren();
    items = data.papers.map((paper, index) => {
      const row = template.content.firstElementChild.cloneNode(true);
      const item = { ...paper, row, dirty: false, submitted: false, loading: false, error: '' };
      const title = row.querySelector('[data-import-title]');
      const link = row.querySelector('[data-import-link]');
      title.id = `import-title-${index}`; title.value = item.title;
      link.id = `import-link-${index}`; link.value = item.input;
      row.querySelector('[data-import-title-label]').htmlFor = title.id;
      row.querySelector('[data-import-link-label]').htmlFor = link.id;
      row.querySelector('[data-import-select]').addEventListener('change', updateSelection);
      title.addEventListener('input', () => { item.title = title.value; updateRow(item); });
      link.addEventListener('input', () => {
        item.dirty = link.value.trim() !== item.input;
        item.error = ''; updateRow(item);
      });
      row.querySelector('[data-import-refresh]').addEventListener('click', () => refreshItem(item));
      candidates.append(row); updateRow(item);
      return item;
    });
    document.getElementById('import-detected').textContent = `识别到 ${items.length} 篇论文${data.duplicates ? ` · 已合并 ${data.duplicates} 个重复链接` : ''}`;
    sourceForm.hidden = true; confirmForm.hidden = false;
    layout.classList.add('is-reviewing');
    confirmForm.elements.force.checked = sourceForm.elements.force.checked;
    back.querySelector('span').textContent = '返回修改';
    progress.hidden = true;
    updateSelection(); refreshIcons(); fitFields();
    document.getElementById('import-confirm-title').focus();
  }

  sourceForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (sourceForm.dataset.busy === 'true') return;
    busy(sourceForm, true); area.readOnly = true;
    message('正在识别论文并获取标题…');
    try { render(await api('/api/import/preview', { input: area.value })); }
    catch (error) { message(error.message, true); }
    finally { busy(sourceForm, false); area.readOnly = false; }
  });

  selectAll.addEventListener('change', () => {
    for (const item of items) if (!item.submitted) item.row.querySelector('[data-import-select]').checked = selectAll.checked;
    updateSelection();
  });

  back.addEventListener('click', () => {
    if (submitting || pending) return;
    if (items.some((item) => item.submitted)) area.value = items.filter((item) => !item.submitted).map((item) => item.row.querySelector('[data-import-link]').value).join('\n');
    sourceForm.elements.force.checked = confirmForm.elements.force.checked;
    confirmForm.hidden = true; sourceForm.hidden = false; progress.hidden = true;
    layout.classList.remove('is-reviewing'); area.focus();
  });

  confirmForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (submitting || pending || !selected().length) return;
    const changed = selected().filter((item) => item.dirty);
    if (changed.length) {
      const results = await Promise.all(changed.map(refreshItem));
      message(results.every(Boolean) ? '链接已更新，请核对论文标题后确认导入。' : '部分链接无法识别，请修改后重试。', !results.every(Boolean));
      return;
    }
    const chosen = selected();
    const identities = new Set();
    for (const item of chosen) {
      if (identities.has(item.paper_id)) {
        item.error = '与列表中的另一篇论文重复，请取消勾选重复项。'; updateRow(item);
        return;
      }
      identities.add(item.paper_id);
    }
    submitting = true;
    busy(confirmForm, true); confirmForm.querySelector('fieldset').disabled = true;
    let succeeded = 0;
    try {
      message('正在提交批次…');
      try {
        const selector = document.getElementById('import-processing').selector;
        if (!selector) throw new Error('处理深度尚未加载，请稍后重试。');
        const result = await api('/api/import/batch', {
          inputs: chosen.map((item) => ({ input: item.input, title: item.title.trim() })),
          force: confirmForm.elements.force.checked,
          profile: selector.value,
        });
        chosen.forEach((item, index) => {
          item.submitted = true; item.error = ''; succeeded += 1;
          const checkbox = item.row.querySelector('[data-import-select]');
          checkbox.checked = false; checkbox.disabled = true;
          const link = item.row.querySelector('[data-import-result]');
          link.href = '/papers/' + encodeURIComponent(result.jobs[index].paper_id); link.hidden = false;
          updateRow(item);
        });
      } catch (error) {
        chosen.forEach((item) => { item.error = error.message; updateRow(item); });
      }
      message(`已提交 ${succeeded} 篇${succeeded < chosen.length ? `，${chosen.length - succeeded} 篇提交失败` : ''}。`, succeeded < chosen.length);
      if (items.some((item) => item.submitted)) {
        const link = document.createElement('a');
        link.className = 'text-link'; link.href = '/jobs'; link.textContent = '查看处理任务';
        progress.append(document.createTextNode(' '), link);
        back.querySelector('span').textContent = '继续添加';
      }
    } finally {
      submitting = false; confirmForm.querySelector('fieldset').disabled = false;
      busy(confirmForm, false); updateSelection();
    }
  });
})();
