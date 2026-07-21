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

import { useMemo, useState } from 'react';
import {
  Container, Header, SpaceBetween, ColumnLayout, PieChart, Box, Grid,
  StatusIndicator,
} from '@cloudscape-design/components';
import { useApi, fmt } from '../api.js';
import { ChartLoading, SectionHeader, CHART_I18N } from '../components/Common.jsx';
import PaginatedTable from '../components/PaginatedTable.jsx';
import ProviderIcon from '../components/ProviderIcon.jsx';
import EndpointSubTabs from '../components/EndpointSubTabs.jsx';

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

// Stat row: label on the left, value right-aligned. One line per stat.
// ColumnLayout
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

function ModelCard({ m, hideCache }) {
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
          {!hideCache && <StatRow label="Cache hit" value={fmtPct(m.cache_hit_pct, 1)} />}
          <StatRow label="Error rate"    value={fmtPct(m.error_rate, 2)} color={errColor} />
          <StatRow label="Throttled"     value={fmt(m.throttled)} />
          <StatRow label="Accounts"      value={fmt(m.unique_accounts)} />
        </div>
      </SpaceBetween>
    </Container>
  );
}

export default function ModelInsightsTab({ filters, onInfo }) {
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
        {({ endpoint: ep }) => <ModelInsightsBody filters={filtersWithEp} onInfo={onInfo} endpoint={ep} />}
      </EndpointSubTabs>
    </SpaceBetween>
  );
}

function ModelInsightsBody({ filters, onInfo, endpoint }) {
  // Mantle CloudWatch publishes no cache-token metric, so cache-hit % is
  // meaningless on the mantle slice — hide that stat there (thumb rule: show
  // only what the endpoint actually exposes). Requests/tokens ARE real.
  const isMantle = endpoint === 'mantle';
  const { data, loading, error } = useApi('/model-insights', filters,
    [filters.start, filters.end, filters.days,
     (filters.accounts || []).join(','),
     filters.provider, filters.region, filters.traffic_type,
     filters.endpoint]);

  // Request shape (D) + multimodal token breakdown (F) — both keyed off the
  // same filter window. Multimodal is server-side filtered to multimodal
  // models, so an empty array means "text-only fleet" → render nothing.
  const shape = useApi('/request-shape', filters, [JSON.stringify(filters)]);
  const multimodal = useApi('/multimodal-tokens', filters, [JSON.stringify(filters)]);
  // Mantle token-size percentiles (A) — only meaningful on the mantle slice.
  const mantlePct = useApi('/mantle-token-percentiles',
    isMantle ? { ...filters, endpoint: 'mantle' } : filters,
    [JSON.stringify(filters), isMantle]);

  // Real per-model cost from Cost Explorer (endpoint-agnostic — spend is not
  // split by endpoint). Anchored on actual CE dollars; `derived` says whether
  // CE gave per-model line items (exact) or only a consolidated Bedrock total
  // that we allocate by token mix. Either way this beats the old token×price
  // estimate, which was never grounded in a real invoice.
  const costByModel = useApi('/cost-by-model', { days: filters.days, accounts: filters.accounts },
    [filters.days, (filters.accounts || []).join(',')]);

  const models = data || [];
  const shapeRows = shape.data || [];
  const multimodalRows = multimodal.data || [];
  const mantlePctRows = mantlePct.data || [];
  const costRows = costByModel.data || [];

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

  // Pie 2: cost share by model — REAL Cost Explorer dollars. Sum each model's
  // daily CE cost across the window, top 8 + "Other". `costDerived` drives the
  // honest title: false = CE per-model line items (exact), true = allocated
  // from the real CE Bedrock total by token mix.
  const costDerived = useMemo(
    () => costRows.length > 0 && costRows.every(r => r.derived), [costRows]);
  const costTotal = useMemo(
    () => costRows.reduce((a, r) => a + Number(r.total_cost || 0), 0), [costRows]);
  // Per-model real-CE cost map (label -> summed $) for the table cell too.
  const costByModelMap = useMemo(() => {
    const m = new Map();
    for (const r of costRows) {
      const k = r.model_label || 'Unknown';
      m.set(k, (m.get(k) || 0) + Number(r.total_cost || 0));
    }
    return m;
  }, [costRows]);
  const costPie = useMemo(() => {
    const byModel = costByModelMap;
    const sorted = [...byModel.entries()].sort((a, b) => b[1] - a[1]);
    const top = sorted.slice(0, 8);
    const rest = sorted.slice(8).reduce((a, [, v]) => a + v, 0);
    const out = top.map(([label, val]) => ({ title: label, value: Number(val.toFixed(2)) }));
    if (rest > 0) out.push({ title: 'Other', value: Number(rest.toFixed(2)) });
    return out;
  }, [costRows]);

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
      exportValue: m => m.provider || '',
    },
    {
      id: 'model', header: 'Model', minWidth: 240,
      cell: (m) => (
        <Box>
          <Box>{m.public_name || m.modelId}</Box>
          <Box color="text-body-secondary" fontSize="body-s"><code>{m.modelId}</code></Box>
        </Box>
      ),
      exportValue: m => m.public_name ? `${m.public_name} (${m.modelId})` : m.modelId,
    },
    { id: 'requests',     header: 'Requests',      cell: m => fmt(m.total_requests), exportValue: m => m.total_requests },
    { id: 'input',        header: 'Input tokens',  cell: m => fmt(m.input_tokens),   exportValue: m => m.input_tokens },
    { id: 'output',       header: 'Output tokens', cell: m => fmt(m.output_tokens),  exportValue: m => m.output_tokens },
    { id: 'avg_in',       header: 'Avg in',        cell: m => fmt(Math.round(m.avg_input)),  exportValue: m => Math.round(m.avg_input ?? 0) },
    { id: 'avg_out',      header: 'Avg out',       cell: m => fmt(Math.round(m.avg_output)), exportValue: m => Math.round(m.avg_output ?? 0) },
    { id: 'io_ratio',     header: 'I/O ratio',     cell: m => m.io_ratio?.toFixed(2) ?? '—', exportValue: m => m.io_ratio != null ? m.io_ratio.toFixed(2) : '' },
    { id: 'cache_hit',    header: 'Cache hit %',   cell: m => fmtPct(m.cache_hit_pct), exportValue: m => m.cache_hit_pct ?? '' },
    {
      id: 'error_rate', header: 'Error rate', minWidth: 130,
      cell: m => (
        <StatusIndicator type={errorSeverity(m.error_rate)}>
          {fmtPct(m.error_rate, 3)}
        </StatusIndicator>
      ),
      exportValue: m => m.error_rate ?? '',
    },
    { id: 'throttled', header: 'Throttled', cell: m => fmt(m.throttled), exportValue: m => m.throttled ?? 0 },
    { id: 'accounts',  header: 'Accounts',  cell: m => fmt(m.unique_accounts), exportValue: m => m.unique_accounts ?? 0 },
    { id: 'cost',
      header: costDerived ? 'Cost (CE, allocated)' : 'Cost (Cost Explorer)',
      // Real Cost Explorer dollars per model (matched on modelId). Falls back
      // to '—' when CE has no row for this model in the window.
      cell: m => {
        const c = costByModelMap.get(m.modelid || m.modelId);
        return c != null ? `$${Number(c).toFixed(2)}` : '—';
      },
      exportValue: m => {
        const c = costByModelMap.get(m.modelid || m.modelId);
        return c != null ? Number(c).toFixed(2) : '';
      } },
  // Mantle CloudWatch has no cache metric → drop the Cache hit % column there.
  ].filter(c => !(isMantle && c.id === 'cache_hit'));

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
            title="Cost share by model"
            description={costRows.length === 0 ? undefined
              : costDerived
                ? `$${costTotal.toFixed(2)} from Cost Explorer, allocated to models by token usage`
                : `$${costTotal.toFixed(2)} from Cost Explorer (exact per-model)`}
            sectionId="insights-cost-pie"
            onInfo={onInfo}
          />
        }>
          {costByModel.loading
            ? <ChartLoading height={260} />
            : <PieChart
                data={costPie}
                hideFilter
                ariaLabel="Cost share by model"
                i18nStrings={{ ...CHART_I18N }}
                detailPopoverContent={(d, sum) => [
                  { key: 'Cost', value: `$${Number(d.value).toFixed(2)}` },
                  { key: 'Share', value: fmtPct(sum ? d.value * 100 / sum : 0) },
                ]}
                empty="No Cost Explorer data in this window"
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
                {top12.map(m => <ModelCard key={m.modelId} m={m} hideCache={isMantle} />)}
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

      {/* Request shape (D) — avg input/output tokens per request + ratio.
           Request shape drives capacity: TPM ≈ RPM × (input + output per
           request); a typical chatbot runs ~10:1 input:output. */}
      <Container header={
        <SectionHeader
          title="Request shape"
          description="Avg input/output tokens per request and the in:out ratio. Capacity planning: TPM ≈ RPM × (avg input + avg output per request)."
          sectionId="insights-request-shape"
          onInfo={onInfo}
        />
      }>
        {shape.loading
          ? <ChartLoading height={200} />
          : <PaginatedTable
              items={shapeRows}
              pageSize={15}
              trackBy="modelid"
              downloadFileName="request-shape.csv"
              empty="No request-shape data in this window."
              searchPlaceholder="Search by model id…"
              columnDefinitions={[
                { id: 'm',    header: 'Model',        cell: r => r.modelid || r.modelId },
                { id: 'ain',  header: 'Avg input/req',  cell: r => fmt(Math.round(r.avg_input_per_req)) },
                { id: 'aout', header: 'Avg output/req', cell: r => fmt(Math.round(r.avg_output_per_req)) },
                { id: 'ratio', header: 'In:Out ratio', cell: r => r.in_out_ratio != null ? `${Number(r.in_out_ratio).toFixed(1)}:1` : '—' },
                { id: 'req',  header: 'Total requests', cell: r => fmt(r.total_requests) },
              ]}
            />
        }
      </Container>

      {/* Multimodal token breakdown (F) — only when the fleet has multimodal
           usage. The endpoint filters to multimodal models server-side, so an
           empty array means a text-only fleet → render nothing here. */}
      {!multimodal.loading && multimodalRows.length > 0 && (
        <Container header={
          <SectionHeader
            title="Multimodal token breakdown"
            description="Text vs. speech token usage per model, plus output image count. Only shown when the fleet has multimodal usage."
            sectionId="insights-multimodal"
            onInfo={onInfo}
          />
        }>
          <PaginatedTable
            items={multimodalRows}
            pageSize={15}
            trackBy="modelid"
            downloadFileName="multimodal-tokens.csv"
            empty="No multimodal usage."
            searchPlaceholder="Search by model id…"
            columnDefinitions={[
              { id: 'm',   header: 'Model',         cell: r => r.modelid || r.modelId },
              { id: 'it',  header: 'Input text',    cell: r => fmt(r.input_text) },
              { id: 'is',  header: 'Input speech',  cell: r => fmt(r.input_speech) },
              { id: 'ot',  header: 'Output text',   cell: r => fmt(r.output_text) },
              { id: 'os',  header: 'Output speech', cell: r => fmt(r.output_speech) },
              { id: 'oi',  header: 'Output images', cell: r => fmt(r.output_images) },
            ]}
          />
        </Container>
      )}

      {/* Mantle token size percentiles (A) — only on the mantle slice.
           Mantle publishes no latency, so per-inference token distribution
           is its primary shape signal. */}
      {isMantle && (
        <Container header={
          <SectionHeader
            title="Token size percentiles (per inference)"
            description="p50/p90/p99 input and output tokens per inference. Mantle publishes no latency, so this is its distribution signal."
            sectionId="insights-mantle-token-pct"
            onInfo={onInfo}
          />
        }>
          {mantlePct.loading
            ? <ChartLoading height={200} />
            : <PaginatedTable
                items={mantlePctRows}
                pageSize={15}
                trackBy="modelid"
                downloadFileName="mantle-token-percentiles.csv"
                empty="No Mantle token-percentile data yet for this window."
                searchPlaceholder="Search by model id…"
                columnDefinitions={[
                  { id: 'm',    header: 'Model',       cell: r => r.modelid || r.modelId },
                  { id: 'i50',  header: 'p50 input',   cell: r => fmt(r.p50_input) },
                  { id: 'i90',  header: 'p90 input',   cell: r => fmt(r.p90_input) },
                  { id: 'i99',  header: 'p99 input',   cell: r => fmt(r.p99_input) },
                  { id: 'o50',  header: 'p50 output',  cell: r => fmt(r.p50_output) },
                  { id: 'o90',  header: 'p90 output',  cell: r => fmt(r.p90_output) },
                  { id: 'o99',  header: 'p99 output',  cell: r => fmt(r.p99_output) },
                ]}
              />
          }
        </Container>
      )}
    </SpaceBetween>
  );
}
