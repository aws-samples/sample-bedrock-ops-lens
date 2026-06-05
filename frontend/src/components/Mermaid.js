// Mermaid integration for the Ops Review narrative.
//
// The "render-off-screen → capture innerHTML → setState" hack is the only
// way to keep the SVG from being trampled by React's next reconciliation
// — Mermaid mutates the rendered SVG asynchronously after `mermaid.render`
// resolves, and React's diffing would replace those mutations the next time
// state changes. Capture the post-render HTML once and stash it as a
// stable string.

import { marked } from 'marked';
import DOMPurify from 'dompurify';

const MAX_MERMAID_SOURCE_LEN = 10000;

let _mermaidLoadingPromise = null;

async function _loadMermaid() {
  if (!_mermaidLoadingPromise) {
    _mermaidLoadingPromise = import('mermaid').then(m => {
      const mermaid = m.default || m;
      const dark = document.body.classList.contains('awsui-dark-mode');
      mermaid.initialize({
        startOnLoad: false,
        theme: dark ? 'dark' : 'default',
        fontSize: 16,
        flowchart: { useMaxWidth: true, htmlLabels: true, padding: 12 },
        gantt: {
          fontSize: 14, sectionFontSize: 14, leftPadding: 120,
          gridLineStartPadding: 40, barHeight: 22, axisFormat: '%b %Y',
        },
        sequence: { actorFontSize: 14, messageFontSize: 13 },
        securityLevel: 'loose',
      });
      return mermaid;
    });
  }
  return _mermaidLoadingPromise;
}

// Custom marked renderer:
//   - Mermaid code blocks become <div class="ops-mermaid-source" data-source="…"></div>
//     placeholders that the post-pass walks and replaces with real SVG.
//   - Every link gets target="_blank" rel="noreferrer".
const _renderer = {
  code(token) {
    const lang = (token.lang || '').toLowerCase();
    if (lang === 'mermaid') {
      const escaped = (token.text || '').replace(/&/g, '&amp;').replace(/"/g, '&quot;');
      return `<div class="ops-mermaid-source" data-source="${escaped}"></div>`;
    }
    return false; // fall through to default
  },
  link(token) {
    const href = (token.href || '').replace(/"/g, '&quot;');
    const text = token.text || token.href || '';
    return `<a href="${href}" target="_blank" rel="noreferrer">${text}</a>`;
  },
};

const _markedInstance = marked.use({ renderer: _renderer });

// DOMPurify config: allow the <details>/<summary> wrapper that the mermaid
// post-pass injects, plus the data-source attribute on .ops-mermaid-source
// placeholders. Default sanitization handles the rest (no <script>, no
// onerror=, no javascript: hrefs).
const _PURIFY_CFG = {
  ADD_TAGS: ['details', 'summary'],
  ADD_ATTR: ['target', 'data-source'],
};

export function renderMarkdownToHtml(md) {
  if (!md) return '';
  try {
    const dirty = _markedInstance.parse(md);
    return DOMPurify.sanitize(dirty, _PURIFY_CFG);
  } catch { return ''; }
}

function _diagramLabel(src) {
  const first = (src || '').split('\n').find(l => l.trim())?.trim() || '';
  if (first.startsWith('gantt')) return 'Lifecycle timeline';
  if (first.startsWith('flowchart') || first.startsWith('graph')) return 'Traffic flow diagram';
  if (first.startsWith('sequenceDiagram')) return 'Sequence diagram';
  return 'Diagram';
}

// Walk every .ops-mermaid-source placeholder in `root`, render its source
// with mermaid, and replace the placeholder with a <details><summary> wrapper
// containing the SVG.
export async function renderMermaidIn(root) {
  if (!root) return;
  const placeholders = Array.from(root.querySelectorAll('.ops-mermaid-source:not([data-rendered])'));
  if (placeholders.length === 0) return;
  const mermaid = await _loadMermaid();
  let i = 0;
  for (const el of placeholders) {
    const src = (el.getAttribute('data-source') || '').replace(/&quot;/g, '"').replace(/&amp;/g, '&');
    if (!src.trim() || src.length > MAX_MERMAID_SOURCE_LEN) {
      // Static literal — no interpolation, no XSS surface.
      el.outerHTML = `<pre>Diagram too large to render</pre>`; // nosemgrep: insecure-document-method
      continue;
    }
    const id = `mermaid-${Date.now()}-${i++}`;
    try {
      const { svg } = await mermaid.render(id, src);
      const label = _diagramLabel(src);
      // Sanitize the mermaid-emitted SVG before splicing into the DOM.
      // Mermaid is trusted code, but defense-in-depth: any user content
      // that flows into a node label could become an XSS vector if mermaid
      // ever has a renderer regression. SVG profile keeps shapes/styles.
      const cleanSvg = DOMPurify.sanitize(svg, { USE_PROFILES: { svg: true, svgFilters: true } });
      // SVG (cleanSvg) and label are both sanitized above; remaining
      // template fragments are static literals.
      const html = `<details class="ops-mermaid-details" open><summary>Show ${label}</summary><div class="ops-mermaid">${cleanSvg}</div></details>`;
      el.outerHTML = html; // nosemgrep: insecure-document-method
    } catch (e) {
      // Use DOM APIs with textContent to avoid any innerHTML/outerHTML
      // sink for the error message (which may include mermaid-surfaced
      // user content). textContent never parses HTML, so no XSS surface.
      const rawMsg = String((e && e.message) || e);
      const pre = document.createElement('pre');
      pre.textContent = `Mermaid render failed: ${rawMsg}`;
      if (el.parentNode) {
        el.parentNode.replaceChild(pre, el);
      }
    }
  }
}
