// Errors tab — 3 containers, with bar→hourly drill.
import { useMemo, useState } from 'react';
import {
  Container, Header, SpaceBetween, BarChart, LineChart, Grid, Box,
  Button, Spinner, Alert,
} from '@cloudscape-design/components';
import { useApi, fmt, fmtPct, api as apiCall } from '../api.js';
import { ChartLoading, SectionHeader, CHART_I18N } from '../components/Common.jsx';
import PaginatedTable from '../components/PaginatedTable.jsx';

export default function ErrorsTab({ filters, onInfo }) {
  const byModel = useApi('/errors-by-model', filters, [JSON.stringify(filters)]);
  const byAcct = useApi('/errors-by-account', filters, [JSON.stringify(filters)]);
  const trend = useApi('/errors-daily-trend', filters, [JSON.stringify(filters)]);

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

  const trendBarSeries = useMemo(() => {
    if (!trend.data) return [];
    return [
      { title: 'Throttled (429)', type: 'bar',
        data: trend.data.map(r => ({ x: new Date(r.year, r.month - 1, r.day), y: Number(r.status_429 || 0) })),
        color: '#ff9900' },
      { title: 'Server (500)', type: 'bar',
        data: trend.data.map(r => ({ x: new Date(r.year, r.month - 1, r.day), y: Number(r.status_500 || 0) })),
        color: '#d13212' },
      { title: 'Server (503)', type: 'bar',
        data: trend.data.map(r => ({ x: new Date(r.year, r.month - 1, r.day), y: Number(r.status_503 || 0) })),
        color: '#7d2105' },
      { title: 'Client (400)', type: 'bar',
        data: trend.data.map(r => ({ x: new Date(r.year, r.month - 1, r.day), y: Number(r.status_400 || 0) })),
        color: '#ffcc00' },
      { title: 'Forbidden (403)', type: 'bar',
        data: trend.data.map(r => ({ x: new Date(r.year, r.month - 1, r.day), y: Number(r.status_403 || 0) })),
        color: '#5a5a5a' },
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

  const hourlySeries = useMemo(() => {
    if (!hourly) return [];
    return [
      { title: '429', type: 'bar', data: hourly.map(h => ({ x: h.hour, y: Number(h.status_429 || 0) })), color: '#ff9900' },
      { title: '500', type: 'bar', data: hourly.map(h => ({ x: h.hour, y: Number(h.status_500 || 0) })), color: '#d13212' },
      { title: '503', type: 'bar', data: hourly.map(h => ({ x: h.hour, y: Number(h.status_503 || 0) })), color: '#7d2105' },
      { title: '400', type: 'bar', data: hourly.map(h => ({ x: h.hour, y: Number(h.status_400 || 0) })), color: '#ffcc00' },
    ];
  }, [hourly]);

  return (
    <SpaceBetween size="l">
      <Container header={<SectionHeader title="Error trend (by status code)" sectionId="error-trend" onInfo={onInfo} />}>
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
              { id: 'c4', header: '400',    cell: r => fmt(r.status_400) },
              { id: 'c3', header: '403',    cell: r => fmt(r.status_403) },
              { id: 'c9', header: '429',    cell: r => fmt(r.status_429) },
              { id: 'c5', header: '500',    cell: r => fmt(r.status_500) },
              { id: 'c0', header: '503',    cell: r => fmt(r.status_503) },
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
              { id: 'c5', header: '500',    cell: r => fmt(r.status_500) },
              { id: 'c0', header: '503',    cell: r => fmt(r.status_503) },
            ]}
            empty="No errors"
          />
        }
      </Container>
    </SpaceBetween>
  );
}
