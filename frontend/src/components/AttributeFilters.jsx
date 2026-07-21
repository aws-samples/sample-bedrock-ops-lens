// Top-bar custom-attribute filter dropdowns — SOURCE-AGNOSTIC.
//
// Renders one Multiselect per surfaced attribute key, driven by whichever
// attribution source the admin enabled (see Settings → Custom attribute
// attribution):
//   • proxy            → keys from pinned_proxy_keys, values from
//                        /api/attribution/values (dim_proxy_dimensions)
//   • invocation_logs  → keys from pinned_tag_keys, values from
//                        /api/tags/{key}/values (dim_tags)
//
// Both write selections to the SAME `filters.tag_filter` array as "key:value"
// strings, so every downstream tab that already honors tag_filter works for
// either source with no per-tab change.
//
// Returned as an ARRAY of elements (not a wrapper component) so each dropdown
// is a direct grid child of the FilterBar — same reason as the old
// useTagFilterFields (Cloudscape counts direct children for layout).

import { useMemo } from 'react';
import { Multiselect } from '@cloudscape-design/components';
import { useApi, fmt } from '../api.js';

const KEY_LABEL_OVERRIDES = {
  env: 'Environment', bu: 'Business Unit', cc: 'Cost Center', app: 'Application',
  svc: 'Service', proj: 'Project', prj: 'Project', org: 'Organization', dept: 'Department',
};
function titleCase(s) {
  return (s || '').replace(/[._-]/g, ' ').replace(/\w\S*/g,
    (w) => w.charAt(0).toUpperCase() + w.slice(1));
}
function prettifyKey(k) {
  if (!k) return k;
  return KEY_LABEL_OVERRIDES[k.toLowerCase()] || titleCase(k);
}
function prettifyValue(v) {
  if (!v) return v;
  if (/^[A-Z0-9][A-Z0-9_-]*$/.test(v)) return v;
  if (/[.:]/.test(v)) return v;
  return titleCase(v);
}

// One dropdown for one attribute key. `valuesPath` differs by source.
function AttrKeyDropdown({ attrKey, valuesPath, valueField, filters, setFilters }) {
  const { data, loading } = useApi(valuesPath, {}, [valuesPath]);

  const selected = useMemo(() => (
    (filters.tag_filter || [])
      .filter(s => s.startsWith(`${attrKey}:`))
      .map(s => { const v = s.slice(attrKey.length + 1); return { value: v, label: prettifyValue(v) }; })
  ), [filters.tag_filter, attrKey]);

  const options = (data || []).map(v => ({
    value: v[valueField],
    label: prettifyValue(v[valueField]),
    description: `${fmt(v.total_requests_30d ?? v.total_requests ?? 0)} req`,
  }));

  const onChange = ({ detail }) => {
    const others = (filters.tag_filter || []).filter(s => !s.startsWith(`${attrKey}:`));
    const added = detail.selectedOptions.map(o => `${attrKey}:${o.value}`);
    setFilters({ ...filters, tag_filter: [...others, ...added] });
  };

  const prettyKey = prettifyKey(attrKey);
  return (
    <Multiselect
      selectedOptions={selected}
      options={options}
      onChange={onChange}
      placeholder={loading ? 'Loading…'
        : options.length ? `All ${prettyKey}${prettyKey.endsWith('s') ? '' : 's'}`
        : `${prettyKey} — no values yet`}
      empty={loading ? 'Loading…' : 'No values ingested yet for this attribute.'}
      tokenLimit={3}
      filteringType="auto"
      disabled={!loading && options.length === 0}
    />
  );
}

// Hook returning the array of attribute dropdowns for the FilterBar. Reads the
// effective attribution source + the surfaced keys for that source.
export function useAttributeFilterFields(filters, setFilters) {
  const cfg = useApi('/attribution/config', {}, []).data;
  const { data: prefs } = useApi('/preferences', {}, []);
  const source = cfg?.effective_source || 'off';

  if (source === 'off') return [];
  const proxy = source === 'proxy';
  const keys = (proxy ? prefs?.pinned_proxy_keys : prefs?.pinned_tag_keys) || [];
  if (!keys.length) return [];

  return keys.map(key => (
    <AttrKeyDropdown
      key={`${source}:${key}`}
      attrKey={key}
      valuesPath={proxy
        ? `/attribution/values?dim_key=${encodeURIComponent(key)}`
        : `/tags/${encodeURIComponent(key)}/values`}
      valueField={proxy ? 'value' : 'tag_value'}
      filters={filters}
      setFilters={setFilters}
    />
  ));
}
