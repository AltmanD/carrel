/* Bilingual reading pane: PDF plus its translation, in three arrangements.
 *
 * The server renders the comparison column; this file only decides where it
 * sits and keeps it in step with the PDF. No DOM is duplicated — the same rows
 * are filtered and re-placed, so anchors and rendered formulas stay unique. */
(function () {
  'use strict';

  const MODES = ['float', 'split', 'stack'];
  const STORE = { open: 'carrel-cmp-on', mode: 'carrel-cmp-mode', sync: 'carrel-cmp-sync', source: 'carrel-cmp-source' };
  const SYNC_LOCK_MS = 900;

  function preference(key, value) {
    try {
      if (value !== undefined) localStorage.setItem(key, value);
      return localStorage.getItem(key);
    } catch { return null; }
  }

  class Reader {
    constructor(root) {
      this.root = root;                                  // #pane-pdf
      this.panel = root.querySelector('#cmp-panel');
      this.scroller = root.querySelector('.cmp-panel .translation-layout');
      this.article = root.querySelector('.translation-article');
      this.toggle = root.querySelector('[data-cmp-toggle]');
      this.syncButton = root.querySelector('[data-cmp-sync]');
      this.modeButtons = [...root.querySelectorAll('[data-cmp-mode]')];
      this.pageLabel = root.querySelector('[data-cmp-page]');
      this.empty = root.querySelector('[data-cmp-empty]');
      this.tocToggle = root.querySelector('#toc-toggle');
      this.toc = root.querySelector('.article-toc');
      this.sourceButton = root.querySelector('[data-cmp-source]');
      this.fullscreenButton = root.querySelector('[data-reader-fullscreen]');
      this.viewer = null;
      this.rows = [];
      this.pageRows = new Map();
      this.currentPage = 1;
      this.syncLockUntil = 0;
      this.scrollScheduled = false;
      this.dragged = null;
      this.mode = 'split';
      this.enabled = Boolean(this.panel);
      this.link = true;
    }

    start() {
      this.readState();
      this.indexRows();
      this.bind();
      this.apply();
      this.setToc(preference('carrel-toc-collapsed') === 'false');
      // The viewer may already be loading when the tab activates again.
      this.attachViewer();
      return this;
    }

    /* ------------------------------------------------------------- state */
    readState() {
      const storedMode = preference(STORE.mode);
      const narrow = window.matchMedia?.('(max-width: 980px)').matches;
      this.mode = MODES.includes(storedMode) ? storedMode : (narrow ? 'stack' : 'split');
      this.enabled = Boolean(this.panel) && (location.hash === '#trans' || preference(STORE.open) !== 'false');
      this.link = preference(STORE.sync) !== 'false';
      this.source = preference(STORE.source) === 'true';
    }

    saveMode(mode) { preference(STORE.mode, mode); }
    saveEnabled(value) { preference(STORE.open, String(value)); }
    saveLink(value) { preference(STORE.sync, String(value)); }

    /* ------------------------------------------------------------- index */
    indexRows() {
      this.rows = [...(this.article?.querySelectorAll('.cmp-row') || [])];
      this.pageRows = new Map();
      for (const [index, row] of this.rows.entries()) {
        const pages = (row.dataset.pages || '').split(/\s+/).filter(Boolean).map(Number);
        row.dataset.index = String(index);
        for (const page of pages) {
          if (!this.pageRows.has(page)) this.pageRows.set(page, []);
          if (!this.pageRows.get(page).includes(row)) this.pageRows.get(page).push(row);
        }
      }
      this.sections = [...(this.article?.querySelectorAll('.trans-section') || [])];
    }

    pagesOf(row) {
      return (row.dataset.pages || '').split(/\s+/).filter(Boolean).map(Number);
    }

    /* ------------------------------------------------------------- bind */
    bind() {
      this.toggle?.addEventListener('click', () => this.setEnabled(!this.enabled));
      for (const button of this.modeButtons) {
        button.addEventListener('click', () => this.setMode(button.dataset.cmpMode));
      }
      this.syncButton?.addEventListener('click', () => this.setLink(!this.link));
      this.panel?.querySelector('[data-cmp-close]')?.addEventListener('click', () => {
        this.setEnabled(false); this.toggle?.focus({ preventScroll: true });
      });
      this.panel?.querySelector('[data-cmp-reset]')?.addEventListener('click', () => this.resetFloat());
      this.tocToggle?.addEventListener('click', () => this.setToc(this.toc.hidden));
      this.sourceButton?.addEventListener('click', () => {
        const anchor = this.topRow();
        this.source = !this.source; preference(STORE.source, String(this.source));
        this.apply();
        if (anchor) this.scrollToElement(anchor);
      });
      this.fullscreenButton?.addEventListener('click', () => this.setFullscreen(!this.fullscreen));
      document.addEventListener('fullscreenchange', () => {
        if (document.fullscreenElement === this.root) {
          if (!this.fullscreen) document.exitFullscreen().catch(() => {});
          else this.nativeFullscreen = true;
        } else if (this.nativeFullscreen) {
          this.nativeFullscreen = false; this.setFullscreen(false);
        }
      });
      document.addEventListener('click', (event) => {
        if (!event.target.closest('.article-toc, #toc-toggle')) this.setToc(false);
      });
      document.addEventListener('keydown', (event) => {
        if (event.key === 'Escape') {
          if (this.toc && !this.toc.hidden) { this.setToc(false); this.tocToggle.focus(); }
          else if (this.fullscreen) this.setFullscreen(false);
        }
        if (event.key === 'Tab' && this.fullscreen) {
          const items = [...this.root.querySelectorAll('button, a, input, [tabindex="0"]')]
            .filter(el => !el.disabled && el.getClientRects().length);
          const target = event.shiftKey ? items.at(-1) : items[0];
          if ((event.shiftKey && document.activeElement === items[0]) || (!event.shiftKey && document.activeElement === items.at(-1))) {
            event.preventDefault(); target?.focus();
          }
        }
      });

      // Clicking a comparison row brings the PDF to that paragraph's page.
      this.article?.addEventListener('click', (event) => {
        if (event.target.closest('a, button, summary, input') || !window.getSelection()?.isCollapsed) return;
        const row = event.target.closest('.cmp-row');
        if (!row) return;
        this.navigateTo(row, false);
      });

      this.scroller?.addEventListener('scroll', () => this.onPanelScroll(), { passive: true });
      for (const name of ['wheel', 'touchstart', 'pointerdown', 'keydown']) {
        this.scroller?.addEventListener(name, () => { this.syncLockUntil = 0; }, { passive: true });
      }
      window.addEventListener('resize', () => { this.apply(); this.retainPdfPage(); });
      // Drag the floating window by its head only, so its text stays selectable.
      this.bindDrag();
    }

    /* --------------------------------------------------------------- pdf */
    attachViewer() {
      const ready = window.CarrelPdf?.ensure?.();
      if (!ready) return;
      Promise.resolve(ready).then((viewer) => {
        if (!viewer || this.viewer) return;
        this.viewer = viewer;
        viewer.onPage?.((page, origin) => this.onPdfPage(page, origin));
        if (typeof viewer.page === 'number') this.onPdfPage(viewer.page, 'init');
      }).catch(() => { /* The PDF failed; the comparison column still reads. */ });
    }

    onPdfPage(page, origin) {
      if (!page) return;
      this.currentPage = page;
      if (this.pageLabel) this.pageLabel.textContent = `原文 ${page} 页`;
      this.markPage(page);
      if (origin === 'sync') return;                    // our own scroll; do not echo
      if (this.enabled && (this.link || origin === 'init')) {
        if (this.effectiveMode === 'float') this.showPage(page);
        else if (this.link) this.revealPage(page);
      }
      this.markSection();
    }

    markPage(page) {
      for (const row of this.rows) {
        const pages = this.pagesOf(row);
        row.classList.toggle('is-current-page', pages.includes(page));
        if (!pages.includes(page)) row.classList.remove('is-active');
      }
      this.currentPage = page;
    }

    /* Floating window: only the current page's rows stay on screen. */
    showPage(page) {
      if (this.filteredPage === page) return;
      this.filteredPage = page;
      let visible = 0;
      for (const row of this.rows) {
        const off = !this.pagesOf(row).includes(page);
        row.classList.toggle('is-offpage', off);
        if (!off) visible += 1;
      }
      // Hide a section heading when none of its rows belong to this page.
      for (const section of this.sections || []) {
        const rows = [...section.querySelectorAll('.cmp-row')];
        section.classList.toggle('is-offpage', rows.every((row) => row.classList.contains('is-offpage')));
      }
      if (this.empty) this.empty.hidden = visible > 0;
      if (this.scroller) this.scrollLocked(() => { this.scroller.scrollTop = 0; });
      this.markSection();
    }

    revealPage(page) {
      const first = (this.pageRows.get(page) || [])[0];
      if (first) this.revealRow(first);
    }

    revealRow(row) {
      if (!this.scroller || this.effectiveMode === 'float') return;
      if (this.scroller.contains(row) && this.isVisible(row)) return;
      this.scrollToElement(row);
    }

    isVisible(row) {
      if (!this.scroller) return false;
      const box = row.getBoundingClientRect();
      const view = this.scroller.getBoundingClientRect();
      return box.height > 0 && box.top < view.bottom - 20 && box.bottom > view.top + 20;
    }

    /* Ignore scroll events we caused. Fresh user input releases the lock so
     * a wheel or touch gesture can always take over immediately. */
    onPanelScroll() {
      this.markSection();
      if (this.scrollScheduled || performance.now() < this.syncLockUntil) return;
      this.runScrollTick();
    }

    runScrollTick() {
      if (this.scrollScheduled) return;
      this.scrollScheduled = true;
      requestAnimationFrame(() => {
        this.scrollScheduled = false;
        if (performance.now() < this.syncLockUntil || !this.link || !this.viewer || !this.enabled
            || this.effectiveMode === 'float' || !this.root.classList.contains('on')) return;
        const row = this.topRow();
        if (!row) return;
        const pages = this.pagesOf(row);
        if (!pages.length) return;
        const page = pages.includes(this.currentPage) ? this.currentPage : pages[0];
        if (page === this.currentPage) return;
        this.viewer.goTo(page, { sync: true });
      });
    }

    scrollLocked(action) {
      this.syncLockUntil = performance.now() + SYNC_LOCK_MS;
      action();
    }

    focusRow(row) {
      for (const other of this.rows) other.classList.toggle('is-active', other === row);
      row.classList.add('is-active');
    }

    topRow() {
      if (!this.scroller) return null;
      const top = this.scroller.getBoundingClientRect().top + 24;
      return this.rows.find(row => {
        const box = row.getBoundingClientRect();
        return box.height > 0 && box.bottom > top;
      });
    }

    markSection() {
      const section = this.topRow()?.closest('.trans-section');
      this.toc?.querySelectorAll('[data-section]').forEach(link => {
        const current = link.dataset.section === section?.id;
        link.classList.toggle('on', current);
        if (current) link.setAttribute('aria-current', 'location');
        else link.removeAttribute('aria-current');
      });
    }

    scrollToElement(element) {
      if (!this.scroller || !element) return;
      const top = element.getBoundingClientRect().top - this.scroller.getBoundingClientRect().top + this.scroller.scrollTop - 12;
      this.scrollLocked(() => this.scroller.scrollTo({ top: Math.max(0, top), behavior: 'instant' }));
      this.markSection();
    }

    navigateTo(element, scroll = true) {
      if (!element || !this.article?.contains(element)) return;
      const row = element.closest('.cmp-row') || element.querySelector('.cmp-row');
      this.setEnabled(true);
      cancelAnimationFrame(this.layoutFrame);
      const pages = row ? this.pagesOf(row) : [];
      const page = scroll ? pages[0] : (pages.includes(this.currentPage) ? this.currentPage : pages[0]);
      if (page) {
        if (this.effectiveMode === 'float') this.showPage(page);
        this.viewer?.goTo(page, { sync: true });
      }
      if (row) this.focusRow(row);
      if (scroll) this.scrollToElement(element);
      const wasOpen = this.toc && !this.toc.hidden;
      this.setToc(false);
      if (wasOpen) this.scroller.focus({ preventScroll: true });
    }

    setToc(open) {
      if (!this.toc) return;
      this.toc.hidden = !open;
      this.root.querySelector('#toc-links').hidden = !open;
      this.tocToggle.setAttribute('aria-expanded', String(open));
      this.tocToggle.classList.toggle('on', open);
      this.tocToggle.title = this.tocToggle.ariaLabel = open ? '收起章节目录' : '展开章节目录';
      preference('carrel-toc-collapsed', String(!open));
    }

    retainPdfPage() {
      const page = this.currentPage;
      cancelAnimationFrame(this.layoutFrame);
      this.layoutFrame = requestAnimationFrame(() => {
        if (this.root.classList.contains('on')) this.viewer?.goTo(page, { sync: true, instant: true });
      });
    }

    setFullscreen(value) {
      this.fullscreen = Boolean(value);
      this.root.classList.toggle('is-fullscreen', this.fullscreen);
      document.body.classList.toggle('reader-expanded', this.fullscreen);
      this.fullscreenButton?.setAttribute('aria-pressed', String(this.fullscreen));
      if (this.fullscreenButton) {
        this.fullscreenButton.title = this.fullscreenButton.ariaLabel = this.fullscreen ? '退出全屏' : '全屏阅读';
      }
      if (this.fullscreen) {
        this.toastRegion = document.getElementById('toast-region');
        if (this.toastRegion) this.root.append(this.toastRegion);
        this.root.requestFullscreen?.().catch(() => { /* Keep the in-page expanded view. */ });
      } else {
        if (this.toastRegion) document.body.append(this.toastRegion);
        if (document.fullscreenElement === this.root) document.exitFullscreen().catch(() => {});
        this.fullscreenButton?.focus({ preventScroll: true });
      }
      this.retainPdfPage();
    }

    /* ------------------------------------------------------------- modes */
    setEnabled(value) {
      if (!this.panel || this.enabled === Boolean(value)) return;
      this.enabled = Boolean(value);
      this.saveEnabled(this.enabled);
      this.apply();
      if (!this.enabled) this.setToc(false);
      if (this.enabled && this.effectiveMode === 'float') this.showPage(this.currentPage);
      if (this.enabled && this.link) this.revealPage(this.currentPage);
      this.retainPdfPage();
    }

    setMode(mode) {
      if (!MODES.includes(mode)) return;
      this.mode = mode;
      this.saveMode(mode);
      if (!this.enabled) this.setEnabled(true);
      else this.apply();
      if (this.enabled && this.link) this.revealPage(this.currentPage);
      this.retainPdfPage();
    }

    setLink(value) {
      this.link = Boolean(value);
      this.saveLink(this.link);
      this.apply();
      if (this.enabled && this.link) {
        if (this.effectiveMode === 'float') this.showPage(this.currentPage);
        else this.revealPage(this.currentPage);
      }
    }

    apply() {
      // Below ~980px neither a side column nor a floating window leaves a
      // readable page, so the stacked arrangement is what actually renders.
      // The stored preference survives for when the window is wide again.
      const narrow = window.matchMedia?.('(max-width: 980px)').matches;
      const effective = narrow && this.mode !== 'stack' ? 'stack' : this.mode;
      const modeChanged = this.effectiveMode !== effective;
      this.effectiveMode = effective;
      this.root.dataset.readerOpen = String(this.enabled);
      this.root.dataset.readerMode = effective;
      this.root.dataset.readerLink = String(this.link);
      this.root.dataset.readerSource = String(this.source);
      this.toggle?.classList.toggle('on', this.enabled);
      this.toggle?.setAttribute('aria-pressed', String(this.enabled));
      if (this.toggle) {
        this.toggle.title = this.toggle.ariaLabel = this.enabled ? '隐藏对照译文' : '显示对照译文';
      }
      for (const button of this.modeButtons) {
        const active = button.dataset.cmpMode === effective;
        button.classList.toggle('on', active);
        button.setAttribute('aria-pressed', String(active));
        button.disabled = !this.enabled;
      }
      this.syncButton?.classList.toggle('on', this.link);
      this.syncButton?.setAttribute('aria-pressed', String(this.link));
      if (this.syncButton) {
        this.syncButton.title = this.syncButton.ariaLabel = this.link ? '关闭联动滚动' : '开启联动滚动';
        this.syncButton.dataset.on = String(this.link);
        this.syncButton.disabled = !this.enabled;
      }
      if (this.sourceButton) {
        this.sourceButton.classList.toggle('on', this.source);
        this.sourceButton.setAttribute('aria-pressed', String(this.source));
        this.sourceButton.title = this.sourceButton.ariaLabel = this.source ? '隐藏原文段落' : '显示原文段落';
      }
      if (modeChanged) {
        if (effective === 'float') this.showPage(this.currentPage);
        else {
          this.filteredPage = null;
          for (const row of [...this.rows, ...(this.sections || [])]) row.classList.remove('is-offpage');
          if (this.empty) this.empty.hidden = true;
          this.resetFloat(true);
          if (this.enabled && this.link) this.revealPage(this.currentPage);
        }
      }
      const head = this.panel?.querySelector('[data-cmp-drag]');
      if (head) head.tabIndex = effective === 'float' ? 0 : -1;
      if (this.enabled) this.clampFloat();
    }

    /* --------------------------------------------------------- floating */
    bindDrag() {
      const head = this.panel?.querySelector('[data-cmp-drag]');
      if (!head) return;
      let origin = null;
      head.addEventListener('pointerdown', (event) => {
        if (this.effectiveMode !== 'float' || event.button > 0) return;
        if (event.target.closest('button')) return;       // let the head's buttons work
        const box = this.panel.getBoundingClientRect();
        const stage = this.panel.offsetParent?.getBoundingClientRect() || box;
        origin = { x: event.clientX, y: event.clientY, left: box.left - stage.left, top: box.top - stage.top };
        head.setPointerCapture?.(event.pointerId);
        this.dragged = { left: origin.left, top: origin.top };
        event.preventDefault();
      });
      head.addEventListener('pointermove', (event) => {
        if (!origin || !this.dragged) return;
        this.placeFloat(origin.left + event.clientX - origin.x, origin.top + event.clientY - origin.y);
      });
      const end = (event) => {
        if (!origin) return;
        origin = null;
        head.releasePointerCapture?.(event.pointerId);
        if (this.dragged) preference('carrel-cmp-float', JSON.stringify(this.dragged));
      };
      head.addEventListener('pointerup', end);
      head.addEventListener('pointercancel', end);
      head.addEventListener('dblclick', (event) => {
        if (this.effectiveMode === 'float' && !event.target.closest('button')) this.resetFloat();
      });
      // Keyboard adjustment, since the head is a drag handle.
      head.tabIndex = 0;
      head.addEventListener('keydown', (event) => {
        if (this.effectiveMode !== 'float' || event.target !== head) return;
        const step = event.shiftKey ? 40 : 10;
        const rect = this.panel.getBoundingClientRect(), stage = this.panel.offsetParent.getBoundingClientRect();
        const box = this.dragged || { left: rect.left - stage.left, top: rect.top - stage.top };
        const moves = {
          ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step],
        };
        if (event.key === 'Escape') { this.resetFloat(); return; }
        const move = moves[event.key];
        if (!move) return;
        event.preventDefault();
        this.placeFloat(box.left + move[0], box.top + move[1]);
        preference('carrel-cmp-float', JSON.stringify(this.dragged));
      });
    }

    placeFloat(left, top) {
      const stage = this.panel.offsetParent;
      if (!stage) return;
      const maxLeft = Math.max(0, stage.clientWidth - this.panel.offsetWidth);
      const maxTop = Math.max(0, stage.clientHeight - this.panel.offsetHeight);
      const x = Math.max(0, Math.min(maxLeft, left));
      const y = Math.max(0, Math.min(maxTop, top));
      this.panel.classList.add('is-dragged');
      this.panel.style.left = `${x}px`;
      this.panel.style.top = `${y}px`;
      this.panel.style.right = 'auto';
      this.dragged = { left: x, top: y };
    }

    readFloat() {
      try {
        const value = JSON.parse(preference('carrel-cmp-float') || 'null');
        return value && Number.isFinite(value.left) && Number.isFinite(value.top) ? value : null;
      } catch { return null; }
    }

    resetFloat(clearOnly = false) {
      this.dragged = null;
      this.panel?.classList.remove('is-dragged');
      if (this.panel) {
        this.panel.style.left = '';
        this.panel.style.top = '';
        this.panel.style.right = '';
      }
      if (clearOnly) return;
      preference('carrel-cmp-float', '');
      this.clampFloat();
    }

    clampFloat() {
      if (this.effectiveMode !== 'float' || !this.panel) return;
      const stored = this.dragged || this.readFloat();
      if (!stored) return;
      this.placeFloat(stored.left, stored.top);
    }
  }

  function create() {
    const root = document.querySelector('#pane-pdf[data-reader]');
    if (!root) return null;
    const reader = new Reader(root);
    window.CarrelReader = reader;
    reader.activate = () => {
      if (!reader.started) { reader.started = true; reader.start(); }
      else { reader.apply(); reader.attachViewer(); }
      return reader;
    };
    // The pane is usually hidden on first load, so wait for the tab to open.
    if (root.classList.contains('on')) reader.activate();
    return reader;
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', create);
  else create();
})();
