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

import { useMemo } from 'react';
import {
  Container, Header, SpaceBetween, Box, Grid, BarChart, Alert, StatusIndicator, Link,
} from '@cloudscape-design/components';
import { useApi, fmt, fmtPct } from '../api.js';
import { ChartLoading, KpiCard, SectionHeader, CHART_I18N } from '../components/Common.jsx';
import PaginatedTable from '../components/PaginatedTable.jsx';

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
    const byCat = new Map();
    for (const r of byModelChart.data) {
      const cat = topSet.has(r.model_label) ? r.model_label : 'Other';
      if (!byCat.has(cat)) byCat.set(cat, []);
      byCat.get(cat).push(r);
    }
    return [...byCat.entries()].map(([cat, rows]) => ({
      title: modelShort(cat),
      type: 'bar',
      data: rows.map(r => ({ x: new Date(r.event_date), y: Number(r.total_cost) })),
    }));
  }, [byModelChart.data]);

  const isCostDerived = useMemo(
    () => (byModelChart.data || []).some(r => r.derived),
    [byModelChart.data],
  );

  return (
    <SpaceBetween size="l">
      {/* 1. KPI ribbon */}
      <Grid gridDefinition={[{ colspan: 3 }, { colspan: 3 }, { colspan: 3 }, { colspan: 3 }]}>
        <KpiCard title={`Total spend (${s.window?.days || '—'}d)`}
                 value={fmtCurrency(s.total_cost, currency)} />
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
