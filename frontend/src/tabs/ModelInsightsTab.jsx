// Model Insights tab — per-model deep-dive across the customer's fleet.
//
// Three sections, all driven by /api/model-insights:
//   1. Two pie charts: requests-by-provider, cost-share-by-model (top 8)
//   2. Twelve model cards (top by request volume) with provider icon,
//      stat grid, and severity-colored error rate
//   3. A full sortable/searchable PaginatedTable with every model
//
// Cost numbers here are APPROXIMATE (in-code provider price table).
// The Cost tab uses real Cost Explorer data — that's the source of
// truth. We label the column "Cost (est.)" to keep the distinction
// honest.

import { useMemo } from 'react';
import {
  Container, Header, SpaceBetween, ColumnLayout, PieChart, Box, Grid,
  StatusIndicator,
} from '@cloudscape-design/components';
import { useApi, fmt } from '../api.js';
import { ChartLoading, SectionHeader } from '../components/Common.jsx';
import PaginatedTable from '../components/PaginatedTable.jsx';
import ProviderIcon from '../components/ProviderIcon.jsx';

function fmtPct(n, digits = 2) {
  if (n === null || n === undefined) return '—';
  const v = Number(n);
  if (Number.isNaN(v)) return '—';
  return v.toFixed(digits) + '%';
}

// Title-case provider names for display. The raw values come from the
// modelId (first dotted segment) and are lowercase by convention. A few
// well-known acronym/brand cases ("AWS", "AI21") need explicit casing.
const PROVIDER_LABEL = {
  anthropic:  'Anthropic',
  amazon:     'Amazon',
  meta:       'Meta',
  cohere:     'Cohere',
  mistral:    'Mistral',
  ai21:       'AI21',
  deepseek:   'DeepSeek',
  qwen:       'Qwen',
  twelvelabs: 'Twelve Labs',
  writer:     'Writer',
  nvidia:     'NVIDIA',
  google:     'Google',
  moonshotai: 'MoonshotAI',
  openai:     'OpenAI',
  forge:      'OpenAI',
  stability:  'Stability AI',
  other:      'Other',
};
function providerLabel(p) {
  if (!p) return '';
  return PROVIDER_LABEL[p.toLowerCase()] || p.charAt(0).toUpperCase() + p.slice(1);
}

function errorSeverity(pct) {
  if (pct >= 5)  return 'error';
  if (pct >= 1)  return 'warning';
  return 'success';
}

// Stat row: label on the left, value right-aligned. One line per stat,
// matching the internal Bedrock Lens model card layout. ColumnLayout
// stacks label-above-value which makes the cards tower at ~600px each
// — this layout fits 8 stats in ~170px.
function StatRow({ label, value, color }) {
  return (
    <div style={{
      display: 'flex',
      justifyContent: 'space-between',
      alignItems: 'baseline',
      padding: '2px 0',
      fontSize: 14,
    }}>
      <span style={{ color: 'var(--awsui-color-text-body-secondary, #5f6b7a)' }}>{label}</span>
      <span style={{ fontVariantNumeric: 'tabular-nums', color }}>{value}</span>
    </div>
  );
}

function ModelCard({ m }) {
  const sev = errorSeverity(m.error_rate);
  // Inline color so right-aligned numerals pop in the same way the
  // internal version does. Same palette as Cloudscape's StatusIndicator.
  const errColor = sev === 'error' ? '#d91515'
                : sev === 'warning' ? '#b35900'
                : undefined;
  return (
    <Container>
      <SpaceBetween size="xs">
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <ProviderIcon provider={m.provider} size={20} />
          <Box variant="h3" margin="n">
            {m.public_name || m.modelId}
          </Box>
        </div>
        <div>
          <StatRow label="Requests"      value={fmt(m.total_requests)} />
          <StatRow label="Input tokens"  value={fmt(m.input_tokens)} />
          <StatRow label="Output tokens" value={fmt(m.output_tokens)} />
          <StatRow label="Avg in / out"  value={`${fmt(Math.round(m.avg_input))} / ${fmt(Math.round(m.avg_output))}`} />
          <StatRow label="Cache hit"     value={fmtPct(m.cache_hit_pct, 1)} />
          <StatRow label="Error rate"    value={fmtPct(m.error_rate, 2)} color={errColor} />
          <StatRow label="Throttled"     value={fmt(m.throttled)} />
          <StatRow label="Accounts"      value={fmt(m.unique_accounts)} />
        </div>
      </SpaceBetween>
    </Container>
  );
}

export default function ModelInsightsTab({ filters, onInfo }) {
  const { data, loading, error } = useApi('/model-insights', filters,
    [filters.start, filters.end, filters.days,
     (filters.accounts || []).join(','),
     filters.provider, filters.region, filters.traffic_type]);

  const models = data || [];

  // Pie 1: requests by provider (top 8 + "Other").
  const providerPie = useMemo(() => {
    const totals = new Map();
    for (const m of models) {
      totals.set(m.provider, (totals.get(m.provider) || 0) + m.total_requests);
    }
    const sorted = [...totals.entries()].sort((a, b) => b[1] - a[1]);
    const top = sorted.slice(0, 8);
    const rest = sorted.slice(8).reduce((a, [, v]) => a + v, 0);
    const out = top.map(([prov, val]) => ({ title: providerLabel(prov), value: val }));
    if (rest > 0) out.push({ title: 'Other', value: rest });
    return out;
  }, [models]);

  // Pie 2: cost share by model (top 8 + "Other").
  const costPie = useMemo(() => {
    const sorted = [...models].sort((a, b) => b.cost_estimate_usd - a.cost_estimate_usd);
    const top = sorted.slice(0, 8);
    const rest = sorted.slice(8).reduce((a, m) => a + m.cost_estimate_usd, 0);
    const out = top.map(m => ({
      title: m.public_name || m.modelId,
      value: m.cost_estimate_usd,
    }));
    if (rest > 0) out.push({ title: 'Other', value: rest });
    return out;
  }, [models]);

  const top12 = useMemo(() => models.slice(0, 12), [models]);

  if (error) {
    return (
      <Container header={<Header variant="h2">Model Insights</Header>}>
        <Box color="text-status-error">Failed to load: {String(error)}</Box>
      </Container>
    );
  }

  const tableColumns = [
    {
      id: 'icon', header: '', minWidth: 40, width: 40,
      cell: (m) => <ProviderIcon provider={m.provider} size={20} />,
    },
    {
      id: 'model', header: 'Model', minWidth: 240,
      cell: (m) => (
        <Box>
          <Box>{m.public_name || m.modelId}</Box>
          <Box color="text-body-secondary" fontSize="body-s"><code>{m.modelId}</code></Box>
        </Box>
      ),
    },
    { id: 'requests',     header: 'Requests',      cell: m => fmt(m.total_requests) },
    { id: 'input',        header: 'Input tokens',  cell: m => fmt(m.input_tokens) },
    { id: 'output',       header: 'Output tokens', cell: m => fmt(m.output_tokens) },
    { id: 'avg_in',       header: 'Avg in',        cell: m => fmt(Math.round(m.avg_input)) },
    { id: 'avg_out',      header: 'Avg out',       cell: m => fmt(Math.round(m.avg_output)) },
    { id: 'io_ratio',     header: 'I/O ratio',     cell: m => m.io_ratio?.toFixed(2) ?? '—' },
    { id: 'cache_hit',    header: 'Cache hit %',   cell: m => fmtPct(m.cache_hit_pct) },
    {
      id: 'error_rate', header: 'Error rate', minWidth: 130,
      cell: m => (
        <StatusIndicator type={errorSeverity(m.error_rate)}>
          {fmtPct(m.error_rate, 3)}
        </StatusIndicator>
      ),
    },
    { id: 'throttled', header: 'Throttled', cell: m => fmt(m.throttled) },
    { id: 'accounts',  header: 'Accounts',  cell: m => fmt(m.unique_accounts) },
    { id: 'cost',      header: 'Cost (est.)', cell: m => `$${m.cost_estimate_usd.toFixed(2)}` },
  ];

  return (
    <SpaceBetween size="m">
      {/* Pies row -----------------------------------------------------
           `fitHeight` on each Container makes both stretch to the row's
           tallest content. Without it, an asymmetric label count between
           the two pies leaves the shorter card with extra whitespace and
           the chart visually offset from its neighbour. */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 20, alignItems: 'stretch' }}>
        <Container fitHeight header={
          <SectionHeader
            title="Requests by provider"
            sectionId="insights-provider-pie"
            onInfo={onInfo}
          />
        }>
          {loading
            ? <ChartLoading height={260} />
            : <PieChart
                data={providerPie}
                hideFilter
                ariaLabel="Requests by provider"
                empty="No data"
              />}
        </Container>
        <Container fitHeight header={
          <SectionHeader
            title="Cost share by model (estimate)"
            sectionId="insights-cost-pie"
            onInfo={onInfo}
          />
        }>
          {loading
            ? <ChartLoading height={260} />
            : <PieChart
                data={costPie}
                hideFilter
                ariaLabel="Cost share by model"
                empty="No data"
              />}
        </Container>
      </div>

      {/* Model cards row --------------------------------------------- */}
      <Container header={
        <SectionHeader
          title={`Top models${top12.length ? ` (${top12.length})` : ''}`}
          description="Ranked by request volume in the selected window."
          sectionId="insights-cards"
          onInfo={onInfo}
        />
      }>
        {loading
          ? <ChartLoading height={300} />
          : top12.length === 0
            ? <Box color="text-body-secondary" textAlign="center" padding="l">
                No model usage in this window. Try a wider date range.
              </Box>
            : <Grid
                gridDefinition={top12.map(() => ({ colspan: { default: 12, xxs: 6, s: 4 } }))}
              >
                {top12.map(m => <ModelCard key={m.modelId} m={m} />)}
              </Grid>
        }
      </Container>

      {/* Full table -------------------------------------------------- */}
      <Container header={
        <SectionHeader
          title={`All models${models.length ? ` (${models.length})` : ''}`}
          description="Sortable, searchable. Use the filters at the top of the page to narrow the window."
          sectionId="insights-table"
          onInfo={onInfo}
        />
      }>
        {loading
          ? <ChartLoading height={200} />
          : <PaginatedTable
              items={models}
              pageSize={25}
              empty="No models in scope."
              searchPlaceholder="Search by model id, name, provider…"
              columnDefinitions={tableColumns}
            />
        }
      </Container>
    </SpaceBetween>
  );
}
