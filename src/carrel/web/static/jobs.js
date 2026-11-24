(() => {
  const root = document.querySelector('[data-batch-dashboard]');
  if (!root) return;
  const labels = { queued: '等待处理', parsing: '解析', structuring: '整理', translating: '翻译',
    classifying: '分类', relating: '关联', retry_wait: '等待重试', finalizing: '整理全库关系',
    completed: '处理完成', running: '处理中', succeeded: '完成', failed: '失败', partial: '翻译待补全',
    ready_with_warning: '关联待补全', ready: '可阅读' };
  const element = (tag, text, cls) => {
    const node = document.createElement(tag);
    node.textContent = text; if (cls) node.className = cls; return node;
  };
  async function refresh() {
    try {
      const response = await fetch('/api/batches');
      if (!response.ok) throw new Error('状态暂时不可用');
      const data = await response.json();
      const active = data.batches.filter(b => !b.finalized);
      root.querySelector('[data-batch-caption]').textContent = active.length ? '各篇并行推进，完成的论文可立即阅读。关闭此页面不影响处理。' : '已完成的阶段会保留，服务重启后自动继续未完成的批次。';
      root.querySelector('[data-batch-total]').textContent = active.length + ' 个进行中的批次 · 最多 ' + data.workers + ' 篇并行';
      const list = root.querySelector('[data-batch-list]');
      const expanded = new Set([...list.querySelectorAll('details[open]')].map(node => node.dataset.batchId));
      list.replaceChildren();
      const visible = [...active, ...data.batches.filter(b => b.finalized).reverse().slice(0, 3)];
      if (!visible.length) list.append(element('p', '暂无导入批次。从导入页确认论文列表后，会在这里显示整体进度。', 'dim'));
      visible.forEach(batch => {
        const counts = {}; batch.items.forEach(item => { counts[item.status] = (counts[item.status] || 0) + 1; });
        const done = ['succeeded', 'failed', 'partial', 'ready_with_warning'].reduce((sum, k) => sum + (counts[k] || 0), 0);
        const card = element('article', '', 'batch-card');
        const head = element('div', '', 'batch-card-head');
        head.append(element('strong', done + ' / ' + batch.items.length + ' 篇'), element('span', labels[batch.status] || batch.status, 'dim'));
        const meter = document.createElement('progress'); meter.max = batch.items.length; meter.value = done; meter.setAttribute('aria-label', '批次完成进度');
        card.append(head, meter, element('p', Object.entries(counts).map(([key, count]) => (labels[key] || key) + ' ' + count).join(' · '), 'dim'));
        if (batch.warning) card.append(element('p', batch.warning, 'batch-warning'));
        const details = document.createElement('details'); details.dataset.batchId = batch.id;
        details.open = expanded.has(batch.id); details.append(element('summary', '查看本批论文与错误说明'));
        batch.items.forEach(item => {
          const line = element('p', '', 'batch-paper');
          const link = element('a', item.title || item.input); link.href = '/papers/' + encodeURIComponent(item.paper_id);
          line.append(link, element('span', labels[item.stage] || item.stage, 'dim'));
          if (item.error_detail) line.append(element('small', item.error_detail, 'batch-warning'));
          details.append(line);
        });
        card.append(details); list.append(card);
      });
      const stages = root.querySelector('[data-batch-stages]'); stages.replaceChildren();
      Object.entries(data.stages).forEach(([stage, n]) => stages.append(element('span', (labels[stage] || stage) + ' ' + n, 'batch-stage')));
      const resources = root.querySelector('[data-batch-resources]'); resources.replaceChildren();
      data.resources.forEach(resource => {
        const name = resource.name.startsWith('llm:') ? '模型 · ' + resource.name.slice(4) : resource.name;
        const state = resource.state === 'open' ? '暂停请求，约 ' + resource.retry_in_s + ' 秒后探测' : resource.state === 'half_open' ? '正在探测恢复' : '正常';
        resources.append(element('p', name + '：' + resource.active + ' / ' + resource.limit + ' 个请求 · ' + state + (resource.waiting ? ' · ' + resource.waiting + ' 个等待' : ''), resource.state === 'closed' ? '' : 'batch-warning'));
      });
      const quota = root.querySelector('[data-batch-quota]');
      quota.textContent = 'MinerU 今日已提交约 ' + data.mineru.pages + ' 页 / 高优先级额度 ' + data.mineru.soft_limit + ' 页' + (data.mineru.over_limit ? (data.mineru.behavior === 'pause' ? ' · 等待次日继续' : ' · 解析可能变慢') : '');
      quota.className = data.mineru.over_limit ? 'batch-warning' : 'dim';
      const retries = root.querySelector('[data-batch-retries]'); retries.replaceChildren();
      data.retries.forEach(item => retries.append(element('p', item.paper_id + ' · ' + item.error_code + ' · 约 ' + Math.max(0, Math.ceil((item.next_at - Date.now()/1000)/60)) + ' 分钟后重试', 'batch-warning')));
    } catch (error) { root.querySelector('[data-batch-caption]').textContent = '暂时无法刷新进度，稍后自动重试。'; }
    window.setTimeout(refresh, 4000);
  }
  refresh();
})();
