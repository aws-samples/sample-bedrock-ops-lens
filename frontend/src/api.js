// Centralized fetch helper with abort support, JSON parsing, and a tiny cache.
// Replaces the reference's `useFetch` hook plumbing.

const BASE = (typeof window !== 'undefined' && window.__API_BASE__) || '/api';

const cache = new Map();          // key → { ts, data }
const CACHE_MS = 60_000;

// Backend expects `accounts` as a single comma-separated string (matches the
// reference dashboard's contract); other repeatable keys (e.g. tag_filter)
// stay as repeated `?k=v&k=v` query params.
const COMMA_JOIN_KEYS = new Set(['accounts']);

// --- Cross-tab attribution redirect ----------------------------------------
// When the PROXY attribution source is active AND the user has selected an
// attribute value in the top bar, the CloudWatch-backed tabs can't filter by
// that attribute (native metrics carry no attribute dimension). The proxy
// stream can, so we transparently redirect the handful of native endpoints
// that have a proxy-sourced sibling (/attribution/xtab/*) and inject the
// dim_key + dim_value params. One central switch instead of editing every tab.
//
// App.jsx sets whether the PROXY attribution source is active. The actual
// attribute selection travels in each request's own `tag_filter` param (the
// same array the top-bar filter writes), so the redirect keys off the params —
// no cross-render timing dependency on module state.
// "Attribution active" = an attribution source (proxy OR invocation_logs) is
// effective AND an attribute filter is selected. App.jsx computes this and calls
// setAttributionContext({active}). The xtab/* endpoints pick the right source
// table (f_proxy_dim_hourly vs f_daily_tagged) by effective_source.
let _attributionActive = false;
export function setAttributionContext(ctx) {
  _attributionActive = !!(ctx && ctx.active);
}
export function isAttributionActive() { return _attributionActive; }

// native path -> attribution-sourced sibling (xtab/* resolves source-aware)
const XTAB_REDIRECT = {
  '/summary': '/attribution/xtab/summary',
  '/daily-trend': '/attribution/xtab/daily-trend',
  '/requests-by-model': '/attribution/xtab/by-model',
  '/latency-by-model': '/attribution/xtab/latency-by-model',
  '/breakdown': '/attribution/xtab/breakdown',   // Overview's main request-volume chart
  '/cost-summary': '/attribution/xtab/cost-summary',   // Total-spend KPI (Overview + Cost)
  '/cost-by-model': '/attribution/xtab/cost-by-model', // cost stacked chart (Overview + Cost)
};

export function buildUrl(path, params = {}) {
  // Redirect to the attribution-sourced sibling when an attribution source is
  // active AND this request carries an attribute filter (tag_filter "key:value").
  const tf = params && params.tag_filter;
  if (_attributionActive && XTAB_REDIRECT[path] && Array.isArray(tf) && tf.length) {
    const dimKey = String(tf[0]).split(':')[0];
    const dimValues = tf.filter(s => String(s).startsWith(`${dimKey}:`))
                        .map(s => String(s).slice(dimKey.length + 1));
    const { tag_filter, ...rest } = params;   // drop tag_filter; use dim_* instead
    return _buildUrl(XTAB_REDIRECT[path], { ...rest, dim_key: dimKey, dim_value: dimValues });
  }
  return _buildUrl(path, params);
}

function _buildUrl(path, params = {}) {
  const usp = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => {
    if (v === undefined || v === null || v === '' || v === 'all') return;
    if (Array.isArray(v)) {
      if (v.length === 0) return;
      if (COMMA_JOIN_KEYS.has(k)) usp.set(k, v.join(','));
      else v.forEach(x => usp.append(k, x));
    } else {
      usp.set(k, String(v));
    }
  });
  const qs = usp.toString();
  return `${BASE}${path}${qs ? '?' + qs : ''}`;
}

export async function api(path, params = {}, { signal, useCache = true } = {}) {
  const url = buildUrl(path, params);
  if (useCache) {
    const hit = cache.get(url);
    if (hit && Date.now() - hit.ts < CACHE_MS) return hit.data;
  }
  const res = await fetch(url, { signal, credentials: 'include' });
  if (res.status === 401) {
    // Not signed in — either never was (initial /me probe) or the session
    // EXPIRED mid-use. UserProvider only checks /me once on mount, so a
    // mid-session expiry used to leave the dashboard mounted with every
    // fetch failing → blank charts that read as "no data" until a manual
    // refresh. Broadcast the expiry so UserProvider can flip to the sign-in
    // screen immediately.
    notifyAuthExpired();
    throw new Error('unauthenticated');
  }
  if (!res.ok) throw new Error(`${res.status} ${res.statusText} for ${url}`);
  const data = await res.json();
  if (useCache) cache.set(url, { ts: Date.now(), data });
  return data;
}

export function clearCache() { cache.clear(); }

// --- Session-expiry broadcast ----------------------------------------------
// UserProvider registers a listener; any 401 from any fetch triggers it, so
// an expired session lands on the sign-in screen instead of a dashboard full
// of blank charts. Debounced: a page renders many panels, each of whose
// fetches will 401 at once.
let _authExpiredHandler = null;
let _authExpiredFired = false;
export function onAuthExpired(fn) { _authExpiredHandler = fn; _authExpiredFired = false; }
function notifyAuthExpired() {
  if (_authExpiredFired) return;
  _authExpiredFired = true;
  cache.clear();                      // stale data must not survive re-login
  if (_authExpiredHandler) _authExpiredHandler();
}

// React hook — kept tiny; full state machine is overkill for this app.
//
// Auto-derive the dep from the resolved URL. Earlier this hook required
// the caller to thread every relevant filter into the deps array
// manually, which led to subtle "the data didn't change when I toggled X"
// bugs (the URL changed but React reused the stale fetch promise because
// X wasn't listed). The URL is the single source of truth — if it changes,
// re-fetch. The optional `extraDeps` argument is preserved for callers
// that genuinely want to force a refetch on something not encoded in the
// URL (e.g. a manual refresh button).
import { useEffect, useState } from 'react';
export function useApi(path, params, extraDeps = []) {
  const [state, setState] = useState({ loading: true, data: null, error: null });
  // Skip convention: a falsy `path` means "not ready yet, don't fetch" — the
  // caller is waiting on a dependency (e.g. a required query param that hasn't
  // resolved). Prevents firing a request with a missing required param, which
  // the backend rejects with 422. Callers do: useApi(key ? '/x' : null, {...}).
  const url = path ? buildUrl(path, params || {}) : null;
  useEffect(() => {
    if (!path) { setState({ loading: true, data: null, error: null }); return; }
    let cancelled = false;
    const ctrl = new AbortController();
    setState(s => ({ ...s, loading: true, error: null }));
    api(path, params, { signal: ctrl.signal })
      .then(data => { if (!cancelled) setState({ loading: false, data, error: null }); })
      .catch(error => {
        if (cancelled || error.name === 'AbortError') return;
        setState({ loading: false, data: null, error });
      });
    return () => { cancelled = true; ctrl.abort(); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [url, ...extraDeps]);
  return state;
}

export function fmt(n) {
  if (n === null || n === undefined) return '—';
  const v = Number(n);
  if (Number.isNaN(v)) return String(n);
  if (Math.abs(v) >= 1e12) return (v / 1e12).toFixed(1) + 'T';
  if (Math.abs(v) >= 1e9)  return (v / 1e9).toFixed(1) + 'B';
  if (Math.abs(v) >= 1e6)  return (v / 1e6).toFixed(1) + 'M';
  if (Math.abs(v) >= 1e3)  return (v / 1e3).toFixed(1) + 'K';
  return v.toLocaleString();
}

export function fmtMs(n) {
  if (n === null || n === undefined) return '—';
  const v = Number(n);
  if (v >= 1000) return (v / 1000).toFixed(1) + 's';
  return Math.round(v) + 'ms';
}

export function fmtPct(n, digits = 2) {
  if (n === null || n === undefined) return '—';
  return Number(n).toFixed(digits) + '%';
}

// --- Account-name resolution (008) ------------------------------------------
// Account names for 12-digit accountIds, resolved once from /api/accounts
// (which merges dim_account: config.yaml map > org name > account API).
// Tables render two separate columns via accountName(id) ("Account name",
// blank when unresolved) next to the raw ID ("Account ID") so CSV exports
// stay machine-readable. fmtAccount(id) → "name (id)" is for single-field
// surfaces only (dropdown option labels). Module-level cache (not a hook)
// so cell renderers can use it without prop drilling; useAccountNames()
// triggers the fetch + re-render on arrival.
let _acctNames = new Map();
let _acctNamesPromise = null;

export function primeAccountNames() {
  if (_acctNamesPromise) return _acctNamesPromise;
  _acctNamesPromise = api('/accounts', {}, { useCache: true })
    .then(rows => {
      const m = new Map();
      for (const r of rows || []) {
        if (r.accountId && r.name) m.set(String(r.accountId), r.name);
      }
      _acctNames = m;
      return m;
    })
    .catch(() => _acctNames);
  return _acctNamesPromise;
}

export function accountName(id) {
  return _acctNames.get(String(id)) || '';
}

export function fmtAccount(id) {
  if (!id) return '—';
  const name = _acctNames.get(String(id));
  return name ? `${name} (${String(id)})` : String(id);
}

export function useAccountNames() {
  const [, setTick] = useState(0);
  useEffect(() => {
    let cancelled = false;
    primeAccountNames().then(() => { if (!cancelled) setTick(t => t + 1); });
    return () => { cancelled = true; };
  }, []);
  return fmtAccount;
}
