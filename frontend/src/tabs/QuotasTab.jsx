// Quotas tab — primary view for "is anything going to break this week".
//
// Two main sections:
//   1. Per-traffic-type panel set: each traffic type (On-Demand, CRIS,
//      Global CRIS) gets a header card with Peak/Avg/Util numbers and a
//      twin time-series chart (TPM line + RPM line) with the applied
//      quota plotted as a dashed reference line.
//
//   2. Per-Account / Per-Model utilization table with severity-coded Avg
//      TPM % (>100% red, >80% amber, ≤80% green), CSV export.
//
// Data sources:
//   /api/ops-peak-rpm     — peak hourly counters per (account, model, region)
//   /api/quotas           — applied quota per (account, model, region, metric)
//                            (added below — uses f_quotas)
//
// The applied-quota join is done client-side for now; the JOIN is small
// enough that pushing it server-side doesn't change perceived latency.

import { useMemo, useState } from 'react';
import {
  Container, Header, SpaceBetween, Box, ColumnLayout, Grid, BarChart, LineChart,
  SegmentedControl, StatusIndicator, Button, Tabs,
} from '@cloudscape-design/components';
import { useApi, fmt, fmtPct } from '../api.js';
import { ChartLoading, SectionHeader, KpiCard, CHART_I18N } from '../components/Common.jsx';
import PaginatedTable from '../components/PaginatedTable.jsx';
import QuotaDrillDown from './QuotaDrillDownTab.jsx';
import EndpointSubTabs from '../components/EndpointSubTabs.jsx';

// Percentile selector removed for now — the underlying f_hourly_peak table
// only stores max-over-hour values from CloudWatch, so there is no p50/p90/p99
// data to switch between. When percentile-aware ingestion lands (sourcing
// from invocation logs at minute resolution), restore the toggle and wire
// it through `peak.data` to swap series.

const SCOPE_OPTIONS = [
  { id: 'per-account', label: 'Per Account' },
  { id: 'per-model',   label: 'Per Model' },
];

// Map traffic_type strings to our quota-side label: every CRIS row in
// f_daily lives in either "Cross-region" or "Global cross-region" quota
// names, and every on-demand row maps to "On-demand".
function trafficGroup(modelId) {
  if ((modelId || '').startsWith('global.')) return 'Global CRIS';
  if (/^(us|eu|apac|jp|au|ca|amer)\./.test(modelId || '')) return 'CRIS';
  return 'On-Demand';
}

function severityForUtil(pct) {
  return pct >= 100 ? 'error' : pct >= 80 ? 'warning' : pct > 0 ? 'success' : 'info';
}

export default function QuotasTab({ filters, onInfo }) {
  // bedrock-mantle quotas are not in AWS Service Quotas (managed internally),
  // so Mantle gets coverage='defaults'. The tab's utilization view needs
  // actual Mantle peak-TPM data to be meaningful, so only show the Mantle
  // sub-tab when such volumetric data exists (else hide it — no blank view).
  const distinct = useApi('/distinct-filters', {}, []).data || {};
  const mantleAvailable = !!distinct.mantle_available?.volumetric;
  const [endpoint, setEndpoint] = useState(filters.endpoint || 'all');
  const filtersWithEp = useMemo(() => ({ ...filters, endpoint }), [filters, endpoint]);
  return (
    <EndpointSubTabs
      selected={endpoint === 'all' ? 'runtime' : endpoint}
      onChange={setEndpoint}
      runtimeCoverage="full"
      mantleCoverage="defaults"
      mantleAvailable={mantleAvailable}
    >
      {() => <QuotasBody filters={filtersWithEp} onInfo={onInfo} />}
    </EndpointSubTabs>
  );
}

function QuotasBody({ filters, onInfo }) {
  const [scope, setScope] = useState('per-account');

  const peak = useApi('/ops-peak-rpm', filters, [JSON.stringify(filters)]);
  const throttle = useApi('/ops-throttle-rate', filters, [JSON.stringify(filters)]);
  const burndown = useApi('/ops-burndown-risk', filters, [JSON.stringify(filters)]);

  // f_quotas via /api/quotas — endpoint is added in extras.py if not present.
  const quotas = useApi('/quotas', filters, [JSON.stringify(filters)]);

  // Build a (account, modelId, region) → applied-quota map keyed by metric.
  // Quota rows come keyed by model_name (e.g. "Anthropic Claude Opus 4.7"),
  // but our peak rows are keyed by modelId (e.g. "us.anthropic.claude-opus-4-7").
  // Matching uses substring against the model_name's lowercase tokens.
  const quotaIndex = useMemo(() => {
    if (!quotas.data) return new Map();
    const map = new Map();
    for (const q of quotas.data) {
      const key = `${q.accountId}|${q.region}|${q.metric}`;
      if (!map.has(key)) map.set(key, []);
      map.get(key).push(q);
    }
    return map;
  }, [quotas.data]);

  function findQuota(accountId, modelId, region, metric) {
    const key = `${accountId}|${region}|${metric}`;
    const candidates = quotaIndex.get(key) || [];
    if (!candidates.length) return null;
    // Pick the candidate whose model_name appears in the modelId (case-
    // insensitive substring of any non-stop word). Fall back to first match.
    const m = (modelId || '').toLowerCase();
    let best = null;
    for (const q of candidates) {
      const name = (q.model_name || '').toLowerCase();
      if (!name) continue;
      // simple heuristic: if every space-separated word > 2 chars in the
      // quota model_name appears in modelId, count as a match.
      const words = name.split(/\s+/).filter(w => w.length > 2 && !['the','for','and'].includes(w));
      if (words.length && words.every(w => m.includes(w.replace(/\./g, '').toLowerCase()))) {
        if (!best || (q.applied_value || 0) > (best.applied_value || 0)) best = q;
      }
    }
    return best || candidates[0];
  }

  // Aggregate peak data per (group, accountId, modelId, region), join with quotas.
  const utilizationRows = useMemo(() => {
    if (!peak.data) return [];
    const out = [];
    for (const r of peak.data) {
      const accountId = r.accountid || r.accountId;
      const modelId = r.modelid || r.modelId;
      const region = r.region;
      // Quota-accurate peak TPM: the backend already applied the per-model
      // output-token burndown multiplier per-hour before taking the max
      // (peak_quota_tpm). Fall back to the raw 1:1 sum only for older API
      // responses that predate the field.
      const tpmHour = r.peak_quota_tpm != null
        ? Number(r.peak_quota_tpm)
        : Number(r.peak_input_tpm || 0) + Number(r.peak_output_tpm || 0);
      const rpmHour = Number(r.peak_requests_hour || 0);
      const peakTpmMin = tpmHour / 60;
      const peakRpmMin = rpmHour / 60;
      const tpmQ = findQuota(accountId, modelId, region, 'TPM');
      const rpmQ = findQuota(accountId, modelId, region, 'RPM');
      out.push({
        group: trafficGroup(modelId),
        accountId, modelId, region,
        peak_tpm_min:    peakTpmMin,
        peak_rpm_min:    peakRpmMin,
        tpm_limit:       tpmQ?.applied_value ?? null,
        rpm_limit:       rpmQ?.applied_value ?? null,
        tpm_util_pct:    tpmQ?.applied_value ? (peakTpmMin / Number(tpmQ.applied_value)) * 100 : null,
        rpm_util_pct:    rpmQ?.applied_value ? (peakRpmMin / Number(rpmQ.applied_value)) * 100 : null,
      });
    }
    return out.sort((a, b) =>
      (b.tpm_util_pct ?? 0) - (a.tpm_util_pct ?? 0)
      || (b.rpm_util_pct ?? 0) - (a.rpm_util_pct ?? 0));
  }, [peak.data, quotaIndex]);

  // KPIs
  const kpis = useMemo(() => {
    const k = {
      max_tpm_util: 0, max_rpm_util: 0,
      over_80: 0, at_limit: 0, no_quota: 0,
    };
    for (const r of utilizationRows) {
      if (r.tpm_util_pct == null && r.rpm_util_pct == null) k.no_quota++;
      const top = Math.max(r.tpm_util_pct ?? 0, r.rpm_util_pct ?? 0);
      if (top > 100) k.at_limit++;
      else if (top > 80) k.over_80++;
      if ((r.tpm_util_pct ?? 0) > k.max_tpm_util) k.max_tpm_util = r.tpm_util_pct ?? 0;
      if ((r.rpm_util_pct ?? 0) > k.max_rpm_util) k.max_rpm_util = r.rpm_util_pct ?? 0;
    }
    return k;
  }, [utilizationRows]);

  // Aggregate by scope (account or model) for the table.
  const aggregated = useMemo(() => {
    if (scope === 'per-account') {
      const m = new Map();
      for (const r of utilizationRows) {
        const k = r.accountId;
        const x = m.get(k) || { key: k, accountId: r.accountId, peak_tpm: 0, peak_rpm: 0, tpm_lim: 0, rpm_lim: 0 };
        x.peak_tpm = Math.max(x.peak_tpm, r.peak_tpm_min);
        x.peak_rpm = Math.max(x.peak_rpm, r.peak_rpm_min);
        x.tpm_lim  = Math.max(x.tpm_lim,  r.tpm_limit || 0);
        x.rpm_lim  = Math.max(x.rpm_lim,  r.rpm_limit || 0);
        m.set(k, x);
      }
      return [...m.values()].map(r => ({
        ...r,
        tpm_util: r.tpm_lim ? (r.peak_tpm / r.tpm_lim) * 100 : null,
        rpm_util: r.rpm_lim ? (r.peak_rpm / r.rpm_lim) * 100 : null,
      })).sort((a, b) => (b.tpm_util ?? 0) - (a.tpm_util ?? 0));
    } else {
      const m = new Map();
      for (const r of utilizationRows) {
        const k = `${r.modelId}|${r.region}`;
        const x = m.get(k) || { key: k, modelId: r.modelId, region: r.region, peak_tpm: 0, peak_rpm: 0, tpm_lim: 0, rpm_lim: 0 };
        x.peak_tpm = Math.max(x.peak_tpm, r.peak_tpm_min);
        x.peak_rpm = Math.max(x.peak_rpm, r.peak_rpm_min);
        x.tpm_lim  = Math.max(x.tpm_lim,  r.tpm_limit || 0);
        x.rpm_lim  = Math.max(x.rpm_lim,  r.rpm_limit || 0);
        m.set(k, x);
      }
      return [...m.values()].map(r => ({
        ...r,
        tpm_util: r.tpm_lim ? (r.peak_tpm / r.tpm_lim) * 100 : null,
        rpm_util: r.rpm_lim ? (r.peak_rpm / r.rpm_lim) * 100 : null,
      })).sort((a, b) => (b.tpm_util ?? 0) - (a.tpm_util ?? 0));
    }
  }, [utilizationRows, scope]);

  if (peak.loading || quotas.loading) {
    return <ChartLoading height={320} label="Loading capacity + quota data..." />;
  }

  return (
    <SpaceBetween size="l">
      {/* KPI ribbon — fleet-wide quota health at a glance. Above the
           drill-down so the oncall sees the summary first, then drills. */}
      <Grid gridDefinition={[{ colspan: 3 }, { colspan: 3 }, { colspan: 3 }, { colspan: 3 }]}>
        <KpiCard title="Peak TPM utilization" value={fmtPct(kpis.max_tpm_util)} />
        <KpiCard title="Peak RPM utilization" value={fmtPct(kpis.max_rpm_util)} />
        <KpiCard title="At quota limit (>100%)"  value={fmt(kpis.at_limit)} />
        <KpiCard title="Approaching limit (80-100%)" value={fmt(kpis.over_80)} />
      </Grid>

      {/* Drill-down chart: per-(account · model · region) time series. */}
      <QuotaDrillDown onInfo={onInfo} />

      {/* Scope + percentile toggle */}
      <Container header={
        <SectionHeader
          title="Capacity utilization"
          sectionId="ops-capacity-health"
          onInfo={onInfo}
          actions={
            <SegmentedControl
              selectedId={scope}
              onChange={({ detail }) => setScope(detail.selectedId)}
              options={SCOPE_OPTIONS.map(o => ({ id: o.id, text: o.label }))}
            />
          }
        />
      }>
        <PaginatedTable
          items={aggregated}
          pageSize={15}
          trackBy="key"
          downloadFileName="bedrock-quota-utilization.csv"
          columnDefinitions={
            scope === 'per-account'
              ? [
                  { id: 'a',     header: 'Account',          cell: r => r.accountId },
                  { id: 'ptpm',  header: 'Peak TPM/min',     cell: r => fmt(Math.round(r.peak_tpm)) },
                  { id: 'tlim',  header: 'TPM limit',        cell: r => r.tpm_lim ? fmt(r.tpm_lim) : '—' },
                  { id: 'tutil', header: 'TPM util %',       cell: r => r.tpm_util != null
                                                                       ? <StatusIndicator type={severityForUtil(r.tpm_util)}>{fmtPct(r.tpm_util)}</StatusIndicator>
                                                                       : '—' },
                  { id: 'prpm',  header: 'Peak RPM/min',     cell: r => fmt(Math.round(r.peak_rpm)) },
                  { id: 'rlim',  header: 'RPM limit',        cell: r => r.rpm_lim ? fmt(r.rpm_lim) : '—' },
                  { id: 'rutil', header: 'RPM util %',       cell: r => r.rpm_util != null
                                                                       ? <StatusIndicator type={severityForUtil(r.rpm_util)}>{fmtPct(r.rpm_util)}</StatusIndicator>
                                                                       : '—' },
                ]
              : [
                  { id: 'm',     header: 'Model',            cell: r => r.modelId },
                  { id: 'r',     header: 'Region',           cell: r => r.region },
                  { id: 'ptpm',  header: 'Peak TPM/min',     cell: r => fmt(Math.round(r.peak_tpm)) },
                  { id: 'tlim',  header: 'TPM limit',        cell: r => r.tpm_lim ? fmt(r.tpm_lim) : '—' },
                  { id: 'tutil', header: 'TPM util %',       cell: r => r.tpm_util != null
                                                                       ? <StatusIndicator type={severityForUtil(r.tpm_util)}>{fmtPct(r.tpm_util)}</StatusIndicator>
                                                                       : '—' },
                  { id: 'prpm',  header: 'Peak RPM/min',     cell: r => fmt(Math.round(r.peak_rpm)) },
                  { id: 'rlim',  header: 'RPM limit',        cell: r => r.rpm_lim ? fmt(r.rpm_lim) : '—' },
                  { id: 'rutil', header: 'RPM util %',       cell: r => r.rpm_util != null
                                                                       ? <StatusIndicator type={severityForUtil(r.rpm_util)}>{fmtPct(r.rpm_util)}</StatusIndicator>
                                                                       : '—' },
                ]
          }
          empty="No utilization data yet — run the ingester to populate f_hourly_peak + f_quotas."
        />
      </Container>

      {/* Throttle hotspots — moved from Engagement Signals */}
      <Container header={<SectionHeader title="Throttle hotspots" sectionId="throttle-rate-account" onInfo={onInfo} />}>
        {throttle.loading ? <ChartLoading /> :
          <PaginatedTable
            items={throttle.data || []}
            columnDefinitions={[
              { id: 'a', header: 'Account', cell: r => r.accountid || r.accountId },
              { id: 'm', header: 'Model',   cell: r => r.modelid || r.modelId },
              { id: 'r', header: 'Region',  cell: r => r.region },
              { id: 'rate', header: 'Throttle %', cell: r => <StatusIndicator type={(Number(r.throttle_pct) > 5) ? 'error' : (Number(r.throttle_pct) > 1) ? 'warning' : 'success'}>{fmtPct(r.throttle_pct, 3)}</StatusIndicator> },
              { id: 'thr', header: 'Throttled', cell: r => fmt(r.throttled) },
              { id: 't', header: 'Total',  cell: r => fmt(r.total_requests) },
            ]}
            empty="No throttling — clean fleet."
          />
        }
      </Container>

      {/* Burndown — moved from Engagement Signals */}
      {(burndown.data || []).length > 0 && (
        <Container header={<SectionHeader title="Claude burndown risk" sectionId="burndown" onInfo={onInfo} />}>
          <PaginatedTable
            items={burndown.data || []}
            columnDefinitions={[
              { id: 'a', header: 'Account', cell: r => r.accountid || r.accountId },
              { id: 'm', header: 'Model',   cell: r => r.modelid || r.modelId },
              { id: 'r', header: 'Region',  cell: r => r.region },
              { id: 'p', header: 'Peak TPM (quota)',  cell: r => fmt(r.peak_tpm) },
              { id: 'b', header: 'Burndown', cell: r => r.burndown_rate != null ? `${r.burndown_rate}×` : '—' },
              { id: 'q', header: 'Applied TPM',       cell: r => fmt(r.effective_tpm) },
              { id: 'o', header: 'Quota util %',      cell: r => <Box color="text-status-error" fontWeight="bold">{fmtPct(r.overhead_pct)}</Box> },
            ]}
            empty="No burndown risk."
          />
        </Container>
      )}
    </SpaceBetween>
  );
}
