# Local Browser Dependencies

- `lucide.min.js`: lucide 0.468.0 UMD build from unpkg, ISC license in `lucide.LICENSE`.
- `pdf.min.js` and `pdf.worker.min.js`: PDF.js 3.11.174 from cdnjs, Apache 2.0 license in `pdfjs.LICENSE`.
- `katex/`: KaTeX 0.19.1 browser build, auto-render extension and fonts from the official npm package, verified against its SHA-512 integrity value. MIT license in `katex/LICENSE`.
- `markdown-it.min.js`: markdown-it 15.0.3 UMD browser build, MIT license in `markdown-it.LICENSE`.
- `texmath.js`: markdown-it-texmath 1.0.0, MIT license in `texmath.LICENSE`.
- `elk.bundled.js`: elkjs 0.10.0 browser bundle from the official npm package,
  verified against its SHA-512 integrity value. EPL-2.0 in `elk.LICENSE`.
  Upstream source: https://github.com/kieler/elkjs/tree/v0.10.0 and
  https://github.com/eclipse-elk/elk/tree/v0.10.0. Used for single-paper graph
  placement and orthogonal routing; the bundled engine runs locally.

The Markdown parser enables only math and escaping, with HTML disabled. It
handles dollar-delimited formulas without treating escaped currency as math.
KaTeX auto-render handles bracketed displays and equation environments. Both
parsers keep source-block boundaries, and KaTeX runs with `trust: false`.

PDF.js retains the version previously used by Carrel. Both document-loading
paths explicitly disable evaluation (`isEvalSupported: false`), following the
upstream workaround for GHSA-wgrm-67xf-hhpq. Keep the main and worker versions
in sync when upgrading. Browser assets are bundled in the Python package and
served locally; viewing papers does not request assets from a CDN.
