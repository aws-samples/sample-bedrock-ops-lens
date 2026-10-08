// Quota drill-down tab. Per-(account, model, region) TPM/RPM time series
// joined to the applied Service Quotas limit, plus headline KPIs. Plots peak
// against the limit over time, so an oncall can see at a glance whether
// throttling is a quota problem or a usage problem.
//
// Source: GET /api/quota-drilldown — hourly buckets normalised to per-minute
// rates by the backend. Hourly granularity is the finest resolution Lens
// stores. "Pull live data" (POST /api/live-pull) reads the last few hours at
// one-minute resolution straight from CloudWatch on demand; nothing is stored.

import { useEffect, useMemo, useRef, useState } from 'react';
import {
  SpaceBetween, Container, Header, Box, Button, SegmentedControl,
  Select, LineChart, StatusIndicator, Spinner, Link,
} from '@cloudscape-design/components';
import { api, apiSend, useApi, fmt, fmtPct } from '../api.js';
import { ChartLoading, SectionHeader, InfoLink, CHART_I18N } from '../components/Common.jsx';
import { fmtHourUTC, fmtMinuteUTC } from '../dates';
import { liveSeries, seriesAverage, utilPct, lineSegments, drilldownParams, liveQuotaRoutingUnknown } from '../livePull.js';

// -- Helpers ---------------------------------------------------------------

function fmtAt(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  // Shape matches the screenshot: "May 26 20:40". Read UTC parts: these
  // timestamps identify a UTC hour bucket, so rendering the browser's local
  // clock named a different hour than the one the number came from (finding 17).
  const month = d.toLocaleString(undefined, { month: 'short', timeZone: 'UTC' });
  const day = d.getUTCDate();
  const hh = String(d.getUTCHours()).padStart(2, '0');
  const mm = String(d.getUTCMinutes()).padStart(2, '0');
  return `${month} ${day} ${hh}:${mm} UTC`;
}

function utilSeverity(pct) {
  if (pct === null || pct === undefined) return 'info';
  if (pct >= 100) return 'error';
  if (pct >= 70)  return 'warning';
  return 'success';
}

function KpiStrip({ limit, isDerived, peak, peakAt, avg, util, fmtVal, routingUnknown, lowerBound, peakLabel, peakLowerBound }) {
  // A lower bound (some minutes could not be established) is prefixed with
  // "≥" rather than hidden, as on the Quotas table.
  const lb = lowerBound ? '≥ ' : '';
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
            : <span style={{ color: '#aaa' }}>{routingUnknown
                ? 'unknown — profile routing unavailable' : 'not published by AWS'}</span>}
        </span>
        <span>·</span>
        <span><b>{peakLabel}:</b> {peak == null ? '—' : (peakLowerBound ? '≥ ' : '') + fmtVal(peak)} <span style={{ color: '#aaa' }}>@ {fmtAt(peakAt)}</span></span>
        <span>·</span>
        <span><b>Avg:</b> {avg == null ? '—' : fmtVal(avg)}</span>
        <span>·</span>
        <span><b>Util:</b>{' '}
          <StatusIndicator type={utilSeverity(util)}>
            {util === null || util === undefined ? '—' : lb + fmtPct(util, 1)}
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
  routingUnknown,
  peak, peakAt, avg, util,
  peakLabel = 'Peak minute',
  lowerBound = false,
  peakLowerBound = lowerBound,
  fmtVal,
  ariaLabel,
  loading,
  sectionId,
  onInfo,
  seriesTitle,
  xTickFormatter = fmtHourUTC,
  emptyText = 'No data in window.',
  testId,
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
    return series.map(p => ({ x: p.x, y: p.y == null ? null : p.y > 0 ? p.y : yFloor }));
  }, [series, useLogScale, yFloor]);

  const chartSeries = useMemo(() => {
    const out = [];
    for (const segment of lineSegments(safeSeries)) out.push({
      title: seriesTitle || title,
      type: 'line',
      color: '#688ae8',
      data: segment,
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
  }, [safeSeries, effectiveLimit, isDerived, title, seriesTitle, fmtVal]);

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
    <Container fitHeight data-testid={testId}
      header={<Header variant="h3" actions={headerActions}>{title}</Header>}>
      <SpaceBetween size="s">
        <KpiStrip
          routingUnknown={routingUnknown}
          limit={effectiveLimit} isDerived={isDerived}
          peak={peak} peakAt={peakAt} avg={avg} util={util}
          peakLabel={peakLabel}
          peakLowerBound={peakLowerBound}
          lowerBound={lowerBound}
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
          : !series.some(p => Number.isFinite(p.y))
            ? <Box textAlign="center" color="text-body-secondary" padding="l">{emptyText}</Box>
            : <LineChart
                series={chartSeries}
                xScaleType="time"
                xDomain={[series[0].x, series[series.length - 1].x]}
                yScaleType={useLogScale ? 'log' : 'linear'}
                yDomain={yDomain}
                hideFilter
                hideLegend
                ariaLabel={ariaLabel}
                height={260}
                i18nStrings={{
                  ...CHART_I18N,
                  yTickFormatter: fmtVal,
                  xTickFormatter: d => xTickFormatter(d),
                }}
              />
        }
        {series.some(p => Number.isFinite(p.y)) && (
          <Box color="text-body-secondary" fontSize="body-s">
            <span style={{ color: '#688ae8' }}>━</span> {seriesTitle || title}
            {effectiveLimit != null && <> · <span style={{ color: '#d13212' }}>━</span>{' '}
              {isDerived ? 'Effective ceiling' : 'Applied limit'} ({fmtVal(effectiveLimit)})</>}
          </Box>
        )}
      </SpaceBetween>
    </Container>
  );
}

// -- Live per-minute pull --------------------------------------------------

const LIVE_HOURS = [1, 3, 6, 12, 24];

function LivePullPanel({ selection, quota, onInfo }) {
  const cfg = useApi('/live-pull', {}, []);
  const [hours, setHours] = useState('3');
  const [pull, setPull] = useState({ key: null, loading: false, result: null, error: null });

  const key = selection
    ? [selection.account_id, selection.model_id, selection.region, selection.endpoint].join('|')
    : null;
  // A result belongs to the selection it was pulled for. Switching the
  // selection hides it, and a response that lands after the switch is dropped.
  const latestKey = useRef(key);
  latestKey.current = key;
  const current = pull.key === key ? pull : { loading: false, result: null, error: null };

  const unavailable = !selection
    ? 'Pick an account · model · Region above.'
    : cfg.error
      ? 'Could not check whether live pull is available.'
      : cfg.data && !cfg.data.enabled
        ? 'Live pull is not configured in this deployment.'
        : selection.endpoint !== 'runtime'
          ? 'Live pull covers the bedrock-runtime endpoint only.'
          : null;

  const onPull = () => {
    const forKey = key;
    setPull({ key: forKey, loading: true, result: null, error: null });
    apiSend('/live-pull', { body: { ...selection, hours: Number(hours) } })
      .then(result => {
        // A success status without the live-pull JSON (for example an HTML
        // page from an intermediary) is an error, not an empty pull.
        if (!result || result.ok !== true || !Array.isArray(result.minutes)) {
          throw new Error('Unexpected response from the live-pull endpoint.');
        }
        if (latestKey.current === forKey) setPull({ key: forKey, loading: false, result, error: null });
      })
      .catch(e => {
        if (latestKey.current === forKey) {
          setPull({ key: forKey, loading: false, result: null, error: String(e.message || e) });
        }
      });
  };

  const r = current.result;
  const tpmSeries = useMemo(() => liveSeries(r, 'quota_tpm'), [r]);
  const rpmSeries = useMemo(() => liveSeries(r, 'requests'), [r]);

  let body;
  if (unavailable) {
    body = <Box color="text-body-secondary">{unavailable}</Box>;
  } else if (current.loading) {
    body = <ChartLoading height={260} label="Reading CloudWatch…" />;
  } else if (current.error) {
    body = <StatusIndicator type="error">{current.error}</StatusIndicator>;
  } else if (!r) {
    body = (
      <Box color="text-body-secondary">
        Press <b>Pull live data</b> to read the last {hours} {hours === '1' ? 'hour' : 'hours'} minute
        by minute from CloudWatch.
      </Box>
    );
  } else {
    const partial = r.status === 'partial';
    const unknown = r.unknown_minutes || {};
    const peak = r.peak || {};
    // Application-profile traffic has no known quota family, so a limit
    // resolved for the model alone does not apply to the combined series.
    const profileTraffic = liveQuotaRoutingUnknown(r);
    const tpmLimit = profileTraffic ? null : quota.tpmLimit;
    const rpmLimit = profileTraffic ? null : quota.rpmLimit;
    const rpmDerived = profileTraffic ? null : quota.rpmLimitDerived;
    const tpmLower = partial || unknown.quota_tpm > 0;
    const rpmLower = partial || unknown.requests > 0;
    const src = r.quota_sources || {};
    const sourceParts = [
      src.aws_estimate ? `AWS EstimatedTPMQuotaUsage for ${fmt(src.aws_estimate)} min` : null,
      src.reconstructed
        ? `reconstructed as input + cache write + output × ${r.burndown_rate} for ${fmt(src.reconstructed)} min`
        : null,
      src.mixed ? `both, across identifiers, for ${fmt(src.mixed)} min` : null,
    ].filter(Boolean);
    const hrs = r.window?.hours;
    body = (
      <SpaceBetween size="s">
        <StatusIndicator type={partial ? 'warning' : 'success'}>
          Pulled {fmtAt(r.pulled_at)} · last {hrs} {hrs === 1 ? 'hour' : 'hours'} ·{' '}
          {fmt(r.active_minutes)} {r.active_minutes === 1 ? 'minute' : 'minutes'} with data
          {r.cached ? ' · same result as a pull moments ago' : ''}
        </StatusIndicator>
        {partial && (
          <Box color="text-status-warning" fontSize="body-s">
            CloudWatch returned incomplete data for some series, so the peaks are lower bounds.
          </Box>
        )}
        {r.active_minutes === 0 ? (
          <Box color="text-body-secondary">
            {partial ? 'CloudWatch data could not be established for this window. Try again.'
              : 'No Bedrock requests reported for this model in this window.'}
          </Box>
        ) : (
          <>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 20, alignItems: 'stretch' }}>
              <MetricCard
                title={r.burndown_rate > 1
                  ? `Quota TPM per minute — ${r.burndown_rate}× output burndown`
                  : 'Quota TPM per minute'}
                seriesTitle="Quota TPM"
                ariaLabel="Quota tokens per minute, live"
                series={tpmSeries}
                limit={tpmLimit ?? null}
                routingUnknown={profileTraffic || !!quota.tpmRoutingUnknown}
                peak={peak.quota_tpm?.value} peakAt={peak.quota_tpm?.at}
                avg={seriesAverage(tpmSeries, !partial)} util={utilPct(peak.quota_tpm?.value, tpmLimit)}
                lowerBound={tpmLower}
                fmtVal={fmt}
                xTickFormatter={fmtMinuteUTC}
                emptyText="No quota TPM could be established in this window."
                testId="live-tpm"
              />
              <MetricCard
                title="Requests per minute"
                seriesTitle="RPM"
                ariaLabel="Requests per minute, live"
                series={rpmSeries}
                limit={rpmLimit ?? null}
                limitDerived={rpmDerived ?? null}
                routingUnknown={profileTraffic || !!quota.rpmRoutingUnknown}
                peak={peak.rpm?.value} peakAt={peak.rpm?.at}
                avg={seriesAverage(rpmSeries, !partial)} util={utilPct(peak.rpm?.value, rpmLimit ?? rpmDerived)}
                lowerBound={rpmLower}
                fmtVal={fmt}
                xTickFormatter={fmtMinuteUTC}
                emptyText="No request counts could be established in this window."
                testId="live-rpm"
              />
            </div>
            <Box color="text-body-secondary" fontSize="body-s">
              <SpaceBetween size="xxs">
                <span>
                  Busiest minute for input: {fmt(peak.input_tokens?.value)} tokens
                  {peak.input_tokens ? ` @ ${fmtAt(peak.input_tokens.at)}` : ''} · for
                  output: {fmt(peak.output_tokens?.value)} tokens
                  {peak.output_tokens ? ` @ ${fmtAt(peak.output_tokens.at)}` : ''}.
                  Input counts input plus cache-write tokens.
                </span>
                {sourceParts.length > 0 && <span>Quota TPM source: {sourceParts.join('; ')}.</span>}
                {r.has_application_profile && (
                  <span>
                    Combines direct calls with {r.identifiers.length - 1} application inference
                    profile {r.identifiers.length - 1 === 1 ? 'identifier' : 'identifiers'} that
                    resolve to this model ({(r.identifiers_with_data || []).length} with traffic).
                  </span>
                )}
                {(tpmLower || rpmLower) && !partial && (
                  <span>
                    Some active minutes reported only part of their metrics; a peak marked ≥ is a
                    lower bound.
                  </span>
                )}
                <span>
                  {partial
                    ? 'Missing minutes are gaps; no average is shown for an incomplete series.'
                    : 'Minutes with no reported activity are drawn as zero. Unknown active measurements are gaps.'}{' '}
                  CloudWatch can take a few minutes to publish recent data.
                </span>
              </SpaceBetween>
            </Box>
          </>
        )}
      </SpaceBetween>
    );
  }

  return (
    <Container data-testid="live-pull" header={
      <SectionHeader
        title="Minute-by-minute (live)"
        description="Per-minute quota TPM and RPM read on demand from CloudWatch for the selected account · model · Region. Nothing is stored."
        actions={
          <SpaceBetween direction="horizontal" size="xs" alignItems="center">
            {onInfo && <InfoLink sectionId="quota-drilldown-live" onInfo={onInfo} />}
            <SegmentedControl
              label="Window"
              selectedId={hours}
              onChange={({ detail }) => setHours(detail.selectedId)}
              options={LIVE_HOURS.map(h => ({ id: String(h), text: `${h}h` }))}
            />
            <Button
              onClick={onPull}
              loading={current.loading}
              disabled={!!unavailable || cfg.loading}
            >
              Pull live data
            </Button>
          </SpaceBetween>
        }
      />
    }>
      {body}
    </Container>
  );
}

// -- Tab -------------------------------------------------------------------

export default function QuotaDrillDownTab({ onInfo, endpoint: selectedEndpoint = 'all' }) {
  const opts = useApi('/quota-drilldown/options', { days: 14, endpoint: selectedEndpoint }, [selectedEndpoint]);
  const optionList = useMemo(() => {
    const arr = (opts.data?.options || [])
      .filter(o => selectedEndpoint === 'all' || o.endpoint === selectedEndpoint).map(o => ({
      label: o.label,
      value: `${o.accountId}|${o.modelId}|${o.region}|${o.endpoint}`,
      description: `${fmt(o.total_requests)} requests in last 14d`,
      _raw: o,
    }));
    return arr;
  }, [opts.data, selectedEndpoint]);

  const [selected, setSelected] = useState(null);

  // Auto-pick the busiest combo on first load — most useful default for
  // an oncall who opens the tab cold during a paging incident.
  const effective = optionList.find(o => o.value === selected?.value) || optionList[0] || null;

  const account_id = effective?._raw?.accountId;
  const model_id   = effective?._raw?.modelId;
  const region     = effective?._raw?.region;
  const endpoint   = effective?._raw?.endpoint || 'runtime';
  const liveSelection = useMemo(
    () => (account_id ? { account_id, model_id, region, endpoint } : null),
    [account_id, model_id, region, endpoint]);

  // useApi() doesn't accept a null-params sentinel — it would Object.entries
  // through it and throw. Manage the conditional fetch manually so the
  // request only fires once a combination is picked.
  const [response, setResponse] = useState(null);
  const dataKey = effective?.value;
  // Only the response for the current selection counts. Before any selection
  // exists both keys are undefined, which must not read as a match.
  const data = response && dataKey != null && response.key === dataKey ? response.data : null;
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  useEffect(() => {
    if (!effective) {
      setResponse(null); setLoading(false); setError(null);
      return;
    }
    let cancelled = false;
    setLoading(true); setError(null);
    api('/quota-drilldown', drilldownParams(effective._raw))
      .then(d => { if (!cancelled) { setResponse({ key: dataKey, data: d }); setLoading(false); } })
      .catch(e => { if (!cancelled) { setError(e); setLoading(false); } });
    return () => { cancelled = true; };
  }, [account_id, model_id, region, endpoint, dataKey, effective]);

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
          description="Hourly-average TPM and RPM versus the applied limit for the selected account · model · Region over the last 14 days. Use the live panel below for minute-by-minute measurements."
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
            seriesTitle="Hourly average TPM"
            series={tpmSeries}
            limit={data?.tpm_limit ?? null}
            routingUnknown={data?.quota_tpm?.quota_routing_unknown}
            peak={k.peak_tpm} peakAt={k.peak_tpm_at}
            peakLabel="Busiest-hour average"
            peakLowerBound={false}
            avg={k.avg_tpm} util={k.util_pct_tpm}
            fmtVal={fmt}
            loading={loading || !effective}
            lowerBound
            sectionId="quota-drilldown-tpm"
            onInfo={onInfo}
          />
          <MetricCard
            title="Requests per minute (RPM)"
            ariaLabel="Requests per minute"
            seriesTitle="Hourly average RPM"
            series={rpmSeries}
            limit={data?.rpm_limit ?? null}
            routingUnknown={data?.quota_rpm?.quota_routing_unknown}
            limitDerived={data?.rpm_limit_derived ?? null}
            peak={k.peak_rpm} peakAt={k.peak_rpm_at}
            peakLabel="Busiest-hour average"
            peakLowerBound={false}
            avg={k.avg_rpm} util={k.util_pct_rpm}
            fmtVal={fmt}
            loading={loading || !effective}
            lowerBound
            sectionId="quota-drilldown-rpm"
            onInfo={onInfo}
          />
        </div>
      </Container>

      <LivePullPanel
        selection={liveSelection}
        // Limits come from the hourly response for the same selection; while
        // that is reloading they are withheld rather than borrowed from the
        // previous selection.
        quota={loading || error ? {} : {
          tpmLimit: data?.tpm_limit ?? null,
          rpmLimit: data?.rpm_limit ?? null,
          rpmLimitDerived: data?.rpm_limit_derived ?? null,
          tpmRoutingUnknown: !!data?.quota_tpm?.quota_routing_unknown,
          rpmRoutingUnknown: !!data?.quota_rpm?.quota_routing_unknown,
        }}
        onInfo={onInfo}
      />
    </SpaceBetween>
  );
}
