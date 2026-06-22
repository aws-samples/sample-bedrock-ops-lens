// Errors tab — 3 containers, with bar→hourly drill.
import { useMemo, useState } from 'react';
import {
  Container, Header, SpaceBetween, BarChart, LineChart, Grid, Box,
  Button, Spinner, Alert, SegmentedControl,
} from '@cloudscape-design/components';
import { useApi, fmt, fmtPct, api as apiCall } from '../api.js';
import { ChartLoading, SectionHeader, InfoLink, CHART_I18N } from '../components/Common.jsx';
import PaginatedTable from '../components/PaginatedTable.jsx';

// Per-code palette + render order for the "Status Codes" stacked chart.
// 200 OK first (bottom of stack), then client codes, then server codes.
// Distinct, high-contrast hues so adjacent stacked segments never blur into
// each other (the previous orange/brown/red ramp was hard to tell apart).
const STATUS_SERIES = [
  { key: 'ok',   title: '200 OK', color: '#2e7d32' },  // green
  { key: 's400', title: '400',    color: '#f59e0b' },  // amber
  { key: 's403', title: '403',    color: '#8b5cf6' },  // violet
  { key: 's404', title: '404',    color: '#0ea5e9' },  // sky blue
  { key: 's408', title: '408',    color: '#ec4899' },  // pink
  { key: 's424', title: '424',    color: '#a16207' },  // brown
  { key: 's429', title: '429',    color: '#ef4444' },  // red
  { key: 's500', title: '500',    color: '#111827' },  // near-black
  { key: 's503', title: '503',    color: '#6b7280' },  // gray
];

export default function ErrorsTab({ filters, onInfo }) {
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
        title: s.title, type: 'bar', color: s.color,
        data: rows.map(r => ({ x: new Date(r.ts), y: Number(r[s.key] || 0) })),
      }));
  }, [statusCodes.data, statusView]);

  // Accurate per-state messaging — the backend tells us WHICH of the distinct
  // "no chart" situations applies, instead of always blaming "logging off".
  const statusState = statusCodes.data?.state;          // ok|no_logging|no_data|out_of_window
  const hasChart = statusState === 'ok' && statusSeries.length > 0;
  const availRange = statusCodes.data?.available_range;  // {min,max} | null
  const statusNotice = useMemo(() => {
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
          header: 'No per-code data ingested yet',
          body: 'Bedrock model invocation logging is wired up, but no per-request log records have been ingested yet. New invocations will appear here after the next ingestion run.',
        };
      case 'no_logging':
      default:
        return {
          header: 'Per-code breakdown unavailable',
          body: 'Bedrock model invocation logging is not enabled for the monitored account(s), so a true per-status-code breakdown (403 / 404 / 408 / 424 / 429 …) can\'t be shown. CloudWatch metrics only expose all-4xx and all-5xx aggregates — those are in the “Error trend” chart below. To populate this chart, enable model invocation logging to S3 (see the deployment README) and re-run ingestion.',
        };
    }
  }, [statusState, availRange]);

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
