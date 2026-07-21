// Bedrock Ops Lens — root component.
//
// Layout:
//   - TopNavigation: logo + theme toggle + user dropdown (name, email, sign out).
//   - Freshness strip: data freshness pill, just below TopNavigation.
//   - AppLayout:
//       - navigation slot: collapsible SideNavigation (replaces horizontal Tabs).
//       - content slot:    page header + filter row + the active view.
//       - tools slot:      contextual HelpPanel populated by `onInfo`.
//
// The active "tab" is now an active "view" tracked via state. Each view is
// lazy-mounted on first visit and stays mounted afterwards so its useApi
// caches survive switches.

import { useEffect, useMemo, useState } from 'react';
import {
  AppLayout, ContentLayout, Header, Box, SpaceBetween,
  Select, Multiselect, ColumnLayout, TopNavigation,
  Badge, HelpPanel, DateRangePicker, Link, SideNavigation, Icon,
  ExpandableSection, Spinner, Alert,
} from '@cloudscape-design/components';
import { applyMode, Mode } from '@cloudscape-design/global-styles';
import { useApi, fmt, setAttributionContext } from './api.js';
import SectionPanel from './components/SectionInfo.jsx';
import { UserProvider, useUser } from './components/UserContext.jsx';
import AuthApp from './views/AuthApp.jsx';
import { useAttributeFilterFields } from './components/AttributeFilters.jsx';
import OverviewTab from './tabs/OverviewTab.jsx';
import ErrorsTab from './tabs/ErrorsTab.jsx';
import LatencyTab from './tabs/LatencyTab.jsx';
import OpsInsightsTab from './tabs/OpsInsightsTab.jsx';
import OpsReviewTab from './tabs/OpsReviewTab.jsx';
import QuotasTab from './tabs/QuotasTab.jsx';
import CostTab from './tabs/CostTab.jsx';
import ModelLifecycleTab from './tabs/ModelLifecycleTab.jsx';
import ModelInsightsTab from './tabs/ModelInsightsTab.jsx';
import WorkloadsTab from './tabs/WorkloadsTab.jsx';
import SettingsView from './views/SettingsView.jsx';

/* --- Theme management ------------------------------------------------- */
const THEME_KEY = 'bedrock-lens-theme';
function loadTheme() {
  try {
    const stored = localStorage.getItem(THEME_KEY);
    if (stored === 'dark' || stored === 'light') return stored;
  } catch {}
  if (typeof window !== 'undefined' && window.matchMedia?.('(prefers-color-scheme: dark)').matches) {
    return 'dark';
  }
  return 'light';
}
function useTheme() {
  const [theme, setTheme] = useState(loadTheme);
  useEffect(() => {
    applyMode(theme === 'dark' ? Mode.Dark : Mode.Light);
    try { localStorage.setItem(THEME_KEY, theme); } catch {}
  }, [theme]);
  return [theme, setTheme];
}

/* --- Top-bar option lists ---------------------------------------------
   Provider and Region are intentionally sourced LIVE from the customer's
   own data (see /api/distinct-filters). A hardcoded list either misses
   options the customer is using (new providers, niche regions) or shows
   ones they don't (and clutter the dropdown).

   Provider display labels — pretty version of the modelId-prefix string.
   Mirrors the same map in tabs/ModelInsightsTab.jsx; drift between the
   two is intentional only if you really mean to spell a provider
   differently in the FilterBar vs the Insights table. */
const PROVIDER_LABEL = {
  anthropic:  'Anthropic',
  amazon:     'Amazon',
  meta:       'Meta',
  cohere:     'Cohere',
  mistral:    'Mistral',
  ai21:       'AI21',
  deepseek:   'DeepSeek',
  qwen:       'Qwen',
  twelvelabs: 'Twelve Labs',
  writer:     'Writer',
  nvidia:     'NVIDIA',
  google:     'Google',
  moonshotai: 'MoonshotAI',
  openai:     'OpenAI',
  stability:  'Stability AI',
};
const labelForProvider = p =>
  PROVIDER_LABEL[p] || (p ? p.charAt(0).toUpperCase() + p.slice(1) : p);

// Friendly region descriptors. Keys are AWS region codes; if the customer's
// data has a code we don't have a description for, fall back to the bare
// code (e.g. `us-gov-east-1`). Not exhaustive — fill in as needed.
const REGION_DESC = {
  'us-east-1':      'N. Virginia',
  'us-east-2':      'Ohio',
  'us-west-1':      'N. California',
  'us-west-2':      'Oregon',
  'ca-central-1':   'Canada Central',
  'sa-east-1':      'São Paulo',
  'eu-west-1':      'Ireland',
  'eu-west-2':      'London',
  'eu-west-3':      'Paris',
  'eu-central-1':   'Frankfurt',
  'eu-north-1':     'Stockholm',
  'eu-south-1':     'Milan',
  'eu-south-2':     'Spain',
  'me-central-1':   'UAE',
  'me-south-1':     'Bahrain',
  'af-south-1':     'Cape Town',
  'ap-northeast-1': 'Tokyo',
  'ap-northeast-2': 'Seoul',
  'ap-northeast-3': 'Osaka',
  'ap-southeast-1': 'Singapore',
  'ap-southeast-2': 'Sydney',
  'ap-southeast-3': 'Jakarta',
  'ap-southeast-4': 'Melbourne',
  'ap-southeast-5': 'Malaysia',
  'ap-southeast-7': 'Thailand',
  'ap-south-1':     'Mumbai',
  'ap-south-2':     'Hyderabad',
  'ap-east-1':      'Hong Kong',
  'il-central-1':   'Tel Aviv',
};
const labelForRegion = r =>
  REGION_DESC[r] ? `${r} (${REGION_DESC[r]})` : r;

const TRAFFIC_OPTIONS = [
  { value: 'all',           label: 'All traffic' },
  { value: 'cris',          label: 'CRIS (any)' },
  { value: 'global_cris',   label: 'CRIS (global)' },
  { value: 'on_demand',     label: 'On-Demand only' },
  { value: 'provisioned',   label: 'Provisioned' },
];

/* --- Freshness pill ---------------------------------------------------- */
function FreshnessPill() {
  const { data } = useApi('/ingestion-status', {}, []);
  if (!data) return null;
  const ts = data?.meta?.last_cw_metrics_refresh?.value;
  if (!ts) {
    return <Badge color="grey">No data yet — run the CW ingester</Badge>;
  }
  const ageMin = (Date.now() - new Date(ts).getTime()) / 60000;
  // Red reads as "alarm/danger" but stale-by-an-hour data isn't a hazard,
  // it's just a heads-up. Use Cloudscape's `grey` for the >6h state to
  // de-escalate the visual weight; green stays the "all good" affordance.
  const color = ageMin < 60 ? 'green' : ageMin < 360 ? 'blue' : 'grey';
  const label =
    ageMin < 60 ? `Fresh (${Math.round(ageMin)}m ago)` :
    ageMin < 1440 ? `${Math.round(ageMin / 60)}h ago` :
    `${Math.round(ageMin / 1440)}d ago`;
  return <Badge color={color}>{label}</Badge>;
}

/* --- Top filter row ---------------------------------------------------- */
const RELATIVE_PRESETS = [
  { key: 'previous-1-day',  amount: 1,  unit: 'day',  type: 'relative' },
  { key: 'previous-3-days', amount: 3,  unit: 'day',  type: 'relative' },
  { key: 'previous-7-days', amount: 7,  unit: 'day',  type: 'relative' },
  { key: 'previous-14-days',amount: 14, unit: 'day',  type: 'relative' },
  { key: 'previous-30-days',amount: 30, unit: 'day',  type: 'relative' },
  { key: 'previous-60-days',amount: 60, unit: 'day',  type: 'relative' },
  { key: 'previous-90-days',amount: 90, unit: 'day',  type: 'relative' },
];

function dateRangeValueFromFilters(f) {
  if (f.start && f.end) {
    return { type: 'absolute', startDate: f.start, endDate: f.end };
  }
  const found = RELATIVE_PRESETS.find(p => p.amount === Number(f.days));
  return found
    ? { type: 'relative', amount: found.amount, unit: found.unit }
    : { type: 'relative', amount: 7, unit: 'day' };
}

// One-line summary of the current filter state, shown in the collapsed
// ExpandableSection header so the user sees at a glance what's filtered
// without having to expand. Empty/all selections are dropped — we only
// surface the active narrowing.
function summarizeFilters(f) {
  const parts = [];
  // Date range — always shown since it's always set.
  if (f.start && f.end) parts.push(`${f.start} → ${f.end}`);
  else parts.push(`Last ${f.days || 14} days`);
  if (f.accounts && f.accounts.length) {
    parts.push(`${f.accounts.length} account${f.accounts.length === 1 ? '' : 's'}`);
  } else {
    parts.push('All accounts');
  }
  if (f.provider && f.provider !== 'all') parts.push(`Provider: ${f.provider}`);
  if (f.region && f.region !== 'all')     parts.push(`Region: ${f.region}`);
  if (f.traffic_type && f.traffic_type !== 'all') {
    parts.push(`Traffic: ${f.traffic_type}`);
  }
  if (f.tag_filter && f.tag_filter.length) {
    parts.push(`${f.tag_filter.length} tag filter${f.tag_filter.length === 1 ? '' : 's'}`);
  }
  return parts.join(' · ');
}

function FilterBar({ filters, setFilters, hideTraffic }) {
  const setVal = (k) => ({ detail }) =>
    setFilters({ ...filters, [k]: detail.selectedOption.value });

  // Provider + region option lists are derived from /api/distinct-filters
  // (live, per-customer). Always-prepend an 'all' sentinel so the dropdown
  // has a default. While the API call is in flight, fall back to just the
  // sentinel — the dropdown still works (defaults to All), nothing crashes.
  const distinct = useApi('/distinct-filters', {}, []).data || {};
  const PROVIDER_OPTIONS = useMemo(() => [
    { value: 'all', label: 'All providers' },
    ...((distinct.providers || []).map(p => ({ value: p, label: labelForProvider(p) }))),
  ], [distinct.providers]);
  const REGION_OPTIONS = useMemo(() => [
    { value: 'all', label: 'All regions' },
    ...((distinct.regions || []).map(r => ({ value: r, label: labelForRegion(r) }))),
  ], [distinct.regions]);

  const pr = PROVIDER_OPTIONS.find(o => o.value === filters.provider) || PROVIDER_OPTIONS[0];
  const rg = REGION_OPTIONS.find(o => o.value === filters.region) || REGION_OPTIONS[0];
  const tt = TRAFFIC_OPTIONS.find(o => o.value === filters.traffic_type) || TRAFFIC_OPTIONS[0];

  // Tag-key dropdowns from the user's pinned-tag-keys preference.
  // Returned as an ARRAY (not a wrapper component) so each dropdown becomes
  // a direct child of <ColumnLayout> and lays out into the grid correctly.
  const tagFilterFields = useAttributeFilterFields(filters, setFilters);

  const accountsState = useApi('/accounts', {}, []);
  const accountOptions = (accountsState.data || []).map(a => {
    const idle = !a.total_requests;
    // Lead with the friendly name when we know it; the 12-digit ID drops
    // into the description line. Falls back to the ID as label when no
    // name is mapped yet (e.g., explicit-mode accounts without org metadata).
    const label = a.name || a.accountId;
    const desc = a.name
      ? `${a.accountId} · ${idle ? 'No traffic in last 30 days' : `${fmt(a.total_requests)} req · ${a.model_count} model${a.model_count === 1 ? '' : 's'}`}`
      : (idle ? 'No traffic in last 30 days' : `${fmt(a.total_requests)} req · ${a.model_count} model${a.model_count === 1 ? '' : 's'}`);
    return { value: a.accountId, label, description: desc, tags: idle ? ['idle'] : [] };
  });
  const selectedAccountOptions = (filters.accounts || []).map(id => {
    const meta = accountOptions.find(o => o.value === id);
    return meta || { value: id, label: id };
  });

  const onRangeChange = ({ detail }) => {
    const v = detail.value;
    if (!v) {
      setFilters({ ...filters, days: 7, start: '', end: '' });
      return;
    }
    if (v.type === 'absolute') {
      const s = (v.startDate || '').slice(0, 10);
      const e = (v.endDate || '').slice(0, 10);
      const days = Math.max(
        1,
        Math.round((new Date(e).getTime() - new Date(s).getTime()) / 86400000) + 1,
      );
      setFilters({ ...filters, days, start: s, end: e });
    } else {
      const unitDays = { second: 1 / 86400, minute: 1 / 1440, hour: 1 / 24,
                         day: 1, week: 7, month: 30, year: 365 }[v.unit] || 1;
      const days = Math.max(1, Math.round(v.amount * unitDays));
      setFilters({ ...filters, days, start: '', end: '' });
    }
  };

  const isValidRange = (range) => {
    if (range.type === 'absolute') {
      const s = new Date(range.startDate);
      const e = new Date(range.endDate);
      if (isNaN(s) || isNaN(e)) return { valid: false, errorMessage: 'Invalid date' };
      if (e < s) return { valid: false, errorMessage: 'End must be after start' };
      const days = (e - s) / 86400000;
      if (days > 90) return { valid: false, errorMessage: 'Max 90 days' };
    }
    return { valid: true };
  };

  // Label-less filter row — matches the internal Bedrock Lens convention.
  // Every control's SELECTED VALUE is its own label:
  //   "All Providers" / "Last 14 days" / "All Regions" — the placeholder /
  //   default option text tells the user what the field controls. When a
  //   value is selected ("Provider: Anthropic"), the label is implicit
  //   from the value itself. Fewer pixels, denser, cleaner.
  //
  // Tag filters need a label-equivalent because the placeholder ("Filter
  // by environment") is replaced once a value is selected — so we render
  // the title-cased tag-key INSIDE the placeholder/value as a hint.
  return (
    <div style={{
      display: 'grid',
      gridTemplateColumns: 'repeat(auto-fill, minmax(200px, 1fr))',
      columnGap: 12,
      rowGap: 10,
      alignItems: 'start',
    }}>
      <DateRangePicker
        value={dateRangeValueFromFilters(filters)}
        onChange={onRangeChange}
        relativeOptions={RELATIVE_PRESETS}
        isValidRange={isValidRange}
        dateOnly
        rangeSelectorMode="default"
        placeholder="Date range"
        i18nStrings={{
          relativeModeTitle: 'Relative range',
          absoluteModeTitle: 'Absolute range',
          relativeRangeSelectionHeading: 'Choose a range',
          customRelativeRangeOptionLabel: 'Custom range',
          customRelativeRangeOptionDescription: 'Set a custom range in the past',
          customRelativeRangeUnitLabel: 'Unit of time',
          customRelativeRangeDurationLabel: 'Duration',
          customRelativeRangeDurationPlaceholder: 'Enter duration',
          startDateLabel: 'Start date',
          endDateLabel: 'End date',
          startTimeLabel: 'Start time',
          endTimeLabel: 'End time',
          clearButtonLabel: 'Clear',
          cancelButtonLabel: 'Cancel',
          applyButtonLabel: 'Apply',
          formatRelativeRange: (e) => `Last ${e.amount} ${e.unit}${e.amount === 1 ? '' : 's'}`,
          formatUnit: (unit, value) => `${value === 1 ? unit : unit + 's'}`,
        }}
      />
      <Multiselect
        selectedOptions={selectedAccountOptions}
        options={accountOptions}
        onChange={({ detail }) =>
          setFilters({ ...filters, accounts: detail.selectedOptions.map(o => o.value) })
        }
        placeholder={
          accountOptions.length
            ? `All accounts${accountOptions.length ? ` (${accountOptions.length})` : ''}`
            : 'No data yet'
        }
        empty="No accounts have data in the selected window."
        tokenLimit={3}
        filteringType="auto"
        disabled={accountOptions.length === 0}
      />
      <Select selectedOption={pr} options={PROVIDER_OPTIONS} onChange={setVal('provider')} />
      <Select selectedOption={rg} options={REGION_OPTIONS} onChange={setVal('region')} />
      {!hideTraffic && (
        <Select selectedOption={tt} options={TRAFFIC_OPTIONS} onChange={setVal('traffic_type')} />
      )}
      {/* Each tag dropdown is a direct grid child so it gets its own cell —
          wrapping in a single component would put them all into one cell
          and stack them vertically (the misalignment bug). */}
      {tagFilterFields}
    </div>
  );
}

/* --- Sidebar nav ------------------------------------------------------ */
//
// Cloudscape's `SideNavigation` accepts a ReactNode for the `text` field on
// `link` items. We render a small icon followed by the label inside a flex
// span — this is how the AWS Console adds iconography to its own
// SideNavigation items today (no per-item `iconName` prop yet).
//
// Icon picks follow the iconography guidelines:
//   - one icon per top-level concept
//   - status-shaped icons (warning, bug) reserved for sections that flag
//     a state to the operator, not used as decoration
//   - `gen-ai` is the AWS sparkle reserved for AI-synthesized features
function navItem(label, href, iconName) {
  return {
    type: 'link',
    href,
    text: (
      <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8 }}>
        <Icon name={iconName} size="small" />
        <span>{label}</span>
      </span>
    ),
  };
}

const NAV_ITEMS_DASHBOARD = [
  { type: 'section-group', title: 'Dashboard', items: [
    navItem('Overview',            '#/overview',   'view-full'),
    navItem('Quotas',              '#/quotas',     'status-warning'),
    // Cost between Quotas and Health & Errors. Cloudscape has no money
    // icon; render a literal `$` glyph at the same size as the other
    // icons. Defined as a custom item below so we don't pass a fake
    // iconName to <Icon name=…> (which would silently render nothing,
    // the `caret-up-down` mistake from earlier).
    {
      type: 'link',
      href: '#/cost',
      text: (
        <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8 }}>
          {/* Match Cloudscape <Icon size="small">: ~16px box, glyph itself
              at 18px / 800 weight reads as the same visual mass as the
              other sidebar icons. */}
          <span style={{
            display: 'inline-block', width: 16, height: 16,
            textAlign: 'center', lineHeight: '16px',
            fontWeight: 800, fontSize: 18,
            fontFamily: 'inherit',
          }}>$</span>
          <span>Cost Insights</span>
        </span>
      ),
    },
    navItem('Health & Errors',     '#/errors',     'bug'),
    // `status-pending` is the closest Cloudscape glyph to a stopwatch /
    // timer — fits "Latency" semantically. The originally proposed
    // `caret-up-down` doesn't exist in Cloudscape's icon library and
    // silently rendered nothing.
    navItem('Latency',             '#/latency',    'status-pending'),
    navItem('Capacity & Adoption', '#/ops',         'check'),
    navItem('Model Insights',      '#/insights',    'suggestions'),
    navItem('Model Lifecycle',     '#/lifecycle',   'calendar'),
    navItem('Ops Review',          '#/ops-review', 'gen-ai'),
  ]},
];

// Dashboard nav with the Workloads item injected only when proxy
// per-workload telemetry exists (Task A). Hidden entirely otherwise — same
// principle as hiding empty Mantle sub-tabs: never show an empty view.
function navItemsDashboard(workloadsAvail) {
  if (!workloadsAvail) return NAV_ITEMS_DASHBOARD;
  const [group] = NAV_ITEMS_DASHBOARD;
  const items = [...group.items];
  const opsIdx = items.findIndex(i => i.href === '#/ops');
  const wl = navItem('Usage · Custom Attributes', '#/workloads', 'group-active');
  items.splice(opsIdx >= 0 ? opsIdx + 1 : items.length, 0, wl);
  return [{ ...group, items }];
}

const NAV_ADMIN_SECTION = (isAdmin) => isAdmin ? [
  { type: 'divider' },
  { type: 'section-group', title: 'Admin', items: [
    navItem('Settings', '#/settings', 'settings'),
  ]},
] : [];

const NAV_FOOTER = [
  { type: 'divider' },
  { type: 'link', external: true, href: 'https://docs.aws.amazon.com/bedrock/',
    text: (
      <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8 }}>
        <Icon name="file" size="small" /><span>Bedrock docs</span>
      </span>
    ) },
  { type: 'link', external: true, href: 'https://aws.amazon.com/bedrock/pricing/',
    text: (
      <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8 }}>
        <Icon name="external" size="small" /><span>Bedrock pricing</span>
      </span>
    ) },
];

const VIEW_FROM_HASH = (h) => (h || '').replace(/^#\/?/, '') || 'overview';

/* --- Root ------------------------------------------------------------- */
function AppShell() {
  const [filters, setFilters] = useState({
    // 7-day default: the dashboard opens on the most recent week — the window
    // most operators care about day-to-day. Widen via the time picker (up to
    // 90 days) to reach historical / bursty bedrock-mantle usage.
    days: 7,
    start: '',
    end: '',
    provider: 'all',
    region: 'all',
    traffic_type: 'all',
    accounts: [],
    // Bedrock endpoint slice. 'all' sums runtime + mantle. Each tab can
    // override locally via its EndpointSubTabs switcher; the global
    // default stays 'all' so the FilterBar / KPI ribbon shows the
    // consolidated picture.
    endpoint: 'all',
  });
  const [activeView, setActiveView] = useState(() => VIEW_FROM_HASH(window.location.hash));
  const [theme, setTheme] = useTheme();
  const [infoSection, setInfoSection] = useState(null);
  const [toolsOpen, setToolsOpen] = useState(false);
  const [navOpen, setNavOpen] = useState(true);

  // Lazy-mount visited views; keep them mounted for cache survival.
  const [visited, setVisited] = useState(() => new Set([activeView]));
  const { user, isAdmin, authEnabled } = useUser();

  // Surface the "Usage · Custom Attributes" view when EITHER attribution source
  // is active — proxy dimensions OR invocation-log tags. effective_source is
  // 'off' only when neither has data and no admin override, in which case the
  // nav item is hidden (same principle as hiding empty Mantle sub-tabs).
  const attrCfg = useApi('/attribution/config', {}, []).data;
  const workloadsAvail = attrCfg ? attrCfg.effective_source !== 'off' : false;

  // Cross-tab redirect: when an attribution source is active (proxy OR
  // invocation_logs) and the user selected attribute value(s) in the top bar,
  // redirect CW-backed endpoints to their attribution-sourced siblings (see
  // api.js). CW metrics carry no attribute dimension, so the re-slice has to
  // come from the source table (f_proxy_dim_hourly or f_daily_tagged); the
  // xtab/* endpoints pick the right one by effective_source. The top-bar filter
  // writes "key:value" strings into filters.tag_filter; we pin the single
  // active key (an attribute filter targets one dimension key at a time).
  useEffect(() => {
    const source = attrCfg?.effective_source;
    const tf = filters.tag_filter || [];
    if ((source === 'proxy' || source === 'invocation_logs') && tf.length) {
      const dimKey = tf[0].split(':')[0];
      const dimValues = tf.filter(s => s.startsWith(`${dimKey}:`)).map(s => s.slice(dimKey.length + 1));
      setAttributionContext({ active: true, dimKey, dimValues });
    } else {
      setAttributionContext({ active: false, dimKey: null, dimValues: [] });
    }
  }, [attrCfg, filters.tag_filter]);

  // Hash-based routing — sidebar links use `#/foo`, the app reads it on
  // mount and listens for hashchange.
  useEffect(() => {
    const onHash = () => {
      const v = VIEW_FROM_HASH(window.location.hash);
      setActiveView(v);
      setVisited(prev => prev.has(v) ? prev : new Set([...prev, v]));
    };
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, []);

  const onNavFollow = (e) => {
    if (!e.detail || e.detail.external) return;
    e.preventDefault();
    const href = e.detail.href || '';
    window.location.hash = href.startsWith('#') ? href.slice(1) : href;
  };

  const onInfo = (sectionId) => {
    setInfoSection(sectionId);
    setToolsOpen(true);
  };

  // Only render the ACTIVE view's body. Earlier we wrapped every view in a
  // hidden <div style="display:none"> so they could pre-cache, but Cloudscape's
  // <SpaceBetween> applies margin-top to every direct child regardless of
  // visibility — so each hidden wrapper added one full "m" gap above the
  // active view. Result: Overview (1st in list) had a small gap, Ops Review
  // (last) had ~8x as much. Returning null for non-active views eliminates
  // those wrapper children entirely.
  const viewBody = (id, node) => (activeView === id ? node : null);

  const isSettings = activeView === 'settings';
  const isOpsReview = activeView === 'ops-review';

  return (
    <>
      <TopNavigation
        identity={{
          href: '#/overview',
          title: 'Bedrock Ops Lens',
          // Public AWS-hosted "smile" mark — same image the internal Bedrock
          // Lens uses in its TopNavigation. Hosted at awsstatic.com (CDN);
          // no auth required.
          logo: {
            src: 'https://a0.awsstatic.com/libra-css/images/logos/aws_smile-header-desktop-en-white_59x35@2x.png',
            alt: 'AWS',
          },
        }}
        utilities={[
          // Theme toggle. Icon-only button matching the internal Bedrock Lens:
          // moon glyph in light mode (clicking switches to dark) and a sun
          // glyph in dark mode (clicking switches back to light). No text
          // label — the icon itself is the affordance, and the aria-label
          // covers screen-reader users.
          {
            type: 'button',
            iconSvg: theme === 'dark' ? (
              <svg viewBox="0 0 16 16" focusable="false" xmlns="http://www.w3.org/2000/svg">
                <circle cx="8" cy="8" r="3" fill="currentColor" />
                <g stroke="currentColor" strokeWidth="1.4" strokeLinecap="round">
                  <line x1="8" y1="1" x2="8" y2="2.5" />
                  <line x1="8" y1="13.5" x2="8" y2="15" />
                  <line x1="1" y1="8" x2="2.5" y2="8" />
                  <line x1="13.5" y1="8" x2="15" y2="8" />
                  <line x1="3" y1="3" x2="4.1" y2="4.1" />
                  <line x1="11.9" y1="11.9" x2="13" y2="13" />
                  <line x1="13" y1="3" x2="11.9" y2="4.1" />
                  <line x1="4.1" y1="11.9" x2="3" y2="13" />
                </g>
              </svg>
            ) : (
              <svg viewBox="0 0 16 16" focusable="false" xmlns="http://www.w3.org/2000/svg">
                <path d="M6.5 1.2a6.6 6.6 0 1 0 8.3 8.3A5.4 5.4 0 0 1 6.5 1.2z"
                      fill="currentColor" />
              </svg>
            ),
            ariaLabel: theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode',
            onClick: () => setTheme(theme === 'dark' ? 'light' : 'dark'),
          },
          {
            type: 'menu-dropdown',
            text: user?.email || user?.sub || 'Account',
            // No description — "Cognito" was a debug-era label that read as
            // user-facing noise. The user already knows they're signed in.
            iconName: 'user-profile',
            items: [
              ...(isAdmin
                ? [{ id: 'settings', text: 'Settings', href: '#/settings' }]
                : []),
              {
                id: 'signout',
                text: authEnabled ? 'Sign out' : 'Sign out (disabled in local dev)',
                disabled: !authEnabled,
              },
            ],
            onItemClick: ({ detail }) => {
              if (detail.id === 'settings') {
                window.location.hash = '/settings';
                return;
              }
              if (detail.id === 'signout' && authEnabled) {
                // Backend /api/auth/logout is a POST that clears the cookie
                // and returns 200. After it succeeds, hard-reload so the
                // UserContext re-fetches /me, sees 401, and renders <AuthApp/>.
                fetch('/api/auth/logout', { method: 'POST', credentials: 'include' })
                  .finally(() => { window.location.href = '/'; });
              }
            },
          },
        ]}
      />
      <div className="mirror-freshness-strip" style={{ padding: '6px 28px' }}>
        <SpaceBetween size="m" direction="horizontal">
          <Box variant="awsui-key-label" display="inline">Data freshness:</Box>
          <FreshnessPill />
        </SpaceBetween>
      </div>
      <AppLayout
        navigationOpen={navOpen}
        onNavigationChange={({ detail }) => setNavOpen(detail.open)}
        navigation={
          // No header — the TopNavigation already shows "Bedrock Ops Lens"
          // and a duplicate at the top of the sidebar is just noise.
          <SideNavigation
            activeHref={`#/${activeView}`}
            items={[
              ...navItemsDashboard(workloadsAvail),
              ...NAV_ADMIN_SECTION(isAdmin),
              ...NAV_FOOTER,
            ]}
            onFollow={onNavFollow}
          />
        }
        toolsOpen={toolsOpen}
        onToolsChange={({ detail }) => setToolsOpen(detail.open)}
        tools={infoSection ? <SectionPanel sectionId={infoSection} /> : (
          <HelpPanel header={<h2>About this dashboard</h2>}>
            <p>
              <strong>Bedrock Ops Lens</strong> gives you fleet-wide
              observability for Amazon Bedrock — usage, errors, latency,
              capacity insights, cost, and an AI-synthesized ops review.
            </p>
            <p>
              Click the <strong>Info</strong> link on any container header
              to see what it shows, why it matters, and what to do about it.
            </p>
          </HelpPanel>
        )}
        content={
          isSettings ? <SettingsView onInfo={onInfo} /> : (
            // No ContentLayout header — TopNavigation already labels the
            // app, and the page-level <h1> was a third "Bedrock Ops Lens"
            // duplicate. The Info link moved into TopNavigation utilities
            // (or it can come back as a sidebar footer item; for now we
            // surface it via every container's per-section Info link).
            <ContentLayout disableOverlap>
              <SpaceBetween size="m">
                {/* FilterBar wrapped in an ExpandableSection so the page
                    isn't dominated by the filter row when the user just
                    wants the default fleet-wide view. The summary line
                    keeps the active selection visible without expanding. */}
                <ExpandableSection
                  variant="container"
                  defaultExpanded={false}
                  headerText="Filters"
                  headerDescription={summarizeFilters(filters)}
                >
                  <FilterBar
                    filters={filters}
                    setFilters={setFilters}
                    hideTraffic={isOpsReview}
                  />
                </ExpandableSection>
                {/* Provenance banner: when a proxy attribute filter is active,
                    the CW-backed tabs (Overview volume/KPIs, Latency) are
                    re-served from the proxy event stream so they can honor the
                    filter. Say so, so numbers aren't mistaken for native CW. */}
                {attrCfg?.effective_source === 'proxy'
                  && (filters.tag_filter || []).length > 0
                  && ['overview', 'latency'].includes(activeView) && (
                  <Alert type="info">
                    Filtered to {(filters.tag_filter || []).join(', ')} — these
                    views are sourced from your proxy event stream (not native
                    CloudWatch) so they can break down by attribute. Clear the
                    attribute filter for the full CloudWatch-based view.
                  </Alert>
                )}
                {viewBody('overview',   <OverviewTab     filters={filters} onInfo={onInfo} />)}
                {viewBody('quotas',     <QuotasTab       filters={filters} onInfo={onInfo} />)}
                {viewBody('cost',       <CostTab         filters={filters} onInfo={onInfo} />)}
                {viewBody('errors',     <ErrorsTab       filters={filters} onInfo={onInfo} />)}
                {viewBody('latency',    <LatencyTab      filters={filters} onInfo={onInfo} />)}
                {viewBody('ops',        <OpsInsightsTab     filters={filters} onInfo={onInfo} />)}
                {viewBody('workloads',  <WorkloadsTab       filters={filters} onInfo={onInfo} />)}
                {viewBody('insights',   <ModelInsightsTab   filters={filters} onInfo={onInfo} />)}
                {viewBody('lifecycle',  <ModelLifecycleTab  filters={filters} onInfo={onInfo} />)}
                {viewBody('ops-review', <OpsReviewTab       filters={filters} onInfo={onInfo} />)}
              </SpaceBetween>
            </ContentLayout>
          )
        }
      />
    </>
  );
}

// Gate the dashboard behind auth: when AUTH_ENABLED=true and the user
// has no valid session cookie, /me returns 401 → UserContext sets
// isAuthenticated=false → we render <AuthApp/> instead of the dashboard.
// In local-dev mode (AUTH_ENABLED=false), /me returns the synthetic user
// → isAuthenticated=true → dashboard renders directly.
//
// While the /me call is in flight (which can take 6-10 seconds on a cold
// Lambda), we render a branded splash instead of a white screen so users
// don't think the page is broken.
function LoadingSplash() {
  return (
    <div style={{
      minHeight: '100vh',
      display: 'flex',
      flexDirection: 'column',
      alignItems: 'center',
      justifyContent: 'center',
      gap: 16,
      color: '#16191f',
      fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif',
    }}>
      <div style={{ fontSize: 22, fontWeight: 600 }}>Bedrock Ops Lens</div>
      <Spinner size="large" />
      <div style={{ fontSize: 13, color: '#5f6b7a' }}>Loading...</div>
    </div>
  );
}

function AppRouter() {
  const { loading, isAuthenticated } = useUser();
  if (loading) return <LoadingSplash />;
  if (!isAuthenticated) return <AuthApp />;
  return <AppShell />;
}

export default function App() {
  return (
    <UserProvider>
      <AppRouter />
    </UserProvider>
  );
}
