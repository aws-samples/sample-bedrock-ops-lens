// Per-user (per-browser) UI preferences, localStorage-backed.
//
// First use: the optional governance/agent tabs (By User, Agents & MCP,
// Compliance, Governance). They're OFF by default because most customers
// don't run AgentCore or Guardrails — a nav full of empty tabs reads as
// "this dashboard isn't for me". Users opt in from Settings; the choice is
// a personal preference (like the theme), not a stack-wide setting.
//
// Same subscribe pattern as api.js's attribution context: App subscribes so
// the sidebar updates live when Settings flips a toggle — no reload needed.

const KEY = 'bedrock-lens-optional-tabs';

// Tab id -> default visibility. Keys double as the toggle ids in Settings.
// NB: workloads ("Usage · Custom Attributes") is double-gated — the toggle
// AND an active attribution source (proxy or invocation-log tags) must both
// be true for the nav item to appear. The other four are toggle-only.
export const OPTIONAL_TABS = {
  workloads:  { href: '#/workloads',  label: 'Usage · Custom Attributes', default: false },
  byUser:     { href: '#/by-user',    label: 'By User',      default: false },
  agents:     { href: '#/agents',     label: 'Agents & MCP', default: false },
  compliance: { href: '#/compliance', label: 'Compliance',   default: false },
  governance: { href: '#/governance', label: 'Governance',   default: false },
};

function _defaults() {
  const d = {};
  for (const [k, v] of Object.entries(OPTIONAL_TABS)) d[k] = v.default;
  return d;
}

export function loadOptionalTabs() {
  try {
    const raw = localStorage.getItem(KEY);
    if (!raw) return _defaults();
    const stored = JSON.parse(raw);
    // merge over defaults so new tabs added later get their default, and
    // stale keys from removed tabs are dropped.
    const out = _defaults();
    for (const k of Object.keys(out)) {
      if (typeof stored[k] === 'boolean') out[k] = stored[k];
    }
    return out;
  } catch {
    return _defaults();
  }
}

const _subs = new Set();

export function saveOptionalTabs(prefs) {
  try { localStorage.setItem(KEY, JSON.stringify(prefs)); } catch {}
  _subs.forEach(fn => { try { fn(prefs); } catch {} });
}

export function subscribeOptionalTabs(fn) {
  _subs.add(fn);
  return () => _subs.delete(fn);
}
