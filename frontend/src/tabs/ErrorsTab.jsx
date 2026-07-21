// Errors tab — 3 containers, with bar→hourly drill.
import { useMemo, useState } from 'react';
import {
  Container, Header, SpaceBetween, BarChart, LineChart, Grid, Box,
  Button, Spinner, Alert, SegmentedControl, ColumnLayout, StatusIndicator,
} from '@cloudscape-design/components';
import { useApi, fmt, fmtPct, api as apiCall } from '../api.js';
import { ChartLoading, KpiCard, SectionHeader, InfoLink, CHART_I18N } from '../components/Common.jsx';
import PaginatedTable from '../components/PaginatedTable.jsx';
import EndpointSubTabs from '../components/EndpointSubTabs.jsx';

// Per-code palette + render order for the "Status Codes" stacked chart.
// 200 OK first (bottom of stack), then client codes, then server codes.
// Distinct, high-contrast hues so adjacent stacked segments never blur into
// each other (the previous orange/brown/red ramp was hard to tell apart).
// Hues chosen to stay visible on BOTH the light and dark Cloudscape themes.
// The old 500 (#111827 near-black) and 503 (#6b7280 dim gray) vanished against
// the dark theme's near-black panel background — a lone 500 bar looked like an
// empty chart. Server codes now use a bright crimson / warm-brown that read on
// either background.
const STATUS_SERIES = [
  { key: 'ok',   title: '200 OK', color: '#2e7d32' },  // green
  { key: 's400', title: '400',    color: '#f59e0b' },  // amber
  { key: 's403', title: '403',    color: '#8b5cf6' },  // violet
  { key: 's404', title: '404',    color: '#0ea5e9' },  // sky blue
  { key: 's408', title: '408',    color: '#ec4899' },  // pink
  { key: 's424', title: '424',    color: '#14b8a6' },  // teal
  { key: 's429', title: '429',    color: '#ef4444' },  // red
  { key: 's500', title: '500',    color: '#dc2626' },  // crimson (was near-black — invisible on dark)
  { key: 's503', title: '503',    color: '#d97706' },  // dark amber (was dim gray)
];

export default function ErrorsTab({ filters, onInfo }) {
  // bedrock-mantle CW publishes 4xx but not 5xx and folds 429 into the
  // 4xx total, so the Errors tab is partial for Mantle. Coverage =
  // 'metric' (live but partial); 5xx breakdown comes from invocation
  // logs only when the customer enabled them.
  const distinct = useApi('/distinct-filters', {}, []).data || {};
  const mantleAvailable = !!distinct.mantle_available?.volumetric;
  const [endpoint, setEndpoint] = useState(filters.endpoint || 'all');
  const filtersWithEp = useMemo(() => ({ ...filters, endpoint }), [filters, endpoint]);
  return (
    <EndpointSubTabs
      selected={endpoint === 'all' ? 'runtime' : endpoint}
      onChange={setEndpoint}
      runtimeCoverage="full"
      mantleCoverage="metric"
      mantleAvailable={mantleAvailable}
    >
      {() => <ErrorsBody filters={filtersWithEp} onInfo={onInfo} />}
    </EndpointSubTabs>
  );
}

function ErrorsBody({ filters, onInfo }) {
  // bedrock-mantle publishes a fundamentally different (and narrower) health
  // surface than runtime: request volume + a single aggregate 4xx client-error
  // count, with NO 5xx, no per-status-code split, and no invocation logs. The
  // runtime layout below (per-code stacked bars, 429/4xx/5xx trend, per-code
  // tables) therefore renders blank/zero for Mantle and reads as "broken".
  // Show a purpose-built health view built from the signals Mantle DOES expose
  // instead. Runtime is unchanged.
  if (filters.endpoint === 'mantle') {
    return <MantleHealthBody filters={filters} onInfo={onInfo} />;
  }
  return <RuntimeErrorsBody filters={filters} onInfo={onInfo} />;
}

// ---- bedrock-mantle: dedicated health view -------------------------------
// Positive, volume-first framing driven entirely by real AWS/BedrockMantle
// CloudWatch signals (Inferences + InferenceClientErrors). Never blank when
// there is traffic; an honest footnote explains the (real) coverage gap.
function MantleHealthBody({ filters, onInfo }) {
  const { data, loading } = useApi('/mantle-health', filters, [JSON.stringify(filters)]);
  const summary = data?.summary || {};
  const trend = data?.trend || [];
  const byModel = data?.by_model || [];
  const hasTraffic = Number(summary.total_requests || 0) > 0;

  const volumeSeries = useMemo(() => {
    if (!trend.length) return [];
    return [{
      title: 'Requests', type: 'bar', color: '#0972d3', valueFormatter: fmt,
      data: trend.map(r => ({ x: new Date(r.year, r.month - 1, r.day), y: Number(r.total_requests || 0) })),
    }];
  }, [trend]);

  const errRateSeries = useMemo(() => {
    if (!trend.length) return [];
    return [{
      title: 'Client-error rate %', type: 'line', color: '#d13212',
      data: trend.map(r => ({ x: `${r.month}/${r.day}`, y: Number(r.error_rate_pct || 0) })),
    }];
  }, [trend]);

  return (
    <SpaceBetween size="l">
      <Alert type="info" header="bedrock-mantle health signals">
        This view is built from bedrock-mantle’s native CloudWatch metrics:
        request volume and an aggregate client-error (4xx) rate, broken down
        per model. Use it to track Mantle throughput and error trends at a
        glance. For per-status-code detail, switch to the bedrock-runtime tab.
      </Alert>

      {/* KPI ribbon ------------------------------------------------------ */}
      <ColumnLayout columns={4} variant="text-grid">
        <KpiCard title="Requests (Inferences)" value={loading ? '—' : fmt(summary.total_requests || 0)} />
        <KpiCard title="Client errors (4xx)" value={loading ? '—' : fmt(summary.client_errors_4xx || 0)} />
        <KpiCard title="Client-error rate"
                 value={loading ? '—' : `${Number(summary.error_rate_pct || 0).toFixed(2)}%`}
                 invert />
        <KpiCard title="Success rate"
                 value={loading ? '—' : `${Number(summary.success_rate_pct || 0).toFixed(2)}%`} />
      </ColumnLayout>

      {/* Volume + error-rate trend -------------------------------------- */}
      <Container header={
        <SectionHeader
          title="Request volume & client-error rate"
          description="Daily Mantle request volume with the aggregate 4xx client-error rate overlaid."
          sectionId="mantle-health-trend"
          onInfo={onInfo}
        />
      }>
        {loading ? <ChartLoading /> :
          !hasTraffic ? (
            <Box textAlign="center" color="text-body-secondary" padding="l">
              No bedrock-mantle traffic in the selected window. Widen the date
              range or switch to bedrock-runtime above.
            </Box>
          ) : (
            <Grid gridDefinition={[{ colspan: 8 }, { colspan: 4 }]}>
              <BarChart
                series={volumeSeries}
                xScaleType="categorical"
                hideFilter
                ariaLabel="Mantle request volume by day"
                i18nStrings={{
                  ...CHART_I18N,
                  xTickFormatter: d => new Date(d).toLocaleDateString(undefined, { month: 'short', day: 'numeric' }),
                }}
                xTitle="Day" yTitle="Requests"
                height={260}
              />
              <LineChart
                series={errRateSeries}
                xScaleType="categorical"
                hideFilter
                ariaLabel="Mantle client-error rate"
                i18nStrings={{ ...CHART_I18N, yTickFormatter: v => `${v.toFixed(2)}%` }}
                xTitle="Day" yTitle="Client-error %"
                height={260}
              />
            </Grid>
          )
        }
      </Container>

      {/* Per-model table ------------------------------------------------ */}
      <Container header={
        <SectionHeader
          title="Health by model"
          description="Per-model request volume and aggregate client-error rate on bedrock-mantle."
          sectionId="mantle-health-by-model"
          onInfo={onInfo}
        />
      }>
        {loading ? <ChartLoading height={200} /> :
          <PaginatedTable
            items={byModel}
            downloadFileName="mantle-health-by-model.csv"
            columnDefinitions={[
              { id: 'model', header: 'Model', cell: r => r.modelid || r.modelId,
                exportValue: r => r.modelid || r.modelId },
              { id: 'requests', header: 'Requests', cell: r => fmt(r.total_requests),
                exportValue: r => r.total_requests },
              { id: 'errs', header: 'Client errors (4xx)', cell: r => fmt(r.client_errors_4xx),
                exportValue: r => r.client_errors_4xx },
              { id: 'rate', header: 'Error rate', cell: (r) => {
                  const p = Number(r.error_rate_pct || 0);
                  const t = p > 5 ? 'error' : p > 1 ? 'warning' : 'success';
                  return <StatusIndicator type={t}>{p.toFixed(2)}%</StatusIndicator>;
                }, exportValue: r => `${Number(r.error_rate_pct || 0).toFixed(2)}%` },
            ]}
            empty="No bedrock-mantle traffic in window"
          />
        }
      </Container>
    </SpaceBetween>
  );
}

function RuntimeErrorsBody({ filters, onInfo }) {
  const byModel = useApi('/errors-by-model', filters, [JSON.stringify(filters)]);
  const byAcct = useApi('/errors-by-account', filters, [JSON.stringify(filters)]);
  const trend = useApi('/errors-daily-trend', filters, [JSON.stringify(filters)]);
  const statusCodes = useApi('/status-codes', filters, [JSON.stringify(filters)]);

  // "All requests" includes 200 OK; "Errors only" drops it so the Y-axis
  // autoscales to the error magnitude (200 OK otherwise dwarfs every error
  // code into an invisible sliver). Default to errors-only — this is an
  // errors view, after all.
  const [statusView, setStatusView] = useState('errors');

  // Real per-code hourly series from invocation logs. Drop all-zero series so
  // the legend stays readable when a code never occurs in the window.
  const statusSeries = useMemo(() => {
    const rows = statusCodes.data?.series || [];
    if (!rows.length) return [];
    const visible = statusView === 'errors'
      ? STATUS_SERIES.filter(s => s.key !== 'ok')   // hide 200 OK
      : STATUS_SERIES;
    return visible
      .filter(s => rows.some(r => Number(r[s.key] || 0) > 0))
      .map(s => ({
        title: s.title, type: 'bar', color: s.color, valueFormatter: fmt,
        data: rows.map(r => ({ x: new Date(r.ts), y: Number(r[s.key] || 0) })),
      }));
  }, [statusCodes.data, statusView]);

  // Accurate per-state messaging — the backend tells us WHICH of the distinct
  // "no chart" situations applies, instead of always blaming "logging off".
  const statusState = statusCodes.data?.state;          // ok|no_logging|no_data|out_of_window
  const hasChart = statusState === 'ok' && statusSeries.length > 0;
  const availRange = statusCodes.data?.available_range;  // {min,max} | null
  const statusNotice = useMemo(() => {
    // bedrock-mantle NEVER exposes per-status-code data: its CloudWatch surface
    // publishes only an aggregate client-error (4xx) count — no per-code split,
    // no 5xx, and no invocation logs. So for the Mantle endpoint the Status
    // Codes chart can never populate; say so honestly rather than promising
    // data "after the next ingestion run" (which will never arrive).
    if (filters.endpoint === 'mantle') {
      return {
        header: 'Per-code breakdown lives on the bedrock-runtime tab',
        body: 'bedrock-mantle reports an aggregate client-error (4xx) rate — see the “Error trend” chart below for Mantle error volume. For a per-status-code breakdown, switch to the bedrock-runtime tab (with model invocation logging enabled).',
      };
    }
    switch (statusState) {
      case 'out_of_window':
        return {
          header: 'No status-code data in the selected date range',
          body: availRange
            ? `Per-code data exists for ${availRange.min} → ${availRange.max}, but not in the window you've selected. Adjust the date filter to that range to see the breakdown.`
            : 'Per-code data exists, but not in the window you\'ve selected. Widen or shift the date filter to see it.',
        };
      case 'no_data':
        return {
          header: 'Logging enabled — no per-code data yet',
          body: 'Bedrock model invocation logging IS enabled and wired to this dashboard, but no per-request records have been ingested for this window yet. New invocations will appear here after the next ingestion run (the ingester runs on a daily schedule).',
        };
      case 'no_logging':
      default:
        return {
          header: 'Per-code breakdown unavailable',
          body: 'Bedrock model invocation logging is not enabled for the monitored account(s), so a true per-status-code breakdown (403 / 404 / 408 / 424 / 429 …) can\'t be shown. CloudWatch metrics only expose all-4xx and all-5xx aggregates — those are in the “Error trend” chart below. To populate this chart, enable model invocation logging to S3 (it can be configured to capture only token counts and metadata — not prompt or response text), then re-run ingestion. The deploy script can set this up for you.',
        };
    }
  }, [statusState, availRange, filters.endpoint]);

  const [drill, setDrill] = useState(null);   // {year, month, day} | null
  const [hourly, setHourly] = useState(null); // payload | null
  const [drillLoading, setDrillLoading] = useState(false);
  const [drillError, setDrillError] = useState('');

  const loadHourly = async (y, m, d) => {
    setDrill({ year: y, month: m, day: d });
    setHourly(null);
    setDrillError('');
    setDrillLoading(true);
    try {
      const res = await apiCall('/errors-hourly-trend',
        { year: y, month: m, day: d, ...filters },
        { useCache: false });
      setHourly(res);
    } catch (e) {
      setDrillError(String(e.message || e));
    } finally {
      setDrillLoading(false);
    }
  };

  // Daily trend comes from f_daily, fed by CloudWatch. AWS/Bedrock gives three
  // trustworthy error counters: real throttles (status_429_count), all-4xx and
  // all-5xx. We surface 429 (Throttles) separately and show the remaining
  // non-throttle 4xx and the 5xx aggregate. Individual non-throttle codes
  // (403/404/408/424/503) come only from invocation logs — see Status Codes.
  const trendBarSeries = useMemo(() => {
    if (!trend.data) return [];
    return [
      { title: 'Throttled (429)', type: 'bar',
        data: trend.data.map(r => ({ x: new Date(r.year, r.month - 1, r.day), y: Number(r.status_429 || 0) })),
        color: '#ef4444' },
      { title: '4xx (non-throttle)', type: 'bar',
        data: trend.data.map(r => ({ x: new Date(r.year, r.month - 1, r.day), y: Number(r.status_400 || 0) })),
        color: '#f59e0b' },
      { title: '5xx Server errors', type: 'bar',
        data: trend.data.map(r => ({ x: new Date(r.year, r.month - 1, r.day), y: Number(r.status_500 || 0) })),
        color: '#b91c1c' },
    ];
  }, [trend.data]);

  const trendLineSeries = useMemo(() => {
    if (!trend.data) return [];
    return [{
      title: 'Error rate %', type: 'line',
      data: trend.data.map(r => ({
        x: `${r.month}/${r.day}`,
        y: r.total_requests ? r.failed_requests * 100 / r.total_requests : 0,
      })),
    }];
  }, [trend.data]);

  // Hourly drill-down is also CloudWatch-sourced (f_hourly_errors): real 429
  // throttles + non-throttle 4xx + 5xx aggregate.
  const hourlySeries = useMemo(() => {
    if (!hourly) return [];
    return [
      { title: 'Throttled (429)', type: 'bar', data: hourly.map(h => ({ x: h.hour, y: Number(h.status_429 || 0) })), color: '#ef4444' },
      { title: '4xx (non-throttle)', type: 'bar', data: hourly.map(h => ({ x: h.hour, y: Number(h.status_400 || 0) })), color: '#f59e0b' },
      { title: '5xx Server errors', type: 'bar', data: hourly.map(h => ({ x: h.hour, y: Number(h.status_500 || 0) })), color: '#b91c1c' },
    ];
  }, [hourly]);

  return (
    <SpaceBetween size="l">
      <Container header={
        <SectionHeader
          title="Status Codes"
          actions={
            <SpaceBetween direction="horizontal" size="xs">
              {hasChart && (
                <SegmentedControl
                  selectedId={statusView}
                  onChange={({ detail }) => setStatusView(detail.selectedId)}
                  label="Status code view"
                  options={[
                    { id: 'errors', text: 'Errors only' },
                    { id: 'all', text: 'All requests' },
                  ]}
                />
              )}
              <InfoLink sectionId="status-codes" onInfo={onInfo} />
            </SpaceBetween>
          }
        />
      }>
        {statusCodes.loading ? <ChartLoading /> :
          !hasChart ? (
            <Alert type="info" header={statusNotice.header}>
              {statusNotice.body}
            </Alert>
          ) : (
            <BarChart
              series={statusSeries}
              xScaleType="categorical"
              stackedBars
              hideFilter
              ariaLabel="Request status codes by hour"
              i18nStrings={{
                ...CHART_I18N,
                xTickFormatter: d => new Date(d).toLocaleString(undefined, {
                  month: 'short', day: 'numeric', hour: 'numeric',
                }),
              }}
              xTitle="Hour (UTC)"
              yTitle={statusView === 'errors' ? 'Error requests' : 'Requests'}
              height={300}
              empty={<Box textAlign="center" color="inherit">
                {statusView === 'errors' ? 'No errors in window' : 'No requests in window'}
              </Box>}
            />
          )
        }
      </Container>

      <Container header={<SectionHeader title="Error trend (4xx / 5xx)" sectionId="error-trend" onInfo={onInfo} />}>
        {trend.loading ? <ChartLoading /> :
          <Grid gridDefinition={[{ colspan: 8 }, { colspan: 4 }]}>
            <BarChart
              series={trendBarSeries}
              xScaleType="categorical"
              stackedBars
              hideFilter
              ariaLabel="Errors by status code"
              i18nStrings={{
                ...CHART_I18N,
                xTickFormatter: d => new Date(d).toLocaleDateString(undefined, { month: 'short', day: 'numeric' }),
              }}
              detailPopoverFooter={(xValue) => {
                const d = new Date(xValue);
                return (
                  <Button variant="inline-link"
                          onClick={() => loadHourly(d.getFullYear(), d.getMonth() + 1, d.getDate())}>
                    Drill into hourly
                  </Button>
                );
              }}
              height={260}
            />
            <LineChart
              series={trendLineSeries}
              xScaleType="categorical"
              hideFilter
              ariaLabel="Error rate"
              i18nStrings={{ ...CHART_I18N, yTickFormatter: v => `${v.toFixed(1)}%` }}
              height={260}
            />
          </Grid>
        }
        {drill && (
          <SpaceBetween size="s">
            <Header variant="h3"
                    actions={<Button onClick={() => { setDrill(null); setHourly(null); setDrillError(''); }}>Back to daily</Button>}>
              Hourly breakdown: {drill.year}-{String(drill.month).padStart(2, '0')}-{String(drill.day).padStart(2, '0')}
            </Header>
            {drillLoading ? <ChartLoading height={150} label="Loading hourly…" /> :
              drillError ? <Alert type="error">{drillError}</Alert> :
              hourly && hourly.length === 0 ? (
                <Box>No errors found on this day (hourly data available for the last 7 days only).</Box>
              ) :
              hourly ? (
                <BarChart
                  series={hourlySeries}
                  xScaleType="categorical"
                  stackedBars
                  hideFilter
                  ariaLabel="Hourly errors"
                  i18nStrings={CHART_I18N}
                  height={220}
                  xTitle="Hour (UTC)" yTitle="Failed requests"
                />
              ) : null
            }
          </SpaceBetween>
        )}
      </Container>

      <Container header={<SectionHeader title="Errors by model" sectionId="errors-by-model" onInfo={onInfo} />}>
        {byModel.loading ? <ChartLoading height={200} /> :
          <PaginatedTable
            items={(byModel.data || []).filter(r => Number(r.failed_requests) > 0)}
            columnDefinitions={[
              { id: 'm',   header: 'Model',     cell: r => r.modelid || r.modelId, sortingField: 'modelid' },
              { id: 'rate', header: 'Error %', cell: (r) => {
                  const p = r.total_requests ? r.failed_requests * 100 / r.total_requests : 0;
                  const t = p > 5 ? 'error' : p > 1 ? 'warning' : 'success';
                  return <Box color={t === 'error' ? 'text-status-error' : t === 'warning' ? 'text-status-warning' : 'text-status-success'}>{p.toFixed(2)}%</Box>;
                } },
              { id: 't',  header: 'Total',  cell: r => fmt(r.total_requests) },
              { id: 'f',  header: 'Failed', cell: r => fmt(r.failed_requests) },
              // CloudWatch gives real throttles (429) + 4xx/5xx aggregates.
              // Individual non-throttle codes are in the Status Codes chart.
              { id: 'c9', header: '429',  cell: r => fmt(r.status_429) },
              { id: 'c4', header: '4xx*', cell: r => fmt(r.status_400) },
              { id: 'c5', header: '5xx',  cell: r => fmt(r.status_500) },
            ]}
            empty="No errors in window"
          />
        }
      </Container>

      <Container header={<Header variant="h2">Errors by account / model / region</Header>}>
        {byAcct.loading ? <ChartLoading height={200} /> :
          <PaginatedTable
            items={byAcct.data || []}
            columnDefinitions={[
              { id: 'a', header: 'Account', cell: r => r.accountid || r.accountId },
              { id: 'm', header: 'Model',   cell: r => r.modelid || r.modelId },
              { id: 'r', header: 'Region',  cell: r => r.region },
              { id: 't', header: 'Total',   cell: r => fmt(r.total_requests) },
              { id: 'f', header: 'Failed',  cell: r => fmt(r.failed_requests) },
              { id: 'c9', header: '429',    cell: r => fmt(r.status_429) },
              { id: 'c4', header: '4xx*',   cell: r => fmt(r.status_400) },
              { id: 'c5', header: '5xx',    cell: r => fmt(r.status_500) },
            ]}
            empty="No errors"
          />
        }
      </Container>
    </SpaceBetween>
  );
}
