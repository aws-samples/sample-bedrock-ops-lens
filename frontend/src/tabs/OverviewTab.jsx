// Overview tab — 12 containers (per reference inventory).
import { useMemo, useState } from 'react';
import {
  Container, Header, SpaceBetween, Grid, BarChart, LineChart, PieChart,
  Spinner, Select, Box, ColumnLayout, StatusIndicator,
} from '@cloudscape-design/components';
import { useApi, fmt, fmtPct } from '../api.js';
import {
  ChartLoading, KpiCard, SectionHeader, SeverityCell, CHART_I18N,
} from '../components/Common.jsx';
import PaginatedTable from '../components/PaginatedTable.jsx';
import EndpointSubTabs from '../components/EndpointSubTabs.jsx';

function modelShort(id) {
  return (id || '').replace(/^us\./, '').replace(/^eu\./, '').replace(/^global\./, '')
    .replace(/^anthropic\./, '').replace(/^amazon\./, '').replace(/^meta\./, '')
    .replace(/^cohere\./, '').replace(/^mistral\./, '')
    .split(':')[0];
}

function categoryFor(modelId) {
  const m = (modelId || '').toLowerCase();
  if (m.includes('embed') || m.includes('rerank')) return 'Embedding';
  if (m.includes('canvas') || m.includes('reel') || m.includes('stability') || m.includes('image')) return 'Image / Video';
  return 'LLM (Text / Chat)';
}

function trafficLabel(tt, profilePrefix) {
  if (tt === 'CROSS_REGION_OD_INFERENCE_REQUEST' || tt === 'SOURCE_REGION_OD_INFERENCE_REQUEST') {
    return profilePrefix === 'global' ? 'OD - Global CRIS' : 'OD - Regional CRIS';
  }
  if (tt === 'ON_DEMAND_INFERENCE_REQUEST') return 'OD - Single Region';
  if (tt === 'PROVISIONED_THROUGHPUT_V1') return 'Provisioned Throughput';
  return 'Other';
}

const GROUP_BY_OPTIONS = [
  // "Model" is the default — most users open the dashboard wanting
  // "what models am I using". Untouched-volumetric "None" stays as an
  // option for folks who want a single total bar.
  { value: 'model',    label: 'Model (default)' },
  { value: 'none',     label: 'None (totals only)' },
  { value: 'provider', label: 'Provider' },
  { value: 'traffic',  label: 'Traffic type' },
  { value: 'region',   label: 'Region' },
  // Endpoint split: runtime vs mantle stacked per day, straight off
  // /daily-trend's runtime_requests / mantle_requests columns (no separate
  // fetch). Lets a manager see the composition over time at a glance.
  { value: 'endpoint', label: 'Endpoint (runtime / mantle)' },
  // "Cost" reads from /cost-by-model rather than /daily-breakdown — the
  // y-axis switches from request count to dollars, but the visual
  // (stacked bars per day, top-N + Other) is the same shape.
  { value: 'cost',     label: 'Cost ($) by model' },
];

export default function OverviewTab({ filters, onInfo }) {
  // Local endpoint state — defaults to 'all' so the consolidated KPI
  // ribbon shows the full picture. The two-segment switcher below is
  // the per-tab override. The bedrock-mantle segment shows only when
  // Mantle volumetric data actually exists (else it's hidden, no blank
  // view) — signalled by /distinct-filters.mantle_available.
  const distinct = useApi('/distinct-filters', {}, []).data || {};
  const mantleAvailable = !!distinct.mantle_available?.volumetric;
  const [endpoint, setEndpoint] = useState(filters.endpoint || 'all');
  const filtersWithEp = useMemo(() => ({ ...filters, endpoint }), [filters, endpoint]);
  return (
    <SpaceBetween size="m">
      <EndpointSubTabs
        selected={endpoint === 'all' ? 'runtime' : endpoint}
        onChange={setEndpoint}
        runtimeCoverage="full"
        mantleCoverage="metric"
        mantleAvailable={mantleAvailable}
      >
        {() => <OverviewBody filters={filtersWithEp} onInfo={onInfo} />}
      </EndpointSubTabs>
    </SpaceBetween>
  );
}

function OverviewBody({ filters, onInfo }) {
  const [groupBy, setGroupBy] = useState(GROUP_BY_OPTIONS[0]);
  // Spend tile in-card toggle: on an endpoint sub-tab, flip between that
  // endpoint's allocated slice ('endpoint') and the combined CE total ('total').
  const [spendView, setSpendView] = useState('endpoint');

  const summary = useApi('/summary', filters, [JSON.stringify(filters)]);
  const wow = useApi('/wow-comparison', {}, []);
  const trend = useApi('/daily-trend', filters, [JSON.stringify(filters)]);
  const cost  = useApi('/cost-summary', filters, [JSON.stringify(filters)]);
  const costByModel = useApi('/cost-by-model', filters, [JSON.stringify(filters)]);
  // /daily-breakdown only supports model/provider/traffic/region. For 'none',
  // 'cost', and 'endpoint' we don't call it (endpoint is served from
  // /daily-trend's split columns; cost from /cost-by-model).
  const breakdownGroupBy = ['model', 'provider', 'traffic', 'region'].includes(groupBy.value)
    ? groupBy.value : undefined;
  const breakdown = useApi(
    '/daily-breakdown',
    { ...filters, group_by: breakdownGroupBy },
    [JSON.stringify(filters), breakdownGroupBy],
  );
  const byModel = useApi('/requests-by-model', filters, [JSON.stringify(filters)]);
  const byTraffic = useApi('/traffic-types', filters, [JSON.stringify(filters)]);
  const byAcct = useApi('/account-type-split', filters, [JSON.stringify(filters)]);
  const byOp = useApi('/operations', filters, [JSON.stringify(filters)]);
  // Regions container always shows all regions regardless of region filter.
  const byRegion = useApi(
    '/regions',
    { ...filters, region: 'all' },
    [JSON.stringify({ ...filters, region: 'all' })],
  );

  // NB: All hook calls (useState/useEffect/useMemo) MUST run on every render
  // in the same order. Don't put an early `return` above the useMemo calls
  // below — React's useState() above this comment will be called on the
  // first (loading) render, then useMemo (which is below in this file) will
  // not, and React then sees a hook-order change on the next render. We let
  // the body run to completion and conditionally render the spinner instead.
  const s = summary.data || {};
  const cur = wow.data?.current || {};
  const prev = wow.data?.previous || {};
  const errorRate = s.total_requests ? (s.failed_requests * 100 / s.total_requests) : 0;
  const errorRatePrev = prev.total_requests ? (prev.failed_requests * 100 / prev.total_requests) : 0;

  // Runtime/mantle composition for the KPI tiles. Show the split only when
  // there IS mantle usage in the window (else runtime-only fleets stay clean).
  // The KPI total above already reflects the current endpoint filter; this
  // subtext always shows the underlying runtime·mantle breakdown so a
  // manager sees "total + what's Mantle" at a glance, no toggle.
  const be = s.by_endpoint || {};
  const mantleReqs = Number(be.mantle?.total_requests || 0);
  const kpiSplit = (rtField, mtField) => (
    mantleReqs > 0
      ? { runtime: fmt(be.runtime?.[rtField] ?? 0), mantle: fmt(be.mantle?.[mtField ?? rtField] ?? 0) }
      : undefined
  );

  // Daily trend → BarChart series (stacked successful + failed)
  const trendSeries = useMemo(() => {
    if (!trend.data) return [];
    return [
      { title: 'Successful', type: 'bar', valueFormatter: fmt,
        data: trend.data.map(r => ({ x: new Date(r.year, r.month - 1, r.day), y: Number(r.successful_requests || 0) })) },
      { title: 'Failed', type: 'bar', valueFormatter: fmt,
        data: trend.data.map(r => ({ x: new Date(r.year, r.month - 1, r.day), y: Number(r.failed_requests || 0) })) },
    ];
  }, [trend.data]);

  // Endpoint split: runtime vs mantle stacked per day (from /daily-trend's
  // conditional-sum columns). Mantle series dropped when there's zero mantle
  // volume so runtime-only fleets don't get an empty second series.
  const endpointSeries = useMemo(() => {
    if (!trend.data) return [];
    const mantleTotal = trend.data.reduce((a, r) => a + Number(r.mantle_requests || 0), 0);
    const series = [
      { title: 'bedrock-runtime', type: 'bar', color: '#0972d3', valueFormatter: fmt,
        data: trend.data.map(r => ({ x: new Date(r.year, r.month - 1, r.day), y: Number(r.runtime_requests || 0) })) },
    ];
    if (mantleTotal > 0) {
      series.push({ title: 'bedrock-mantle', type: 'bar', color: '#12cdd4', valueFormatter: fmt,
        data: trend.data.map(r => ({ x: new Date(r.year, r.month - 1, r.day), y: Number(r.mantle_requests || 0) })) });
    }
    return series;
  }, [trend.data]);

  // Breakdown series (top categories, "Other" pinned to end)
  const breakdownSeries = useMemo(() => {
    if (!breakdown.data || groupBy.value === 'none') return null;
    const byCat = new Map();
    for (const r of breakdown.data) {
      if (!byCat.has(r.category)) byCat.set(r.category, []);
      byCat.get(r.category).push(r);
    }
    const cats = Array.from(byCat.keys());
    cats.sort((a, b) => {
      if (a === 'Other') return 1;
      if (b === 'Other') return -1;
      const sa = byCat.get(a).reduce((s, r) => s + Number(r.total_requests), 0);
      const sb = byCat.get(b).reduce((s, r) => s + Number(r.total_requests), 0);
      return sb - sa;
    });
    return cats.map(cat => ({
      title: cat,
      type: 'bar',
      valueFormatter: fmt,
      data: byCat.get(cat)
        .map(r => ({ x: new Date(r.year, r.month - 1, r.day), y: Number(r.total_requests) }))
        .sort((a, b) => a.x - b.x),
    }));
  }, [breakdown.data, groupBy.value]);

  // Shared chronological x-domain for the request-volume charts (breakdown +
  // endpoint), so the categorical axis is ordered regardless of the order
  // categories introduce dates.
  const volumeXDomain = useMemo(() => {
    const src = (groupBy.value === 'endpoint' ? (trend.data || []) : (breakdown.data || []));
    const ds = new Set();
    for (const r of src) ds.add(new Date(r.year, r.month - 1, r.day).getTime());
    return [...ds].sort((a, b) => a - b).map(t => new Date(t));
  }, [breakdown.data, trend.data, groupBy.value]);

  // Cost-by-model stacked series — same shape as breakdownSeries but
  // sourced from /cost-by-model. Used when groupBy='cost'.
  const costSeries = useMemo(() => {
    if (!costByModel.data || costByModel.data.length === 0) return null;
    const byCat = new Map();
    for (const r of costByModel.data) {
      if (!byCat.has(r.model_label)) byCat.set(r.model_label, []);
      byCat.get(r.model_label).push(r);
    }
    // Top 7 models by total spend, "Other" pinned to end.
    const totals = new Map();
    for (const [label, rows] of byCat.entries()) {
      totals.set(label, rows.reduce((s, r) => s + Number(r.total_cost), 0));
    }
    const top = [...totals.entries()].sort((a, b) => b[1] - a[1]).slice(0, 7).map(([k]) => k);
    const topSet = new Set(top);
    const folded = new Map();
    for (const r of costByModel.data) {
      const cat = topSet.has(r.model_label) ? r.model_label : 'Other';
      if (!folded.has(cat)) folded.set(cat, new Map());
      const day = r.event_date;
      folded.get(cat).set(day, (folded.get(cat).get(day) || 0) + Number(r.total_cost));
    }
    const cats = [...folded.keys()].sort((a, b) => {
      if (a === 'Other') return 1;
      if (b === 'Other') return -1;
      return (totals.get(b) || 0) - (totals.get(a) || 0);
    });
    return cats.map(cat => ({
      title: cat,
      type: 'bar',
      // Sort by real date, not string compare — event_date formats can vary and
      // a lexical sort scrambles the categorical x-axis (Jun 3, Jun 22, Jun 7…).
      data: [...folded.get(cat).entries()]
        .sort((a, b) => new Date(a[0]) - new Date(b[0]))
        .map(([day, amt]) => ({ x: new Date(day), y: amt })),
    }));
  }, [costByModel.data]);

  // Explicit chronological x-domain shared by all cost series, so the
  // categorical axis is ordered even when series introduce dates in different
  // orders.
  const costXDomain = useMemo(() => {
    const ds = new Set();
    for (const r of (costByModel.data || [])) ds.add(r.event_date);
    return [...ds].sort((a, b) => new Date(a) - new Date(b)).map(d => new Date(d));
  }, [costByModel.data]);

  // Health indicators
  const healthSeries = useMemo(() => {
    if (!trend.data) return [];
    const xs = trend.data.map(r => `${r.month}/${r.day}`);
    return [
      { title: 'Success rate %', type: 'line',
        data: trend.data.map((r, i) => ({ x: xs[i], y: r.total_requests ? r.successful_requests * 100 / r.total_requests : 0 })) },
      { title: 'Throttle rate %', type: 'line',
        data: trend.data.map((r, i) => ({ x: xs[i], y: r.total_requests ? r.throttled * 100 / r.total_requests : 0 })) },
    ];
  }, [trend.data]);

  // Requests stacked (success vs failed)
  const stackSuccess = trendSeries;
  // Tokens stacked
  const tokenSeries = useMemo(() => {
    if (!trend.data) return [];
    return [
      { title: 'Input tokens',  type: 'bar', valueFormatter: fmt, data: trend.data.map(r => ({ x: new Date(r.year, r.month - 1, r.day), y: Number(r.input_tokens || 0) })) },
      { title: 'Output tokens', type: 'bar', valueFormatter: fmt, data: trend.data.map(r => ({ x: new Date(r.year, r.month - 1, r.day), y: Number(r.output_tokens || 0) })) },
    ];
  }, [trend.data]);

  // Pie data
  const modelCategoryData = useMemo(() => {
    if (!byModel.data) return [];
    const buckets = new Map();
    for (const r of byModel.data) {
      const c = categoryFor(r.modelid || r.modelId);
      buckets.set(c, (buckets.get(c) || 0) + Number(r.total_requests));
    }
    return Array.from(buckets, ([title, value]) => ({ title, value }));
  }, [byModel.data]);

  const trafficTypeData = useMemo(() => {
    if (!byTraffic.data) return [];
    const buckets = new Map();
    for (const r of byTraffic.data) {
      const label = trafficLabel(r.traffic_type, r.inference_profile_prefix);
      buckets.set(label, (buckets.get(label) || 0) + Number(r.total_requests));
    }
    return Array.from(buckets, ([title, value]) => ({ title, value }));
  }, [byTraffic.data]);

  const accountTypeData = useMemo(() => {
    if (!byAcct.data) return [];
    return byAcct.data.map(r => ({
      title: `${r.account_type} (${fmt(r.unique_accounts)} accts)`,
      value: Number(r.total_requests),
    }));
  }, [byAcct.data]);

  const opsData = useMemo(() => {
    if (!byOp.data) return [];
    // The AWS/Bedrock CloudWatch metrics carry no `operation` dimension, so the
    // ingester stores the '__none__' sentinel unless invocation logs (which do
    // record the API operation) are enabled. Show a human label instead of the
    // raw sentinel.
    return byOp.data.map(r => ({
      title: (r.operation && r.operation !== '__none__') ? r.operation : 'Not attributed',
      value: Number(r.total_requests),
    }));
  }, [byOp.data]);

  // When operation is entirely unattributed (CW-only, no invocation logs), the
  // pie is a single "Not attributed" slice — call that out honestly.
  const opsAllUnattributed = useMemo(
    () => opsData.length === 1 && opsData[0].title === 'Not attributed',
    [opsData]);

  const topReqData = useMemo(() => {
    if (!byModel.data) return [];
    return byModel.data.slice(0, 7).map(r => ({
      x: modelShort(r.modelid || r.modelId), y: Number(r.total_requests),
    }));
  }, [byModel.data]);

  const topTokenData = useMemo(() => {
    if (!byModel.data) return [];
    const sorted = [...byModel.data].sort((a, b) =>
      (Number(b.input_tokens || 0) + Number(b.output_tokens || 0)) -
      (Number(a.input_tokens || 0) + Number(a.output_tokens || 0)),
    );
    return sorted.slice(0, 7).map(r => ({
      x: modelShort(r.modelid || r.modelId),
      y: Number(r.input_tokens || 0) + Number(r.output_tokens || 0),
    }));
  }, [byModel.data]);

  // Spend by Model — stacked bar per day. Top 7 models by total spend in window
  // + "Other" bucket so the chart stays readable.
  const spendSeries = useMemo(() => {
    if (!costByModel.data || costByModel.data.length === 0) return [];
    const totals = new Map();
    for (const r of costByModel.data) {
      totals.set(r.model_label, (totals.get(r.model_label) || 0) + Number(r.total_cost || 0));
    }
    const top = [...totals.entries()].sort((a, b) => b[1] - a[1]).slice(0, 7).map(([k]) => k);
    const topSet = new Set(top);
    // Fold non-top models into 'Other', summing per (category, date) so a day
    // never has two segments for the same category.
    const byCatDate = new Map();   // cat -> Map(dateStr -> cost)
    for (const r of costByModel.data) {
      const cat = topSet.has(r.model_label) ? r.model_label : 'Other';
      if (!byCatDate.has(cat)) byCatDate.set(cat, new Map());
      const m = byCatDate.get(cat);
      m.set(r.event_date, (m.get(r.event_date) || 0) + Number(r.total_cost || 0));
    }
    return [...byCatDate.entries()].map(([cat, m]) => ({
      title: modelShort(cat),
      type: 'bar',
      // Sort by real date, not string compare — the API returns rows grouped by
      // model, so without this the categorical x-axis renders days out of order.
      data: [...m.entries()]
        .sort((a, b) => new Date(a[0]) - new Date(b[0]))
        .map(([d, cost]) => ({ x: new Date(d), y: cost })),
    }));
  }, [costByModel.data]);
  const isCostDerived = useMemo(() => {
    return (costByModel.data || []).some(r => r.derived);
  }, [costByModel.data]);

  if (summary.loading && !summary.data) {
    return <Spinner size="large" />;
  }

  return (
    <SpaceBetween size="l">
      {/* 1. KPI Grid — 5 tiles. 12-col grid + colspan 3 doesn't divide
           cleanly into 5, but Cloudscape supports `m: 2` (=> 6 of 12) at
           the breakpoints we care about; using `default: 12` for stack-on-
           narrow + `xs: 6` for 2x3 + `s: 4` for 3x2 + `m: 2.4`-equivalent
           via custom widths below. Simpler: equal {colspan: 2} fits
           5 tiles in a 10-column-wide subset; remaining 2 cols are gutter. */}
      <Grid gridDefinition={[
        { colspan: { default: 12, xs: 6, s: 4, m: 2 } },
        { colspan: { default: 12, xs: 6, s: 4, m: 2 } },
        { colspan: { default: 12, xs: 6, s: 4, m: 2 } },
        { colspan: { default: 12, xs: 6, s: 4, m: 3 } },
        { colspan: { default: 12, xs: 6, s: 4, m: 3 } },
      ]}>
        <KpiCard title="Total requests"
                 value={fmt(s.total_requests)}
                 wow={[cur.total_requests, prev.total_requests]}
                 split={kpiSplit('total_requests')} />
        <KpiCard title="Active accounts"
                 value={fmt(s.unique_accounts)}
                 wow={[cur.unique_accounts, prev.unique_accounts]} />
        <KpiCard title="Input tokens"
                 value={fmt(s.total_input_tokens)}
                 wow={[cur.total_input_tokens, prev.total_input_tokens]}
                 split={kpiSplit('total_input_tokens')} />
        <KpiCard title="Error rate"
                 value={fmtPct(errorRate)}
                 wow={[errorRate, errorRatePrev]} invert />
        {(() => {
          // Cost Explorer gives an invoice-accurate TOTAL but no runtime-vs-
          // mantle dimension. On a specific endpoint sub-tab, show that
          // endpoint's ALLOCATED share (total split by each endpoint's
          // token-cost weight) rather than repeating the same flat total —
          // labeled as an allocation so it's honest.
          const ep = filters.endpoint;
          const be = cost.data?.by_endpoint;
          const onEndpoint = (ep === 'runtime' || ep === 'mantle') && be;
          // On an endpoint sub-tab, the in-card toggle picks which figure shows:
          // that endpoint's allocated slice, or the combined CE total.
          const showAlloc = onEndpoint && spendView === 'endpoint';
          const cur = cost.data?.currency === 'USD' ? '$' : (cost.data?.currency || '') + ' ';
          const amount = showAlloc ? Number(be[ep] || 0) : Number(cost.data?.total_cost || 0);
          const fmtUsd = (n) => `${cur}${n.toLocaleString(undefined, {
            minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
          return (
            <KpiCard title="Total spend"
                     tabs={onEndpoint ? {
                       selectedId: spendView,
                       onChange: setSpendView,
                       options: [
                         { id: 'endpoint', text: ep === 'mantle' ? 'Mantle' : 'Runtime' },
                         { id: 'total', text: 'Total' },
                       ],
                     } : undefined}
                     value={cost.data ? fmtUsd(amount) : '—'}
                     wow={showAlloc ? undefined : [cost.data?.total_cost, cost.data?.previous_total_cost]}
                     invert
                     split={be && ep === 'all'
                       ? { runtime: fmtUsd(Number(be.runtime || 0)),
                           mantle: be.mantle != null ? fmtUsd(Number(be.mantle)) : null }
                       : undefined}
                     note={showAlloc ? 'token-cost share' : undefined} />
          );
        })()}
      </Grid>

      {/* 2. Request Volume + Group by */}
      <Container header={
        <SectionHeader
          title={groupBy.value === 'cost' ? 'Spend' : 'Request volume'}
          sectionId="daily-trend" onInfo={onInfo}
          actions={
            <Select
              selectedOption={groupBy}
              onChange={({ detail }) => setGroupBy(detail.selectedOption)}
              options={GROUP_BY_OPTIONS}
              ariaLabel="Group by"
            />
          }
        />
      }>
        {trend.loading ? <ChartLoading /> :
          // Cost mode: use /cost-by-model series, currency Y-axis.
          groupBy.value === 'cost' ? (
            costSeries && costSeries.length ? (
              <BarChart
                series={costSeries}
                xScaleType="categorical"
                xDomain={costXDomain}
                stackedBars
                hideFilter
                ariaLabel="Daily spend by model"
                i18nStrings={{
                  ...CHART_I18N,
                  xTickFormatter: d => new Date(d).toLocaleDateString(undefined, { month: 'short', day: 'numeric' }),
                  yTickFormatter: v => `$${v >= 1000 ? (v/1000).toFixed(1) + 'K' : v.toFixed(0)}`,
                }}
                height={250}
                xTitle="Day" yTitle="USD"
              />
            ) : (
              <Box color="text-body-secondary" textAlign="center" padding="l">
                No Cost Explorer data in this window. Run
                {' '}<code>python -m ingestion.cost --days 30</code>{' '}
                to backfill.
              </Box>
            )
          ) :
          groupBy.value === 'endpoint' ? (
            <BarChart
              series={endpointSeries}
              xScaleType="categorical"
              xDomain={volumeXDomain}
              stackedBars
              hideFilter
              ariaLabel="Daily request volume by endpoint"
              i18nStrings={{
                ...CHART_I18N,
                xTickFormatter: d => new Date(d).toLocaleDateString(undefined, { month: 'short', day: 'numeric' }),
                yTickFormatter: fmt,
              }}
              height={250}
              empty={<ChartLoading />}
              xTitle="Day" yTitle="Requests"
            />
          ) :
          breakdownSeries ? (
            <BarChart
              series={breakdownSeries}
              xScaleType="categorical"
              xDomain={volumeXDomain}
              stackedBars
              hideFilter
              ariaLabel="Daily request volume by category"
              i18nStrings={{
                ...CHART_I18N,
                xTickFormatter: d => new Date(d).toLocaleDateString(undefined, { month: 'short', day: 'numeric' }),
                yTickFormatter: fmt,
              }}
              height={250}
              empty={<ChartLoading />}
              xTitle="Day" yTitle="Requests"
            />
          ) : (
            <BarChart
              series={trendSeries}
              xScaleType="categorical"
              stackedBars
              hideFilter
              ariaLabel="Daily request volume"
              i18nStrings={{
                ...CHART_I18N,
                xTickFormatter: d => new Date(d).toLocaleDateString(undefined, { month: 'short', day: 'numeric' }),
                yTickFormatter: fmt,
              }}
              height={250}
              xTitle="Day" yTitle="Requests"
            />
          )
        }
      </Container>

      {/* 3. Health indicators */}
      <Container header={<SectionHeader title="Health indicators" sectionId="health-indicators" onInfo={onInfo} />}>
        {trend.loading ? <ChartLoading /> :
          <LineChart
            series={healthSeries}
            xScaleType="categorical"
            ariaLabel="Health indicators"
            i18nStrings={{ ...CHART_I18N, yTickFormatter: v => `${v.toFixed(0)}%` }}
            height={220}
            xTitle="Day" yTitle="Percent"
          />
        }
      </Container>

      {/* 4. Requests: success vs failed (stacked) — 5. Tokens I/O (stacked) */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 20, alignItems: 'stretch' }}>
        <Container fitHeight header={<Header variant="h2">Requests: success vs failed</Header>}>
          {trend.loading ? <ChartLoading height={200} /> :
            <BarChart
              series={stackSuccess} stackedBars hideFilter xScaleType="categorical"
              ariaLabel="Requests success/failed"
              i18nStrings={{ ...CHART_I18N, xTickFormatter: d => new Date(d).toLocaleDateString(undefined, { month: 'short', day: 'numeric' }), yTickFormatter: fmt }}
              height={200}
            />
          }
        </Container>
        <Container fitHeight header={<Header variant="h2">Tokens: input vs output</Header>}>
          {trend.loading ? <ChartLoading height={200} /> :
            <BarChart
              series={tokenSeries} stackedBars hideFilter xScaleType="categorical"
              ariaLabel="Tokens input/output"
              i18nStrings={{ ...CHART_I18N, xTickFormatter: d => new Date(d).toLocaleDateString(undefined, { month: 'short', day: 'numeric' }), yTickFormatter: fmt }}
              height={200}
            />
          }
        </Container>
      </div>

      {/* 6. Model Category — 7. Traffic Types — 8. Internal/External — 9. Operations (4-col pies) */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr 1fr', gap: 20, alignItems: 'stretch' }}>
        <Container fitHeight header={<SectionHeader title="Model category" sectionId="model-category" onInfo={onInfo} />}>
          {byModel.loading ? <ChartLoading height={180} /> :
            <PieChart data={modelCategoryData} size="small" hideFilter
                      ariaLabel="Model category"
                      empty="No data" />
          }
        </Container>
        <Container fitHeight header={<SectionHeader title="Traffic types" sectionId="traffic-types" onInfo={onInfo} />}>
          {byTraffic.loading ? <ChartLoading height={180} /> :
            <PieChart data={trafficTypeData} size="small" hideFilter
                      ariaLabel="Traffic types"
                      empty="No data" />
          }
        </Container>
        <Container fitHeight header={<SectionHeader title="Account split" sectionId="internal-external" onInfo={onInfo} />}>
          {byAcct.loading ? <ChartLoading height={180} /> :
            <PieChart data={accountTypeData} size="small" hideFilter
                      ariaLabel="Account split"
                      empty="No data" />
          }
        </Container>
        <Container fitHeight header={<SectionHeader title="Operations" sectionId="operations" onInfo={onInfo} />}>
          {byOp.loading ? <ChartLoading height={180} /> :
            opsAllUnattributed ? (
              <Box textAlign="center" color="text-body-secondary" padding={{ vertical: 'l' }}>
                <b>Operation not available from CloudWatch.</b>
                <div style={{ fontSize: '12px', marginTop: 4 }}>
                  AWS/Bedrock metrics aren’t broken out by API operation
                  (Converse / InvokeModel). Enable Bedrock model invocation
                  logging to attribute requests by operation.
                </div>
              </Box>
            ) :
            <PieChart data={opsData} size="small" hideFilter
                      ariaLabel="Operations"
                      empty="No data" />
          }
        </Container>
      </div>

      {/* 10. Top by requests — 11. Top by tokens (2-col bars) */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 20, alignItems: 'stretch' }}>
        <Container fitHeight header={<SectionHeader title="Top models by requests" sectionId="top-models-requests" onInfo={onInfo} />}>
          {byModel.loading ? <ChartLoading height={250} /> :
            <BarChart
              series={[{ title: 'Requests', type: 'bar', data: topReqData, valueFormatter: fmt }]}
              xScaleType="categorical"
              hideFilter
              ariaLabel="Top models by requests"
              i18nStrings={{ ...CHART_I18N, yTickFormatter: fmt }}
              height={250}
            />
          }
        </Container>
        <Container fitHeight header={<SectionHeader title="Top models by tokens" sectionId="top-models-tokens" onInfo={onInfo} />}>
          {byModel.loading ? <ChartLoading height={250} /> :
            <BarChart
              series={[{ title: 'Tokens', type: 'bar', data: topTokenData, valueFormatter: fmt }]}
              xScaleType="categorical"
              hideFilter
              ariaLabel="Top models by tokens"
              i18nStrings={{ ...CHART_I18N, yTickFormatter: fmt }}
              height={250}
            />
          }
        </Container>
      </div>

      {/* Spend by Model — stacked $ bars from Cost Explorer (with derived disclosure) */}
      <Container header={
        <SectionHeader
          title="Spend by model"
          sectionId="spend-by-model"
          onInfo={onInfo}
          description={isCostDerived
            ? `Total spend ${cost.data ? `${cost.data.currency} ${fmt(cost.data.total_cost)}` : '—'}; per-model values estimated from token mix because Cost Explorer returns a consolidated 'Amazon Bedrock' line.`
            : `Total spend ${cost.data ? `${cost.data.currency} ${fmt(cost.data.total_cost)}` : '—'}; sourced from AWS Cost Explorer.`}
        />
      }>
        {costByModel.loading ? <ChartLoading height={260} /> :
          spendSeries.length === 0 ? (
            <Box color="text-body-secondary">
              No Bedrock spend in this window. Cost Explorer may take 24-48h
              to surface today's data, and requires <code>ce:GetCostAndUsage</code>
              in the runtime IAM policy.
            </Box>
          ) :
          <BarChart
            series={spendSeries}
            xScaleType="categorical"
            xDomain={costXDomain}
            stackedBars
            hideFilter
            ariaLabel="Spend by model"
            // Right-positioned legend keeps the chart wide and lets long
            // model IDs read top-to-bottom instead of wrapping under the
            // x-axis.
            legendTitle="Model"
            i18nStrings={{
              ...CHART_I18N,
              xTickFormatter: d => new Date(d).toLocaleDateString(undefined, { month: 'short', day: 'numeric' }),
              yTickFormatter: v => `$${v >= 1000 ? (v/1000).toFixed(1) + 'K' : v.toFixed(0)}`,
            }}
            height={300}
            xTitle="Day" yTitle="USD"
          />
        }
      </Container>

      {/* 12. Regions table */}
      <Container header={
        <SectionHeader
          title="Regions — volume & capacity pressure"
          sectionId="regions-health"
          onInfo={onInfo}
          description="Always shows all regions for cross-region comparison; the region filter does not apply to this panel."
        />
      }>
        {byRegion.loading ? <ChartLoading height={200} /> :
          <PaginatedTable
            items={byRegion.data || []}
            columnDefinitions={[
              { id: 'region',   header: 'Region',         cell: r => r.region, sortingField: 'region' },
              { id: 'requests', header: 'Requests',       cell: r => fmt(r.total_requests), sortingField: 'total_requests' },
              { id: 'accts',    header: 'Accounts',       cell: r => fmt(r.unique_accounts), sortingField: 'unique_accounts' },
              { id: 'failed',   header: 'Failed',         cell: r => fmt(r.failed_requests), sortingField: 'failed_requests' },
              {
                id: 'errpct', header: 'Error %',
                cell: (r) => {
                  const p = r.total_requests ? r.failed_requests * 100 / r.total_requests : 0;
                  const t = p > 5 ? 'error' : p > 1 ? 'warning' : 'success';
                  return <StatusIndicator type={t}>{p.toFixed(2)}%</StatusIndicator>;
                },
              },
              { id: 'thr',     header: 'Throttled',  cell: r => fmt(r.throttled), sortingField: 'throttled' },
              {
                id: 'thrpct', header: 'Throttle %',
                cell: (r) => {
                  const p = r.total_requests ? r.throttled * 100 / r.total_requests : 0;
                  const t = p > 5 ? 'error' : p > 1 ? 'warning' : 'success';
                  return <StatusIndicator type={t}>{p.toFixed(3)}%</StatusIndicator>;
                },
              },
            ]}
            empty="No regions in the window"
            sortingDisabled={false}
          />
        }
      </Container>
    </SpaceBetween>
  );
}
