(() => {
  const form = document.getElementById('sync-scan-form');
  if (!form) return;
  const preview = document.getElementById('sync-preview');
  const status = document.getElementById('sync-status');
  const result = document.getElementById('sync-result');
  const applyButton = document.getElementById('sync-apply');
  const all = document.getElementById('sync-all');
  const filter = document.getElementById('sync-filter');
  const groups = document.getElementById('sync-groups');
  let data = null;
  let selected = new Set();
  let applying = false;
  const e = escapeHtml;

  function updateSelection() {
    const total = data?.papers.length || 0;
    all.checked = total > 0 && selected.size === total;
    all.indeterminate = selected.size > 0 && selected.size < total;
    document.getElementById('sync-selected').textContent = `已选 ${selected.size} / ${total} 篇`;
    document.getElementById('sync-apply-count').textContent = `${selected.size} 篇论文待收录`;
    applyButton.disabled = applying || !selected.size;
    groups.querySelectorAll('[data-sha]').forEach(input => { input.checked = selected.has(input.dataset.sha); });
  }

  function diagnostics() {
    const sections = [
      ['跳过的目录或文件', data.skipped, row => `${row.path}：${row.reason}`],
      ['需要核对', data.warnings, row => `${row.path}：${row.reason}`],
      ['未关联到论文的笔记', data.unmatched_notes, row => `${row.text} (${row.source_ref})`],
    ];
    const count = sections.reduce((n, [, rows]) => n + rows.length, 0);
    document.getElementById('sync-diagnostics').hidden = count === 0;
    document.getElementById('sync-diagnostics-label').textContent = `扫描详情 · ${count} 项`;
    document.getElementById('sync-diagnostic-items').innerHTML = sections.filter(([, rows]) => rows.length).map(([title, rows, describe]) =>
      `<h3>${e(title)} · ${rows.length}</h3><ul>${rows.map(row => `<li>${e(describe(row))}</li>`).join('')}</ul>`).join('');
  }

  function renderGroups() {
    const names = new Map(data.collections.map(c => [c.id, c.name]));
    let rowId = 0;
    groups.innerHTML = data.collections.map((c, groupIndex) => {
      const rank = p => Math.min(...p.sources.filter(s => s.collection_id === c.id).map(s => s.rank ?? Number.MAX_SAFE_INTEGER));
      const papers = data.papers.filter(p => p.sources.some(s => s.collection_id === c.id)).sort((a, b) => rank(a) - rank(b) || a.title.localeCompare(b.title));
      return `<details class="sync-group" ${groupIndex === 0 ? 'open' : ''}><summary>${icon('folder')}<span>${e(c.name)}</span><span class="count">${papers.length} 篇</span></summary>${papers.map(p => {
        const id = `sync-paper-${++rowId}`;
        const collections = [...new Set(p.sources.map(s => names.get(s.collection_id)))];
        const notes = data.notes.filter(n => n.pdf_sha256 === p.pdf_sha256);
        const source = p.sources.find(s => s.collection_id === c.id);
        return `<div class="sync-paper" data-search="${e([p.title, ...collections].join(' ').toLowerCase())}">
          <input type="checkbox" id="${id}" data-sha="${p.pdf_sha256}" checked>
          <div><label class="sync-paper-title" for="${id}">${e(p.title)}</label><div class="sync-paper-meta">${p.year ? `<span>${p.year}</span>` : ''}${source.rank ? `<span>阅读顺序 ${source.rank}</span>` : ''}<span>${notes.length} 条笔记</span>${p.sources.length > 1 ? `<span>${p.sources.length} 份 PDF 合并</span>` : ''}${collections.length > 1 ? `<span>${e(collections.join(' / '))}</span>` : ''}</div><div class="sync-paper-path">${e(source.path)}</div></div>
          <span class="badge">${p.existing ? '已在库中' : '新增'}</span></div>`;
      }).join('')}</details>`;
    }).join('');
    updateSelection(); refreshIcons();
  }

  form.addEventListener('submit', async event => {
    event.preventDefault();
    busy(form, true); preview.hidden = true; result.hidden = true; data = null;
    status.textContent = '正在扫描目录、核对 PDF 内容并读取笔记…';
    try {
      data = await api('/api/sync/scan', { root: form.elements.root.value.trim() });
      selected = new Set(data.papers.map(p => p.pdf_sha256));
      const s = data.stats;
      document.getElementById('sync-summary').textContent = `${s.pdfs} 份 PDF · ${s.papers} 篇论文 · ${s.collections} 个集合 · ${s.notes} 条笔记 · 合并 ${s.duplicates} 份副本`;
      filter.value = ''; renderGroups(); diagnostics(); preview.hidden = false;
      status.textContent = s.papers ? '' : '没有找到可收录的 PDF。';
      document.getElementById('sync-preview-title').focus();
    } catch (error) { status.textContent = ''; showError(form, error); }
    finally { busy(form, false); }
  });
  form.elements.root.addEventListener('input', () => { data = null; preview.hidden = true; });
  groups.addEventListener('change', event => {
    const sha = event.target.dataset.sha;
    if (!sha) return;
    if (event.target.checked) selected.add(sha); else selected.delete(sha);
    updateSelection();
  });
  all.addEventListener('change', () => {
    selected = new Set(all.checked ? data.papers.map(p => p.pdf_sha256) : []); updateSelection();
  });
  filter.addEventListener('input', () => {
    const query = filter.value.trim().toLowerCase();
    groups.querySelectorAll('.sync-group').forEach(group => {
      const rows = [...group.querySelectorAll('.sync-paper')];
      rows.forEach(row => { row.hidden = !row.dataset.search.includes(query); });
      group.hidden = rows.every(row => row.hidden);
      if (query && !group.hidden) group.open = true;
    });
  });
  applyButton.addEventListener('click', async () => {
    if (!data || !selected.size || applying) return;
    applying = true; updateSelection(); busy(form, true);
    form.elements.root.disabled = true;
    preview.querySelectorAll('input').forEach(input => { input.disabled = true; });
    status.textContent = '正在备份资料库并保存所选条目…';
    try {
      const report = await api('/api/sync/apply', { scan_id: data.scan_id, selected: [...selected] });
      preview.hidden = true; result.hidden = false; status.textContent = '';
      result.innerHTML = `<h2>${icon('circle-check')} 收录完成</h2><p>新增 ${report.added} 篇，关联库中已有论文 ${report.existing} 篇；新增 ${report.collections_added} 个阅读集合，新增 ${report.notes_added} 条笔记，更新 ${report.notes_updated} 条笔记。</p>${report.human_notes_preserved ? `<p>保留 ${report.human_notes_preserved} 条人工修改的笔记。</p>` : ''}<p class="sync-result-path">备份：${e(report.backup)}</p>${report.warnings.length ? `<details><summary>核对事项 · ${report.warnings.length}</summary><ul>${report.warnings.map(w => `<li>${e(w.reason)}</li>`).join('')}</ul></details>` : ''}<div class="settings-row-actions"><a class="btn primary" href="/">${icon('library')}去论文库查看</a><a class="btn" href="/collections">${icon('folders')}阅读集合</a></div>`;
      refreshIcons(); result.scrollIntoView({ block: 'nearest' });
    } catch (error) { status.textContent = ''; toast(error.message, { error: true }); }
    finally {
      applying = false; updateSelection(); busy(form, false); form.elements.root.disabled = false;
      preview.querySelectorAll('input').forEach(input => { input.disabled = false; });
    }
  });
})();
