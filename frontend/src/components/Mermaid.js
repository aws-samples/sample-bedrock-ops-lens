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
        // htmlLabels:true so node labels render as HTML (inside <foreignObject>)
        // and support <br/> line breaks (name / account / metric on separate
        // lines). NB: mermaid 11 emits <foreignObject> regardless of this flag,
        // so the sanitize step MUST allow foreignObject + its label HTML (see
        // renderMermaidIn); stripping it leaves visible-but-empty node boxes.
        flowchart: { useMaxWidth: true, htmlLabels: true, padding: 12 },
        gantt: {
          fontSize: 14, sectionFontSize: 14, leftPadding: 120,
          gridLineStartPadding: 40, barHeight: 22, axisFormat: '%b %Y',
        },
        sequence: { actorFontSize: 14, messageFontSize: 13 },
        // 'strict': mermaid sanitizes label text and emits no click handlers.
        // Label HTML is limited to span/p/br (no script), so allowing it through
        // our DOMPurify pass (which re-sanitizes anyway) carries no XSS risk.
        securityLevel: 'strict',
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
// Base64-encode/decode the mermaid source we stash in the data-source
// attribute. This is REQUIRED, not cosmetic: mermaid flowcharts contain `-->`
// edge arrows, and when that string sits in an HTML attribute value DOMPurify
// treats `-->` as an HTML-comment terminator and strips the WHOLE attribute —
// leaving an empty data-source and a silently-missing diagram. Base64 output
// is only [A-Za-z0-9+/=], none of which trip the sanitizer, so the source
// round-trips through any number of DOMPurify passes intact. Unicode-safe via
// encodeURIComponent (btoa alone throws on non-Latin1 chars like em dashes).
function _b64encode(s) {
  return btoa(unescape(encodeURIComponent(s)));
}
function _b64decode(s) {
  try { return decodeURIComponent(escape(atob(s))); } catch { return ''; }
}

const _renderer = {
  code(token) {
    const lang = (token.lang || '').toLowerCase();
    if (lang === 'mermaid') {
      // data-b64 holds the base64 source (survives DOMPurify); the legacy
      // data-source is dropped by the sanitizer for `-->`, so we no longer
      // rely on it.
      return `<div class="ops-mermaid-source" data-b64="${_b64encode(token.text || '')}"></div>`;
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
  ADD_ATTR: ['target', 'data-source', 'data-b64'],
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

// Targeted XSS scrub for a mermaid-produced SVG node tree. We can't use
// DOMPurify here (it strips foreignObject label content — see renderMermaidIn),
// so we remove the executable vectors ourselves: <script> elements, any on*
// event-handler attribute, and javascript:/data: URLs on href/xlink:href/src.
// Everything presentational (<style>, <foreignObject>, shapes, text) is kept.
function _scrubSvgNode(node) {
  if (!node || node.nodeType !== 1) return;
  const tag = (node.tagName || '').toLowerCase();
  if (tag === 'script') { node.remove(); return; }
  // Copy attributes to an array first — we mutate during iteration.
  for (const attr of Array.from(node.attributes || [])) {
    const name = attr.name.toLowerCase();
    const val = (attr.value || '').replace(/\s+/g, '').toLowerCase();
    if (name.startsWith('on')) { node.removeAttribute(attr.name); continue; }
    if ((name === 'href' || name === 'xlink:href' || name === 'src' || name === 'xlink:href') &&
        (val.startsWith('javascript:') || val.startsWith('data:text/html'))) {
      node.removeAttribute(attr.name);
    }
  }
  for (const child of Array.from(node.childNodes)) _scrubSvgNode(child);
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
    // Prefer the base64 attribute (survives DOMPurify); fall back to the legacy
    // plain data-source for any cached HTML that predates the b64 encoding.
    const b64 = el.getAttribute('data-b64');
    const src = b64
      ? _b64decode(b64)
      : (el.getAttribute('data-source') || '').replace(/&quot;/g, '"').replace(/&amp;/g, '&');
    if (!src.trim()) { el.remove(); continue; }
    if (src.length > MAX_MERMAID_SOURCE_LEN) {
      // Oversized source (rare — the LLM overshot the diagram size guardrail).
      // Rather than a bare "too large" error, degrade gracefully: keep the
      // section readable by offering the raw diagram source in a collapsed
      // <details>. Built with DOM APIs + textContent so the source (model
      // output) is never parsed as HTML — no innerHTML/outerHTML sink, no XSS.
      const details = document.createElement('details');
      details.className = 'ops-mermaid-details';
      const summary = document.createElement('summary');
      summary.textContent = `Show ${_diagramLabel(src)} (source)`;
      const pre = document.createElement('pre');
      pre.textContent = src;
      details.appendChild(summary);
      details.appendChild(pre);
      if (el.parentNode) el.parentNode.replaceChild(details, el);
      continue;
    }
    const id = `mermaid-${Date.now()}-${i++}`;
    try {
      const { svg } = await mermaid.render(id, src);
      const label = _diagramLabel(src);
      // Splice the mermaid SVG in via a PARSED, TARGETED-SCRUBBED node — not
      // DOMPurify. Why not DOMPurify: its SVG profile (and even its default
      // config) strips the xhtml content inside <foreignObject>, and mermaid 11
      // renders every node label as HTML inside foreignObject. Sanitizing with
      // DOMPurify therefore yields black/empty node boxes (fills + labels gone).
      //
      // This is still safe. The SVG is produced by mermaid.render() under
      // securityLevel:'strict' (no click handlers, label text pre-escaped) from
      // a source we control (our own base64-decoded diagram). We additionally
      // scrub the real XSS vectors ourselves: drop <script>, any on* event
      // handler attribute, and javascript:/data: URLs. Nothing executable
      // survives; the presentational <style> + foreignObject labels are kept.
      const container = document.createElement('div');
      container.className = 'ops-mermaid';
      // Parse as text/html (lenient), NOT image/svg+xml (strict XML). Mermaid's
      // foreignObject labels contain unclosed HTML void tags like <br>, which
      // strict XML rejects ("tag mismatch: br"), aborting the parse after the
      // first node. The HTML parser handles <br> and still builds the <svg>
      // subtree correctly.
      const doc = new DOMParser().parseFromString(svg, 'text/html');
      const svgEl = doc.querySelector('svg');
      if (!svgEl) { el.remove(); continue; }
      _scrubSvgNode(svgEl);
      container.appendChild(document.importNode(svgEl, true));

      const details = document.createElement('details');
      details.className = 'ops-mermaid-details';
      details.setAttribute('open', '');
      const summary = document.createElement('summary');
      summary.textContent = `Show ${label}`;
      details.appendChild(summary);
      details.appendChild(container);
      if (el.parentNode) el.parentNode.replaceChild(details, el);
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
