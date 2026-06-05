// Tag-key filter dropdowns. One Multiselect per pinned tag key (configured
// in Settings → Pinned tag keys), populated from /api/tags/{key}/values.
//
// Bedrock per-request metadata (the tags ingested into f_daily_tagged) is
// dynamic — customers can attach `team`, `environment`, `business_unit`,
// or anything else they want. The dashboard doesn't ship a hardcoded set;
// it shows whatever the customer pinned.
//
// Selection state lives on the parent's `filters.tag_filter` as an array
// of "key:value" strings (e.g. ["env:prod", "env:staging", "team:platform"]).
// The backend's parse_filters() already consumes this format.

import { useMemo } from 'react';
import { Multiselect } from '@cloudscape-design/components';
import { useApi, fmt } from '../api.js';

function titleCase(s) {
  return (s || '').replace(/[._-]/g, ' ').replace(/\w\S*/g,
    (w) => w.charAt(0).toUpperCase() + w.slice(1));
}

// Common abbreviations customers use as tag KEYS get expanded so the dropdown
// labels read like full words ("Business Unit", not "Bu"). The override only
// affects the *displayed label* — the raw key is what we use to query
// /api/tags/{key}/values, so customer data is untouched. Add entries here as
// you see customer naming conventions.
const KEY_LABEL_OVERRIDES = {
  env: 'Environment',
  bu: 'Business Unit',
  cc: 'Cost Center',
  app: 'Application',
  svc: 'Service',
  proj: 'Project',
  prj: 'Project',
  org: 'Organization',
  dept: 'Department',
};
function prettifyKey(k) {
  if (!k) return k;
  const override = KEY_LABEL_OVERRIDES[k.toLowerCase()];
  if (override) return override;
  return titleCase(k);   // 'business_unit' → 'Business Unit'
}

// Tag VALUES get a plain title-case pass — no opinionated overrides, so the
// dropdown reflects the customer's data verbatim except for capitalization
// of the first letter of each token. Skip values that look like codes
// (CC-1001) or model IDs (anything with a dot or colon).
function prettifyValue(v) {
  if (!v) return v;
  if (/^[A-Z0-9][A-Z0-9_-]*$/.test(v)) return v;          // e.g. CC-1001, AB123
  if (/[.:]/.test(v)) return v;                            // e.g. nova.micro:v1
  return titleCase(v);
}

// Single dropdown for one pinned tag key.
function TagKeyDropdown({ tagKey, filters, setFilters }) {
  const { data, loading } = useApi(`/tags/${encodeURIComponent(tagKey)}/values`, {}, [tagKey]);

  // Selected: pull values from filters.tag_filter that match this key.
  const selected = useMemo(() => {
    return (filters.tag_filter || [])
      .filter(s => s.startsWith(`${tagKey}:`))
      .map(s => {
        const v = s.slice(tagKey.length + 1);
        return { value: v, label: prettifyValue(v) };
      });
  }, [filters.tag_filter, tagKey]);

  const options = (data || []).map(v => ({
    value: v.tag_value,
    label: prettifyValue(v.tag_value),
    description: `${fmt(v.total_requests_30d)} req`,
  }));

  const onChange = ({ detail }) => {
    // Strip out any prior key:* entries, then re-add the newly-selected ones.
    const others = (filters.tag_filter || []).filter(s => !s.startsWith(`${tagKey}:`));
    const newPairs = detail.selectedOptions.map(o => `${tagKey}:${o.value}`);
    setFilters({ ...filters, tag_filter: [...others, ...newPairs] });
  };

  // Label-less style — the placeholder doubles as the field label, just
  // like the other filter dropdowns in App.jsx (Provider / Region / etc).
  // When no value is selected: "All Environments" / "All Business Units".
  // When a value is selected: the chosen tokens replace the placeholder
  // and the title-cased key is implicit from the values themselves.
  const prettyKey = prettifyKey(tagKey);
  return (
    <Multiselect
      selectedOptions={selected}
      options={options}
      onChange={onChange}
      placeholder={
        loading ? 'Loading…' :
        options.length ? `All ${prettyKey}${prettyKey.endsWith('s') ? '' : 's'}` :
        `${prettyKey} — no values yet`
      }
      empty={
        loading ? 'Loading…' :
        'No tagged Bedrock calls have ingested yet for this key.'
      }
      tokenLimit={3}
      filteringType="auto"
      disabled={!loading && options.length === 0}
    />
  );
}

// Outer hook (NOT a component) — reads pinned keys from /preferences and
// returns an ARRAY of <TagKeyDropdown> elements that the caller can spread
// directly into a ColumnLayout's children. We can't return them from a
// component wrapper because Cloudscape's ColumnLayout iterates React.Children
// on its direct children only — so a single <TagFilters> element would be
// counted as ONE column slot regardless of how many fields it renders, and
// the dropdowns would stack vertically inside that slot. (That was the
// "multi-tag misalignment" UI bug.)
export function useTagFilterFields(filters, setFilters) {
  const { data: prefs } = useApi('/preferences', {}, []);
  const pinned = prefs?.pinned_tag_keys || [];
  if (!pinned.length) return [];
  return pinned.map(key => (
    <TagKeyDropdown key={key} tagKey={key} filters={filters} setFilters={setFilters} />
  ));
}

// Backwards-compat default export — kept so any caller using <TagFilters />
// keeps working, even though it triggers the layout bug. Prefer the hook.
export default function TagFilters({ filters, setFilters }) {
  const fields = useTagFilterFields(filters, setFilters);
  return fields.length ? <>{fields}</> : null;
}
