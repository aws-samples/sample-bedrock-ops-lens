// Cost tab — dedicated view for Bedrock spend.
//
// Spend is sourced from AWS Cost Explorer (ce:GetCostAndUsage), refreshed
// daily, and lags 24-48h. When CE returns a consolidated "Amazon Bedrock"
// line item (most non-EDP customers), per-model spend is derived by
// allocating the daily total proportionally to each model's token volume
// from CloudWatch — this is disclosed wherever the derived numbers appear.
//
// Sections:
//   1. KPI ribbon: Total spend · WoW Δ · Active services · Active accounts
//   2. Daily spend stacked-area (top 7 models + Other)
//   3. Spend by account table (with WoW Δ column, severity-coded)
//   4. Spend by model table ($/1M tokens, $/request)
//   5. Cost concentration: top-N (account, model) by spend with WoW

import { useMemo, useState } from 'react';
import {
  Container, Header, SpaceBetween, Box, Grid, BarChart, Alert, StatusIndicator, Link,
} from '@cloudscape-design/components';
import { useApi, fmt, fmtPct } from '../api.js';
import { ChartLoading, KpiCard, SectionHeader, CHART_I18N } from '../components/Common.jsx';
import PaginatedTable from '../components/PaginatedTable.jsx';
import EndpointSubTabs from '../components/EndpointSubTabs.jsx';

// Currency formatter:
//   - For axis ticks where space is tight: pass `compact: true` for $1.2K.
//   - Default (table cells, KPI): full localized $1,016.67 — never K-abbreviate
//     currency on KPI tiles because it makes neighboring values that differ
//     by hundreds of dollars look identical (`$1.0K` in two windows that
//     are actually $784 vs $1016).
function fmtCurrency(amount, currency = 'USD', { compact = false } = {}) {
  if (amount == null) return '—';
  const v = Number(amount);
  if (Number.isNaN(v)) return '—';
  const sym = currency === 'USD' ? '$' : currency + ' ';
  if (compact && Math.abs(v) >= 10000) return `${sym}${(v / 1000).toFixed(1)}K`;
  return `${sym}${v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function modelShort(id) {
  return (id || '').replace(/^us\./, '').replace(/^eu\./, '').replace(/^global\./, '')
    .replace(/^anthropic\./, '').replace(/^amazon\./, '').replace(/^meta\./, '')
    .replace(/^cohere\./, '').replace(/^mistral\./, '').replace(/^deepseek\./, '')
    .split(':')[0];
}

function deltaCell(cur, prev) {
  if (!prev) return <Box variant="span" color="text-body-secondary">new</Box>;
  const pct = (cur - prev) / prev * 100;
  const t = pct > 20 ? 'error' : pct > 0 ? 'warning' : pct < -20 ? 'success' : 'info';
  const sign = pct >= 0 ? '+' : '';
  return <StatusIndicator type={t}>{`${sign}${pct.toFixed(1)}%`}</StatusIndicator>;
}

export default function CostTab({ filters, onInfo }) {
  // Cost Explorer bills Bedrock per account/service/region — it has NO
  // runtime-vs-mantle dimension. The 'all' view shows the exact CE dollars;
  // the runtime/mantle sub-tabs show that endpoint's ALLOCATED share (the real
  // CE total apportioned per model/account/day by token-cost weight). Labeled
  // as an allocation so it's never mistaken for a billed per-endpoint figure.
  const distinct = useApi('/distinct-filters', {}, []).data || {};
  const mantleAvailable = !!distinct.mantle_available?.volumetric;
  const [endpoint, setEndpoint] = useState(filters.endpoint || 'all');
  const filtersWithEp = useMemo(() => ({ ...filters, endpoint }), [filters, endpoint]);
  return (
    <EndpointSubTabs
      selected={endpoint === 'all' ? 'runtime' : endpoint}
      onChange={setEndpoint}
      runtimeCoverage="metric"
      mantleCoverage="metric"
      mantleAvailable={mantleAvailable}
    >
      {() => <CostBody filters={filtersWithEp} endpoint={endpoint} onInfo={onInfo} />}
    </EndpointSubTabs>
  );
}

function CostBody({ filters, endpoint, onInfo }) {
  // In-card toggle on the Total spend tile (endpoint sub-tabs only): flip
  // between this endpoint's allocated slice and the combined CE total —
  // mirrors the Overview tab's spend tile.
  const [spendView, setSpendView] = useState('endpoint');
  const summary  = useApi('/cost-summary',           filters, [JSON.stringify(filters)]);
  const daily    = useApi('/cost-daily',             filters, [JSON.stringify(filters)]);
  const byAcct   = useApi('/cost-by-account',        filters, [JSON.stringify(filters)]);
  const byModel  = useApi('/cost-by-model-detailed', filters, [JSON.stringify(filters)]);
  const concen   = useApi('/cost-concentration',     filters, [JSON.stringify(filters)]);
  const byModelChart = useApi('/cost-by-model',      filters, [JSON.stringify(filters)]);

  const s = summary.data || {};
  const currency = s.currency || 'USD';
  const wowPct = s.previous_total_cost
    ? ((s.total_cost - s.previous_total_cost) / s.previous_total_cost * 100)
    : null;

  // Daily spend chart — stacked by model. Re-uses cost-by-model output
  // because that's already daily × model-shaped; the simpler /cost-daily
  // is only used for the KPI line.
  const spendStackedSeries = useMemo(() => {
    if (!byModelChart.data || byModelChart.data.length === 0) return [];
    const totals = new Map();
    for (const r of byModelChart.data) {
      totals.set(r.model_label, (totals.get(r.model_label) || 0) + Number(r.total_cost || 0));
    }
    const top = [...totals.entries()].sort((a, b) => b[1] - a[1]).slice(0, 7).map(([k]) => k);
    const topSet = new Set(top);
    // Fold non-top models into 'Other', summing per (category, date) so a day
    // never has two segments for the same category.
    const byCatDate = new Map();   // cat -> Map(dateStr -> cost)
    for (const r of byModelChart.data) {
      const cat = topSet.has(r.model_label) ? r.model_label : 'Other';
      if (!byCatDate.has(cat)) byCatDate.set(cat, new Map());
      const m = byCatDate.get(cat);
      m.set(r.event_date, (m.get(r.event_date) || 0) + Number(r.total_cost || 0));
    }
    return [...byCatDate.entries()].map(([cat, m]) => ({
      title: modelShort(cat),
      type: 'bar',
      // Sort points chronologically — the API returns rows grouped by model,
      // so without this the categorical x-axis renders days out of order
      // (Jun 3, Jun 22, Jun 7, … Jul 3, Jun 5). Sort by real date ascending.
      data: [...m.entries()]
        .sort((a, b) => new Date(a[0]) - new Date(b[0]))
        .map(([d, cost]) => ({ x: new Date(d), y: cost })),
    }));
  }, [byModelChart.data]);

  // Explicit sorted x-domain so the categorical axis is chronological across
  // ALL series (per-series sort alone can still interleave when series
  // introduce different dates first). Union of every date, ascending.
  const spendXDomain = useMemo(() => {
    const ds = new Set();
    for (const r of (byModelChart.data || [])) ds.add(r.event_date);
    return [...ds].sort((a, b) => new Date(a) - new Date(b)).map(d => new Date(d));
  }, [byModelChart.data]);

  const isCostDerived = useMemo(
    () => (byModelChart.data || []).some(r => r.derived),
    [byModelChart.data],
  );

  return (
    <SpaceBetween size="l">
      {/* Provenance banner on the endpoint sub-tabs: these dollars are the real
          CE total apportioned to this endpoint by token-cost weight, not a
          billed per-endpoint figure (AWS bills Bedrock with no endpoint dim). */}
      {(endpoint === 'runtime' || endpoint === 'mantle') && (
        <Alert type="info">
          Showing <strong>bedrock-{endpoint}</strong>'s allocated share of spend —
          the invoice-accurate Cost Explorer total apportioned per model/account/day
          by token-cost weight. AWS bills Bedrock without a runtime/mantle
          dimension, so this is an allocation, not a billed per-endpoint amount.
          Switch to the combined view for exact CE dollars.
        </Alert>
      )}
      {/* 1. KPI ribbon */}
      <Grid gridDefinition={[{ colspan: 3 }, { colspan: 3 }, { colspan: 3 }, { colspan: 3 }]}>
        {(() => {
          // CE gives an invoice-accurate TOTAL but no runtime-vs-mantle
          // dimension. On an endpoint sub-tab, the in-card toggle picks which
          // figure shows: that endpoint's ALLOCATED slice (total split by
          // token-cost weight) or the combined CE total. On 'all' there's no
          // toggle — just show the total plus the runtime·mantle split line.
          const be = s.by_endpoint;
          const onEndpoint = (endpoint === 'runtime' || endpoint === 'mantle') && be;
          const showAlloc = onEndpoint && spendView === 'endpoint';
          const amount = showAlloc ? Number(be[endpoint] || 0) : Number(s.total_cost || 0);
          return (
            <KpiCard title={`Total spend (${s.window?.days || '—'}d)`}
                     tabs={onEndpoint ? {
                       selectedId: spendView,
                       onChange: setSpendView,
                       options: [
                         { id: 'endpoint', text: endpoint === 'mantle' ? 'Mantle' : 'Runtime' },
                         { id: 'total', text: 'Total' },
                       ],
                     } : undefined}
                     value={s.total_cost != null ? fmtCurrency(amount, currency) : '—'}
                     // Endpoint split line only on the combined 'all' view, and
                     // only when there's actual mantle spend to break out.
                     split={!onEndpoint && be && Number(be.mantle) > 0
                       ? { runtime: fmtCurrency(Number(be.runtime || 0), currency),
                           mantle: fmtCurrency(Number(be.mantle || 0), currency) }
                       : undefined}
                     note={showAlloc ? 'token-cost share' : undefined} />
          );
        })()}
        <KpiCard title="WoW change"
                 value={wowPct == null ? '—' : `${wowPct >= 0 ? '+' : ''}${wowPct.toFixed(1)}%`} />
        <KpiCard title="Active services"  value={fmt(s.unique_services)} />
        <KpiCard title="Active accounts"  value={fmt(s.unique_accounts)} />
      </Grid>

      {/* 2. Daily spend stacked */}
      <Container header={
        <SectionHeader
          title="Daily spend"
          sectionId="spend-by-model"
          onInfo={onInfo}
          description={isCostDerived
            ? 'Per-model spend allocated proportionally from a consolidated Cost Explorer line item.'
            : 'Per-model spend sourced directly from Cost Explorer service-level lines.'}
        />
      }>
        {byModelChart.loading ? <ChartLoading height={300} /> :
          spendStackedSeries.length === 0 ? (
            <Alert type="info" header="No Cost Explorer data in this window">
              <p>
                Cost data lags 24-48h. If you just deployed, the first refresh
                runs after the next ingestion cycle. Verify the runtime
                principal has <code>ce:GetCostAndUsage</code> on its IAM
                policy, then run <code>python -m ingestion.cost --days 30</code>.
              </p>
              <Link external href="https://docs.aws.amazon.com/cost-management/latest/userguide/ce-what-is.html">
                AWS Cost Explorer docs
              </Link>
            </Alert>
          ) :
          <BarChart
            series={spendStackedSeries}
            xScaleType="categorical"
            xDomain={spendXDomain}
            stackedBars
            hideFilter
            ariaLabel="Daily spend"
            i18nStrings={{
              ...CHART_I18N,
              xTickFormatter: d => new Date(d).toLocaleDateString(undefined, { month: 'short', day: 'numeric' }),
              yTickFormatter: v => fmtCurrency(v, currency, { compact: true }),
            }}
            height={300}
            xTitle="Day" yTitle={currency}
          />
        }
      </Container>

      {/* 3. Spend by account */}
      <Container header={<SectionHeader title="Spend by account" sectionId="spend-by-model" onInfo={onInfo} />}>
        {byAcct.loading ? <ChartLoading height={200} /> :
          <PaginatedTable
            items={byAcct.data || []}
            columnDefinitions={[
              { id: 'a',    header: 'Account',  cell: r => r.accountId },
              { id: 'cost', header: 'Spend',    cell: r => fmtCurrency(r.total_cost, r.currency) },
              { id: 'prev', header: 'Previous window', cell: r => fmtCurrency(r.previous_cost, r.currency) },
              { id: 'wow',  header: 'WoW change',  cell: r => deltaCell(r.total_cost, r.previous_cost) },
            ]}
            empty="No spend in this window"
          />
        }
      </Container>

      {/* 4. Spend by model — with $/1M tokens + $/request derived columns */}
      <Container header={
        <SectionHeader
          title="Spend by model"
          sectionId="spend-by-model"
          onInfo={onInfo}
          description={isCostDerived
            ? 'Spend is allocated by token share, so cost-per-token is uniform across models. Direct per-model line items appear here unaltered when Cost Explorer returns them.'
            : undefined}
        />
      }>
        {byModel.loading ? <ChartLoading height={200} /> :
          <PaginatedTable
            items={byModel.data || []}
            columnDefinitions={[
              { id: 'm',     header: 'Model',     cell: r => r.modelId },
              { id: 'cost',  header: 'Spend',     cell: r => fmtCurrency(r.total_cost, r.currency) },
              { id: 'reqs',  header: 'Requests',  cell: r => fmt(r.total_requests) },
              { id: 'tokin', header: 'Input tok', cell: r => fmt(r.input_tokens) },
              { id: 'tkout', header: 'Output tok',cell: r => fmt(r.output_tokens) },
              { id: 'cpr',   header: '$/request', cell: r => r.cost_per_request != null ? `$${r.cost_per_request.toFixed(4)}` : '—' },
              { id: 'cpm',   header: '$/1M tok',  cell: r => r.cost_per_million_tokens != null ? `$${r.cost_per_million_tokens.toFixed(2)}` : '—' },
            ]}
            empty="No spend / usage data"
          />
        }
      </Container>

      {/* 5. Cost concentration */}
      <Container header={<SectionHeader title="Top spend concentration" sectionId="spend-by-model" onInfo={onInfo} />}>
        {concen.loading ? <ChartLoading height={200} /> :
          <PaginatedTable
            items={concen.data || []}
            columnDefinitions={[
              { id: 'a',    header: 'Account',  cell: r => r.accountId },
              { id: 'm',    header: 'Model',    cell: r => r.modelId },
              { id: 'cost', header: 'Spend',    cell: r => fmtCurrency(r.total_cost) },
              { id: 'prev', header: 'Previous', cell: r => fmtCurrency(r.previous_cost) },
              { id: 'wow',  header: 'WoW change', cell: r => deltaCell(r.total_cost, r.previous_cost) },
            ]}
            empty="No spend concentration data"
          />
        }
      </Container>
    </SpaceBetween>
  );
}
