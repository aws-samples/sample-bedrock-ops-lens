// Quota drill-down tab. Per-(account, model, region) TPM/RPM time series
// joined to the applied Service Quotas limit, plus headline KPIs. Renders
// the same diagnostic shape an internal Bedrock CRIS dashboard does (peak
// vs limit over time), so an oncall can see at a glance whether throttling
// is a quota problem or a usage problem.
//
// Source: GET /api/quota-drilldown — hourly buckets normalised to per-minute
// rates by the backend. Hourly granularity is the finest CW resolution we
// keep; the chart shape is faithful to the original.

import { useEffect, useMemo, useState } from 'react';
import {
  SpaceBetween, Container, Header, Box,
  Select, LineChart, StatusIndicator, Spinner, Link,
} from '@cloudscape-design/components';
import { api, useApi, fmt, fmtPct } from '../api.js';
import { ChartLoading, SectionHeader, CHART_I18N } from '../components/Common.jsx';

// -- Helpers ---------------------------------------------------------------

function fmtAt(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  // Shape matches the screenshot: "May 26 20:40"
  const month = d.toLocaleString(undefined, { month: 'short' });
  const day = d.getDate();
  const hh = String(d.getHours()).padStart(2, '0');
  const mm = String(d.getMinutes()).padStart(2, '0');
  return `${month} ${day} ${hh}:${mm}`;
}

function utilSeverity(pct) {
  if (pct === null || pct === undefined) return 'info';
  if (pct >= 100) return 'error';
  if (pct >= 70)  return 'warning';
  return 'success';
}

function KpiStrip({ limit, isDerived, peak, peakAt, avg, util, fmtVal }) {
  return (
    <Box color="text-body-secondary" fontSize="body-s">
      <SpaceBetween direction="horizontal" size="m">
        <span>
          <b>{isDerived ? 'Effective ceiling:' : 'Limit:'}</b>{' '}
          {limit !== null && limit !== undefined
            ? <>
                {fmtVal(limit)}
                {isDerived && <span style={{ color: '#aaa' }}> (derived from TPM ÷ avg tokens/req)</span>}
              </>
            : <span style={{ color: '#aaa' }}>not published by AWS</span>}
        </span>
        <span>·</span>
        <span><b>Peak:</b> {fmtVal(peak)} <span style={{ color: '#aaa' }}>@ {fmtAt(peakAt)}</span></span>
        <span>·</span>
        <span><b>Avg:</b> {fmtVal(avg)}</span>
        <span>·</span>
        <span><b>Util:</b>{' '}
          <StatusIndicator type={utilSeverity(util)}>
            {util === null || util === undefined ? '—' : fmtPct(util, 1)}
          </StatusIndicator>
        </span>
      </SpaceBetween>
    </Box>
  );
}

// One half of the row — KPI strip on top, time-series LineChart below with
// the quota line as a dashed `thresholds` annotation.
function MetricCard({
  title,
  series,
  limit,
  limitDerived,
  peak, peakAt, avg, util,
  fmtVal,
  ariaLabel,
  loading,
  sectionId,
  onInfo,
}) {
  // Effective limit = published if available, else derived (TPM÷avg-tokens)
  // for cards where AWS doesn't publish one. Derived ceiling is labelled
  // explicitly so users know it's a calculation, not a real quota.
  const effectiveLimit = limit ?? limitDerived ?? null;
  const isDerived = limit == null && limitDerived != null;
  // Find peak so we can choose a sensible y-axis range.
  const peakValue = useMemo(() => {
    let mx = 0;
    for (const p of series) if (p.y > mx) mx = p.y;
    return mx;
  }, [series]);

  // When peak is dwarfed by the (effective) limit (>100x ratio), a linear
  // y-axis either crushes the data flat (axis anchored to limit) or hides
  // the limit (axis anchored to data). Switch to log scale so both are
  // visible on the same chart.
  const useLogScale =
    effectiveLimit !== null && peakValue > 0 && effectiveLimit / peakValue > 100;

  // For log scale we need a strictly positive floor; substitute zero
  // datapoints with a small value so the line keeps drawing through
  // idle hours instead of breaking.
  const yFloor = useLogScale ? Math.max(peakValue * 0.001, 0.1) : 0;
  const safeSeries = useMemo(() => {
    if (!useLogScale) return series;
    return series.map(p => ({ x: p.x, y: p.y > 0 ? p.y : yFloor }));
  }, [series, useLogScale, yFloor]);

  const chartSeries = useMemo(() => {
    const out = [];
    out.push({
      title: title.includes('Tokens') ? 'Peak TPM' : 'Peak RPM',
      type: 'line',
      data: safeSeries,
      valueFormatter: fmtVal,
    });
    // Render the limit line as a flat 2-point line. Solid red for a
    // published AWS quota; same red but labelled "ceiling" when this is
    // the derived TPM÷avg-tokens fallback.
    if (effectiveLimit !== null && safeSeries.length > 0) {
      const xMin = safeSeries[0].x;
      const xMax = safeSeries[safeSeries.length - 1].x;
      out.push({
        title: isDerived
          ? `Effective ceiling (${fmtVal(effectiveLimit)})`
          : `Limit (${fmtVal(effectiveLimit)})`,
        type: 'line',
        color: '#d13212',
        data: [{ x: xMin, y: effectiveLimit }, { x: xMax, y: effectiveLimit }],
        valueFormatter: fmtVal,
      });
    }
    return out;
  }, [safeSeries, effectiveLimit, isDerived, title, fmtVal]);

  const yDomain = useMemo(() => {
    if (useLogScale) {
      // Log-scale: axis spans the floor up to slightly above the limit.
      const top = (effectiveLimit || peakValue) * 1.1;
      return [yFloor, top];
    }
    // Linear: stretch slightly above whichever is taller so the line
    // isn't pinned to the top edge.
    const top = Math.max(peakValue, effectiveLimit || 0);
    return [0, top > 0 ? top * 1.1 : 1];
  }, [peakValue, effectiveLimit, useLogScale, yFloor]);

  // Header with optional Info link, mirroring SectionHeader's layout.
  const headerActions = sectionId && onInfo
    ? <Link variant="info" onFollow={(e) => { e?.preventDefault?.(); onInfo(sectionId); }}>Info</Link>
    : undefined;

  return (
    <Container fitHeight header={<Header variant="h3" actions={headerActions}>{title}</Header>}>
      <SpaceBetween size="s">
        <KpiStrip
          limit={effectiveLimit} isDerived={isDerived}
          peak={peak} peakAt={peakAt} avg={avg} util={util}
          fmtVal={fmtVal}
        />
        {useLogScale && (
          <Box color="text-body-secondary" fontSize="body-s">
            Y-axis is logarithmic so both peak usage and the much higher
            limit fit on the same chart.
          </Box>
        )}
        {loading
          ? <ChartLoading height={260} />
          : series.length === 0
            ? <Box textAlign="center" color="text-body-secondary" padding="l">No data in window.</Box>
            : <LineChart
                series={chartSeries}
                xScaleType="time"
                yScaleType={useLogScale ? 'log' : 'linear'}
                yDomain={yDomain}
                hideFilter
                ariaLabel={ariaLabel}
                height={260}
                i18nStrings={{
                  ...CHART_I18N,
                  yTickFormatter: fmtVal,
                  xTickFormatter: d => {
                    const dt = new Date(d);
                    return `${dt.toLocaleString(undefined, { month: 'short', day: 'numeric' })} ${String(dt.getHours()).padStart(2,'0')}:00`;
                  },
                }}
              />
        }
      </SpaceBetween>
    </Container>
  );
}

// -- Tab -------------------------------------------------------------------

export default function QuotaDrillDownTab({ onInfo }) {
  const opts = useApi('/quota-drilldown/options', { days: 14 }, []);
  const optionList = useMemo(() => {
    const arr = (opts.data?.options || []).map(o => ({
      label: o.label,
      value: `${o.accountId}|${o.modelId}|${o.region}`,
      description: `${fmt(o.total_requests)} requests in last 14d`,
      _raw: o,
    }));
    return arr;
  }, [opts.data]);

  const [selected, setSelected] = useState(null);

  // Auto-pick the busiest combo on first load — most useful default for
  // an oncall who opens the tab cold during a paging incident.
  const effective = selected || optionList[0] || null;

  const account_id = effective?._raw?.accountId;
  const model_id   = effective?._raw?.modelId;
  const region     = effective?._raw?.region;

  // useApi() doesn't accept a null-params sentinel — it would Object.entries
  // through it and throw. Manage the conditional fetch manually so the
  // request only fires once a combination is picked.
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  useEffect(() => {
    if (!effective) {
      setData(null); setLoading(false); setError(null);
      return;
    }
    let cancelled = false;
    setLoading(true); setError(null);
    api('/quota-drilldown', { account_id, model_id, region, days: 14 })
      .then(d => { if (!cancelled) { setData(d); setLoading(false); } })
      .catch(e => { if (!cancelled) { setError(e); setLoading(false); } });
    return () => { cancelled = true; };
  }, [account_id, model_id, region, effective]);

  // LineChart needs [{ x: Date, y: number }, …]; series carries a few
  // metrics we slice client-side.
  const tpmSeries = useMemo(() => {
    if (!data?.series) return [];
    return data.series.map(p => ({ x: new Date(p.ts), y: p.tpm }));
  }, [data]);
  const rpmSeries = useMemo(() => {
    if (!data?.series) return [];
    return data.series.map(p => ({ x: new Date(p.ts), y: p.rpm }));
  }, [data]);

  const trafficType = data?.matched_quota_traffic_type;
  const k = data?.kpis || {};

  return (
    <SpaceBetween size="m">
      <Container header={
        <SectionHeader
          title="Quota drill-down"
          description="Per-(account · model · region) TPM and RPM versus the applied Service Quotas limit, hourly granularity over the last 14 days."
          sectionId="quota-drilldown"
          onInfo={onInfo}
        />
      }>
        <SpaceBetween size="s">
          {opts.loading ? <Spinner /> : (
            <Select
              selectedOption={effective}
              onChange={({ detail }) => setSelected(detail.selectedOption)}
              options={optionList}
              placeholder="Select an account · model · region"
              filteringType="auto"
              empty="No (account · model · region) combinations have data in the last 14 days."
            />
          )}
          {trafficType && (
            <Box color="text-body-secondary" fontSize="body-s">
              Matched quota family: <b>{trafficType}</b>
              {trafficType !== 'On-demand' && ' (CRIS)'}
            </Box>
          )}
        </SpaceBetween>
      </Container>

      {error && (
        <Box color="text-status-error">
          Failed to load quota series: {String(error.message || error)}
        </Box>
      )}

      {/* CRIS / On-demand group — TPM left, RPM right. Same fitHeight + grid
          stretch pattern we use everywhere else. */}
      <Container header={
        <Header
          variant="h2"
          description={effective
            ? `${effective._raw.accountId} · ${effective._raw.modelId} · ${effective._raw.region}`
            : 'Pick a combination above'}
        >
          {trafficType || 'Quota usage vs limit'}
        </Header>
      }>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 20, alignItems: 'stretch' }}>
          <MetricCard
            title={data?.burndown_rate > 1
              ? `Tokens per minute (TPM) — ${data.burndown_rate}× output burndown`
              : "Tokens per minute (TPM)"}
            ariaLabel="Tokens per minute"
            series={tpmSeries}
            limit={data?.tpm_limit ?? null}
            peak={k.peak_tpm} peakAt={k.peak_tpm_at}
            avg={k.avg_tpm} util={k.util_pct_tpm}
            fmtVal={fmt}
            loading={loading || !effective}
            sectionId="quota-drilldown-tpm"
            onInfo={onInfo}
          />
          <MetricCard
            title="Requests per minute (RPM)"
            ariaLabel="Requests per minute"
            series={rpmSeries}
            limit={data?.rpm_limit ?? null}
            limitDerived={data?.rpm_limit_derived ?? null}
            peak={k.peak_rpm} peakAt={k.peak_rpm_at}
            avg={k.avg_rpm} util={k.util_pct_rpm}
            fmtVal={fmt}
            loading={loading || !effective}
            sectionId="quota-drilldown-rpm"
            onInfo={onInfo}
          />
        </div>
      </Container>
    </SpaceBetween>
  );
}
