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
// /api/ops-peak-rpm supplies measured minute peaks, hourly averages and the
// matching quota per account, model, Region and endpoint.
//
// Applied limits use the backend's shared model/version/routing-family resolver.

import { useMemo, useState } from 'react';
import {
  Container, Header, SpaceBetween, Box, ColumnLayout, Grid, BarChart, LineChart,
  SegmentedControl, StatusIndicator, Button, Tabs, Badge,
} from '@cloudscape-design/components';
import { useApi, fmt, fmtPct, accountName, useAccountNames } from '../api.js';
import { ChartLoading, SectionHeader, KpiCard, CHART_I18N } from '../components/Common.jsx';
import PaginatedTable from '../components/PaginatedTable.jsx';
import QuotaDrillDown from './QuotaDrillDownTab.jsx';
import EndpointSubTabs from '../components/EndpointSubTabs.jsx';
import { summarizeQuotaRows, quotaUtilization } from '../quotaSummary.js';

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

// KPI value: "≥" marks a lower bound; "Unknown" only when no row has a limit.
function fmtUtil(value, lowerBound) {
  if (value == null) return 'Unknown';
  return (lowerBound ? '≥ ' : '') + fmtPct(value);
}

export default function QuotasTab({ filters, onInfo }) {
  // bedrock-mantle quotas are not in AWS Service Quotas (managed internally),
  // so Mantle gets coverage='defaults'. The tab's utilization view needs
  // actual Mantle peak-TPM data to be meaningful, so only show the Mantle
  // sub-tab when such volumetric data exists (else hide it — no blank view).
  const distinct = useApi('/distinct-filters', {}, []).data || {};
  const mantleAvailable = !!distinct.mantle_available?.volumetric;
  const [endpoint, setEndpoint] = useState(
    filters.endpoint && filters.endpoint !== 'all' ? filters.endpoint : 'runtime');
  const filtersWithEp = useMemo(() => ({ ...filters, endpoint }), [filters, endpoint]);
  return (
    <EndpointSubTabs
      selected={endpoint === 'all' ? 'runtime' : endpoint}
      onChange={setEndpoint}
      runtimeCoverage="full"
      mantleCoverage="defaults"
      mantleAvailable={mantleAvailable}
    >
      {({ endpoint: activeEndpoint }) => (
        <QuotasBody filters={{ ...filtersWithEp, endpoint: activeEndpoint }} onInfo={onInfo} />
      )}
    </EndpointSubTabs>
  );
}

function QuotasBody({ filters, onInfo }) {
  useAccountNames();   // resolve account names for the Account name cells
  const [scope, setScope] = useState('per-account');

  const peak = useApi('/ops-peak-rpm', { ...filters, include_quotas: true }, [JSON.stringify(filters)]);
  const throttle = useApi('/ops-throttle-rate', filters, [JSON.stringify(filters)]);
  const burndown = useApi('/ops-burndown-risk', filters, [JSON.stringify(filters)]);

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
      // Current responses already include hourly-average per-minute rates.
      // Divide only legacy hourly totals; dividing the new fields again is 60x low.
      const peakTpmMin = r.busiest_hour_avg_quota_tpm != null
        ? Number(r.busiest_hour_avg_quota_tpm) : tpmHour / 60;
      const peakRpmMin = r.busiest_hour_avg_rpm != null
        ? Number(r.busiest_hour_avg_rpm) : rpmHour / 60;
      const routingUnknown = !!r.has_application_profile;
      const tpmLimit = routingUnknown ? null : r.quota_tpm?.limit_per_minute;
      const rpmLimit = routingUnknown ? null : r.quota_rpm?.limit_per_minute;
      // MEASURED busiest minute, when the minute collector has data for this
      // row. AWS enforces per minute, so this is the number that predicts
      // throttling; the hourly average is retained beside it as a baseline and
      // is a lower bound. Never silently substitute one for the other.
      const minuteTpm = r.measured_minute_available && r.peak_minute_estimated_quota_tpm != null
        ? Number(r.peak_minute_estimated_quota_tpm) : null;
      const minuteRpm = r.measured_minute_available && r.peak_minute_rpm != null
        ? Number(r.peak_minute_rpm) : null;
      const coverageComplete = r.minute_coverage_complete === true;
      const tpm = quotaUtilization(minuteTpm, peakTpmMin, tpmLimit,
        r.minute_quota_complete ?? coverageComplete);
      const rpm = quotaUtilization(minuteRpm, peakRpmMin, rpmLimit,
        r.minute_rpm_complete ?? coverageComplete);
      out.push({
        group: routingUnknown ? 'Unknown' : trafficGroup(modelId),
        routing_unknown: routingUnknown,
        accountId, modelId, region, endpoint: r.endpoint,
        // Both bases kept explicitly so a reader can tell them apart.
        measured_minute: minuteTpm != null,
        minute_tpm:      minuteTpm,
        minute_rpm:      minuteRpm,
        hourly_avg_tpm:  peakTpmMin,
        hourly_avg_rpm:  peakRpmMin,
        burstiness_x:    r.quota_tpm_burstiness_x ?? null,
        quota_src:       r.peak_minute_quota_tpm_source || 'unavailable',
        minute_at:       r.peak_minute_quota_tpm_at || null,
        minute_days:     r.minute_days_with_data ?? 0,
        minute_coverage: r.minute_collection || null,
        minute_coverage_complete: coverageComplete,
        minute_coverage_status: r.minute_coverage_status || 'not_collected',
        tpm_limit:       tpmLimit ?? null,
        rpm_limit:       rpmLimit ?? null,
        // Utilization is EXACT only from a measured minute with complete
        // coverage. Otherwise the row's best observation — the larger of any
        // observed minute peak and its busiest-hour average, both lower bounds
        // on the true minute peak — divided by the row's OWN limit is a lower
        // bound, flagged so the UI prefixes it with "≥". Withholding it instead
        // blanked every percentage whenever the still-open current day was in
        // the window, which is always.
        tpm_util_pct: tpm.percentage,
        rpm_util_pct: rpm.percentage,
        tpm_util_lower_bound: tpm.lowerBound,
        rpm_util_lower_bound: rpm.lowerBound,
      });
    }
    return out.sort((a, b) =>
      (b.tpm_util_pct ?? 0) - (a.tpm_util_pct ?? 0)
      || (b.rpm_util_pct ?? 0) - (a.rpm_util_pct ?? 0));
  }, [peak.data]);

  // KPIs
  const kpis = useMemo(() => {
    const k = {
      max_tpm_util: null, max_rpm_util: null,
      over_80: 0, at_limit: 0, no_quota: 0,
    };
    for (const r of utilizationRows) {
      if (r.tpm_util_pct == null && r.rpm_util_pct == null) k.no_quota++;
      const top = Math.max(r.tpm_util_pct ?? 0, r.rpm_util_pct ?? 0);
      if (top > 100) k.at_limit++;
      else if (top > 80) k.over_80++;
      if (r.tpm_util_pct != null && (k.max_tpm_util == null || r.tpm_util_pct > k.max_tpm_util)) {
        k.max_tpm_util = r.tpm_util_pct; k.max_tpm_lb = r.tpm_util_lower_bound;
      }
      if (r.rpm_util_pct != null && (k.max_rpm_util == null || r.rpm_util_pct > k.max_rpm_util)) {
        k.max_rpm_util = r.rpm_util_pct; k.max_rpm_lb = r.rpm_util_lower_bound;
      }
    }
    return k;
  }, [utilizationRows]);

  // Aggregate by scope (account or model) for the table.
  const aggregated = useMemo(
    () => summarizeQuotaRows(utilizationRows, scope), [utilizationRows, scope]);
  const sourceLabel = r => scope === 'per-account'
    ? `${r.modelId} · ${r.region}` : r.accountId;
  const peakTpmCell = r => (
    <Box>
      {r.minute_tpm != null
        ? <>{fmt(r.minute_tpm)}{' '}
            <Badge color="grey">{r.quota_src === 'aws_estimate' ? 'AWS est.'
              : r.quota_src === 'mixed' ? 'mixed' : 'computed'}</Badge>
          </>
        : 'unavailable'}
      <Box fontSize="body-s" color="text-body-secondary">
        {sourceLabel(r.tpm_observation)}
      </Box>
    </Box>
  );
  const peakRpmCell = r => (
    <Box>
      {r.peak_rpm != null ? fmt(r.peak_rpm) : 'unavailable'}
      <Box fontSize="body-s" color="text-body-secondary">
        {sourceLabel(r.rpm_observation)}
      </Box>
    </Box>
  );

  // A percentage is shown whenever a limit and a measurement exist. A lower
  // bound is prefixed with "≥" rather than hidden.
  const utilCell = (value, lowerBound) => value == null ? '—' : (
    <StatusIndicator type={severityForUtil(value)}>
      {(lowerBound ? '≥ ' : '') + fmtPct(value)}
    </StatusIndicator>
  );
  // Application-profile traffic has no known quota family, so it has no limit.
  const limitCell = (limit, routingUnknown) => limit ? fmt(limit)
    : routingUnknown ? <Box color="text-status-inactive">routing unknown</Box> : '—';

  if (peak.loading) {
    return <ChartLoading height={320} label="Loading capacity + quota data..." />;
  }
  if (peak.error) {
    return <StatusIndicator type="error">Could not load quota utilization. Refresh to try again.</StatusIndicator>;
  }

  return (
    <SpaceBetween size="l">
      {/* KPI ribbon — fleet-wide quota health at a glance. Above the
           drill-down so the oncall sees the summary first, then drills. */}
      <Grid gridDefinition={[{ colspan: 3 }, { colspan: 3 }, { colspan: 3 }, { colspan: 3 }]}>
        <KpiCard title="Highest known TPM utilization" value={fmtUtil(kpis.max_tpm_util, kpis.max_tpm_lb)} />
        <KpiCard title="Highest known RPM utilization" value={fmtUtil(kpis.max_rpm_util, kpis.max_rpm_lb)} />
        <KpiCard title="At quota limit (>100%)"  value={fmt(kpis.at_limit)} />
        <KpiCard title="Approaching limit (80-100%)" value={fmt(kpis.over_80)} />
      </Grid>

      {/* Drill-down chart: per-(account · model · region) time series. */}
      <QuotaDrillDown endpoint={filters.endpoint} onInfo={onInfo} />

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
        <Box variant="p" color="text-body-secondary">
          Each metric shows the most utilized account, model and Region within the group,
          with its own limit. A percentage marked ≥ is a lower bound because coverage is incomplete.
        </Box>
        <PaginatedTable
          items={aggregated}
          pageSize={15}
          trackBy="key"
          downloadFileName="bedrock-quota-utilization.csv"
          columnDefinitions={
            scope === 'per-account'
              ? [
                  { id: 'a', header: 'Account ID', cell: r => r.accountId, exportValue: r => r.accountId },
                  { id: 'an', header: 'Account name', cell: r => accountName(r.accountId) || '—', exportValue: r => accountName(r.accountId) },
                  { id: 'ptpm',  header: 'Peak est. quota TPM (1 min)',
                    cell: peakTpmCell,
                    exportValue: r => r.measured_minute ? Math.round(r.minute_tpm) : '' },
                  { id: 'coverage', header: 'Minute coverage',
                    cell: r => r.incomplete ? 'Partial / unavailable' : 'Complete to collection time' },
                  { id: 'havg',  header: 'Hourly avg TPM/min',
                    cell: r => fmt(Math.round(r.hourly_avg_tpm)) },
                  { id: 'burst', header: 'Burstiness',
                    cell: r => r.burstiness_x ? `${r.burstiness_x}x` : '—' },
                  { id: 'tlim',  header: 'TPM limit',        cell: r => limitCell(r.tpm_lim, r.routing_unknown) },
                  { id: 'tutil', header: 'TPM util %',       cell: r => utilCell(r.tpm_util, r.tpm_util_lower_bound) },
                  { id: 'prpm',  header: 'Peak RPM (1 min)', cell: peakRpmCell,
                    exportValue: r => r.peak_rpm ?? '' },
                  { id: 'rlim',  header: 'RPM limit',        cell: r => limitCell(r.rpm_lim, r.routing_unknown) },
                  { id: 'rutil', header: 'RPM util %',       cell: r => utilCell(r.rpm_util, r.rpm_util_lower_bound) },
                ]
              : [
                  { id: 'm',     header: 'Model',            cell: r => r.modelId },
                  { id: 'r',     header: 'Region',           cell: r => r.region },
                  { id: 'ptpm',  header: 'Peak est. quota TPM (1 min)',
                    cell: peakTpmCell,
                    exportValue: r => r.measured_minute ? Math.round(r.minute_tpm) : '' },
                  { id: 'coverage', header: 'Minute coverage',
                    cell: r => r.incomplete ? 'Partial / unavailable' : 'Complete to collection time' },
                  { id: 'havg',  header: 'Hourly avg TPM/min',
                    cell: r => fmt(Math.round(r.hourly_avg_tpm)) },
                  { id: 'burst', header: 'Burstiness',
                    cell: r => r.burstiness_x ? `${r.burstiness_x}x` : '—' },
                  { id: 'tlim',  header: 'TPM limit',        cell: r => limitCell(r.tpm_lim, r.routing_unknown) },
                  { id: 'tutil', header: 'TPM util %',       cell: r => utilCell(r.tpm_util, r.tpm_util_lower_bound) },
                  { id: 'prpm',  header: 'Peak RPM (1 min)', cell: peakRpmCell,
                    exportValue: r => r.peak_rpm ?? '' },
                  { id: 'rlim',  header: 'RPM limit',        cell: r => limitCell(r.rpm_lim, r.routing_unknown) },
                  { id: 'rutil', header: 'RPM util %',       cell: r => utilCell(r.rpm_util, r.rpm_util_lower_bound) },
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
              { id: 'a', header: 'Account ID', cell: r => r.accountid || r.accountId, exportValue: r => r.accountid || r.accountId },
              { id: 'an', header: 'Account name', cell: r => accountName(r.accountid || r.accountId) || '—', exportValue: r => accountName(r.accountid || r.accountId) },
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
              { id: 'a', header: 'Account ID', cell: r => r.accountid || r.accountId, exportValue: r => r.accountid || r.accountId },
              { id: 'an', header: 'Account name', cell: r => accountName(r.accountid || r.accountId) || '—', exportValue: r => accountName(r.accountid || r.accountId) },
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
