/* Carrel knowledge graph: local layout, interaction, detail panel and export.
 *
 * The server returns topology and display fields; this file owns layout. There
 * is no build step and no CDN. The library uses category grids; paper and
 * motivation graphs use bundled ELK for placement and orthogonal edge routes.
 */
(function () {
  'use strict';

  const SVG_NS = 'http://www.w3.org/2000/svg';
  const LAYER_FOR_ROLE = { category: 'category', paper: 'paper', tag: 'tag', external: 'external' };
  const DEFAULT_LIBRARY_MAX_EXTERNAL = 12;
  const FONT = '14px -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", "PingFang SC", sans-serif';
  const FONT_SMALL = '12px -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", "PingFang SC", sans-serif';

  function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>"']/g, (c) => (
      { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }
  function icon(name) { return `<i data-lucide="${name}" class="icon" aria-hidden="true"></i>`; }
  function refreshIcons() { window.lucide?.createIcons(); }
  function el(name, attrs = {}) {
    const node = document.createElementNS(SVG_NS, name);
    for (const [key, value] of Object.entries(attrs)) if (value !== undefined && value !== null) node.setAttribute(key, value);
    return node;
  }
  function measure(text, font = FONT) {
    measure.cache ||= new Map();
    const key = font + '\n' + text;
    if (measure.cache.has(key)) return measure.cache.get(key);
    if (!measure.ctx) measure.ctx = document.createElement('canvas').getContext('2d');
    measure.ctx.font = font;
    const width = measure.ctx.measureText(text).width;
    if (measure.cache.size > 5000) measure.cache.clear();
    measure.cache.set(key, width);
    return width;
  }
  function clamp(value, low, high) { return Math.max(low, Math.min(high, value)); }
  function edgeKey(edge) { return `${edge.source}\u0000${edge.target}\u0000${edge.role}`; }
  function domainTone(key) {
    let hash = 0;
    for (const character of String(key || '未分类')) hash = (hash * 31 + character.charCodeAt(0)) >>> 0;
    return ((hash ^ (hash >>> 16)) >>> 0) % 8;
  }

  function roundedRoute(points) {
    let path = `M${points[0].x},${points[0].y}`;
    for (let index = 1; index < points.length - 1; index++) {
      const previous = points[index - 1], point = points[index], next = points[index + 1];
      const before = Math.hypot(point.x - previous.x, point.y - previous.y);
      const after = Math.hypot(next.x - point.x, next.y - point.y);
      const radius = Math.min(7, before / 2, after / 2);
      if (!radius) continue;
      const start = { x: point.x + (previous.x - point.x) * radius / before, y: point.y + (previous.y - point.y) * radius / before };
      const end = { x: point.x + (next.x - point.x) * radius / after, y: point.y + (next.y - point.y) * radius / after };
      path += ` L${start.x},${start.y} Q${point.x},${point.y} ${end.x},${end.y}`;
    }
    const end = points[points.length - 1];
    return `${path} L${end.x},${end.y}`;
  }

  /* Wrap complete titles, including CJK text and unbroken identifiers. */
  function wrapLabel(text, maxWidth, font = FONT) {
    const words = String(text || '').match(/[\u3400-\u9fff]|[^\s\u3400-\u9fff]+|\s+/gu) || [];
    const lines = [];
    let line = '';
    const push = () => { if (line.trim()) lines.push(line.trim()); line = ''; };
    for (const word of words) {
      if (/^\s+$/.test(word)) { if (line) line += ' '; continue; }
      if (measure(line + word, font) <= maxWidth) { line += word; continue; }
      push();
      for (const character of word) {
        if (line && measure(line + character, font) > maxWidth) push();
        line += character;
      }
    }
    push();
    return lines.length ? lines : [''];
  }

  /* ------------------------------------------------------------- controller */
  class GraphController {
    constructor(root, options = {}) {
      this.root = root;
      this.scope = root.dataset.graphScope || 'library';
      this.endpoint = root.dataset.endpoint || '/api/graph';
      this.canvas = root.querySelector('[data-graph-canvas]');
      this.status = root.querySelector('[data-graph-status]');
      this.detail = root.querySelector('[data-graph-detail]');
      this.detailBody = root.querySelector('[data-graph-detail-body]');
      this.detailTitle = root.querySelector('[data-graph-detail-title]');
      this.nodeList = root.querySelector('[data-graph-node-list]');
      this.filterBar = root.querySelector('[data-graph-filters]');
      this.zoomValue = root.querySelector('[data-graph-zoom-value]');
      this.countEl = root.querySelector('[data-graph-count]');
      this.searchInput = root.querySelector('[data-graph-search] input');
      this.searchCount = root.querySelector('.graph-search-count');
      this.options = options;
      this.data = null;
      this.nodes = new Map();
      this.edgeEls = new Map();
      this.edges = [];
      this.byId = new Map();
      this.selected = '';
      this.hovered = '';
      this.renderVersion = 0;
      this.showEvidence = false;
      this.previousFocus = null;
      this.fullscreenRequested = false;
      this.nativeFullscreenActive = false;
      this.transform = { x: 0, y: 0, k: 1 };
      this.overview = false;
      this.minScale = 0.2;
      this.layers = { category: true, paper: true, tag: true, external: this.scope === 'paper', cites: true, similar: true, citation: true, semantic: true };
      this.root.querySelectorAll('[data-layer]').forEach(input => { this.layers[input.dataset.layer] = input.checked; });
      this.markerId = `graph-arrow-${this.scope}`;
      this.searchTerm = '';
      this.filter = { kind: 'all', value: '' };
      this.filterIds = null;
      this.filterOptions = [];
      this.regions = [];
      this.abort = null;
      this.destroyed = false;
      this.payload = null;
    }

    /* ------------------------------------------------------------ lifecycle */
    start(payload) {
      this.payload = payload || this.readPayload();
      if (!this.payload) {
        this.setStatus('图谱数据缺失，暂时无法绘制。', { error: true });
        return this;
      }
      this.ready = Promise.race([
        document.fonts?.ready || Promise.resolve(),
        new Promise((resolve) => setTimeout(resolve, 1500)),
      ]).then(() => { if (!this.destroyed && !this.data) return this.render(this.payload); });
      this.bind();
      return this;
    }

    readPayload() {
      const holder = this.scope === 'library'
        ? document.getElementById('graph-data')
        : this.root.querySelector('[data-graph-payload]')
          || this.root.parentElement?.querySelector('[data-graph-payload]');
      if (!holder) return null;
      try { return JSON.parse(holder.textContent); } catch { return null; }
    }

    makeFilterOptions(nodes = this.nodes) {
      const options = [{ kind: 'all', label: '全部', value: '' }];
      if (!nodes.size) return options;
      if (this.scope === 'motivation_tree') {
        const branches = new Map();
        for (const node of nodes.values()) {
          const branch = node.meta?.branch;
          if (!branch?.key) continue;
          branches.set(branch.key, branch.label || branch.key);
        }
        return options.concat([...branches.entries()]
          .sort((a, b) => a[1].localeCompare(b[1], 'zh-CN'))
          .map(([value, label]) => ({ kind: 'domain', value, label })));
      }
      const domains = new Set();
      const categories = new Map();
      const tags = new Map();
      for (const node of nodes.values()) {
        if (node.role === 'paper' && node.meta?.discipline) domains.add(String(node.meta.discipline));
        if (node.role === 'paper') for (const tag of node.meta?.tags || []) tags.set(tag, tag);
        if (node.role === 'category') {
          const path = node.meta?.path || {};
          if (path.discipline) domains.add(String(path.discipline));
          categories.set(node.id, node.label || node.title);
        }
        if (node.role === 'tag') tags.set(node.meta?.tag || node.label, node.label || node.title);
      }
      const sorted = (values) => [...values].sort((a, b) => String(a[1] || a).localeCompare(String(b[1] || b), 'zh-CN'));
      options.push(...sorted([...domains].map((value) => [value, value]))
        .map(([value, label]) => ({ kind: 'domain', value, label })));
      options.push(...sorted(categories)
        .map(([value, label]) => ({ kind: 'category', value, label })));
      options.push(...sorted(tags)
        .map(([value, label]) => ({ kind: 'tag', value, label })));
      return options;
    }

    renderFilters() {
      if (!this.filterBar) return;
      const valid = this.filterOptions.some((item) => item.kind === this.filter.kind && item.value === this.filter.value);
      if (!valid) this.filter = { kind: 'all', value: '' };
      const groups = [
        { kind: 'all', label: '范围', items: this.filterOptions.filter((item) => item.kind === 'all') },
        { kind: 'domain', label: '领域', items: this.filterOptions.filter((item) => item.kind === 'domain') },
        { kind: 'category', label: '分类', items: this.filterOptions.filter((item) => item.kind === 'category') },
        { kind: 'tag', label: '标签', items: this.filterOptions.filter((item) => item.kind === 'tag') },
      ].filter((group) => group.items.length);
      this.filterBar.replaceChildren(...groups.map((group) => {
        const wrapper = document.createElement('div');
        wrapper.className = `graph-filter-group graph-filter-${group.kind}`;
        wrapper.setAttribute('role', 'group');
        wrapper.setAttribute('aria-label', group.label);
        const heading = document.createElement('span');
        heading.className = 'graph-filter-label';
        heading.textContent = group.label;
        wrapper.append(heading);
        for (const item of group.items) {
          const button = document.createElement('button');
          button.type = 'button';
          button.className = 'graph-filter-button';
          button.dataset.graphFilterKind = item.kind;
          button.dataset.graphFilterValue = item.value;
          button.textContent = item.label;
          if (this.scope === 'motivation_tree' && item.kind === 'domain') {
            button.dataset.tone = domainTone(item.value);
            button.classList.add('graph-domain-filter');
          }
          button.title = item.kind === 'category' ? this.nodes.get(item.value)?.title || item.label : item.label;
          const active = this.filter.kind === item.kind && this.filter.value === item.value;
          button.classList.toggle('active', active);
          button.setAttribute('aria-pressed', String(active));
          wrapper.append(button);
        }
        return wrapper;
      }));
    }

    async setFilter(kind, value) {
      this.filter = { kind, value };
      this.selected = '';
      this.root.classList.remove('detail-open');
      if (this.detail) this.detail.hidden = true;
      await this.render(this.payload);
    }

    selectedFilterIds(nodes = this.nodes, edges = this.edges) {
      if (!this.filter || this.filter.kind === 'all') return null;
      const ids = new Set();
      if (this.scope === 'motivation_tree') {
        for (const node of nodes.values()) {
          if (node.meta?.branch?.key === this.filter.value) ids.add(node.id);
        }
        return ids;
      }
      const paperIds = new Set();
      const categoryIds = new Set();
      const tagIds = new Set();
      const addPaper = (id) => { if (nodes.get(id)?.role === 'paper') paperIds.add(id); };
      if (this.filter.kind === 'domain') {
        for (const node of nodes.values()) {
          const path = node.meta?.path || {};
          if (node.role === 'paper' && String(node.meta?.discipline || '') === this.filter.value) addPaper(node.id);
          if (node.role === 'category' && String(path.discipline || node.meta?.discipline || '') === this.filter.value) categoryIds.add(node.id);
        }
      } else if (this.filter.kind === 'category') {
        const selected = nodes.get(this.filter.value);
        if (selected?.role === 'category') categoryIds.add(selected.id);
        for (const node of nodes.values()) {
          if (node.role !== 'category') continue;
          let parent = node.parent;
          const visited = new Set([node.id]);
          while (parent && !visited.has(parent)) {
            visited.add(parent);
            if (parent === this.filter.value) { categoryIds.add(node.id); break; }
            parent = nodes.get(parent)?.parent || '';
          }
        }
      } else if (this.filter.kind === 'tag') {
        for (const node of nodes.values()) {
          if (node.role === 'paper' && node.meta?.tags?.includes(this.filter.value)) addPaper(node.id);
          if (node.role === 'tag' && (node.meta?.tag || node.label) === this.filter.value) tagIds.add(node.id);
        }
      }
      for (const edge of edges) {
        if (edge.role === 'membership' && categoryIds.has(edge.target)) addPaper(edge.source);
        if (edge.role === 'tag' && (tagIds.has(edge.target) || tagIds.has(edge.source))) {
          addPaper(edge.source); addPaper(edge.target);
        }
      }
      for (const edge of edges) {
        if (edge.role === 'membership' && paperIds.has(edge.source)) categoryIds.add(edge.target);
        if (edge.role === 'tag' && paperIds.has(edge.source)) tagIds.add(edge.target);
        if (paperIds.has(edge.source) && nodes.get(edge.target)?.role === 'external') ids.add(edge.target);
        if (paperIds.has(edge.target) && nodes.get(edge.source)?.role === 'external') ids.add(edge.source);
      }
      for (const id of paperIds) ids.add(id);
      categoryIds.forEach((id) => ids.add(id));
      tagIds.forEach((id) => ids.add(id));
      return ids;
    }

    bind() {
      const canvas = this.canvas;
      if (!canvas || this.bound) return;
      this.bound = true;
      this.events = new AbortController();
      const listen = (target, type, handler, options = {}) => target?.addEventListener(type, handler, {...options, signal: this.events.signal});
      listen(canvas, 'pointerdown', (event) => this.onPointerDown(event));
      listen(canvas, 'pointermove', (event) => this.onPointerMove(event));
      listen(canvas, 'pointerup', (event) => this.onPointerUp(event));
      listen(canvas, 'pointercancel', (event) => this.onPointerCancel(event));
      listen(canvas, 'wheel', (event) => this.onWheel(event), { passive: false });
      listen(canvas, 'keydown', (event) => this.onKeyDown(event));
      if (this.scope !== 'library') {
        listen(canvas, 'pointerover', (event) => {
          if (event.pointerType === 'touch' || this.pointers?.size) return;
          this.hovered = event.target.closest?.('[data-node-id]')?.dataset.nodeId || '';
          this.highlight(this.hovered || this.selected);
        });
        listen(canvas, 'pointerleave', () => {
          this.hovered = '';
          this.highlight(this.selected);
        });
      }
      this.root.querySelectorAll('[data-graph-zoom]').forEach((button) => listen(button, 'click', () => {
        const factor = button.dataset.graphZoom === 'in' ? 1.25 : 0.8;
        this.zoomAt(factor, this.viewCenter());
      }));
      listen(this.root.querySelector('[data-graph-fit]'), 'click', () => this.fit({ overview: true }));
      listen(this.root.querySelector('[data-graph-home]'), 'click', () => {
        this.userZoomed = false; this.fit();
      });
      listen(this.root.querySelector('[data-tree-rebuild]'), 'click', () => this.rebuildTree());
      listen(this.root.querySelector('[data-graph-fullscreen]'), 'click', () => this.toggleFullscreen());
      listen(this.root.querySelector('[data-graph-detail-close]'), 'click', () => this.closeDetail());
      this.root.querySelectorAll('[data-graph-menu-toggle]').forEach((button) => listen(button, 'click', (event) => {
        event.stopPropagation();
        this.toggleMenu(button.dataset.graphMenuToggle);
      }));
      listen(document, 'click', (event) => {
        if (!event.target.closest('.graph-menu')) this.closeMenus();
      });
      listen(document, 'keydown', (event) => {
        if (event.key === 'Escape') { this.closeMenus(); if (this.fullscreen) this.toggleFullscreen(false); }
      });
      this.root.querySelectorAll('[data-layer]').forEach((input) => listen(input, 'change', () => {
        this.layers[input.dataset.layer] = input.checked;
        if (input.dataset.layer === 'external') this.reload();
        else this.applyLayers();
      }));
      listen(this.root.querySelector('[data-graph-max-papers]'), 'change', (event) => {
        this.root.dataset.maxPapers = event.target.value;
        this.reload();
      });
      listen(this.root.querySelector('[data-graph-evidence]'), 'change', (event) => {
        this.showEvidence = event.target.checked;
        this.applyLayers();
      });
      this.root.querySelectorAll('[data-graph-export]').forEach((button) => listen(button, 'click', () => {
        this.closeMenus();
        this.exportGraph(button.dataset.graphExport, button.dataset.exportScope === 'viewport');
      }));
      listen(this.searchInput, 'input', () => {
        this.searchTerm = this.searchInput.value.trim().toLowerCase();
        this.applySearch();
      });
      listen(this.searchInput, 'keydown', (event) => {
        if (event.key !== 'Enter') return;
        event.preventDefault();
        const first = [...this.nodes.values()].find((node) => !node.hidden && this.matchesSearch(node));
        if (first) this.select(first.id, { focus: true, reveal: true });
      });
      listen(this.filterBar, 'click', (event) => {
        const button = event.target.closest?.('[data-graph-filter-kind]');
        if (!button || !this.filterBar.contains(button)) return;
        this.setFilter(button.dataset.graphFilterKind || 'all', button.dataset.graphFilterValue || '');
      });
      const resize = () => {
        const narrow = this.canvas.parentElement.clientWidth < 600;
        // Keep SVG units aligned with CSS pixels before an asynchronous layout.
        // Otherwise a desktop viewBox scales every label down on mobile.
        this.fit({ keepZoom: true });
        if (this.scope !== 'library' && this.data && narrow !== this.narrow) this.render(this.data, { keepView: true });
      };
      this.observer = new ResizeObserver(resize);
      if (this.canvas.parentElement) this.observer.observe(this.canvas.parentElement);
      listen(document, 'fullscreenchange', () => {
        const nativeActive = Boolean(document.fullscreenElement && this.root.contains(document.fullscreenElement));
        if (!nativeActive && this.nativeFullscreenActive) this.fullscreenRequested = false;
        const staleEntry = nativeActive && !this.fullscreenRequested;
        this.nativeFullscreenActive = nativeActive;
        if (staleEntry) document.exitFullscreen().catch(() => {});
        this.fullscreen = staleEntry ? false : (nativeActive || this.fullscreenRequested);
        this.root.classList.toggle('is-fullscreen', this.fullscreen);
        const button = this.root.querySelector('[data-graph-fullscreen]');
        if (button) {
          button.setAttribute('aria-pressed', String(this.fullscreen));
          button.title = button.ariaLabel = this.fullscreen ? '退出全屏' : '全屏查看图谱';
          button.innerHTML = icon(this.fullscreen ? 'minimize' : 'maximize');
          refreshIcons();
        }
        this.fit({ keepZoom: true });
      });
    }

    destroy() {
      this.events?.abort();
      this.layoutEngine?.terminateWorker();
      this.destroyed = true;
      this.renderVersion += 1;
      this.abort?.abort();
      this.observer?.disconnect();
      clearTimeout(this.treePoll);
      this.canvas?.replaceChildren();
    }

    /* ---------------------------------------------------------------- data */
    async reload() {
      if (!this.endpoint) return;
      this.abort?.abort();
      this.abort = new AbortController();
      const params = new URLSearchParams();
      if (this.scope !== 'motivation_tree') params.set('include_external', String(Boolean(this.layers.external)));
      const scope = this.root.dataset;
      for (const key of ['discipline', 'field', 'subfield']) if (scope[key]) params.set(key, scope[key]);
      if (this.scope === 'paper') {
        params.set('max_papers', this.root.dataset.maxPapers || '5');
      } else if (this.scope === 'library') {
        params.set('max_external', String(this.options.maxExternal || 40));
      } else {
        params.set('focus', new URLSearchParams(location.search).get('focus') || '');
      }
      this.setStatus('正在加载图谱…');
      try {
        const response = await fetch(`${this.endpoint}?${params}`, { signal: this.abort.signal });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.message || `图谱加载失败（${response.status}）`);
        await this.render(data, { keepView: this.scope === 'motivation_tree' && Boolean(this.data?.nodes.length) });
      } catch (error) {
        if (error.name === 'AbortError') return;
        // Keep the last good drawing; say plainly that it is stale.
        this.setStatus(`数据更新失败：${error.message}。下方清单仍可继续使用。`, { error: true, retry: true });
      }
    }

    async render(data, { keepView = false } = {}) {
      if (!data || !Array.isArray(data.nodes)) { this.setStatus('图谱数据格式异常。', { error: true }); return; }
      this.clearCitationOverlay();
      this.layoutEngine?.terminateWorker();
      const version = ++this.renderVersion;
      const previous = this.nodes;
      const incoming = new Set(data.nodes.map(n => n.id));
      const retain = this.options.managed && data.scope === "library" && data.focus && previous.has(data.focus);
      if (retain) {
        data = { ...data, nodes: [...data.nodes, ...[...previous.values()].filter(n => !incoming.has(n.id)).map(n => ({ ...n, retained: true }))] };
        keepView = true;
      }
      this.root.setAttribute('aria-busy', 'true');
      let layout;
      try {
        layout = await this.computeLayout(data);
      } catch (error) {
        if (version !== this.renderVersion || this.destroyed) return;
        this.root.setAttribute('aria-busy', 'false');
        this.setStatus(`图谱布局失败：${error.message}。可在下方清单继续查看。`, { error: true, retry: true });
        return;
      }
      if (version !== this.renderVersion || this.destroyed) return;
      if (retain) {
        const placed = [];
        for (const node of layout.nodes.values()) {
          const old = previous.get(node.id);
          if (old) { node.x = old.x; node.y = old.y; placed.push(node); }
        }
        const anchor = previous.get(data.focus);
        for (const node of layout.nodes.values()) {
          if (previous.has(node.id)) continue;
          search: for (let ring = 1; ; ring++) {
            for (let dx = -ring; dx <= ring; dx++) for (let dy = -ring; dy <= ring; dy++) {
              if (Math.max(Math.abs(dx), Math.abs(dy)) !== ring) continue;
              const x = anchor.x + dx * Math.max(320, node.width + 40);
              const y = anchor.y + dy * Math.max(160, node.height + 40);
              if (placed.some(p => Math.abs(p.x-x) < (p.width+node.width)/2+24 && Math.abs(p.y-y) < (p.height+node.height)/2+24)) continue;
              node.x=x; node.y=y; placed.push(node); break search;
            }
          }
        }
        layout.regions = [];
      }
      this.data = data;
      this.payload = data;
      this.nodes = layout.nodes;
      this.byId = layout.nodes;
      this.edges = layout.edges;
      this.routes = layout.routes;
      this.regions = layout.regions || [];
      this.narrow = layout.narrow;
      this.hovered = '';
      this.selected = this.selected && data.nodes.some((n) => n.id === this.selected)
        ? this.selected : (data.scope !== 'paper' ? (data.focus || '') : '');
      if (this.scope === 'paper') {
        const width = this.canvas.parentElement.clientWidth;
        const scale = Math.min(1, (width - 48) / Math.max(1, layout.width));
        this.canvas.parentElement.style.height = !this.fullscreen
          ? Math.min(this.narrow ? 1200 : 840, Math.max(this.narrow ? 440 : 480, layout.height * scale + 56)) + 'px' : '';
      }
      this.draw();
      this.filterOptions = this.options.managed ? [] : this.makeFilterOptions();
      this.renderFilters();
      this.applyLayers();
      this.applySearch();
      this.updateCounts();
      this.setStatus('');
      if (this.scope === 'motivation_tree') this.updateTreeState(data);
      if (!keepView) { this.userZoomed = false; this.fit(); }
      else this.applyTransform();
      if (this.selected) this.select(this.selected, { reveal: !keepView && this.scope === 'motivation_tree' });
      else if (this.detail) { this.detail.hidden = true; this.root.classList.remove('detail-open'); }
      this.root.setAttribute('aria-busy', 'false');
    }

    /* -------------------------------------------------------------- layout */
    async computeLayout(data) {
      this.layoutContext = JSON.stringify([data.revision, data.scope, data.focus, FONT, FONT_SMALL, document.fonts?.status, Math.floor(this.canvas.parentElement.clientWidth / 120)]);
      const narrow = this.scope !== 'library' && this.canvas.parentElement.clientWidth < 600;
      const nodes = data.nodes.map((raw) => {
        return this.prepareNode(raw, data);
      });
      const byId = new Map(nodes.map((node) => [node.id, node]));
      const edges = data.edges.filter((edge) => byId.has(edge.source) && byId.has(edge.target)).map((edge) => ({ ...edge }));
      const options = this.makeFilterOptions(byId);
      if (!options.some((item) => item.kind === this.filter.kind && item.value === this.filter.value)) {
        this.filter = { kind: 'all', value: '' };
      }
      const filterIds = this.selectedFilterIds(byId, edges);
      const visible = nodes.filter((node) => !filterIds || filterIds.has(node.id));
      const visibleIds = new Set(visible.map((node) => node.id));
      const visibleEdges = edges.filter((edge) => visibleIds.has(edge.source) && visibleIds.has(edge.target));
      for (const node of nodes) node.layoutHidden = !visibleIds.has(node.id);
      for (const edge of edges) {
        edge.supporting = data.scope === 'motivation_tree'
          ? edge.role === 'motivation' && edge.primary === false
          : Boolean(edge.supporting);
      }
      const layout = data.mode === 'aggregate' ? this.layoutAggregate(visible)
        : data.scope === 'paper'
        ? await this.layoutFocusGraph(nodes, edges, data.focus, narrow)
        : data.scope === 'motivation_tree' ? await this.layoutMotivation(visible, visibleEdges)
          : this.layoutLibrary(visible, byId, visibleEdges);
      return { ...layout, nodes: byId, edges, narrow };
    }

    prepareNode(raw, data) {
      const compact = raw.role === 'category' || raw.role === 'tag';
      const font = compact ? FONT_SMALL : FONT;
      const maxWidth = compact ? 220 : 260;
      const lines = wrapLabel(raw.label || raw.title || '', maxWidth, font);
      const domain = raw.meta?.aggregate ? raw.meta.count + ' 篇 · 点击展开'
        : data.scope === 'motivation_tree' ? raw.meta?.branch?.label || '未分类' : '';
      const boundary = (data.boundary || this.options.boundary || []).some(item => item.paper_id === raw.meta?.paper_id);
      const domainLines = domain ? wrapLabel(domain, maxWidth, FONT_SMALL) : [];
      if (raw.meta?.citation_context) domainLines.push('图外论文');
      else if (boundary || raw.meta?.predecessor_out_of_scope) domainLines.push('前驱在范围外');
      else if (raw.meta?.out_of_scope) domainLines.push('范围外上下文');
      return {...raw, lines, font, domainLines, tone: domainTone(raw.meta?.branch?.key),
        width: Math.max(compact ? 80 : 132, 36 + Math.max(...lines.map(line => measure(line, font)),
          ...domainLines.map(line => measure(line, FONT_SMALL)))),
        height: 28 + lines.length * 18 + (raw.focus ? 18 : 0)
          + (domainLines.length ? 10 + domainLines.length * 16 : 0)};
    }

    setCitationOverlay(graph) {
      if (this.destroyed || this.scope !== 'library' || !this.data) return;
      const previousAnchor = this.nodes.get(graph.focus);
      this.clearCitationOverlay();
      const anchor = this.nodes.get(graph.focus) || (previousAnchor && {...previousAnchor});
      if (!anchor) return;
      this.citationOverlay = {edges: this.edges, cites: this.layers.cites, added: new Set()};
      const overlay = this.citationOverlay, added = [];
      for (const raw of graph.nodes || []) {
        if (this.nodes.has(raw.id)) continue;
        const node = this.prepareNode({...raw, focus: false, meta: {...raw.meta, citation_context: true}}, graph);
        if (raw.id === graph.focus) { node.x = anchor.x; node.y = anchor.y; }
        else added.push(node);
        this.nodes.set(node.id, node); overlay.added.add(node.id);
      }
      const placed = [...this.nodes.values()].filter(n => !added.includes(n) && !n.hidden);
      const rect = this.canvas.getBoundingClientRect(), {x, y, k} = this.transform;
      for (const node of added) {
        const incoming = (graph.edges || []).some(e => e.source === node.id && e.target === graph.focus);
        const candidates = [];
        for (let ring = 1; ring <= placed.length + 2; ring++) {
          for (let dx = -ring; dx <= ring; dx++) for (let dy = -ring; dy <= ring; dy++) {
            if (Math.max(Math.abs(dx), Math.abs(dy)) !== ring) continue;
            const nx = anchor.x + dx * Math.max(260, (anchor.width + node.width) / 2 + 36);
            const ny = anchor.y + dy * Math.max(100, (anchor.height + node.height) / 2 + 28);
            if (placed.some(p => Math.abs(p.x - nx) < (p.width + node.width) / 2 + 20 && Math.abs(p.y - ny) < (p.height + node.height) / 2 + 20)) continue;
            const onScreen = nx*k+x > node.width*k/2+12 && nx*k+x < rect.width-node.width*k/2-12
              && ny*k+y > node.height*k/2+12 && ny*k+y < rect.height-node.height*k/2-55;
            candidates.push({x: nx, y: ny, score: (onScreen ? 0 : 10000) + ring*100 + Math.abs(dy)*10 + ((incoming ? dx <= 0 : dx >= 0) ? 0 : 20)});
          }
          if (candidates.some(c => c.score < 10000)) break;
          if (candidates.length && ring >= 3) break;
        }
        candidates.sort((a,b) => a.score-b.score);
        Object.assign(node, candidates[0] || {x: anchor.x, y: anchor.y + 180});
        placed.push(node);
      }
      const edges = new Map(overlay.edges.map(e => [edgeKey(e), e]));
      for (const edge of graph.edges || []) {
        if (this.nodes.has(edge.source) && this.nodes.has(edge.target)) edges.set(edgeKey(edge), {...edge, citationOverlay: true});
      }
      this.edges = [...edges.values()]; this.layers.cites = true; this.selected = graph.focus;
      this.draw(); this.applyLayers(); this.highlight(this.selected); this.applyTransform();
    }

    clearCitationOverlay() {
      const overlay = this.citationOverlay;
      if (!overlay) return;
      overlay.added.forEach(id => this.nodes.delete(id));
      this.edges = overlay.edges; this.layers.cites = overlay.cites; this.citationOverlay = null;
      if (!this.nodes.has(this.selected)) this.selected = '';
      this.draw(); this.applyLayers(); this.highlight(this.selected); this.applyTransform();
    }

    clearSelection() {
      this.clearCitationOverlay(); this.selected = ''; this.hovered = '';
      this.highlight(''); this.degradeLabels(); this.applyTransform();
    }

    layoutAggregate(nodes) {
      // Bounded navigation nodes only: no invented relationships between areas.
      const width = this.canvas.parentElement.clientWidth || 800;
      const cellWidth = Math.max(240, ...nodes.map(n => n.width + 20));
      const columns = Math.max(1, Math.min(4, Math.floor((width - 56) / (cellWidth + 32))));
      let rowY = 32;
      for (let i = 0; i < nodes.length; i += columns) {
        const row = nodes.slice(i, i + columns);
        const height = Math.max(...row.map(n => n.height)) + 26;
        row.forEach((node, column) => {
          node.width = Math.max(node.width, cellWidth);
          node.height = Math.max(node.height, 106);
          node.x = 32 + column * (cellWidth + 32) + cellWidth / 2;
          node.y = rowY + node.height / 2;
        });
        rowY += Math.max(height, 136);
      }
      return { routes: new Map(), regions: [], width: columns * (cellWidth + 32) + 32, height: rowY };
    }

    async layoutMotivation(nodes, edges) {
      const primary = edges.filter((edge) => edge.role === 'motivation' && edge.primary !== false);
      const mainlineIds = new Set(primary.flatMap((edge) => [edge.source, edge.target]));
      for (const node of nodes) node.layoutHidden = !this.options.managed && !mainlineIds.has(node.id);
      if (!primary.length && !this.options.managed) return { routes: new Map() };
      this.layoutEngine ||= window.CarrelGraphLayout.create();
      const result = await this.layoutEngine.layout({
        cacheContext: this.layoutContext,
        id: 'motivation-mainline',
        layoutOptions: {
          'elk.algorithm': 'layered', 'elk.direction': 'RIGHT', 'elk.edgeRouting': 'ORTHOGONAL',
          'elk.randomSeed': '1', 'elk.spacing.nodeNode': '44', 'elk.spacing.componentComponent': '64',
          'elk.layered.spacing.nodeNodeBetweenLayers': '110', 'elk.spacing.edgeNode': '20',
          'elk.layered.spacing.edgeEdgeBetweenLayers': '18',
          'elk.layered.nodePlacement.strategy': 'NETWORK_SIMPLEX',
          'elk.layered.considerModelOrder.strategy': 'NODES_AND_EDGES',
        },
        children: nodes.filter((node) => !node.layoutHidden).map((node) => ({
          id: node.id, width: node.width, height: node.height,
        })),
        edges: primary.map((edge, index) => ({ id: String(index), sources: [edge.source], targets: [edge.target] })),
      });
      const byId = new Map(nodes.map((node) => [node.id, node]));
      for (const child of result.children) {
        const node = byId.get(child.id);
        node.x = child.x + child.width / 2;
        node.y = child.y + child.height / 2;
      }
      const routes = new Map();
      for (const line of result.edges || []) {
        const section = line.sections[0];
        routes.set(edgeKey(primary[Number(line.id)]), [section.startPoint, ...(section.bendPoints || []), section.endPoint]);
      }
      // Supporting relations remain in the detail panel. Hovering or selecting
      // a paper must not reintroduce crossings into the mainline canvas.
      return { routes, width: result.width, height: result.height };
    }

    layoutLibrary(nodes, byId, edges) {
      const ordered = nodes.slice().sort((a, b) => String(a.sort_key).localeCompare(String(b.sort_key)));
      const categories = ordered.filter((node) => node.role === 'category');
      const papers = ordered.filter((node) => node.role === 'paper');
      const tags = ordered.filter((node) => node.role === 'tag');
      const externals = ordered.filter((node) => node.role === 'external');
      const categoryById = new Map(categories.map((node) => [node.id, node]));
      const primaryParent = new Map();
      for (const paper of papers) {
        const branch = paper.meta?.branch;
        if (!branch?.key) continue;
        if (!categoryById.has(branch.key)) categoryById.set(branch.key, { id: branch.key, label: branch.label, title: branch.label });
        primaryParent.set(paper.id, branch.key);
      }
      const preferred = (category) => {
        if (this.filter.kind === 'domain') return category?.meta?.discipline === this.filter.value;
        if (this.filter.kind !== 'category') return false;
        const visited = new Set();
        while (category && !visited.has(category.id)) {
          if (category.id === this.filter.value) return true;
          visited.add(category.id);
          category = byId.get(category.parent);
        }
        return false;
      };
      for (const paper of papers) {
        const memberships = edges.filter((edge) => edge.role === 'membership' && edge.source === paper.id);
        memberships.sort((a, b) => Number(preferred(byId.get(b.target))) - Number(preferred(byId.get(a.target)))
          || Number(b.primary) - Number(a.primary) || a.target.localeCompare(b.target));
        if (memberships.length) primaryParent.set(paper.id, memberships[0].target);
      }
      const groups = new Map();
      for (const paper of papers) {
        const categoryId = categoryById.has(primaryParent.get(paper.id)) ? primaryParent.get(paper.id) : '__uncategorized__';
        if (!groups.has(categoryId)) groups.set(categoryId, []);
        groups.get(categoryId).push(paper);
      }
      const leafIds = [...groups.keys()].sort((a, b) => {
        const left = categoryById.get(a)?.title || '未分类';
        const right = categoryById.get(b)?.title || '未分类';
        return left.localeCompare(right, 'zh-CN');
      });
      const regions = [];
      // Categories remain in the data for filtering/details, not as duplicate nodes.
      for (const category of categories) category.layoutHidden = true;
      for (const categoryId of leafIds) {
        const category = categoryById.get(categoryId);
        const papersInGroup = groups.get(categoryId) || [];
        const cellWidth = Math.max(200, ...papersInGroup.map((paper) => paper.width));
        const meanHeight = papersInGroup.reduce((sum, paper) => sum + paper.height, 0) / papersInGroup.length;
        const columns = clamp(Math.ceil(Math.sqrt(papersInGroup.length * (meanHeight + 32) / (cellWidth + 28) * 1.6)),
          Math.min(2, papersInGroup.length), papersInGroup.length);
        const breadcrumb = category?.parent ? byId.get(category.parent)?.title || '' : '';
        const width = Math.max(columns * cellWidth + (columns - 1) * 28 + 48,
          measure(category?.label || '未分类', FONT) + 64, measure(breadcrumb, FONT_SMALL) + 48);
        const header = breadcrumb ? 72 : 52;
        const members = [];
        let rowY = header;
        for (let index = 0; index < papersInGroup.length; index += columns) {
          const row = papersInGroup.slice(index, index + columns);
          row.forEach((paper, column) => {
            paper.x = 24 + column * (cellWidth + 28) + cellWidth / 2;
            paper.y = rowY + paper.height / 2;
            paper.regionId = categoryId;
            members.push(paper);
          });
          rowY += Math.max(...row.map((paper) => paper.height)) + 24;
        }
        regions.push({ id: `region:${categoryId}`, categoryId,
          label: breadcrumb || (category ? '' : '未分类'),
          title: category?.label || '未分类', tone: domainTone(category?.title),
          width, height: rowY, members, papers: papersInGroup, header });
      }
      // Pack whole category regions into balanced rows; sizes include every title.
      const area = regions.reduce((sum, region) => sum + (region.width + 40) * (region.height + 40), 0);
      const rowWidth = Math.max(600, ...regions.map((region) => region.width), Math.sqrt(area * 1.5));
      let x = 24, y = 24, rowHeight = 0, maxRight = rowWidth;
      for (const region of regions) {
        if (x > 24 && x + region.width > rowWidth + 24) { x = 24; y += rowHeight + 40; rowHeight = 0; }
        region.x = x; region.y = y;
        for (const node of region.members) {
          node.x += x; node.y += y;
        }
        x += region.width + 40;
        maxRight = Math.max(maxRight, x - 40);
        rowHeight = Math.max(rowHeight, region.height);
      }
      let bottom = y + rowHeight;
      for (const extra of [tags, externals]) {
        const columns = Math.max(1, Math.floor(rowWidth / (Math.max(148, ...extra.map((node) => node.width)) + 28)));
        const cellWidth = Math.max(148, ...extra.map((node) => node.width));
        let rowY = bottom + 40;
        for (let index = 0; index < extra.length; index += columns) {
          const row = extra.slice(index, index + columns);
          row.forEach((node, column) => {
            node.x = 24 + column * (cellWidth + 28) + cellWidth / 2;
            node.y = rowY + node.height / 2;
          });
          rowY += Math.max(...row.map((node) => node.height)) + 24;
        }
        if (extra.length && this.layers[LAYER_FOR_ROLE[extra[0].role]] !== false) bottom = rowY;
      }
      return { routes: new Map(), regions, width: maxRight + 24, height: bottom + 24 };
    }

    async layoutFocusGraph(nodes, edges, focusId, narrow) {
      const focus = nodes.find((node) => node.id === focusId) || nodes.find((node) => node.role === 'paper');
      if (!focus) return { routes: new Map(), width: 320, height: 200 };
      const contexts = nodes.filter((node) => node.role === 'category' || node.role === 'tag');
      const contextIds = new Set(contexts.map((node) => node.id));
      const peers = nodes.filter((node) => node !== focus && !contextIds.has(node.id));
      const order = [...contexts, focus, ...peers];
      const rank = new Map(order.map((node, index) => [node.id, index]));
      const connected = new Set(edges.filter((edge) => edge.source === focus.id || edge.target === focus.id)
        .flatMap((edge) => [edge.source, edge.target]));
      // Keep every peer connected; additional metadata evidence opens on selection.
      for (const edge of edges) edge.supporting = edge.source !== focus.id && edge.target !== focus.id
        && (edge.role === 'membership' || edge.role === 'tag');
      for (const peer of peers) {
        if (connected.has(peer.id)) continue;
        const bridge = edges.filter((edge) => (edge.source === peer.id && connected.has(edge.target))
          || (edge.target === peer.id && connected.has(edge.source)))
          .sort((a, b) => Number(b.role === 'tag') - Number(a.role === 'tag'))[0];
        if (bridge) bridge.supporting = false;
      }

      const children = order.map((node) => ({
        id: node.id, width: node.width, height: node.height,
        layoutOptions: narrow ? { 'elk.portConstraints': 'FIXED_POS' }
          : { 'elk.partitioning.partition': String(node === focus ? 1 : contextIds.has(node.id) ? 0 : 2) },
        ports: narrow ? [
          { id: node.id + ':in', x: node.width / 2, y: 0, width: 0, height: 0, layoutOptions: { 'elk.port.side': 'NORTH' } },
          { id: node.id + ':out', x: node.width / 2, y: node.height, width: 0, height: 0, layoutOptions: { 'elk.port.side': 'SOUTH' } },
        ] : [],
      }));
      const childrenById = new Map(children.map((node) => [node.id, node]));
      const entries = edges.map((edge, index) => {
        let source = edge.source, target = edge.target;
        if (rank.get(source) > rank.get(target)) [source, target] = [target, source];
        const reversed = source !== edge.source;
        if (narrow) {
          const side = edge.role === 'membership' || edge.role === 'tag' ? 'EAST' : 'WEST';
          for (const id of [source, target]) childrenById.get(id).ports.push({
            id: `${id}:edge:${index}`, width: 0, height: 0, layoutOptions: { 'elk.port.side': side },
          });
          source += ':edge:' + index;
          target += ':edge:' + index;
        }
        return { id: String(index), sources: [source], targets: [target], reversed };
      });
      const constraints = [];
      if (narrow) {
        for (const child of children) {
          for (const side of ['EAST', 'WEST']) {
            const ports = child.ports.filter((port) => port.layoutOptions['elk.port.side'] === side);
            ports.forEach((port, index) => {
              port.x = side === 'EAST' ? child.width : 0;
              port.y = (index + 1) * child.height / (ports.length + 1);
            });
          }
        }
        // Invisible ordering constraints keep the mobile graph in one column.
        order.slice(1).forEach((node, index) => constraints.push({
          id: 'order:' + index, sources: [order[index].id + ':out'], targets: [node.id + ':in'],
          layoutOptions: { 'elk.layered.priority.straightness': '100' },
        }));
      }
      this.layoutEngine ||= window.CarrelGraphLayout.create();
      const result = await this.layoutEngine.layout({
        cacheContext: this.layoutContext,
        id: 'paper-graph',
        layoutOptions: {
          'elk.algorithm': 'layered', 'elk.direction': narrow ? 'DOWN' : 'RIGHT',
          'elk.edgeRouting': 'ORTHOGONAL', 'elk.randomSeed': '1',
          'elk.partitioning.activate': String(!narrow),
          'elk.spacing.nodeNode': narrow ? '16' : '24', 'elk.spacing.edgeNode': narrow ? '10' : '16',
          'elk.spacing.edgeEdge': '8', 'elk.layered.spacing.nodeNodeBetweenLayers': narrow ? '22' : '72',
          'elk.layered.spacing.edgeEdgeBetweenLayers': narrow ? '8' : '12',
          'elk.layered.nodePlacement.strategy': 'NETWORK_SIMPLEX',
          'elk.layered.considerModelOrder.strategy': 'NODES_AND_EDGES',
        },
        children, edges: [...entries, ...constraints],
      });
      const nodesById = new Map(nodes.map((node) => [node.id, node]));
      for (const child of result.children) {
        const node = nodesById.get(child.id);
        node.x = child.x + child.width / 2;
        node.y = child.y + child.height / 2;
      }
      const routes = new Map();
      for (const line of result.edges || []) {
        if (line.id.startsWith('order:')) continue;
        const index = Number(line.id), section = line.sections[0];
        const points = [section.startPoint, ...(section.bendPoints || []), section.endPoint];
        if (entries[index].reversed) points.reverse();
        routes.set(edgeKey(edges[index]), points);
      }
      return { routes, width: result.width, height: result.height };
    }

    computeBounds(nodes) {
      let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
      for (const node of nodes) {
        if (node.x === undefined) { node.x = 0; node.y = 0; }
        minX = Math.min(minX, node.x - node.width / 2);
        maxX = Math.max(maxX, node.x + node.width / 2);
        minY = Math.min(minY, node.y - node.height / 2);
        maxY = Math.max(maxY, node.y + node.height / 2);
      }
      if (this.routes?.size) {
        for (const points of this.routes.values()) for (const point of points) {
          minX = Math.min(minX, point.x); maxX = Math.max(maxX, point.x);
          minY = Math.min(minY, point.y); maxY = Math.max(maxY, point.y);
        }
      }
      if (!Number.isFinite(minX)) { minX = 0; maxX = 320; minY = 0; maxY = 200; }
      for (const region of this.regions || []) {
        if (region.hidden) continue;
        minX = Math.min(minX, region.x); maxX = Math.max(maxX, region.x + region.width);
        minY = Math.min(minY, region.y); maxY = Math.max(maxY, region.y + region.height);
      }
      this.bounds = { minX, maxX, minY, maxY, width: maxX - minX, height: maxY - minY };
    }

    /* ---------------------------------------------------------------- draw */
    draw() {
      const canvas = this.canvas;
      if (!canvas) return;
      const reusable = this.nodeViews || new Map();
      const reusableEdges = this.edgeEls || new Map();
      this.nodeViews = new Map();
      const oldGroup = this.group;
      const defs = el('defs');
      const marker = el('marker', { id: this.markerId, viewBox: '0 0 8 8', refX: 8, refY: 4,
        markerWidth: 7, markerHeight: 7, markerUnits: 'userSpaceOnUse', orient: 'auto-start-reverse' });
      marker.append(el('path', { class: 'graph-arrow', d: 'M0,0 L8,4 L0,8 z' }));
      defs.append(marker);
      if (this.scope === 'motivation_tree') {
        const semantic = marker.cloneNode(true);
        semantic.id += '-semantic'; semantic.querySelector('path').classList.add('semantic'); defs.append(semantic);
      }
      this.group = el('g', { class: 'graph-viewport' });
      this.regionLayer = el('g', { class: 'graph-regions' });
      this.edgeLayer = el('g', { class: 'graph-edges' });
      this.nodeLayer = el('g', { class: 'graph-nodes' });
      this.labelLayer = el('g', { class: 'graph-labels' });
      this.group.append(this.regionLayer, this.edgeLayer, this.nodeLayer, this.labelLayer);
      if (oldGroup) {
        canvas.querySelector("defs")?.remove();
        oldGroup.replaceWith(this.group);
        canvas.prepend(defs);
      } else canvas.append(defs, this.group);
      this.canvasRect = canvas.getBoundingClientRect();

      this.regionEls = new Map();
      for (const region of this.regions || []) this.drawRegion(region);
      const edgeEls = new Map();
      for (const edge of this.edges) {
        if (this.scope === 'library' && edge.role === 'membership') continue;
        if (this.scope === 'motivation_tree' && edge.primary === false) continue;
        const old = reusableEdges.get(edgeKey(edge));
        const path = this.edgeGeometry(edge);
        if (old && path) {
          old.setAttribute("d", path);
          old.dataset.basis = edge.basis || "";
          old.dataset.primary = String(edge.primary !== false);
          this.edgeLayer.append(old);
          edgeEls.set(edgeKey(edge), old);
        } else this.drawEdge(edge, edgeEls);
      }
      for (const node of this.nodes.values()) {
        if (node.layoutHidden) continue;
        const signature = JSON.stringify([node.label, node.title, node.width, node.height, node.focus, node.domainLines, node.tone, node.meta?.citation_context]);
        const old = reusable.get(node.id);
        if (old?.signature === signature) {
          node.el = old.el; node.shape = old.shape; node.text = old.text;
          node.el.setAttribute("transform", `translate(${node.x},${node.y})`);
          node.el.classList.toggle("retained", Boolean(node.retained));
          this.nodeLayer.append(node.el);
        } else this.drawNode(node);
        this.nodeViews.set(node.id, { signature, el: node.el, shape: node.shape, text: node.text });
      }
      this.edgeEls = edgeEls;
      this.degradeLabels();
      this.applyTransform();
    }

    edgeGeometry(edge) {
      const routed = this.routes?.get(edgeKey(edge));
      if (routed) return roundedRoute(routed);
      const a = this.nodes.get(edge.source);
      const b = this.nodes.get(edge.target);
      if (!a || !b || ![a.x, a.y, b.x, b.y].every(Number.isFinite)) return null;
      const dx = b.x - a.x, dy = b.y - a.y;
      if (Math.abs(dx) + Math.abs(dy) < 0.01) return null;
      // Intersect the centre line with both variable-size rectangular outlines.
      const port = (node, x, y) => {
        const scale = Math.min((node.width / 2 + 1) / Math.abs(x || 1e-9),
          (node.height / 2 + 1) / Math.abs(y || 1e-9));
        return { x: node.x + x * scale, y: node.y + y * scale };
      };
      const start = port(a, dx, dy), end = port(b, -dx, -dy);
      return `M${start.x},${start.y} L${end.x},${end.y}`;
    }

    drawEdge(edge, map) {
      const path = this.edgeGeometry(edge);
      if (!path) return;
      const line = el('path', {
        d: path,
        class: `graph-edge ${edge.role}${edge.primary ? '' : ' secondary'}`,
        'data-edge-role': edge.role,
        'data-basis': edge.basis || undefined,
        'data-edge-kind': this.edgeKind(edge),
        'data-primary': String(edge.primary !== false),
        'data-supporting': edge.supporting ? 'true' : undefined,
        'data-source': edge.source,
        'data-target': edge.target,
      });
      const title = el('title');
      title.textContent = this.edgeLabel(edge);
      line.append(title);
      if (edge.role === 'cites' || edge.role === 'motivation') {
        line.setAttribute('marker-end', `url(#${this.markerId}${edge.role === 'motivation' && edge.basis === 'semantic' ? '-semantic' : ''})`);
      }
      this.edgeLayer.append(line);
      map.set(edgeKey(edge), line);
    }

    drawRegion(region) {
      if (!this.regionLayer) return;
      const group = el('g', { class: 'graph-region', 'data-region-id': region.id,
        'data-tone': region.tone,
        'aria-hidden': 'true', 'pointer-events': 'none' });
      group.append(el('rect', { class: 'graph-region-shape', x: region.x, y: region.y,
        width: region.width, height: region.height, rx: 10 }));
      const title = el('text', { class: 'graph-region-title', x: region.x + 24, y: region.y + 28 });
      title.textContent = region.title || region.label || '未分类';
      group.append(title);
      if (region.label && region.label !== region.title) {
        const parent = el('text', { class: 'graph-region-parent', x: region.x + 24, y: region.y + 47 });
        parent.textContent = region.label;
        group.append(parent);
      }
      this.regionLayer.append(group);
      region.el = group;
      this.regionEls?.set(region.id, group);
    }

    drawNode(node) {
      const labelX = 0;
      const labelY = -node.height / 2 + 28 + (node.focus ? 18 : 0);
      const group = el('g', {
        class: `graph-node ${node.role}${node.focus ? ' focus' : ''}${node.meta?.aggregate ? ' aggregate' : ''}${node.meta?.citation_context ? ' citation-context' : ''}`,
        'data-node-id': node.id,
        'data-node-role': node.role,
        'data-tone': node.tone,
        transform: `translate(${node.x},${node.y})`,
        tabindex: '0',
        role: 'button',
        'aria-pressed': 'false',
        'aria-label': node.title || node.label,
      });
      const shape = el('rect', { class: 'graph-shape', x: -node.width / 2, y: -node.height / 2,
        width: node.width, height: node.height, rx: 6 });
      const text = el('text', {
        class: 'graph-label', 'text-anchor': 'middle',
        x: labelX,
        y: labelY,
      });
      node.labelLines = node.lines.length;
      node.lines.forEach((line, index) => {
        const span = el('tspan', { x: labelX, dy: index ? '18' : '0' });
        span.textContent = line;
        text.append(span);
      });
      const title = el('title');
      title.textContent = node.title || node.label;
      group.append(shape, text, title);
      if (node.domainLines?.length) {
        const domain = el('text', { class: 'graph-domain', 'text-anchor': 'middle', x: labelX,
          y: labelY + node.lines.length * 18 + 7 });
        node.domainLines.forEach((line, index) => {
          const span = el('tspan', { x: labelX, dy: index ? '16' : '0' });
          span.textContent = line;
          domain.append(span);
        });
        group.append(domain);
      }
      if (node.focus) {
        const caption = el('text', { class: 'graph-caption', 'text-anchor': 'middle', x: labelX, y: -node.height / 2 + 22 });
        caption.textContent = '当前论文';
        group.append(caption);
      }
      this.nodeLayer.append(group);
      const texts = [...group.querySelectorAll('text')];
      const boxes = texts.map(item => item.getBBox());
      const top = Math.min(...boxes.map(box => box.y));
      const bottom = Math.max(...boxes.map(box => box.y + box.height));
      for (const item of texts) item.setAttribute('transform', `translate(0,${-(top + bottom) / 2})`);
      group.classList.toggle("retained", Boolean(node.retained));
      node.el = group;
      node.shape = shape;
      node.text = text;
    }

    /* Node dimensions reserve the entire title, so labels never disappear. */
    degradeLabels() {
      for (const node of this.nodes.values()) {
        if (node.text) node.text.style.display = node.hidden ? 'none' : '';
      }
    }

    /* ------------------------------------------------------------ viewport */
    applyTransform() {
      if (this.group) this.group.setAttribute('transform', `translate(${this.transform.x},${this.transform.y}) scale(${this.transform.k})`);
      if (this.zoomValue) this.zoomValue.textContent = `${Math.round(this.transform.k * 100)}%`;
      this.cullViewport();
    }

    cullViewport(all = false) {
      if (!this.nodeLayer) return;
      const {x, y, k} = this.transform;
      const rect = this.canvas.getBoundingClientRect();
      for (const node of this.nodes.values()) {
        if (!node.el || node.layoutHidden) continue;
        const visible = all || (node.x + node.width/2 > (-x-160)/k && node.x-node.width/2 < (rect.width-x+160)/k && node.y+node.height/2 > (-y-160)/k && node.y-node.height/2 < (rect.height-y+160)/k);
        if (visible && !node.el.isConnected) this.nodeLayer.append(node.el);
        else if (!visible) node.el.remove();
      }
      for (const [key, line] of this.edgeEls || []) {
        const [source, target] = key.split("\u0000");
        const a = this.nodes.get(source), b = this.nodes.get(target);
        if (!a || !b) continue;
        const visible = all || (Math.max(a.x,b.x) > (-x-160)/k && Math.min(a.x,b.x) < (rect.width-x+160)/k && Math.max(a.y,b.y) > (-y-160)/k && Math.min(a.y,b.y) < (rect.height-y+160)/k);
        if (visible && !line.isConnected) this.edgeLayer.append(line);
        else if (!visible) line.remove();
      }
    }

    viewCenter() {
      const rect = this.canvas?.getBoundingClientRect();
      return { x: (rect?.width || 600) / 2, y: (rect?.height || 400) / 2 };
    }

    fit({ keepZoom = false, overview = false } = {}) {
      if (overview) { this.overview = true; this.userZoomed = false; }
      else if (!keepZoom) this.overview = false;
      const rect = this.canvas?.parentElement?.getBoundingClientRect();
      const width = rect?.width || 600;
      const height = rect?.height || 420;
      this.canvas?.setAttribute('width', width);
      this.canvas?.setAttribute('height', height);
      this.canvas?.setAttribute('viewBox', `0 0 ${width} ${height}`);
      if (!this.bounds) return;
      this.computeBounds([...this.nodes.values()].filter((node) => !node.hidden));
      const padding = 28;
      const ideal = Math.min((width - padding * 2) / Math.max(1, this.bounds.width),
        (height - padding * 2) / Math.max(1, this.bounds.height));
      if (this.scope !== 'paper' && !this.overview) {
        this.minScale = Math.min(0.2, Math.max(0.01, ideal));
        if (!(keepZoom && this.userZoomed)) this.transform = {
          k: 1, x: padding - this.bounds.minX, y: padding - this.bounds.minY,
        };
        this.applyTransform();
        return;
      }
      // Initial views favor readable labels; the fit command must still show
      // the whole graph, and subsequent zooming must not jump to a fixed floor.
      this.minScale = Math.min(0.2, Math.max(0.01, ideal));
      const floor = this.overview ? this.minScale : 0.5;
      const scale = keepZoom && this.userZoomed
        ? this.transform.k
        : clamp(Math.max(ideal, floor), this.minScale, 1.2);
      const anchor = this.overview || (ideal >= floor && !this.userZoomed)
        ? { x: (this.bounds.minX + this.bounds.maxX) / 2, y: (this.bounds.minY + this.bounds.maxY) / 2 }
        : this.anchorPoint();
      this.transform = {
        k: scale,
        x: width / 2 - anchor.x * scale,
        y: height / 2 - anchor.y * scale,
      };
      this.applyTransform();
    }

    /* Where the fitted view should look when the whole graph does not fit:
     * the focus paper, the selection, or the centre of the paper cluster. */
    anchorPoint() {
      const pick = [this.selected, this.data?.focus]
        .map((id) => (id ? this.nodes.get(id) : null)).find(Boolean);
      if (pick) return { x: pick.x, y: pick.y };
      const papers = [...this.nodes.values()].filter((node) => node.role === 'paper');
      if (papers.length) {
        return {
          x: papers.reduce((sum, node) => sum + node.x, 0) / papers.length,
          y: papers.reduce((sum, node) => sum + node.y, 0) / papers.length,
        };
      }
      return {
        x: (this.bounds.minX + this.bounds.maxX) / 2,
        y: (this.bounds.minY + this.bounds.maxY) / 2,
      };
    }

    zoomAt(factor, point) {
      const k = clamp(this.transform.k * factor, this.minScale, 3);
      const real = k / this.transform.k;
      this.transform = {
        k,
        x: point.x - (point.x - this.transform.x) * real,
        y: point.y - (point.y - this.transform.y) * real,
      };
      this.userZoomed = true;
      this.overview = false;
      this.applyTransform();
    }

    screenPoint(event) {
      const rect = this.canvas.getBoundingClientRect();
      return { x: event.clientX - rect.left, y: event.clientY - rect.top };
    }

    onWheel(event) {
      const fullscreen = this.fullscreen;
      // Embedded graphs keep normal page scrolling; Ctrl/Meta + wheel zooms.
      if (!fullscreen && !event.ctrlKey && !event.metaKey) return;
      event.preventDefault();
      this.zoomAt(event.deltaY < 0 ? 1.12 : 1 / 1.12, this.screenPoint(event));
    }

    onPointerDown(event) {
      if (event.button !== 0 && event.pointerType === 'mouse') return;
      this.pointers = this.pointers || new Map();
      this.pointers.set(event.pointerId, this.screenPoint(event));
      if (this.pointers.size === 1) {
        this.dragStart = { point: this.screenPoint(event), transform: { ...this.transform } };
        this.dragged = false;
        // Capture keeps a drag alive outside the canvas, but it also retargets
        // the click to the <svg>, so the pressed node is remembered here.
        this.pressedNode = event.target.closest?.('[data-node-id]')?.dataset.nodeId || '';
        this.canvas.setPointerCapture?.(event.pointerId);
      } else if (this.pointers.size === 2) {
        const [a, b] = [...this.pointers.values()];
        this.pinchStart = { distance: Math.hypot(a.x - b.x, a.y - b.y), k: this.transform.k };
      }
    }

    onPointerMove(event) {
      if (!this.pointers?.has(event.pointerId)) return;
      this.pointers.set(event.pointerId, this.screenPoint(event));
      if (this.pointers.size === 2 && this.pinchStart) {
        const [a, b] = [...this.pointers.values()];
        const distance = Math.hypot(a.x - b.x, a.y - b.y);
        if (this.pinchStart.distance > 0) {
          const target = clamp(this.pinchStart.k * (distance / this.pinchStart.distance), 0.2, 3);
          const center = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
          this.zoomAt(target / this.transform.k, center);
        }
        this.dragged = true;
        return;
      }
      if (!this.dragStart) return;
      const point = this.screenPoint(event);
      const dx = point.x - this.dragStart.point.x;
      const dy = point.y - this.dragStart.point.y;
      if (Math.abs(dx) + Math.abs(dy) > 4) this.dragged = true;
      if (!this.dragged) return;
      this.userZoomed = true;
      this.overview = false;
      this.transform = { ...this.transform, x: this.dragStart.transform.x + dx, y: this.dragStart.transform.y + dy };
      this.applyTransform();
    }

    onPointerUp(event) {
      const wasSingle = this.pointers?.size === 1;
      this.pointers?.delete(event.pointerId);
      const pressed = this.pressedNode;
      this.pressedNode = '';
      if (!this.pointers?.size) { this.dragStart = null; this.pinchStart = null; }
      else if (this.pointers.size === 1) {
        const [point] = [...this.pointers.values()];
        this.dragStart = { point, transform: { ...this.transform } };
      }
      // A tap selects; a drag pans and must never navigate or select.
      if (wasSingle && !this.dragged && pressed && this.nodes.has(pressed)) {
        event.preventDefault();
        this.select(pressed, { focus: true, activate: true });
      } else if (wasSingle && !this.dragged && !pressed) {
        this.options.onBackground?.();
      }
      if (!this.pointers?.size) this.dragged = false;
    }

    onPointerCancel(event) {
      this.pointers?.delete(event.pointerId);
      this.pressedNode = '';
      this.dragged = true;
      if (!this.pointers?.size) {
        this.dragStart = null;
        this.pinchStart = null;
        this.dragged = false;
      } else if (this.pointers.size === 1) {
        const [point] = [...this.pointers.values()];
        this.dragStart = { point, transform: { ...this.transform } };
      }
    }

    /* ----------------------------------------------------------- selection */
    select(nodeId, { focus = false, reveal = false, activate = false } = {}) {
      if (!this.nodes.has(nodeId)) return;
      if (this.scope === 'library' && this.nodes.get(nodeId).role === 'category') {
        this.setFilter('category', nodeId);
        return;
      }
      const changed = this.selected !== nodeId;
      this.selected = nodeId;
      this.previousFocus = document.activeElement;
      const node = this.nodes.get(nodeId);
      this.highlight(nodeId);
      if (activate && this.options.onActivate) this.options.onActivate(node.meta?.paper_id || node.id);
      else if ((changed || activate) && this.options.onSelect) this.options.onSelect(node.meta?.paper_id || node.id);
      if (!this.options.managed) this.showDetail(nodeId);
      if (node.el) {
        this.canvas.querySelectorAll('[data-node-id]').forEach((element) => {
          element.setAttribute('tabindex', element.dataset.nodeId === nodeId ? '0' : '-1');
          element.classList.toggle('selected', element.dataset.nodeId === nodeId);
        });
        node.el.setAttribute('tabindex', '0');
        if (focus) node.el.focus({ preventScroll: true });
      }
      if (reveal && !node.hidden && Number.isFinite(node.x) && Number.isFinite(node.y)) this.reveal(node);
      if (changed) this.degradeLabels();
    }

    reveal(node) {
      const rect = this.canvas.getBoundingClientRect();
      const k = this.transform.k;
      const sx = node.x * k + this.transform.x;
      const sy = node.y * k + this.transform.y;
      if (sx > 40 && sx < rect.width - 40 && sy > 40 && sy < rect.height - 40) return;
      this.transform = {
        ...this.transform,
        x: this.transform.x + (rect.width / 2 - sx),
        y: this.transform.y + (rect.height / 2 - sy),
      };
      this.userZoomed = true;
      this.overview = false;
      this.applyTransform();
    }

    neighbours(nodeId) {
      const ids = new Set();
      for (const edge of this.edges) {
        if (edge.source === nodeId) ids.add(edge.target);
        if (edge.target === nodeId) ids.add(edge.source);
      }
      return ids;
    }

    highlight(nodeId) {
      if (this.destroyed) return;
      const near = nodeId ? this.neighbours(nodeId) : new Set();
      for (const node of this.nodes.values()) {
        node.el?.classList.toggle('selected', node.id === this.selected);
        node.el?.setAttribute('aria-pressed', String(node.id === this.selected));
        node.el?.classList.toggle('dimmed', Boolean(nodeId) && node.id !== nodeId && !near.has(node.id));
        node.el?.classList.toggle('near', near.has(node.id));
      }
      for (const [key, line] of this.edgeEls || []) {
        const [source, target] = key.split('\u0000');
        line.classList.toggle('active', Boolean(nodeId) && (source === nodeId || target === nodeId));
        line.classList.toggle('dimmed', Boolean(nodeId) && source !== nodeId && target !== nodeId);
      }
      if (this.scope !== 'library') this.applyEdgeVisibility();
    }

    closeDetail() {
      this.detail.hidden = true;
      this.selected = '';
      this.hovered = '';
      this.root.classList.remove('detail-open');
      this.highlight('');
      this.degradeLabels();
      const target = this.previousFocus?.closest?.('[data-node-id]') ? this.previousFocus : null;
      (target || this.canvas)?.focus?.({ preventScroll: true });
    }

    showDetail(nodeId) {
      const node = this.nodes.get(nodeId);
      if (!node || !this.detail) return;
      const incoming = this.edges.filter((e) => e.target === nodeId);
      const outgoing = this.edges.filter((e) => e.source === nodeId);
      const rows = [];
      const linkTo = (id, edge) => {
        const other = this.nodes.get(id);
        if (!other) return '';
        const label = this.edgeLabel(edge);
        const evidence = edge.role === 'motivation' ? `<p class="tree-edge-evidence">${nodeId === edge.target ? '先前工作 → 本文' : '本文 → 后续研究'} · ${escapeHtml(edge.evidence)}</p>` : '';
        return `<li${evidence ? ' class="tree-relation"' : ''}><button type="button" class="graph-link" data-graph-goto="${escapeHtml(id)}">${escapeHtml(other.label)}</button><span class="dim">${escapeHtml(label)}</span>${evidence}</li>`;
      };
      this.detailTitle.textContent = node.title || node.label;
      const meta = [];
      if (node.role === 'paper') {
        meta.push(`<span class="status ${escapeHtml(node.meta?.status || '')}"><span class="dot ${escapeHtml(node.meta?.status || '')}"></span>${escapeHtml(this.statusLabel(node.meta?.status))}</span>`);
        if (node.meta?.year) meta.push(`<span>${escapeHtml(node.meta.year)}</span>`);
      } else if (node.role === 'external') {
        meta.push('<span class="dim">未入库</span>');
        if (node.meta?.year) meta.push(`<span>${escapeHtml(node.meta.year)}</span>`);
        if (node.link_kind === 'search') meta.push('<span class="dim">按标题检索</span>');
        if (node.link_kind === 'unavailable') meta.push('<span class="dim">暂无可用链接</span>');
      } else if (node.role === 'tag') {
        meta.push(`<span class="dim">${node.meta?.count ? `${node.meta.count} 篇论文` : '标签'}</span>`);
      } else if (node.role === 'category') {
        meta.push(`<span class="dim">${escapeHtml(node.meta?.discipline || '分类')}</span>`);
      }
      rows.push(`<div class="graph-detail-meta">${meta.join('')}</div>`);
      if (node.meta?.title_original && node.meta.title_original !== node.title) {
        rows.push(`<p class="graph-detail-orig">${escapeHtml(node.meta.title_original)}</p>`);
      }
      if (node.meta?.reasons?.length) {
        rows.push(`<h3>关联依据</h3><ul class="graph-reasons">${node.meta.reasons.map((reason) => `<li>${escapeHtml(reason)}</li>`).join('')}</ul>`);
      }
      if (this.scope === 'motivation_tree' && node.role === 'paper') {
        rows.push('<p class="tree-model-label">模型判断 · 依据论文引言与总结</p>');
        for (const [key, label] of [['motivation', '研究动机'], ['gap', '前人不足'], ['direction', '研究方向']]) {
          if (node.meta?.[key]) rows.push(`<h3>${label}</h3><p class="tree-detail-text">${escapeHtml(node.meta[key])}</p>`);
        }
        if (node.meta?.keywords?.length) rows.push(`<p class="tree-keywords">${node.meta.keywords.map((word) => `<span>${escapeHtml(word)}</span>`).join('')}</p>`);
        if (node.meta?.evidence_block_ids?.length) rows.push(`<p class="dim">引言证据：${node.meta.evidence_block_ids.map(escapeHtml).join('、')}</p>`);
      }
      const actions = [];
      if (node.role === 'paper' || node.role === 'category') {
        if (node.url) actions.push(`<a class="btn small" href="${escapeHtml(node.url)}">${icon('book-open')}打开论文</a>`);
      }
      if (node.role === 'category') {
        const params = new URLSearchParams();
        for (const key of ['discipline', 'field', 'subfield']) {
          if (node.meta?.[key]) params.set(key, node.meta[key]);
        }
        actions.push(`<a class="btn small" href="/?${params}">筛选论文</a>`);
        actions.push(`<a class="btn small" href="/categories">管理分类</a>`);
      }
      if (node.role === 'external') {
        if (node.url) actions.push(`<a class="btn small" href="${escapeHtml(node.url)}" target="_blank" rel="noreferrer">${icon('external-link')}${node.link_kind === 'search' ? '按标题检索' : '打开来源'}</a>`);
        else actions.push('<p class="dim small-text">外部元数据不全，暂无可打开的链接。</p>');
      }
      if (node.role === 'tag') {
        actions.push(`<a class="btn small" href="/?tag=${encodeURIComponent(node.meta?.tag || '')}">筛选论文</a>`);
      }
      if (actions.length) rows.push(`<div class="graph-detail-actions">${actions.join('')}</div>`);

      if (outgoing.length || incoming.length) {
        rows.push('<h3>关系</h3><ul class="graph-detail-list">');
        for (const edge of outgoing) rows.push(linkTo(edge.target, { ...edge, label: edge.role === 'cites' ? '引用' : edge.label }));
        for (const edge of incoming) rows.push(linkTo(edge.source, { ...edge, label: edge.role === 'cites' ? '被引用' : edge.label }));
        rows.push('</ul>');
      } else {
        rows.push('<p class="dim small-text">这一项还没有其他关联。</p>');
      }
      this.detailBody.innerHTML = rows.join('');
      this.detailBody.querySelectorAll('[data-graph-goto]').forEach((button) => {
        button.addEventListener('click', () => this.select(button.dataset.graphGoto, { focus: true, reveal: true }));
      });
      this.detail.hidden = false;
      this.root.classList.add('detail-open');
      refreshIcons();
    }

    statusLabel(status) {
      return { ready: '可阅读', ready_with_warning: '关联待补全', partial: '翻译待补全' }[status] || status || '';
    }

    edgeKind(edge) {
      if (edge.role === 'motivation') return edge.basis || 'semantic';
      if (edge.role !== 'membership') return edge.role;
      if (this.nodes.get(edge.source)?.role === 'category' && this.nodes.get(edge.target)?.role === 'category') return 'hierarchy';
      return edge.primary === false ? 'secondary' : 'membership';
    }

    edgeLabel(edge) {
      if (edge.role === 'motivation') return `${edge.label || '递进'} · ${edge.basis === 'citation' ? '引用支撑' : '纯语义判断'}`;
      if (this.scope === 'library' && edge.role === 'membership') {
        return { hierarchy: '分类层级', membership: '主分类归属', secondary: '其他分类归属' }[this.edgeKind(edge)];
      }
      return edge.label || { membership: '分类归属', tag: '标签关联', cites: '引用', similar: '相似' }[edge.role] || edge.role || '';
    }

    /* -------------------------------------------------------------- layers */
    applyLayers() {
      for (const node of this.nodes.values()) {
        const visible = this.layers[LAYER_FOR_ROLE[node.role]] !== false;
        const hidden = node.layoutHidden || !visible || (this.layers.external === false && node.role === 'external');
        node.el?.toggleAttribute('hidden', hidden);
        if (node.text) node.text.style.display = hidden ? 'none' : '';
        node.hidden = hidden;
      }
      for (const region of this.regions || []) {
        const visible = (region.members || []).some((node) => !node.hidden);
        region.hidden = !visible;
        region.el?.toggleAttribute('hidden', region.hidden);
      }
      this.applyEdgeVisibility();
      this.updateCounts();
      this.degradeLabels();
      this.computeBounds([...this.nodes.values()].filter((node) => !node.hidden));
      this.renderNodeList();
    }

    applyEdgeVisibility() {
      const emphasized = this.hovered || this.selected;
      for (const [key, line] of this.edgeEls || []) {
        const [source, target, role] = key.split('\u0000');
        const a = this.nodes.get(source);
        const b = this.nodes.get(target);
        const roleVisible = role === 'motivation' ? this.layers[line.dataset.basis || 'semantic'] !== false : role === 'cites' || role === 'similar'
          ? this.layers[role] !== false
          : (a && b && !a.hidden && !b.hidden);
        const evidenceVisible = !line.dataset.supporting || this.showEvidence
          || (emphasized && (source === emphasized || target === emphasized));
        line.toggleAttribute('hidden', !roleVisible || !evidenceVisible || Boolean(a?.hidden) || Boolean(b?.hidden));
      }
      this.updateLegend();
    }

    updateLegend() {
      const nodeRoles = new Set([...this.nodes.values()].filter((node) => !node.hidden).map((node) => node.role));
      const edgeKinds = new Set([...this.edgeEls.values()].filter((line) => !line.hasAttribute('hidden')).map((line) => line.dataset.edgeKind));
      this.root.querySelectorAll('[data-legend-node]').forEach((item) => {
        item.hidden = !nodeRoles.has(item.dataset.legendNode);
      });
      this.root.querySelectorAll('[data-legend-edge]').forEach((item) => {
        item.hidden = !edgeKinds.has(item.dataset.legendEdge);
      });
    }

    applySearch() {
      const term = this.searchTerm;
      let matches = 0;
      for (const node of this.nodes.values()) {
        const hit = term && this.matchesSearch(node);
        if (hit && !node.hidden) matches += 1;
        node.el?.classList.toggle('match', Boolean(hit));
      }
      if (this.searchCount) {
        this.searchCount.textContent = term ? `${matches} 项匹配` : '';
      }
      // Search highlights only; the visible range is the layer and filter set.
      this.updateCounts();
    }

    matchesSearch(node) {
      const term = this.searchTerm;
      if (!term) return false;
      return `${node.label} ${node.title} ${node.meta?.title_original || ''} ${node.meta?.motivation || ''} ${node.meta?.gap || ''} ${(node.meta?.keywords || []).join(' ')}`.toLowerCase().includes(term);
    }

    updateCounts() {
      const visible = [...this.nodes.values()].filter(node => !node.hidden);
      const nodeTotal = this.root.querySelector('[data-graph-node-total]');
      if (nodeTotal) nodeTotal.textContent = visible.length;
      if (!this.countEl) return;
      const stat = this.data?.stats || {};
      const external = stat.external || {};
      const parts = [];
      if (this.scope === 'motivation_tree') {
        const edges = [...(this.edgeEls?.values() || [])].filter((edge) => !edge.hasAttribute('hidden')).length;
        const branches = new Set(visible.map(node => node.meta?.branch?.key));
        this.countEl.textContent = `${visible.length} 篇论文 · ${edges} 条递进 · ${branches.size} 个方向`;
        return;
      }
      if (this.scope === 'paper') {
        const shown = [...this.nodes.values()].filter((node) => !node.hidden && (node.role === 'paper' || node.role === 'external'));
        parts.push(`${shown.length} 篇论文`);
        if (external.shown && this.layers.external) parts.push(`外部 ${external.shown} 篇`);
        if (stat.selection?.available > shown.length) parts.push(`候选 ${stat.selection.available - 1} 篇`);
      } else {
        parts.push(`显示 ${visible.filter(node => node.role === 'paper').length} 篇论文`);
        if (external.available) parts.push(`外部 ${external.shown} / ${external.available}`);
      }
      if (this.scope !== 'paper' && (stat.omitted || external.omitted)) parts.push(`省略 ${stat.omitted || external.omitted}`);
      this.countEl.textContent = parts.join(' · ');
    }

    updateTreeState(data) {
      const stat = data.stats || {}, button = this.root.querySelector('[data-tree-rebuild]');
      if (button) { button.disabled = Boolean(stat.rebuilding); button.querySelector('span').textContent = stat.rebuilding ? '梳理中…' : '重新梳理'; }
      const feedback = this.root.querySelector('[data-tree-feedback]');
      if (feedback) {
        feedback.classList.toggle('bad', Boolean(stat.error));
        feedback.textContent = stat.rebuilding ? '正在用已就绪的论文梳理递进关系，完成后自动更新。'
          : stat.error ? `上次梳理失败：${stat.error} 可点击“重新梳理”重试，已有脉络仍可查看。`
            : stat.generated_at ? `最近梳理：${new Date(stat.generated_at).toLocaleString()}${stat.papers_skipped ? ` · ${stat.papers_skipped} 篇待补齐，可重新梳理重试` : ''}`
              : '点击“重新梳理”，从已有论文的引言与总结生成动机脉络。';
      }
      if (!data.nodes.length) this.setStatus(stat.rebuilding ? '正在梳理，请稍候…' : '暂无脉络。点击“重新梳理”开始。');
      clearTimeout(this.treePoll);
      if (stat.rebuilding) this.treePoll = setTimeout(() => this.pollTree(), 2500);
    }

    async pollTree() {
      if (this.destroyed) return;
      try {
        const focus = new URLSearchParams(location.search).get('focus') || '';
        const response = await fetch(`${this.endpoint}?focus=${encodeURIComponent(focus)}`);
        if (!response.ok) throw new Error('状态读取失败');
        const data = await response.json();
        if (this.destroyed) return;
        if (data.revision !== this.data?.revision || !data.stats?.rebuilding) await this.render(data, { keepView: Boolean(this.data?.nodes.length) });
        else this.updateTreeState(data);
      } catch {
        if (this.destroyed) return;
        const feedback = this.root.querySelector('[data-tree-feedback]');
        if (feedback) feedback.textContent = '暂时无法读取进度，正在重新连接；已有脉络仍可查看。';
        this.treePoll = setTimeout(() => this.pollTree(), 5000);
      }
    }

    async rebuildTree() {
      const button = this.root.querySelector('[data-tree-rebuild]');
      button.disabled = true;
      button.querySelector('span').textContent = '正在提交…';
      try {
        const choice = await CarrelProcessing.graphChoice('motivation_tree', true);
        if (choice.action !== 'ready') {
          button.disabled = false; button.querySelector('span').textContent = '重新梳理';
          if (choice.action === 'fill') {
            const feedback = this.root.querySelector('[data-tree-feedback]');
            feedback.replaceChildren();
            const link = document.createElement('a'); link.href = '/jobs'; link.textContent = '正在补齐所选论文，完成后自动梳理 · 查看任务'; feedback.append(link);
            const waitBatch = async () => {
              if (this.destroyed) return;
              try { const response = await fetch('/api/batches/' + choice.batch_id); const batch = await response.json();
                if (!response.ok) throw new Error(batch.message || '读取失败');
                if (batch.finalized) {
                  await this.reload();
                  await CarrelProcessing.refreshReadiness(this.root);
                  if (batch.warning) feedback.textContent = batch.warning;
                }
                else setTimeout(waitBatch, 3000);
              } catch(error) { feedback.textContent = error.message + '，请到任务页查看'; }
            }; setTimeout(waitBatch, 3000);
          }
          return;
        }
        const response = await fetch('/api/motivation-tree/rebuild', { method: 'POST' });
        const result = await response.json();
        if (!response.ok && response.status !== 409) throw new Error(result.message || '提交失败');
        this.updateTreeState({ ...this.data, stats: { ...this.data?.stats, rebuilding: true, error: '' } });
        await this.pollTree();
      } catch (error) {
        button.disabled = false; button.querySelector('span').textContent = '重新梳理';
        const feedback = this.root.querySelector('[data-tree-feedback]');
        feedback.textContent = `无法开始梳理：${error.message}。请重试。`; feedback.classList.add('bad');
      }
    }

    setStatus(message, { error = false, retry = false } = {}) {
      if (!this.status) return;
      this.status.hidden = !message;
      this.status.textContent = message || '';
      this.status.classList.toggle('error', Boolean(error));
      this.status.querySelector?.('button')?.remove();
      if (retry) {
        const button = document.createElement('button');
        button.className = 'btn tiny';
        button.textContent = '重试';
        button.addEventListener('click', () => this.reload());
        this.status.append(button);
      }
    }

    renderNodeList() {
      if (!this.nodeList) return;
      const groups = {};
      for (const node of [...this.nodes.values()].sort((a, b) => String(a.sort_key).localeCompare(String(b.sort_key)))) {
        if (node.hidden) continue;
        (groups[node.role] ||= []).push(node);
      }
      const labels = { category: '分类', paper: '库内论文', tag: '标签', external: '外部文献' };
      const html = Object.entries(labels).filter(([role]) => groups[role]?.length).map(([role, label]) => `
        <section class="graph-node-group"><h3>${label} <span class="count">${groups[role].length}</span></h3><ul>
        ${groups[role].map((node) => `<li>${node.url && node.role !== 'external'
          ? `<a href="${escapeHtml(node.url)}">${escapeHtml(node.title || node.label)}</a>`
          : `<button type="button" class="graph-link" data-graph-goto="${escapeHtml(node.id)}">${escapeHtml(node.title || node.label)}</button>`}</li>`).join('')}
        </ul></section>`).join('');
      this.nodeList.innerHTML = html || '<p class="dim small-text">没有可显示的节点。</p>';
      this.nodeList.querySelectorAll('[data-graph-goto]').forEach((button) => {
        button.addEventListener('click', () => this.select(button.dataset.graphGoto, { focus: true, reveal: true }));
      });
    }

    /* ------------------------------------------------------------ keyboard */
    onKeyDown(event) {
      const keys = ['ArrowRight', 'ArrowLeft', 'ArrowUp', 'ArrowDown', 'Enter', ' ', 'Escape', 'Home'];
      if (!keys.includes(event.key)) return;
      if (event.key === 'Escape') {
        if (this.options.onBackground) this.options.onBackground();
        else if (this.detail) this.closeDetail();
        return;
      }
      const list = [...this.nodes.values()].filter((node) => !node.hidden)
        .sort((a, b) => (a.y - b.y) || (a.x - b.x));
      if (!list.length) return;
      event.preventDefault();
      if (!this.selected) { this.select(list[0].id, { focus: true, reveal: true }); return; }
      const current = this.nodes.get(this.selected);
      if (event.key === 'Enter' || event.key === ' ') {
        if (this.options.managed) this.select(this.selected, { activate: true });
        else this.showDetail(this.selected);
        return;
      }
      if (event.key === 'Home') { this.select(list[0].id, { focus: true, reveal: true }); return; }
      // Neighbours first, then the nearest node in the arrow's direction.
      const near = [...this.neighbours(this.selected)].map((id) => this.nodes.get(id)).filter((n) => n && !n.hidden);
      const direction = { ArrowRight: [1, 0], ArrowLeft: [-1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] }[event.key];
      const score = (node) => {
        const dx = node.x - current.x, dy = node.y - current.y;
        const dot = dx * direction[0] + dy * direction[1];
        if (dot <= 0) return Infinity;
        return Math.hypot(dx, dy) - (near.includes(node) ? 60 : 0);
      };
      const next = list.filter((node) => node !== current)
        .map((node) => ({ node, score: score(node) }))
        .filter((item) => Number.isFinite(item.score))
        .sort((a, b) => a.score - b.score)[0]?.node;
      if (next) this.select(next.id, { focus: true, reveal: true });
    }

    /* ------------------------------------------------------------ fullscreen */
    toggleFullscreen(force) {
      const want = force === undefined ? !this.fullscreen : force;
      this.fullscreenRequested = want;
      if (want) {
        this.root.classList.add('is-fullscreen');
        if (this.root.requestFullscreen) {
          this.root.requestFullscreen().catch(() => { /* in-page expand is the fallback */ });
        }
        this.fullscreen = true;
      } else {
        this.root.classList.remove('is-fullscreen');
        if (document.fullscreenElement && this.root.contains(document.fullscreenElement)) document.exitFullscreen().catch(() => {});
        this.fullscreen = false;
      }
      const button = this.root.querySelector('[data-graph-fullscreen]');
      if (button) {
        button.setAttribute('aria-pressed', String(Boolean(want)));
        button.title = button.ariaLabel = want ? '退出全屏' : '全屏查看图谱';
        button.innerHTML = icon(want ? 'minimize' : 'maximize');
        refreshIcons();
      }
      setTimeout(() => this.fit({ keepZoom: true }), 60);
    }

    toggleMenu(name) {
      const popover = this.root.querySelector(`[data-graph-popover="${name}"]`);
      const button = this.root.querySelector(`[data-graph-menu-toggle="${name}"]`);
      if (!popover) return;
      const open = popover.hidden;
      this.closeMenus();
      popover.hidden = !open;
      button?.setAttribute('aria-expanded', String(open));
    }

    closeMenus() {
      this.root.querySelectorAll('[data-graph-popover]').forEach((popover) => { popover.hidden = true; });
      this.root.querySelectorAll('[data-graph-menu-toggle]').forEach((button) => button.setAttribute('aria-expanded', 'false'));
    }

    /* -------------------------------------------------------------- export */
    exportGraph(format, viewportOnly) {
      if (!this.group) return;
      try {
        const source = this.buildExportSvg(viewportOnly);
        if (format === 'png') this.downloadPng(source);
        else this.download(this.toBlobUrl(source, 'image/svg+xml'), 'carrel-graph.svg');
      } catch (error) {
        this.setStatus(`导出失败：${error.message}`, { error: true });
      }
    }

    buildExportSvg(viewportOnly) {
      this.cullViewport(true);
      const hover = this.hovered;
      this.hovered = "";
      this.highlight(this.selected);
      const rect = this.canvas.getBoundingClientRect();
      const box = viewportOnly
        ? { x: -this.transform.x / this.transform.k, y: -this.transform.y / this.transform.k, width: rect.width / this.transform.k, height: rect.height / this.transform.k }
        : { x: this.bounds.minX - 24, y: this.bounds.minY - 24, width: this.bounds.width + 48, height: this.bounds.height + 48 };
      const svg = el('svg', {
        xmlns: SVG_NS, width: Math.round(box.width), height: Math.round(box.height),
        viewBox: `${box.x} ${box.y} ${box.width} ${box.height}`,
        'font-family': 'sans-serif',
      });
      const defs = this.canvas.querySelector('defs').cloneNode(true);
      const originalMarkers = this.canvas.querySelectorAll('defs path');
      defs.querySelectorAll('path').forEach((path, index) => path.setAttribute('fill', getComputedStyle(originalMarkers[index]).fill));
      svg.append(defs);
      const background = el('rect', { x: box.x, y: box.y, width: box.width, height: box.height,
        fill: getComputedStyle(this.canvas.parentElement).backgroundColor });
      svg.append(background);
      const clone = this.group.cloneNode(true);
      clone.removeAttribute('transform');
      const originals = [...this.group.querySelectorAll('*')];
      // Use the live styles so both themes, selection and exports agree.
      clone.querySelectorAll('*').forEach((element, index) => {
        const style = getComputedStyle(originals[index]);
        for (const property of ['fill', 'stroke', 'stroke-width', 'stroke-dasharray', 'opacity',
          'font-size', 'font-family', 'font-weight', 'vector-effect', 'stroke-linejoin']) {
          element.style.setProperty(property, style.getPropertyValue(property));
        }
        element.removeAttribute('tabindex');
        element.removeAttribute('class');
      });
      clone.querySelectorAll('[hidden]').forEach((node) => node.remove());
      svg.append(clone);
      const clip = el("clipPath", {id: "export-content-clip"});
      clip.append(el("rect", {x: box.x, y: box.y, width: box.width, height: box.height}));
      defs.append(clip);
      clone.setAttribute("clip-path", "url(#export-content-clip)");
      const caption = el("text", {x: box.x + 12, y: box.y + box.height + 20, fill: getComputedStyle(this.root).color, "font-size": 12});
      caption.textContent = `${viewportOnly ? "当前视口" : "当前已展开内容"} · ${this.data?.scope_label || this.root.dataset.exportCaption || this.scope} · ${[...this.nodes.values()].filter(n => !n.hidden).length} 篇 · 未展开 ${this.data?.omitted ?? "—"}`;
      svg.append(caption);
      const captionWidth = measure(caption.textContent, '12px sans-serif') + 24;
      box.width = Math.max(box.width, captionWidth);
      svg.setAttribute('width', Math.round(box.width));
      background.setAttribute('width', box.width);
      svg.setAttribute("height", Math.round(box.height + 32));
      svg.setAttribute("viewBox", `${box.x} ${box.y} ${box.width} ${box.height + 32}`);
      background.setAttribute("height", box.height + 32);
      const source = new XMLSerializer().serializeToString(svg);
      this.hovered = hover;
      this.highlight(hover || this.selected);
      this.cullViewport();
      return source;
    }

    toBlobUrl(source, type) {
      return URL.createObjectURL(new Blob([source], { type }));
    }

    download(url, name) {
      const link = document.createElement('a');
      link.href = url; link.download = name;
      document.body.append(link); link.click(); link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 2000);
    }

    downloadPng(source) {
      const image = new Image();
      const url = this.toBlobUrl(source, 'image/svg+xml');
      image.onload = () => {
        const ratio = Math.min(8192 / image.width, 8192 / image.height,
          Math.sqrt(16e6 / Math.max(1, image.width * image.height)), 2);
        const canvas = document.createElement('canvas');
        canvas.width = Math.round(image.width * ratio);
        canvas.height = Math.round(image.height * ratio);
        const context = canvas.getContext('2d');
        context.drawImage(image, 0, 0, canvas.width, canvas.height);
        canvas.toBlob((blob) => {
          URL.revokeObjectURL(url);
          if (!blob) { this.setStatus('PNG 生成失败，已保留所选格式。', { error: true }); return; }
          const pngUrl = URL.createObjectURL(blob);
          this.download(pngUrl, 'carrel-graph.png');
        }, 'image/png');
      };
      image.onerror = () => {
        URL.revokeObjectURL(url);
        this.setStatus('PNG 生成失败，已保留所选格式。', { error: true });
      };
      image.src = url;
    }
  }

  window.CarrelGraph = {
    mount(root, payload, options = {}) {
      root.dataset.graphStarted = 'true';
      root.dataset.graphScope = payload.scope || 'library';
      root.classList.add('graph-workspace');
      if (!root.querySelector('[data-graph-canvas]')) {
        root.innerHTML = '<div class="graph-stage"><svg class="graph-canvas" data-graph-canvas tabindex="0" role="application" aria-label="Paper graph"></svg><p data-graph-status role="status"></p></div>';
      }
      if (root.graphController && !root.graphController.destroyed && root.graphController.scope === (payload.scope || "library")) {
        const controller = root.graphController;
        controller.options = options;
        controller.layers.cites = Boolean(payload.focus);
        controller.layers.similar = Boolean(payload.focus);
        controller.ready = controller.render(payload, { keepView: Boolean(payload.focus) });
        return controller;
      }
      root.graphController?.destroy();
      const controller = new GraphController(root, options).start(payload);
      if (options.managed) {
        controller.layers.cites = Boolean(payload.focus);
        controller.layers.similar = Boolean(payload.focus);
      }
      root.graphController = controller;
      window.CarrelGraph.last = controller;
      return controller;
    },
    create(root, options) {
      const controller = new GraphController(root, options).start();
      window.CarrelGraph.last = controller;  // inspectable from tests and console
      return controller;
    },
    GraphController,
  };

  // The library page embeds its payload; the paper page loads it lazily, only
  // once its tab becomes visible — a hidden pane has no real container size.
  const activators = [];
  const ready = () => {
    const root = document.querySelector('[data-graph-scope="library"]:not([hidden]), [data-graph-scope="motivation_tree"]:not([hidden])');
    if (root && !root.dataset.graphStarted) {
      root.dataset.graphStarted = 'true';
      const controller = window.CarrelGraph.create(root, { maxExternal: DEFAULT_LIBRARY_MAX_EXTERNAL });
      if (root.dataset.graphScope === 'motivation_tree') {
        document.addEventListener('carrel:organization-changed', () => controller.reload());
        window.addEventListener('pagehide', () => controller.destroy(), { once: true });
      }
    }
    document.querySelectorAll('[data-graph-scope="paper"]').forEach((holder) => {
      const pane = holder.closest('.pane');
      let controller, dirty = false;
      const start = () => {
        if (!controller) {
          holder.dataset.graphStarted = 'true';
          controller = window.CarrelGraph.create(holder);
        }
        if (dirty) { dirty = false; controller.reload(); }
      };
      document.addEventListener('carrel:organization-changed', () => {
        dirty = true;
        if (pane?.classList.contains('on')) start();
      });
      if (pane && pane.classList.contains('on')) start();
      activators.push(() => {
        if (pane && pane.classList.contains('on')) start();
      });
    });
  };
  window.CarrelGraph.activate = () => activators.forEach((fn) => fn());

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', ready);
  else ready();
})();
