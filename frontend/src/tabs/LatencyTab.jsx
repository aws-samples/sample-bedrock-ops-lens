// Latency tab — 4 containers, with E2E ↔ TTFT segmented control.
import { useMemo, useState } from 'react';
import {
  Container, Header, SpaceBetween, BarChart, SegmentedControl, Box,
} from '@cloudscape-design/components';
import { useApi, fmt, fmtMs } from '../api.js';
import { ChartLoading, SectionHeader, CHART_I18N } from '../components/Common.jsx';
import PaginatedTable from '../components/PaginatedTable.jsx';
import EndpointSubTabs, { EndpointNotAvailable } from '../components/EndpointSubTabs.jsx';

function isLLM(modelId) {
  const m = (modelId || '').toLowerCase();
  return !(m.includes('embed') || m.includes('rerank'));
}

function modelShort(id) {
  return (id || '').replace(/^us\./, '').replace(/^eu\./, '').replace(/^global\./, '')
    .replace(/^anthropic\./, '').replace(/^amazon\./, '').replace(/^meta\./, '')
    .replace(/^cohere\./, '').replace(/^mistral\./, '')
    .split(':')[0];
}

export default function LatencyTab({ filters, onInfo }) {
  // bedrock-mantle does not publish latency to CloudWatch. The dashboard
  // can still surface Mantle latency, but only when the customer enabled
  // Bedrock Model Invocation Logging — invocation_logs.py parses the
  // per-request latencyMs and writes percentiles to f_latency_daily for
  // the 'mantle' slice. So we SHOW the Mantle sub-tab only when such
  // log-derived rows actually exist; otherwise hide it entirely (no blank
  // Mantle view). The signal comes from /distinct-filters.mantle_available.
  const distinct = useApi('/distinct-filters', {}, []).data || {};
  const mantleAvailable = !!distinct.mantle_available?.latency;

  const [endpoint, setEndpoint] = useState(filters.endpoint || 'all');
  const filtersWithEp = useMemo(() => ({ ...filters, endpoint }), [filters, endpoint]);
  return (
    <EndpointSubTabs
      selected={endpoint === 'all' ? 'runtime' : endpoint}
      onChange={setEndpoint}
      runtimeCoverage="full"
      mantleCoverage="log-derived"
      mantleAvailable={mantleAvailable}
    >
      {({ endpoint: ep }) => (
        <LatencyBody
          filters={filtersWithEp}
          onInfo={onInfo}
          mantleHint={ep === 'mantle'}
        />
      )}
    </EndpointSubTabs>
  );
}

function LatencyBody({ filters, onInfo, mantleHint }) {
  const [metric, setMetric] = useState('e2e');
  const byModel = useApi('/latency-by-model', filters, [JSON.stringify(filters)]);
  const cris = useApi('/latency-cris-vs-od', filters, [JSON.stringify(filters)]);
  const ops = useApi('/operation-latency', filters, [JSON.stringify(filters)]);

  const chartSeries = useMemo(() => {
    const data = (byModel.data || []).filter(r => isLLM(r.modelid || r.modelId));
    const f = metric === 'e2e' ? ['p50_e2e', 'p90_e2e', 'p99_e2e']
                                : ['p50_ttft', 'p90_ttft', 'p99_ttft'];
    return [
      { title: 'p50', type: 'bar', data: data.map(r => ({ x: modelShort(r.modelid || r.modelId), y: Number(r[f[0]] || 0) })) },
      { title: 'p90', type: 'bar', data: data.map(r => ({ x: modelShort(r.modelid || r.modelId), y: Number(r[f[1]] || 0) })) },
      { title: 'p99', type: 'bar', data: data.map(r => ({ x: modelShort(r.modelid || r.modelId), y: Number(r[f[2]] || 0) })) },
    ];
  }, [byModel.data, metric]);

  return (
    <SpaceBetween size="l">
      <Container header={
        <SectionHeader
          title="Latency by model (ms)"
          sectionId="latency-chart"
          onInfo={onInfo}
          actions={
            <SegmentedControl
              selectedId={metric}
              onChange={({ detail }) => setMetric(detail.selectedId)}
              options={[
                { id: 'e2e',  text: 'End-to-end' },
                { id: 'ttft', text: 'Time to first token' },
              ]}
            />
          }
        />
      }>
        {byModel.loading ? <ChartLoading height={300} /> :
          <BarChart
            series={chartSeries}
            xScaleType="categorical"
            hideFilter
            ariaLabel="Latency by model"
            i18nStrings={{ ...CHART_I18N, yTickFormatter: v => `${v >= 1000 ? (v / 1000).toFixed(1) + 's' : Math.round(v) + 'ms'}` }}
            height={300}
            xTitle="Model" yTitle="Latency"
          />
        }
      </Container>

      <Container header={
        <SectionHeader
          title="Latency table"
          description="OTPS = output tokens/sec after the first token (generation speed; higher = faster). TTFT is time to the first token. — means the endpoint does not publish that metric."
          sectionId="latency-table"
          onInfo={onInfo}
        />
      }>
        {byModel.loading ? <ChartLoading /> :
          <PaginatedTable
            items={(byModel.data || []).filter(r => isLLM(r.modelid || r.modelId))}
            columnDefinitions={[
              { id: 'm',    header: 'Model',     cell: r => r.modelid || r.modelId },
              { id: 'n',    header: 'Samples',   cell: r => fmt(r.sample_count) },
              { id: 'p50',  header: 'E2E p50',   cell: r => fmtMs(r.p50_e2e) },
              { id: 'p90',  header: 'E2E p90',   cell: r => fmtMs(r.p90_e2e) },
              { id: 'p99',  header: 'E2E p99',   cell: r => fmtMs(r.p99_e2e) },
              { id: 'att',  header: 'Avg TTFT',  cell: r => fmtMs(r.avg_ttft) },
              { id: 'tt50', header: 'TTFT p50',  cell: r => fmtMs(r.p50_ttft) },
              { id: 'tt90', header: 'TTFT p90',  cell: r => fmtMs(r.p90_ttft) },
              { id: 'tt99', header: 'TTFT p99',  cell: r => fmtMs(r.p99_ttft) },
              { id: 'otps', header: 'OTPS (out tok/s)', cell: r => r.otps != null ? Number(r.otps).toFixed(1) : '—' },
              { id: 'aotr', header: 'Avg out tok/req', cell: r => r.avg_output_tokens_per_req != null ? fmt(Math.round(r.avg_output_tokens_per_req)) : '—' },
            ]}
            empty="No latency data"
            sortingDisabled
          />
        }
      </Container>

      <Container header={<SectionHeader title="Latency by operation" sectionId="op-latency" onInfo={onInfo} />}>
        {ops.loading ? <ChartLoading /> :
          <PaginatedTable
            items={ops.data || []}
            columnDefinitions={[
              { id: 'o',    header: 'Operation', cell: r => r.operation },
              { id: 'n',    header: 'Samples',   cell: r => fmt(r.sample_count) },
              { id: 'p50',  header: 'E2E p50',   cell: r => fmtMs(r.p50_e2e) },
              { id: 'p90',  header: 'E2E p90',   cell: r => fmtMs(r.p90_e2e) },
              { id: 'p99',  header: 'E2E p99',   cell: r => fmtMs(r.p99_e2e) },
            ]}
            empty="No data"
            sortingDisabled
          />
        }
      </Container>

      <Container header={<SectionHeader title="CRIS vs On-Demand latency by model" sectionId="cris-latency" onInfo={onInfo} />}>
        {cris.loading ? <ChartLoading /> :
          <PaginatedTable
            items={(cris.data || []).filter(r => isLLM(r.modelid || r.modelId))}
            columnDefinitions={[
              { id: 'm',   header: 'Model',     cell: r => r.modelid || r.modelId },
              { id: 'tt',  header: 'Path',      cell: r => r.traffic_type || '—' },
              { id: 'n',   header: 'Samples',   cell: r => fmt(r.sample_count) },
              { id: 'p50', header: 'E2E p50',   cell: r => fmtMs(r.p50_e2e) },
              { id: 'p90', header: 'E2E p90',   cell: r => fmtMs(r.p90_e2e) },
              { id: 'p99', header: 'E2E p99',   cell: r => fmtMs(r.p99_e2e) },
              { id: 'tt50', header: 'TTFT p50', cell: r => fmtMs(r.p50_ttft) },
              { id: 'tt90', header: 'TTFT p90', cell: r => fmtMs(r.p90_ttft) },
            ]}
            empty="No data"
          />
        }
      </Container>
    </SpaceBetween>
  );
}
